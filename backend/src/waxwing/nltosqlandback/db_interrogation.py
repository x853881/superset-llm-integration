"""Interrogate a database: read the structure of every schema through Superset, and keep it.

There's no semantic schema for most databases a Superset points at, and a model asked to write SQL
against tables it has never been told about can only guess. But a database knows a lot about
itself without anyone reading its data: schema, table and view names, columns and their types,
primary and foreign keys, indexes, enum types, and - from its planner - roughly how big each table
is. That's what this reads, for every schema of the tab's database its connection can see (all
through ``superset_db``, so through Superset's own connection, and never a ``SELECT`` of anyone's
rows - the data dictionary's below, a fixed query of a configured table, aside). What's kept is
shared, and narrowed to the tables each user may see when it's used. ``knowledge_reads`` says when
it runs.

Where the database keeps its own descriptions of its tables and columns - a data dictionary,
``NLSQL_DATA_DICTIONARY`` - it reads those too (see ``data_dictionary``), and they come first.

Optionally (``NLSQL_INTERROGATE_DESCRIPTIONS``, off by default) it then hands the structure to the
model in batches, to write a sentence about each table, a few words about each column whose purpose
isn't obvious from its name, and the joins it can see that no foreign key declares. Off by default
because a database of many schemas makes that a lot of model calls, and a much bigger prompt.

The result (``schema_knowledge.DatabaseKnowledge``) is kept per database and given to every prompt
about that database afterwards - see ``nlsql_backend.schema_preamble``. Whole, when it fits; when it
doesn't, and ``NLSQL_SCHEMA_EMBEDDINGS`` is on, the tables closest to the request, which is why
this embeds them too (see ``table_embeddings``).

Like every other pipeline here it's a generator of progress events, so the panel can say which
schema and table it has got to.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from flask import current_app

from . import bulk_structure, data_dictionary, llm_client, nlsql_backend, progress, request_context, schema_knowledge, superset_db, table_embeddings
from .cancellation import CancelToken, CancelledError
from .data_dictionary import DataDictionary
from .embedding_provider import resolve_embedding_provider
from .llm_provider import resolve_llm_provider
from .progress import EventStream
from .schema_knowledge import DatabaseKnowledge, Problem
from .semantic_schema import ColumnDescription, RelationshipDescription, TableDescription
from .superset_db import TableMeta

logger = logging.getLogger(__name__)

#: How much table structure goes into one model call - enough for ~20 ordinary tables.
BATCH_CHARS = 9_000
BATCH_TABLES = 25
DEFAULT_MAX_TABLES = 3_000


class InterrogationError(RuntimeError):
    """The database couldn't be interrogated at all (no database, no access, nothing in it)."""


