"""Interrogate a database: read the structure of every schema through Superset, and keep it.

There's no semantic schema for most databases a Superset points at, and a model asked to write SQL
against tables it has never been told about can only guess. But a database knows a lot about
itself without anyone reading its data: schema, table and view names, columns and their types,
primary and foreign keys, indexes, enum types, and - from its planner - roughly how big each table
is. That's what this reads, for every schema of the tab's database the user may see (all through
``superset_db``, so through Superset's own connection and permission checks, and never a ``SELECT``
of anyone's rows).

Optionally (``NLSQL_INTERROGATE_DESCRIPTIONS``, off by default) it then hands the structure to the
model in batches, to write a sentence about each table, a few words about each column whose purpose
isn't obvious from its name, and the joins it can see that no foreign key declares. Off by default
because a database of many schemas makes that a lot of model calls, and a much bigger prompt.

The result (``schema_knowledge.DatabaseKnowledge``) is kept per database and given, whole, to
every prompt about that database afterwards - see ``nlsql_backend.schema_preamble``.

Like every other pipeline here it's a generator of progress events, so the panel can say which
schema and table it has got to.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from flask import current_app

from . import llm_client, nlsql_backend, progress, request_context, schema_knowledge, superset_db
from .cancellation import CancelToken, CancelledError
from .llm_provider import resolve_llm_provider
from .progress import EventStream
from .schema_knowledge import DatabaseKnowledge
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


def interrogate(cancel_token: Optional[CancelToken] = None, describe: Optional[bool] = None) -> EventStream:
    """Interrogates every schema of the current request's database and returns its `summary()`.
    `describe` (default ``NLSQL_INTERROGATE_DESCRIPTIONS``) has the model describe the tables too."""
    started = time.monotonic()
    if describe is None:
        describe = bool(_config("NLSQL_INTERROGATE_DESCRIPTIONS", False))
    row_estimates = bool(_config("NLSQL_INTERROGATE_ROW_ESTIMATES", True))
    steps = progress.Steps([
        ("schemas", "Listing the schemas", "Listed the schemas"),
        ("read", "Reading the tables, columns and indexes", "Read the tables, columns and indexes"),
        *([("rows", "Estimating table sizes", "Estimated table sizes")] if row_estimates else []),
        *([("describe", "Describing the tables", "Described the tables")] if describe else []),
        ("save", "Saving what was found", "Saved what was found"),
    ])
    yield steps.plan_event

    context = request_context.current()
    if context is None or context.database_id is None:
        raise InterrogationError("Pick a database for this tab first.")
    database = superset_db.resolve_database(context.database_id)
    assert database is not None
    catalog = context.catalog

    yield steps.start("schemas", f"In {database.name}")
    schemas, skipped = superset_db.list_schemas(database, catalog, refresh=True)
    if not schemas:
        raise InterrogationError(f"There are no schemas in {database.name} that you can see.")
    yield steps.done(
        "schemas",
        f"{len(schemas)} schema{'s' if len(schemas) != 1 else ''}"
        + (f" ({skipped} of the database's own left out)" if skipped else ""),
    )

    max_tables = int(_config("NLSQL_INTERROGATE_MAX_TABLES", DEFAULT_MAX_TABLES))
    problems: list[str] = []
    metas: dict[str, list[TableMeta]] = {}
    read_count = 0
    yield steps.start("read", f"{len(schemas)} schemas")
    for schema_index, schema in enumerate(schemas, start=1):
        _check(cancel_token)
        where = f"Schema {schema_index} of {len(schemas)}: {schema}"
        yield steps.update("read", where)
        try:
            listed = superset_db.list_tables(database, catalog, schema, refresh=True)
        except Exception as ex:  # noqa: BLE001 - one unreadable schema shouldn't lose the rest
            problems.append(f"{schema}: couldn't list its tables ({str(ex)[:160]})")
            continue
        if read_count + len(listed) > max_tables:
            room = max(0, max_tables - read_count)
            problems.append(
                f"{schema}: only {room} of its {len(listed)} tables were read - the limit of {max_tables} "
                "tables in all was reached (NLSQL_INTERROGATE_MAX_TABLES)."
            )
            listed = listed[:room]
        metas[schema] = []
        if not listed:
            continue
        with superset_db.schema_reader(database, catalog, schema, comments=describe) as read:
            for index, (name, kind) in enumerate(listed, start=1):
                _check(cancel_token)
                if index == 1 or index % 10 == 0 or index == len(listed):
                    yield steps.update("read", f"{where} - {index} of {len(listed)} ({name})")
                meta = read(name, kind)
                if meta.error:
                    problems.append(f"{schema}.{name}: couldn't read its columns ({meta.error[:160]})")
                metas[schema].append(meta)
        read_count += len(listed)
        if read_count >= max_tables:
            break
    every = [(schema, meta) for schema, schema_metas in metas.items() for meta in schema_metas]
    if not every:
        raise InterrogationError(f"There are no tables in {database.name} that you can see.")
    views = sum(1 for _schema, meta in every if meta.kind == "view")
    indexes = sum(len(meta.indexes) for _schema, meta in every)
    keys = sum(len(meta.foreign_keys) for _schema, meta in every)
    yield steps.done(
        "read",
        f"{len(every) - views} tables, {views} views, {indexes} indexes, {keys} foreign key{'s' if keys != 1 else ''}"
        f" in {sum(1 for schema_metas in metas.values() if schema_metas)} schemas",
    )

    estimates: dict[tuple[str, str], float] = {}
    refused: dict[tuple[str, str], str] = {}
    if row_estimates:
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
            problems.append(f"{schema}.{name}: the database wouldn't plan a query on it ({message})")
        if sized.error:
            problems.append(f"No table sizes: {sized.error[:200]}")
        yield steps.done(
            "rows",
            f"{len(estimates)} of {len(tables_only)} tables sized" if estimates else f"No estimates: {sized.error}",
        )

    described: dict[tuple[str, str], dict[str, Any]] = {}
    schema_descriptions: dict[str, str] = {}
    provider = None
    if describe:
        provider = resolve_llm_provider()
        yield from _describe_all(provider, metas, estimates, steps, described, schema_descriptions, problems, cancel_token)

    tables = [
        _table_description(
            meta,
            schema,
            described.get((schema, meta.name.lower())),
            estimates.get((schema, meta.name)),
            metas[schema],
            refused.get((schema, meta.name)),
        )
        for schema, meta in every
    ]

    yield steps.start("save")
    knowledge = DatabaseKnowledge(
        database_id=database.id,
        database_name=database.name,
        catalog=catalog,
        tables=tables,
        schemas=list(metas),
        schema_descriptions=schema_descriptions,
        described=describe,
        created_by=context.username,
        model=provider.model if provider is not None else None,
        dialect=database.dialect,
        skipped_schemas=skipped,
        problems=problems,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    schema_knowledge.store.put(knowledge)
    yield steps.done("save", "Kept for everyone using this database, and given to every prompt about it")
    return knowledge.summary()


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
        label = f"Batch {number} of {len(plan)}: {schema} - {', '.join(names[:4])}{'…' if len(names) > 4 else ''}"
        yield steps.update("describe", label)
        try:
            raw = yield from _describe_batch(provider, schema, batch, estimates, steps, label, cancel_token)
        except CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 - keep what the structure alone says about them
            logger.warning("describing batch %d failed: %s", number, ex)
            problems.append(f"{schema}: {', '.join(names)}: the model's description couldn't be read ({str(ex)[:160]})")
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
        yield steps.update("describe", f"Summarising {schema} as a whole")
        lines = [
            (meta.name, (described.get((schema, meta.name.lower())) or {}).get("description"))
            for meta in metas[schema]
        ]
        summary = _summarise_schema(provider, schema, lines, cancel_token)
        if summary:
            schema_descriptions[schema] = summary
    yield steps.done("describe", f"{sum(1 for entry in described.values() if entry.get('description'))} tables described")


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
) -> TableDescription:
    """The kept description of one table: its structure, plus whatever the model said about it
    (when it was asked). `metas` are the other tables of its schema, for checking inferred joins."""
    described = described or {}
    notes = described.get("columns") if isinstance(described.get("columns"), dict) else {}
    lowered_notes = {str(key).lower(): str(value).strip() for key, value in (notes or {}).items() if value}
    fk_columns = {column for fk in meta.foreign_keys for column in fk.columns}
    columns = [
        ColumnDescription(
            name=column.name,
            data_type=column.data_type,
            description=_join(column.comment, lowered_notes.get(column.name.lower())),
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
    description = _join(meta.comment, str(described.get("description") or "").strip() or None)
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


def _join(comment: Optional[str], note: Optional[str]) -> Optional[str]:
    """The database's own comment and the model's note, without saying the same thing twice."""
    if comment and note and note.lower() not in comment.lower():
        return f"{comment} ({note})"
    return comment or note