def _config(name: str, default: Any) -> Any:
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def interrogate(
    cancel_token: Optional[CancelToken] = None, describe: Optional[bool] = None, changes_only: bool = False
) -> EventStream:
    """Reads every schema of the current request's database that its connection can see, merges it
    into what's kept, and returns the `summary()` of what this user may see. `describe` (default
    ``NLSQL_INTERROGATE_DESCRIPTIONS``) has the model describe the tables too. `changes_only` is the
    cheap check: it lists the tables, reads only those added since and drops those gone (reading
    everything when nothing is kept yet).

    A schema that can't be listed, or a table that can't be read, keeps what was kept for it. What's
    read is narrowed to each user when it's used, but the progress goes to whoever started the read,
    so it never names a schema or table; the problems kept say which they're about
    (``schema_knowledge.Problem``) and are narrowed like the tables."""
    started = time.monotonic()
    if not superset_db.db_info_enabled():
        raise InterrogationError("This Superset is set to run without database information (NLSQL_DB_INFO).")
    if describe is None:
        describe = bool(_config("NLSQL_INTERROGATE_DESCRIPTIONS", False))
    row_estimates = bool(_config("NLSQL_INTERROGATE_ROW_ESTIMATES", True))
    dictionary_config = data_dictionary.configured()
    embed = table_embeddings.enabled()

    context = request_context.current()
    if context is None or context.database_id is None:
        raise InterrogationError("Pick a database for this tab first.")
    database = superset_db.resolve_database(context.database_id)
    assert database is not None
    catalog = context.catalog
    previous = schema_knowledge.store.get(database.id, catalog, fresh=True)
    changes_only = changes_only and previous is not None

    steps = progress.Steps([
        ("schemas", "Listing the schemas", "Listed the schemas"),
        ("read", "Looking for added and dropped tables", "Looked for added and dropped tables") if changes_only
        else ("read", "Reading the tables, columns and indexes", "Read the tables, columns and indexes"),
        *([("rows", "Estimating table sizes", "Estimated table sizes")] if row_estimates else []),
        *([("dictionary", "Reading the data dictionary", "Read the data dictionary")] if dictionary_config else []),
        *([("describe", "Describing the tables", "Described the tables")] if describe else []),
        *([("embed", "Embedding the tables", "Embedded the tables")] if embed else []),
        ("save", "Saving what was found", "Saved what was found"),
    ])
    yield steps.plan_event

    dictionary_tables = data_dictionary.tables_in(dictionary_config, database)
    yield steps.start("schemas", f"In {database.name}")
    schemas, skipped = superset_db.list_schemas(database, catalog, refresh=True, everyone=True)
    if not schemas:
        raise InterrogationError(f"There are no schemas in {database.name}.")
    yield steps.done(
        "schemas",
        f"{len(schemas)} schema{'s' if len(schemas) != 1 else ''}"
        + (f" ({skipped} of the database's own left out)" if skipped else ""),
    )

    problems: list[str] = []
    listed_by_schema: dict[str, list[tuple[str, str]]] = {}
    unlisted: set[str] = set()
    yield steps.start("read", f"{len(schemas)} schemas")
    with superset_db.table_lister(database, catalog) as list_tables:
        for schema_index, schema in enumerate(schemas, start=1):
            _check(cancel_token)
            yield steps.update("read", f"Listing schema {schema_index} of {len(schemas)}")
            try:
                listed = list_tables(schema)
            except Exception as ex:  # noqa: BLE001 - one unreadable schema shouldn't lose the rest
                problems.append(Problem(f"{schema}: couldn't list its tables ({_brief(ex)})", schema))
                unlisted.add(schema)
                continue
            # The data dictionary describes the data rather than being any, so prompts never see it -
            # nor its schema, when that holds nothing else.
            kept = [(name, kind) for name, kind in listed if not data_dictionary.is_dictionary(dictionary_tables, schema, name)]
            if listed and not kept:
                continue
            listed_by_schema[schema] = kept

    stored: dict[str, dict[str, TableDescription]] = {}
    for table in previous.tables if previous is not None else []:
        stored.setdefault(table.schema, {})[table.name] = table
    if changes_only:
        to_read = {
            schema: [(name, kind) for name, kind in listed if name not in stored.get(schema, {})]
            for schema, listed in listed_by_schema.items()
        }
        still_there = {(schema, name) for schema, listed in listed_by_schema.items() for name, _ in listed}
        unchanged = [
            table for schema, tables in stored.items() for name, table in tables.items()
            if schema in unlisted or (schema, name) in still_there
        ]
        added = sum(len(listed) for listed in to_read.values())
        gone = sum(len(tables) for tables in stored.values()) - len(unchanged)
        if not added and not gone:
            assert previous is not None
            previous.checked_at = time.time()
            schema_knowledge.store.put(previous)
            yield steps.done("read", "No tables added or dropped")
            return _summary_for_user(previous, database, catalog)
    else:
        to_read = dict(listed_by_schema)
        unchanged = [table for schema in unlisted for table in stored.get(schema, {}).values()]

    max_tables = int(_config("NLSQL_INTERROGATE_MAX_TABLES", DEFAULT_MAX_TABLES))
    room = max(0, max_tables - len(unchanged))
    for schema, listed in to_read.items():
        if len(listed) > room:
            problems.append(Problem(
                f"{schema}: only {room} of its {len(listed)} tables were read - the limit of {max_tables} "
                "tables in all was reached (NLSQL_INTERROGATE_MAX_TABLES).",
                schema,
            ))
            to_read[schema] = listed[:room]
        room -= len(to_read[schema])

    mode = bulk_structure.mode_for(database)
    if mode == "per_table":
        metas = yield from _read_per_table(database, catalog, to_read, describe, steps, cancel_token, problems)
    else:
        metas = yield from _read_in_bulk(database, catalog, mode, to_read, describe, steps, cancel_token, problems)
    # A table that couldn't be read this time keeps what was read of it before.
    for schema, schema_metas in metas.items():
        for meta in schema_metas:
            if meta.error and meta.name in stored.get(schema, {}):
                unchanged.append(stored[schema][meta.name])
    unchanged_keys = {(table.schema, table.name) for table in unchanged}
    every = [(schema, meta) for schema, schema_metas in metas.items() for meta in schema_metas if (schema, meta.name) not in unchanged_keys]
    if not every and not unchanged:
        raise InterrogationError(f"There are no tables in {database.name}.")
    views = sum(1 for _schema, meta in every if meta.kind == "view")
    if changes_only:
        yield steps.done("read", f"{len(every)} added, {gone} dropped")
    else:
        indexes = sum(len(meta.indexes) for _schema, meta in every)
        keys = sum(len(meta.foreign_keys) for _schema, meta in every)
        yield steps.done(
            "read",
            f"{len(every) - views} tables, {views} views, {indexes} indexes, {keys} foreign key{'s' if keys != 1 else ''}"
            f" in {sum(1 for schema_metas in metas.values() if schema_metas)} schemas",
        )

    estimates: dict[tuple[str, str], float] = {}
    refused: dict[tuple[str, str], str] = {}
    if row_estimates and every:
        yield steps.start("rows", "Asking the planner - nothing is run")
        tables_only = [(schema, meta.name) for schema, meta in every if meta.kind == "table" and not meta.error]
        sizing = superset_db.estimate_rows_progressively(database, catalog, tables_only)
        while True:
            _check(cancel_token)
            try:
                done, total = next(sizing)
            except StopIteration as stop:
                sized = stop.value
                break
            yield steps.update("rows", f"Plan {done} of {total} - nothing is run")
        estimates = sized.estimates
        refused = sized.refused
        for (schema, name), message in refused.items():
            problems.append(Problem(f"{schema}.{name}: the database wouldn't plan a query on it ({message})", schema, name))
        if sized.error:
            problems.append(f"No table sizes: {_brief(sized.error)}")
        yield steps.done(
            "rows",
            f"{len(estimates)} of {len(tables_only)} tables sized" if estimates else f"No estimates: {_brief(sized.error)}",
        )

    dictionary: Optional[DataDictionary] = None
    coverage: Optional[data_dictionary.Coverage] = None
    if dictionary_config and every:
        where = ", ".join(dictionary_config.tables) or "nothing configured"
        yield steps.start("dictionary", where)
        dictionary, dictionary_problems = data_dictionary.read(database, catalog)
        problems.extend(dictionary_problems)
        if dictionary is not None:
            read_tables: dict[str, list[tuple[str, list[str]]]] = {}
            for schema, meta in every:
                read_tables.setdefault(schema, []).append((meta.name, [column.name for column in meta.columns]))
            for table in unchanged:
                read_tables.setdefault(table.schema, []).append((table.name, [column.name for column in table.columns]))
            coverage = data_dictionary.coverage(dictionary, read_tables)
        yield steps.done(
            "dictionary",
            f"{dictionary.size} descriptions in {dictionary.table}: it {coverage.summary()}"
            if dictionary is not None and coverage is not None else f"Couldn't read {where}",
        )

    described: dict[tuple[str, str], dict[str, Any]] = {}
    schema_descriptions: dict[str, str] = {
        schema: text for schema, text in (previous.schema_descriptions.items() if previous is not None else [])
        if changes_only or schema in unlisted
    }
    provider = None
    if describe and every:
        provider = resolve_llm_provider()
        new_metas = {schema: [meta for meta in schema_metas if (schema, meta.name) not in unchanged_keys] for schema, schema_metas in metas.items()}
        yield from _describe_all(provider, new_metas, estimates, steps, described, schema_descriptions, problems, cancel_token)

    tables = unchanged + [
        _table_description(
            meta,
            schema,
            described.get((schema, meta.name.lower())),
            estimates.get((schema, meta.name)),
            metas[schema],
            refused.get((schema, meta.name)),
            dictionary,
        )
        for schema, meta in every
    ]
    order = {schema: index for index, schema in enumerate(schemas)}
    tables.sort(key=lambda table: (order.get(table.schema, len(order)), table.name))
    read_schemas = [schema for schema in schemas if schema in listed_by_schema or schema in stored and schema in unlisted]
    if dictionary is not None:
        for schema in read_schemas:
            curated = dictionary.schema_description(schema)
            if curated:
                schema_descriptions[schema] = curated

    embedding_model: Optional[str] = None
    embedding_keys: dict[tuple[str, str], str] = {}
    if embed:
        embedder = resolve_embedding_provider()
        yield steps.start("embed", f"{len(tables)} tables with {embedder.model}")
        try:
            embedding_keys, reused = yield from table_embeddings.embed(tables, schema_descriptions, embedder, steps, cancel_token)
            embedding_model = embedder.model
            yield steps.done("embed", f"{len(tables)} tables" + (f", {reused} already embedded" if reused else ""))
        except CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 - prompts still get the tables, just not matched to the request
            logger.warning("embedding the tables failed: %s", ex)
            problems.append(f"The tables couldn't be embedded, so prompts get them without matching ({_brief(ex)})")
            yield steps.failed("embed", _brief(ex))

    yield steps.start("save")
    now = time.time()
    if changes_only and previous is not None:
        present = {(table.schema, table.name) for table in tables}
        problems = [
            problem for problem in previous.problems
            if getattr(problem, "table", None) is None or (problem.schema, problem.table) in present
        ] + problems
    problems = list({str(problem): problem for problem in problems}.values())
    knowledge = DatabaseKnowledge(
        database_id=database.id,
        database_name=database.name,
        catalog=catalog,
        tables=tables,
        schemas=read_schemas,
        schema_descriptions={schema: text for schema, text in schema_descriptions.items() if schema in read_schemas},
        described=describe or (changes_only and previous is not None and previous.described),
        dictionary=dictionary.table if dictionary is not None else (previous.dictionary if changes_only and previous else None),
        dictionary_coverage=coverage.to_json() if coverage is not None else (previous.dictionary_coverage if changes_only and previous else None),
        embedding_model=embedding_model,
        embedding_keys=embedding_keys,
        created_at=previous.created_at if changes_only and previous else now,
        created_by=previous.created_by if changes_only and previous else context.username,
        checked_at=now,
        changed_at=now,
        model=provider.model if provider is not None else (previous.model if changes_only and previous else None),
        dialect=database.dialect,
        skipped_schemas=skipped,
        problems=problems,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    schema_knowledge.store.put(knowledge)
    yield steps.done(
        "save",
        f"{len(every)} tables added, {gone} dropped" if changes_only
        else "Kept for everyone using this database; each user sees only the tables they may",
    )
    return _summary_for_user(knowledge, database, catalog)


def _summary_for_user(knowledge: DatabaseKnowledge, database: superset_db.DatabaseInfo, catalog: Optional[str]) -> dict[str, Any]:
    """`knowledge.summary()` of only the tables the current user may see - none when Superset can't say."""
    try:
        visible = set(superset_db.accessible_tables(database, catalog, knowledge.table_keys))
    except superset_db.AccessCheckFailed:
        visible = set()
    return knowledge.summary(visible)


def _brief(error: Any) -> str:
    """An error's first line, without any SQL a driver put in it, which can name schemas and tables."""
    text = str(error).split("[SQL:")[0].strip().splitlines()
    return (text[0] if text else "")[:160]


def _describe_all(
    provider: Any,
    metas: dict[str, list[TableMeta]],
    estimates: dict[tuple[str, str], float],
    steps: progress.Steps,
    described: dict[tuple[str, str], dict[str, Any]],
    schema_descriptions: dict[str, str],
    problems: list[str],
    cancel_token: Optional[CancelToken],
) -> EventStream:
    """The optional model pass: each schema's tables in batches, the answers into `described`
    (keyed by schema and lower-cased table name) and `schema_descriptions`."""
    plan = [(schema, batch) for schema, schema_metas in metas.items() for batch in _batches(schema_metas, schema, estimates)]
    yield steps.start("describe", f"{len(plan)} call{'s' if len(plan) != 1 else ''} to {provider.model}")
    summaries: dict[str, list[str]] = {}
    for number, (schema, batch) in enumerate(plan, start=1):
        _check(cancel_token)
        names = [meta.name for meta in batch]
        label = f"Batch {number} of {len(plan)}"
        yield steps.update("describe", label)
        try:
            raw = yield from _describe_batch(provider, schema, batch, estimates, steps, label, cancel_token)
        except CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 - keep what the structure alone says about them
            logger.warning("describing batch %d failed: %s", number, ex)
            problems.extend(Problem(f"{schema}.{name}: the model's description couldn't be read ({_brief(ex)})", schema, name) for name in names)
            continue
        for entry in raw.get("tables") or []:
            if isinstance(entry, dict) and entry.get("name"):
                described[(schema, str(entry["name"]).lower())] = entry
        if raw.get("schemaSummary"):
            summaries.setdefault(schema, []).append(str(raw["schemaSummary"]).strip())
    for schema, texts in summaries.items():
        if len(texts) == 1:
            schema_descriptions[schema] = texts[0]
            continue
        yield steps.update("describe", "Summarising a schema as a whole")
        lines = [
            (meta.name, (described.get((schema, meta.name.lower())) or {}).get("description"))
            for meta in metas[schema]
        ]
        summary = _summarise_schema(provider, schema, lines, cancel_token)
        if summary:
            schema_descriptions[schema] = summary
    yield steps.done("describe", f"{sum(1 for entry in described.values() if entry.get('description'))} tables described")


def _read_per_table(
    database: superset_db.DatabaseInfo,
    catalog: Optional[str],
    to_read: dict[str, list[tuple[str, str]]],
    describe: bool,
    steps: progress.Steps,
    cancel_token: Optional[CancelToken],
    problems: list[str],
) -> Any:
    """Each listed table's structure, read through the inspector one table at a time."""
    metas: dict[str, list[TableMeta]] = {}
    for schema_index, (schema, listed) in enumerate(to_read.items(), start=1):
        metas[schema] = []
        if not listed:
            continue
        where = f"Schema {schema_index} of {len(to_read)}"
        yield steps.update("read", where)
        with superset_db.schema_reader(database, catalog, schema, comments=describe) as read:
            for index, (name, kind) in enumerate(listed, start=1):
                _check(cancel_token)
                if index == 1 or index % 10 == 0 or index == len(listed):
                    yield steps.update("read", f"{where} - {index} of {len(listed)}")
                meta = read(name, kind)
                if meta.error:
                    problems.append(Problem(f"{schema}.{name}: couldn't read its columns ({_brief(meta.error)})", schema, name))
                metas[schema].append(meta)
    return metas


def _read_in_bulk(
    database: superset_db.DatabaseInfo,
    catalog: Optional[str],
    mode: str,
    to_read: dict[str, list[tuple[str, str]]],
    describe: bool,
    steps: progress.Steps,
    cancel_token: Optional[CancelToken],
    problems: list[str],
) -> Any:
    """The listed tables' structure from the catalog views, a group of schemas at a time (see
    ``bulk_structure``). A group whose result was cut short is split and read again; a schema that
    can't be read that way - too big for one result, or a query the database refused - is read per
    table instead, and says so."""
    run = bulk_structure.superset_runner(database, catalog)
    pending = bulk_structure.groups(mode, [schema for schema, listed in to_read.items() if listed])
    found: dict[str, list[TableMeta]] = {}
    per_table: dict[str, list[tuple[str, str]]] = {}
    done = 0
    total = sum(1 for listed in to_read.values() if listed)
    while pending:
        _check(cancel_token)
        group = pending.pop(0)
        yield steps.update("read", f"Reading {len(group)} schema{'s' if len(group) != 1 else ''} from the catalog ({done} of {total} done)")
        try:
            found.update(bulk_structure.read_group(database.dialect or "", run, catalog, {schema: to_read[schema] for schema in group}, describe))
            done += len(group)
        except bulk_structure.TooManyRows:
            if len(group) > 1:
                middle = len(group) // 2
                pending[:0] = [group[:middle], group[middle:]]
                continue
            problems.append(Problem(f"{group[0]}: too big to read from the catalog in one go, so read table by table", group[0]))
            per_table[group[0]] = to_read[group[0]]
        except Exception as ex:  # noqa: BLE001 - the inspector can still read it
            logger.warning("catalog read of %s failed: %s", group, ex)
            problems.append(
                f"{len(group)} schema{'s' if len(group) != 1 else ''} couldn't be read from the catalog ({_brief(ex)}), "
                "so were read table by table"
            )
            per_table.update({schema: to_read[schema] for schema in group})
    if per_table:
        found.update((yield from _read_per_table(database, catalog, per_table, describe, steps, cancel_token, problems)))
    metas: dict[str, list[TableMeta]] = {}
    for schema, listed in to_read.items():
        metas[schema] = found.get(schema, [])
        for meta in metas[schema]:
            if meta.error and schema not in per_table:
                problems.append(Problem(f"{schema}.{meta.name}: couldn't read its columns ({_brief(meta.error)})", schema, meta.name))
    return metas


def _check(cancel_token: Optional[CancelToken]) -> None:
    if cancel_token is not None and cancel_token.cancelled:
        raise CancelledError("The request was cancelled.")


def _batches(metas: list[TableMeta], schema: str, estimates: dict[tuple[str, str], float]) -> list[list[TableMeta]]:
    batches: list[list[TableMeta]] = []
    current: list[TableMeta] = []
    size = 0
    for meta in metas:
        length = len(structure_text(meta, estimates.get((schema, meta.name))))
        if current and (size + length > BATCH_CHARS or len(current) >= BATCH_TABLES):
            batches.append(current)
            current, size = [], 0
        current.append(meta)
        size += length
    if current:
        batches.append(current)
    return batches


def structure_text(meta: TableMeta, estimate: Optional[float] = None) -> str:
    """One table's structure as the model reads it."""
    header = f"{meta.kind.upper()} {meta.name}"
    if estimate is not None:
        header += f" (≈{int(estimate):,} rows, planner estimate)"
    if meta.comment:
        header += f" -- comment: {meta.comment}"
    lines = [header]
    references = {column: fk for fk in meta.foreign_keys for column in fk.columns}
    for column in meta.columns:
        text = f"  {column.name} {column.data_type}"
        if not column.nullable:
            text += " NOT NULL"
        if column.name in meta.primary_key:
            text += " PK"
        if column.name in references:
            fk = references[column.name]
            target = fk.referred_columns[fk.columns.index(column.name)] if len(fk.referred_columns) == len(fk.columns) else ",".join(fk.referred_columns)
            where = f"{fk.referred_schema}." if fk.referred_schema else ""
            text += f" FK -> {where}{fk.referred_table}.{target}"
        if column.enum_values:
            text += f" one of: {', '.join(column.enum_values[:20])}"
        if column.comment:
            text += f" -- comment: {column.comment}"
        lines.append(text)
    indexed = [f"{'unique ' if unique else ''}({', '.join(columns)})" for columns, unique in meta.indexes]
    if indexed:
        lines.append(f"  indexes: {'; '.join(indexed)}")
    return "\n".join(lines)


def _describe_batch(
    provider: Any,
    schema: str,
    batch: list[TableMeta],
    estimates: dict[tuple[str, str], float],
    steps: progress.Steps,
    label: str,
    cancel_token: Optional[CancelToken],
) -> EventStream:
    prompt = _describe_prompt(schema, batch, estimates)
    text = yield from _complete(provider, prompt, steps, label, cancel_token)
    payload = json.loads(nlsql_backend.extract_json(text))
    if not isinstance(payload, dict):
        raise ValueError("the response wasn't a JSON object")
    return payload


def _complete(provider: Any, prompt: str, steps: progress.Steps, label: str, cancel_token: Optional[CancelToken]) -> EventStream:
    """One model call, reporting how much it has written every so often."""
    stream = llm_client.stream_complete(provider, prompt, cancel_token, purpose="interrogate")
    written = 0
    reported = 0
    try:
        while True:
            try:
                chunk = next(stream)
            except StopIteration as stop:
                return stop.value or ""
            if chunk.reasoning:
                continue
            written += len(chunk.text)
            if written - reported >= 600:
                reported = written
                yield steps.update("describe", f"{label} - {written:,} characters written")
    finally:
        stream.close()


def _describe_prompt(schema: str, batch: list[TableMeta], estimates: dict[tuple[str, str], float]) -> str:
    structure = "\n\n".join(structure_text(meta, estimates.get((schema, meta.name))) for meta in batch)
    return (
        f"{nlsql_backend.dialect_note()}"
        f"Here is the structure of some of the tables in the schema \"{schema}\": names, column types, keys,\n"
        "indexes and any comments the database holds. You can't see any of the data.\n\n"
        f"{structure}\n\n"
        "Describe them for someone who will write SQL against them. Respond with ONLY a single JSON object\n"
        "(no markdown fences, no other text) with this shape:\n"
        "{\n"
        '  "schemaSummary": "...",\n'
        '  "tables": [{"name": "...", "description": "...", "synonyms": ["..."],\n'
        '              "columns": {"column_name": "..."},\n'
        '              "joins": [{"fromColumn": "...", "toTable": "...", "toColumn": "...", "note": "..."}]}]\n'
        "}\n\n"
        "- schemaSummary: one sentence on what these tables are about as a whole.\n"
        "- One entry in tables per table above, with its name exactly as given.\n"
        "- description: one sentence - what one row is, and what the table is for (\"One row per order line:\n"
        "  the product, quantity and price of each item ordered\"). Say if it looks like a fact table, a\n"
        "  lookup/dimension table, a log, a staging copy, or a view over others.\n"
        "- synonyms: up to three words a person might use for it (\"customers\" for \"cust_mst\"); may be empty.\n"
        "- columns: a few words for each column whose meaning or units aren't obvious from its name and\n"
        "  type - codes, abbreviations, flags, amounts (which currency? cents?), dates (of what?). Leave\n"
        "  obvious ones (id, name, created_at) out.\n"
        "- joins: columns that look like they join to another table above but have no FK declared\n"
        "  (\"customer_id\" -> customers.id). Only ones you're fairly sure of; may be empty.\n"
        "- Base everything on the names, types, keys and comments. Where you're guessing, say \"probably\".\n"
    )


def _summarise_schema(provider: Any, schema: str, tables: list[tuple[str, Optional[str]]], cancel_token: Optional[CancelToken]) -> Optional[str]:
    """One sentence about the whole schema, from the tables' own descriptions - when it took more
    than one call to describe them, no single call saw them all."""
    lines = "\n".join(f"- {name}: {description or '(no description)'}" for name, description in tables)
    prompt = (
        f"These are the tables in the database schema \"{schema}\":\n{lines}\n\n"
        "In one sentence, what is this schema about? Respond with ONLY the sentence."
    )
    try:
        text = llm_client.drain(llm_client.stream_complete(provider, prompt, cancel_token, purpose="interrogate"))
    except CancelledError:
        raise
    except Exception as ex:  # noqa: BLE001
        logger.info("schema summary failed: %s", ex)
        return None
    return text.strip() or None


def _table_description(
    meta: TableMeta,
    schema: str,
    described: Optional[dict[str, Any]],
    estimate: Optional[float],
    metas: list[TableMeta],
    refused: Optional[str] = None,
    dictionary: Optional[DataDictionary] = None,
) -> TableDescription:
    """The kept description of one table: its structure, plus whatever the model said about it
    (when it was asked). `metas` are the other tables of its schema, for checking inferred joins.

    Where `dictionary` describes the table or a column, that description stands in for the
    database's own comment: the people who own the data wrote it, and a comment saying the same
    again in other words is no help. The model's note still follows, in brackets."""
    described = described or {}
    notes = described.get("columns") if isinstance(described.get("columns"), dict) else {}
    lowered_notes = {str(key).lower(): str(value).strip() for key, value in (notes or {}).items() if value}
    fk_columns = {column for fk in meta.foreign_keys for column in fk.columns}
    columns = [
        ColumnDescription(
            name=column.name,
            data_type=column.data_type,
            description=_join(
                _curated(dictionary, column.comment, schema, meta.name, column.name),
                lowered_notes.get(column.name.lower()),
            ),
            nullable=column.nullable,
            enum_values=column.enum_values,
            is_primary_key=column.name in meta.primary_key,
            is_foreign_key=column.name in fk_columns,
        )
        for column in meta.columns
    ]
    relationships: list[RelationshipDescription] = []
    for fk in meta.foreign_keys:
        for index, column in enumerate(fk.columns):
            target = fk.referred_columns[index] if index < len(fk.referred_columns) else (fk.referred_columns or ["?"])[0]
            to_table = f"{fk.referred_schema or schema}.{fk.referred_table}"
            relationships.append(schema_knowledge.relationship(column, to_table, target, declared=True))
    by_name = {other.name.lower(): other for other in metas}
    own_columns = {column.name.lower() for column in meta.columns}
    for join in described.get("joins") or []:
        if not isinstance(join, dict):
            continue
        from_column = str(join.get("fromColumn") or "")
        target_table = by_name.get(str(join.get("toTable") or "").split(".")[-1].lower())
        to_column = str(join.get("toColumn") or "")
        # Kept only when it names columns that exist - a model's guess at a join is worth having,
        # a join to a column that isn't there is worse than none.
        if from_column.lower() not in own_columns or target_table is None:
            continue
        if to_column.lower() not in {column.name.lower() for column in target_table.columns}:
            continue
        if from_column in fk_columns:
            continue
        note = str(join.get("note") or "").strip() or None
        relationships.append(schema_knowledge.relationship(from_column, f"{schema}.{target_table.name}", to_column, declared=False, note=note))
    synonyms = [str(word) for word in described.get("synonyms") or [] if str(word).strip()][:3]
    description = _join(_curated(dictionary, meta.comment, schema, meta.name), str(described.get("description") or "").strip() or None)
    return TableDescription(
        schema=schema,
        name=meta.name,
        columns=columns,
        description=description,
        synonyms=synonyms,
        relationships=relationships,
        row_count_estimate=int(estimate) if estimate is not None else None,
        kind=meta.kind,
        indexes=[(tuple(columns), unique) for columns, unique in meta.indexes],
        usage_notes=f"The database refused to plan a query on it: {refused}" if refused else None,
    )


def _curated(dictionary: Optional[DataDictionary], comment: Optional[str], schema: str, table: str, column: Optional[str] = None) -> Optional[str]:
    """The data dictionary's description of the table (or its `column`), else the database's comment."""
    if dictionary is None:
        return comment
    if column is None:
        return dictionary.table_description(schema, table) or comment
    return dictionary.column_description(schema, table, column) or comment


def _join(comment: Optional[str], note: Optional[str]) -> Optional[str]:
    """The database's own comment and the model's note, without saying the same thing twice."""
    if comment and note and note.lower() not in comment.lower():
        return f"{comment} ({note})"
    return comment or note
