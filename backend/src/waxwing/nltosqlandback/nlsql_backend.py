"""Turns natural-language requests into SQL and back, grounding each
direction in what came before rather than regenerating independently.

- ``update_sql_from_nl`` grounds the new SQL in the *previous* SQL - an
  edit, not a fresh translation of the new request alone.
- ``update_nl_from_sql_diff`` grounds the new description in the *previous*
  description plus the diff between old and new SQL - an edit of the
  existing description, not a fresh description of the whole new query.
- ``refine_sql`` applies a free-text instruction to the current SQL,
  independent of whichever natural-language request (if any) produced it.

``generate_sql``, ``update_sql_from_nl`` and ``refine_sql`` don't just hand back SQL text - the
model is asked to respond with a ``SqlGenerationResult``-shaped JSON object (see
``SQL_RESPONSE_FORMAT_INSTRUCTIONS``) so it can self-report an ambiguous or unanswerable request
instead of guessing. A "Success" result's SQL is then checked by ``sql_checks`` - parsed in the
database's own dialect, run past Superset's SQL validator where one is configured, and planned by
the database itself - since the model's own claimed confidence doesn't guarantee any of that. If
a check fails the call is retried once: the same request plus the invalid SQL and what was wrong
with it, asking the model to fix it. If the retry's SQL still fails, this gives up and reports a
"Failure" result instead of handing back SQL known not to work.

The schema section of each prompt comes from one of two places:

- the bundled semantic schema (an embedding search over its table descriptions), when
  ``NLSQL_SEMANTIC_SCHEMA_ENABLED`` is set - off by default, since a deployment with a semantic
  description of its own databases is the exception;
- otherwise, the tab's database as interrogated by "Interrogate DB" (see ``db_interrogation`` and
  ``schema_knowledge``): every table of every schema the user may see, whole - no embeddings,
  because the structure alone is small enough to give the model all of it. (SQL Lab's selected
  schema plays no part: queries here qualify every table with its schema.)

With neither, the model works from the SQL itself, which is enough for describing, refining, fixing
and speeding up a query, if not for writing one from nothing.

Every one of those entry points is a *generator*, yielding ``progress`` events as it goes (which
stage it has reached, and each piece of text as the model writes it) and returning the finished
result - so a caller can show the work in progress rather than only its outcome. See ``progress``
for why generators, and ``api`` for how they reach the browser.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
from typing import Callable, Optional

from flask import current_app

from . import change_anchors, llm_client, progress, prose, query_tables, request_context, schema_knowledge, sql_checks, sql_format, superset_db
from .cancellation import CancelToken
from .embedding_cache import EmbeddingCache
from .embedding_provider import resolve_embedding_provider
from .line_diff import line_diff
from .llm_provider import resolve_llm_provider
from .progress import EventStream
from .schema_filter import EmbeddingSchemaFilter
from .semantic_schema import load_semantic_schema
from .sql_generation_result import STATUS_FAILURE, STATUS_SUCCESS, SqlGenerationResult
from .streaming_json import StreamingJsonString

logger = logging.getLogger(__name__)

_filter_lock = threading.Lock()
_filter_instance: Optional[EmbeddingSchemaFilter] = None
_embedding_cache: Optional[EmbeddingCache] = None

# The field of the JSON response each flow streams into the panel as the model writes it - the SQL
# itself for the SQL-generating flows (see `streaming_json`), the whole completion for the
# plain-text description flows.
SQL_FIELD = "sqlText"
NL_FIELD = "naturalLanguage"
#: The other field of a SQL response worth watching arrive: the model writes it straight after the
#: SQL, so it starting is what says the SQL itself is finished.
SQL_SUMMARY_FIELD = "sqlDetailedDescription"


def schema_enabled() -> bool:
    """Whether prompts get a schema section from the bundled semantic schema - see the module
    docstring. Off outside an app context, so nothing tries to embed without config."""
    try:
        return bool(current_app.config.get("NLSQL_SEMANTIC_SCHEMA_ENABLED", False))
    except RuntimeError:
        return False


TABLES_STEP = ("tables", "Finding the right tables", "Found the right tables")
CHECK_STEP = ("parse", "Checking it's valid", "Checked it's valid")


def with_tables_step(steps: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """`steps` with the schema lookup taken out when there is no schema description to look in - a
    step that never does anything is noise in the progress display."""
    if schema_enabled() or interrogated_schema() is not None:
        return steps
    return [step for step in steps if step[0] != "tables"]


_KNOWLEDGE_KEY = "_schema_knowledge"


def interrogated_schema() -> Optional[schema_knowledge.DatabaseKnowledge]:
    """What "Interrogate DB" found in the current request's database, if it has been run on it.
    Looked up once per request."""
    context = request_context.current()
    if context is None or context.database_id is None:
        return None
    if _KNOWLEDGE_KEY in context.attributes:
        return context.attributes[_KNOWLEDGE_KEY]  # type: ignore[return-value]
    knowledge = None
    try:
        knowledge = schema_knowledge.store.get(context.database_id, context.catalog)
    except Exception as ex:  # noqa: BLE001 - a prompt without it is still a prompt
        logger.warning("could not read the database description: %s", ex)
    context.attributes[_KNOWLEDGE_KEY] = knowledge
    return knowledge


def _interrogated_preamble(knowledge: schema_knowledge.DatabaseKnowledge, query_text: str, steps: progress.Steps) -> EventStream:
    reporting = steps.has("tables")
    if reporting:
        yield steps.start("tables", f"Using what Interrogate DB found in {knowledge.database_name}")
    visible = None
    database = sql_checks.current_database()
    if database is not None:
        visible = set(superset_db.accessible_tables(database, knowledge.catalog, knowledge.table_keys))
    max_chars = int(_config("NLSQL_SCHEMA_MAX_CHARS", schema_knowledge.DEFAULT_MAX_PROMPT_CHARS))
    text, shown, left_out = schema_knowledge.prompt_text(knowledge, query_text, visible, max_chars)
    schemas = len({table.schema for table in knowledge.visible_tables(visible)})
    where = f"{schemas} schema{'s' if schemas != 1 else ''} of {knowledge.database_name}"
    detail = f"All {shown} tables in {where}" if not left_out else (
        f"{shown} of {shown + left_out} tables in {where} - the rest didn't fit"
    )
    if reporting:
        yield steps.done("tables", detail)
    return f"{dialect_note()}Database schema:\n{text}\n\n"


def query_schema_preamble(sql: str, steps: progress.Steps) -> EventStream:
    """`schema_preamble` for a call that describes `sql` rather than writing new SQL: only the
    tables the query reads (see `query_tables`), out of what Interrogate DB found - a describer needs
    no more, and it keeps the prompt to a few thousand characters however big the database is.

    Falls back to `schema_preamble` when there's a semantic schema configured (its embedding search
    already narrows), and to the whole interrogated database when none of the query's tables can be
    found in it (it doesn't parse, or names tables the interrogation never saw)."""
    if schema_enabled():
        return (yield from schema_preamble(sql, steps))
    knowledge = interrogated_schema()
    if knowledge is None:
        return dialect_note() + "\n" if dialect_note() else ""
    used = query_tables_in_knowledge(sql, knowledge)
    if not used:
        return (yield from _interrogated_preamble(knowledge, sql, steps))
    reporting = steps.has("tables")
    if reporting:
        yield steps.start("tables", f"Looking up the query's tables in what Interrogate DB found in {knowledge.database_name}")
    max_chars = int(_config("NLSQL_SCHEMA_MAX_CHARS", schema_knowledge.DEFAULT_MAX_PROMPT_CHARS))
    keys = {(table.schema, table.name) for table in used}
    text, shown, _left_out = schema_knowledge.prompt_text(knowledge, sql, keys, max_chars, only_used=True)
    if reporting:
        names = ", ".join(f"{table.schema}.{table.name}" for table in used[:6]) + (" …" if len(used) > 6 else "")
        yield steps.done("tables", f"{shown} table{'s' if shown != 1 else ''} the query reads: {names}")
    return f"{dialect_note()}Database schema:\n{text}\n\n"


def query_tables_in_knowledge(sql: str, knowledge: schema_knowledge.DatabaseKnowledge) -> list:
    """The interrogated tables `sql` reads that this user may see, in the order the query names them."""
    database = sql_checks.current_database()
    visible = None
    if database is not None:
        visible = set(superset_db.accessible_tables(database, knowledge.catalog, knowledge.table_keys))
    dialect = database.dialect if database else (knowledge.dialect or None)
    wanted = query_tables.tables_used(sql, dialect)
    return query_tables.match_tables(wanted, knowledge.visible_tables(visible))


def _config(name: str, default: object) -> object:
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _sql_steps(draft_running: str, draft_done: str) -> progress.Steps:
    """The plan for a call that produces SQL. `draft_*` differ per flow - drafting from scratch,
    updating to match a changed question, applying a refinement - and the rest is the same work
    every time.

    Ordered as it runs, which puts the summary before the validity check: the model writes its own
    description of the query immediately after the query itself, while the check can only happen
    once the whole response has arrived and been decoded.
    """
    return progress.Steps(with_tables_step([
        ("read", "Read the question", "Read the question"),
        TABLES_STEP,
        ("draft", draft_running, draft_done),
        ("summary", "Writing the plain-English summary", "Wrote the summary"),
        CHECK_STEP,
    ]))


def _nl_steps(draft_running: str, draft_done: str) -> progress.Steps:
    """The plan for a call that produces a description of SQL, which has neither a summary of its
    own nor anything to check."""
    return progress.Steps(with_tables_step([
        ("read", "Read the SQL", "Read the SQL"),
        TABLES_STEP,
        ("draft", draft_running, draft_done),
    ]))


def dialect_note() -> str:
    """A line telling the model which database the SQL is for, when the request says - which
    engine's functions, quoting and date arithmetic to use is half of writing SQL that works."""
    dialect = sql_checks.current_dialect()
    if not dialect:
        return ""
    database = sql_checks.current_database()
    name = DIALECT_NAMES.get(dialect, dialect)
    where = f" ({database.name})" if database else ""
    return f"The database is {name}{where}: write SQL in its dialect.\n"


DIALECT_NAMES = {
    "postgres": "PostgreSQL",
    "trino": "Trino",
    "presto": "Presto",
    "oracle": "Oracle",
    "mysql": "MySQL",
    "sqlite": "SQLite",
    "tsql": "SQL Server",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Amazon Redshift",
    "duckdb": "DuckDB",
}


def _quoted(text: str, limit: int = 120) -> str:
    """`text` as a one-line quotation, short enough to sit under a step as its detail."""
    flattened = " ".join(text.split())
    return f"“{flattened[:limit]}…”" if len(flattened) > limit else f"“{flattened}”"


def _get_schema_filter() -> EmbeddingSchemaFilter:
    """Builds the embedding-backed schema filter once per process and reuses it - computing every
    table's embedding is the expensive part, and the bundled cache means most of those calls are
    free anyway (as long as the configured embedding provider/model matches what the cache was
    built with)."""
    global _filter_instance, _embedding_cache
    with _filter_lock:
        if _filter_instance is None:
            if _embedding_cache is None:
                _embedding_cache = EmbeddingCache.load()
            schema = load_semantic_schema()
            provider = resolve_embedding_provider()
            _filter_instance = EmbeddingSchemaFilter.for_schema(schema, provider, _embedding_cache)
        return _filter_instance


def schema_preamble(query_text: str, steps: progress.Steps) -> EventStream:
    """The schema section of a prompt: the tables most relevant to `query_text`, as prompt text.

    A generator so it can report what it settled on - which tables a request was answered against
    is the single most useful thing to know about an answer that turns out to be wrong.

    Without a semantic schema configured (see `schema_enabled`) it's what "Interrogate DB" found in
    the tab's database, whole, if it has been run on it; failing that, the dialect note is all the
    prompt gets.
    """
    if not schema_enabled():
        knowledge = interrogated_schema()
        if knowledge is not None:
            return (yield from _interrogated_preamble(knowledge, query_text, steps))
        return dialect_note() + "\n" if dialect_note() else ""
    yield steps.start("tables", "Matching the request against the database schema")
    top_k = current_app.config.get("NLSQL_SCHEMA_TOP_K", 8)
    schema = _get_schema_filter().filter(query_text, top_k)
    table_names = [table.name for table in schema.tables]
    columns = sum(len(table.columns) for table in schema.tables)
    yield steps.done(
        "tables",
        f"{len(table_names)} tables, {columns} columns: {', '.join(table_names)}"
        if table_names
        else "No tables matched",
    )
    return f"{dialect_note()}Database schema:\n{schema.to_prompt_text()}\n\n"


def generate_sql(natural_language: str, cancel_token: Optional[CancelToken] = None) -> EventStream:
    """Cold start: generate SQL for a natural-language request with no prior SQL to ground against."""
    steps = _sql_steps("Drafting SQL", "Drafted the SQL")
    yield steps.plan_event
    yield steps.done("read", _quoted(natural_language))
    preamble = yield from schema_preamble(natural_language, steps)
    prompt = _cold_start_sql_prompt(natural_language, preamble)
    result = yield from _request_sql_generation(prompt, steps, cancel_token)
    # Nothing to have changed from, so nothing to show changes of - whatever the model volunteered.
    return dataclasses.replace(result, changes=[]).to_dict()


def update_sql_from_nl(
    previous_nl: str, previous_sql: str, new_nl: str, cancel_token: Optional[CancelToken] = None
) -> EventStream:
    """The request changed from `previous_nl` to `new_nl` after `previous_sql` had already been
    generated for `previous_nl`. Produces updated SQL grounded in `previous_sql`."""
    steps = _sql_steps("Updating the SQL", "Updated the SQL")
    yield steps.plan_event
    yield steps.done("read", _quoted(new_nl))
    preamble = yield from schema_preamble(new_nl, steps)
    prompt = _grounded_sql_update_prompt(previous_nl, previous_sql, new_nl, preamble)
    result = yield from _request_sql_generation(prompt, steps, cancel_token)
    return _with_placed_changes(result, previous_sql).to_dict()


def refine_sql(command: str, sql: str, cancel_token: Optional[CancelToken] = None) -> EventStream:
    """Applies the free-text instruction `command` to the current `sql`."""
    steps = _sql_steps("Applying your refinement", "Applied your refinement")
    yield steps.plan_event
    yield steps.done("read", _quoted(command))
    preamble = yield from schema_preamble(sql, steps)
    prompt = _refine_prompt(command, sql, preamble)
    result = yield from _request_sql_generation(prompt, steps, cancel_token)
    return _with_placed_changes(result, sql).to_dict()


def generate_nl(sql: str, cancel_token: Optional[CancelToken] = None) -> EventStream:
    """Cold start: describe an existing SQL query in natural language, with no prior description."""
    steps = _nl_steps("Writing the description", "Wrote the description")
    yield steps.plan_event
    yield steps.done("read", f"{len(sql.splitlines())} lines from the editor")
    preamble = yield from query_schema_preamble(sql, steps)
    prompt = _cold_start_nl_prompt(sql, preamble)
    text = yield from _request_text(prompt, steps, NL_FIELD, cancel_token)
    return {NL_FIELD: text}


def update_nl_from_sql_diff(
    previous_nl: str, previous_sql: str, new_sql: str, cancel_token: Optional[CancelToken] = None
) -> EventStream:
    """The SQL changed from `previous_sql` to `new_sql` while it was described by `previous_nl`.
    Produces an updated description conditioned on `previous_nl` and the diff between the two SQL
    versions, rather than re-describing the new SQL independently."""
    steps = _nl_steps("Updating the description", "Updated the description")
    yield steps.plan_event
    diff = line_diff(previous_sql, new_sql)
    yield steps.done("read", f"{len(diff.splitlines())} changed lines since it was last described")
    preamble = yield from query_schema_preamble(new_sql, steps)
    prompt = _grounded_nl_update_prompt(previous_nl, diff, new_sql, preamble)
    text = yield from _request_text(prompt, steps, NL_FIELD, cancel_token)
    return {NL_FIELD: text}


def _request_sql_generation(prompt: str, steps: progress.Steps, cancel_token: Optional[CancelToken] = None) -> EventStream:
    """One SQL-generation attempt, plus the parse check on what it produced and - if that fails -
    one repair attempt pointing out the exact parse error. Returns a `SqlGenerationResult`.

    The repair is reported as a second attempt at the same step rather than as steps of its own:
    from the outside it is the same work being done again, and a plan that grew two extra rows
    every time the model slipped would be harder to read, not easier.
    """
    result = yield from request_and_decode(prompt, steps, cancel_token)
    if result.status != STATUS_SUCCESS:
        return result
    analysis = yield from sql_checks.check(result.sql_text, steps)
    if analysis.valid:
        return formatted(result)

    retry_result = yield from request_and_decode(
        retry_prompt_for(prompt, result.sql_text, analysis.error or ""),
        steps,
        cancel_token,
        attempt_note="Second attempt - the first draft wasn't valid",
    )
    if retry_result.status != STATUS_SUCCESS:
        return retry_result
    retry_analysis = yield from sql_checks.check(retry_result.sql_text, steps)
    if retry_analysis.valid:
        return formatted(retry_result)

    return SqlGenerationResult(
        status=STATUS_FAILURE,
        failure_message=(
            "The SQL the model produced wasn't valid, even after being asked to fix it: "
            f"{retry_analysis.error or analysis.error}"
        ),
        what_was_tried_on_failure=[result.sql_text, retry_result.sql_text],
        suggestions_for_fixing_on_failure=[
            "Try rephrasing the request",
            "Try again - the model may do better on a retry",
        ],
    )


def _with_placed_changes(result: SqlGenerationResult, previous_sql: str) -> SqlGenerationResult:
    """`result` with its changes placed by the SQL they quote (see `change_anchors`): against the
    query as it was and the one now on offer - after it was laid out, so against what the editor
    will hold. Without SQL to offer there is nothing to place them in."""
    if result.status != STATUS_SUCCESS:
        return dataclasses.replace(result, changes=[])
    changes = change_anchors.anchor(result.changes, previous_sql, result.sql_text)
    change_anchors.note_on_request(changes)
    return dataclasses.replace(result, changes=changes)


def formatted(result: SqlGenerationResult) -> SqlGenerationResult:
    """`result` with its SQL laid out one clause per line, as SQL Lab's "Format SQL" would (see
    `sql_format`) - unless ``NLSQL_FORMAT_GENERATED_SQL`` is off, or it can't be laid out without
    changing what it means, when it's left as the model wrote it."""
    if not result.sql_text or not _config("NLSQL_FORMAT_GENERATED_SQL", True):
        return result
    database = sql_checks.current_database()
    text = sql_format.format_sql(
        result.sql_text,
        database.dialect if database else None,
        database.backend if database else None,
    )
    if text is None or text == result.sql_text:
        return result
    return dataclasses.replace(result, sql_text=text)


def request_and_decode(
    prompt: str,
    steps: progress.Steps,
    cancel_token: Optional[CancelToken] = None,
    attempt_note: Optional[str] = None,
) -> EventStream:
    """Sends `prompt`, streaming out the response's `sqlText` and then its summary as the model
    writes them, then decodes the finished response and strips a stray fence from `sqlText` on
    success."""
    raw_text = yield from _stream_completion(
        prompt, steps, [SQL_FIELD, SQL_SUMMARY_FIELD], cancel_token, attempt_note
    )
    result = SqlGenerationResult.from_dict(json.loads(extract_json(raw_text)))
    if result.status == STATUS_SUCCESS:
        result.sql_text = strip_sql_fence(result.sql_text)
    return result


def stream_json_fields(
    prompt: str,
    steps: progress.Steps,
    fields: list[str],
    cancel_token: Optional[CancelToken] = None,
    attempt_note: Optional[str] = None,
    on_text: Optional[Callable[[str], list[dict]]] = None,
) -> EventStream:
    """Runs one model call, yielding each of `fields`' values out of the JSON response as they are
    written (see `streaming_json`), and returns the whole raw completion for the caller to decode
    properly. More than one field because a response can have more than one part worth watching
    appear - an explanation and the corrected SQL under it, say. `on_text`, given the response so
    far after each chunk, returns any further events to send - for a part of the response that isn't
    a string, like the breakdown's list of sections, which it sends a whole section at a time."""
    return (yield from _stream_completion(prompt, steps, fields, cancel_token, attempt_note, on_text=on_text))


def _request_text(prompt: str, steps: progress.Steps, field: str, cancel_token: Optional[CancelToken]) -> EventStream:
    """Runs one model call whose whole response is the answer (no JSON wrapper), streaming it out
    as it's written, and returns it stripped."""
    text = yield from _stream_completion(prompt, steps, None, cancel_token, plain_field=field)
    return text.strip()


def _stream_completion(
    prompt: str,
    steps: progress.Steps,
    fields: Optional[list[str]],
    cancel_token: Optional[CancelToken],
    attempt_note: Optional[str] = None,
    plain_field: str = NL_FIELD,
    on_text: Optional[Callable[[str], list[dict]]] = None,
) -> EventStream:
    """One model call, reported as the "draft" step and streamed into `fields` - or, when `fields`
    is None, streamed verbatim into `plain_field` because the whole response is the answer.

    The three things a watcher sees here are the three phases a call actually has, which against a
    local model can each run to minutes: the prompt being read (nothing comes back at all, so this
    says how much was sent and to whom), the model thinking (a reasoning model's own words, streamed
    as it writes them), and the answer being written (the SQL itself, appearing as it is typed).

    Pulled with `next()` rather than `yield from` because this interleaves its own progress events
    with the stream's chunks, which `yield from` (which forwards a stream verbatim) can't do.
    """
    provider = resolve_llm_provider()
    partials = [(field, StreamingJsonString(field)) for field in (fields or [])]
    summary_started = False

    sent = f"Sent {len(prompt):,} characters to {provider.model} - waiting for the first token"
    yield steps.start("draft", f"{attempt_note}. {sent}" if attempt_note else sent)
    yield progress.reset(progress.REASONING_FIELD)
    for field in fields or []:
        yield progress.reset(field)

    context = request_context.current()
    stream = llm_client.stream_complete(
        provider,
        prompt,
        cancel_token,
        purpose=context.endpoint if context else "completion",
        attempt=2 if attempt_note else 1,
    )
    buffer = ""
    reasoning_chars = 0
    raw_text = ""
    try:
        while True:
            try:
                chunk = next(stream)
            except StopIteration as stop:
                raw_text = stop.value or ""
                break
            if chunk.reasoning:
                if not reasoning_chars:
                    yield steps.update("draft", f"{provider.model} is thinking it through")
                reasoning_chars += len(chunk.text)
                yield progress.delta(progress.REASONING_FIELD, chunk.text)
                continue
            if not buffer:
                yield steps.update(
                    "draft",
                    f"{provider.model} is writing"
                    + (f", after {reasoning_chars:,} characters of reasoning" if reasoning_chars else ""),
                )
            buffer += chunk.text
            if on_text is not None:
                for event in on_text(buffer):
                    yield event
            if not partials:
                yield progress.delta(plain_field, chunk.text)
                continue
            for field, partial in partials:
                text = partial.feed(buffer)
                if not text:
                    continue
                # The model writes its summary of the query directly after the query itself, so the
                # summary starting is what says the SQL is finished - the one moment in the call
                # where one step genuinely hands over to the next.
                if field == SQL_SUMMARY_FIELD and not summary_started:
                    summary_started = True
                    yield steps.done("draft", f"{len(buffer):,} characters from {provider.model}")
                    yield steps.start("summary", f"{provider.model} is describing what it wrote")
                yield progress.delta(field, text)
    finally:
        stream.close()

    written = f"{len(raw_text):,} characters from {provider.model}"
    yield steps.done("summary" if summary_started else "draft", written)
    return raw_text


def retry_prompt_for(original_prompt: str, invalid_sql: str, parse_error: str) -> str:
    """The same prompt again, with the SQL it produced and what was wrong with it - a parse error,
    Superset's validator, or the database refusing to plan it (see `sql_checks`) - the one retry
    any generated SQL gets before it's given up on. Shared with `sql_error_fix` and `performance`,
    whose SQL is checked the same way."""
    return (
        f"{original_prompt}\n\n"
        "You previously responded with this SQL, but it isn't valid:\n"
        f"{invalid_sql}\n\n"
        f"Problem: {parse_error}\n\n"
        "Fix the SQL so it is valid, and respond again in the same JSON format as before.\n"
    )


SQL_RESPONSE_FORMAT_INSTRUCTIONS = """\
Respond with ONLY a single JSON object (no markdown fences, no other text) with this shape:
{
  "status": "Success" | "Failure" | "ClarificationsRequired",
  "sqlText": "...",
  "sqlDetailedDescription": "...",
  "tablesUsed": ["..."],
  "suggestionsForRefinement": ["..."],
  "assumptionsMade": ["..."],
  "specificClarificationsNeeded": [{"question": "...", "options": ["..."]}],
  "generalClarificationsNeeded": ["..."],
  "failureMessage": "...",
  "whatWasTriedOnFailure": ["..."],
  "suggestionsForFixingOnFailure": ["..."]
}

Write the fields in the order listed above - the SQL is shown to the user as you write it, so
"status" and "sqlText" coming first is what makes that possible.

Only populate the fields relevant to the status you report:
- "Success": sqlText, sqlDetailedDescription, tablesUsed, suggestionsForRefinement, assumptionsMade.
- "ClarificationsRequired": specificClarificationsNeeded and/or generalClarificationsNeeded.
- "Failure": failureMessage, whatWasTriedOnFailure, suggestionsForFixingOnFailure.

Only report "Success" and produce SQL when you are confident it is correct and uses only
tables and columns that actually exist - those in the schema above, or where no schema is given,
those the existing query already uses. If you need to make a reasonable
assumption to proceed, make it, produce the SQL anyway, and list the assumption in
assumptionsMade rather than asking about it - for example:
["\\"Last quarter\\" means the latest quarter present in the data, not the current calendar quarter",
 "Cancelled orders are excluded (status <> 'Cancelled')"]

Use specificClarificationsNeeded when you can offer a concrete, enumerable choice between a
small number of interpretations - for example:
[{"question": "Two columns could mean \\"revenue\\". Pick one and I will retry",
  "options": ["The recorded order value",
               "quantity_ordered x price_each - computed from the line items. Differs from sales on discounted orders"]}]
Use generalClarificationsNeeded for anything more open-ended than a multiple-choice question
can capture - for example: ["Please describe what you mean by total sales"]

suggestionsForRefinement are short, concrete follow-up requests the user might want next, not
questions - for example:
["Only the top 5 product lines", "Add the percentage change between the two quarters", "Break this down by territory as well"]

Only use "Failure" when you cannot produce SQL at all - e.g. the request doesn't relate to
anything in the schema.
"""


def _sql_prose(with_changes: bool = False) -> str:
    """How to write a SQL response's prose (see `prose`). Not its refinement suggestions or
    clarifications, which go back into the panel as input when picked, so stay unmarked."""
    if with_changes:
        return prose.instructions(
            "sqlDetailedDescription, assumptionsMade and each change's summary and why",
            "sqlDetailedDescription in one sentence, each assumption a short phrase, each change's summary a few words "
            "and its why a short phrase",
        )
    return prose.instructions(
        "sqlDetailedDescription and assumptionsMade",
        "sqlDetailedDescription in one sentence, each assumption a short phrase",
    )


def _changes_format(original: str, reason: str) -> str:
    """The `changes` field a refinement or an update adds to the response format - what each change
    was, why, and the SQL either side of it, quoted so the panel can show it beside the lines."""
    return (
        f'On "Success", also include, after the fields above, {change_anchors.CHANGES_SHAPE}:\n'
        + change_anchors.changes_instructions("sqlText", original, f"why, in a sentence - what in the {reason} it answers.")
        + "\n"
    )


def _description_length() -> str:
    return "one short plain-English sentence" if prose.is_brief() else "one or two plain-English sentences"


def _brief_description_note() -> str:
    """A description is the question box's input, so it gets the brief note but never term markup."""
    return "Keep it to one short sentence - the user has asked for brief descriptions.\n" if prose.is_brief() else ""


def _cold_start_sql_prompt(natural_language: str, schema_preamble_text: str) -> str:
    return (
        f"{schema_preamble_text}"
        "Translate the following request into a single SQL query.\n\n"
        f"{SQL_RESPONSE_FORMAT_INSTRUCTIONS}\n"
        f"{_sql_prose()}"
        f"Request: {natural_language}\n"
    )


def _grounded_sql_update_prompt(
    previous_nl: str, previous_sql: str, new_nl: str, schema_preamble_text: str
) -> str:
    return (
        f"{schema_preamble_text}"
        "You previously translated a request into SQL.\n"
        f"Previous request: {previous_nl}\n"
        "Previous SQL:\n"
        f"{previous_sql}\n\n"
        f"The request has changed to: {new_nl}\n\n"
        "Update the SQL to match the new request. Change only what needs to change - keep the\n"
        "rest of the query's structure and style as close to the previous SQL as possible.\n\n"
        f"{SQL_RESPONSE_FORMAT_INSTRUCTIONS}"
        f"{_changes_format('the previous SQL', 'new request')}"
        f"{_sql_prose(with_changes=True)}"
    )


def _refine_prompt(command: str, sql: str, schema_preamble_text: str) -> str:
    return (
        f"{schema_preamble_text}"
        "The SQL editor currently contains the following query:\n"
        f"{sql}\n\n"
        "Refine this SQL according to the following instruction. Change only what the instruction\n"
        "asks for - keep everything else about the query's structure and style unchanged.\n\n"
        f"{SQL_RESPONSE_FORMAT_INSTRUCTIONS}\n"
        f"{_changes_format('the query in the editor', 'instruction')}"
        f"{_sql_prose(with_changes=True)}"
        f"Instruction: {command}\n"
    )


def _cold_start_nl_prompt(sql: str, schema_preamble_text: str) -> str:
    return (
        f"{schema_preamble_text}"
        f"Describe what the following SQL query does, in {_description_length()},\n"
        "as if stating the request that produced it. Respond with ONLY the description.\n\n"
        "SQL:\n"
        f"{sql}\n"
    )


def _grounded_nl_update_prompt(
    previous_nl: str, sql_diff: str, new_sql: str, schema_preamble_text: str
) -> str:
    return (
        f"{schema_preamble_text}"
        "A SQL query was described in natural language as:\n"
        f"{previous_nl}\n\n"
        "The SQL was then edited. Diff (- old, + new):\n"
        f"{sql_diff}\n\n"
        "Full updated SQL, for reference:\n"
        f"{new_sql}\n\n"
        "Update the description to reflect this change. Edit the existing description - keep\n"
        "its wording where the change doesn't affect it - rather than writing a fresh\n"
        "description of the whole query. Respond with ONLY the updated description.\n"
        f"{_brief_description_note()}"
    )


def extract_json(text: str) -> str:
    """Strips a leading/trailing ```json fence, in case the model wraps its JSON response in one
    despite instructions, then raises if nothing but the fence (or nothing at all) is left.

    A `<think>` block goes too: a reasoning model whose provider doesn't separate its thinking (see
    `llm_client`) writes it inline, ahead of the answer, and it is not JSON."""
    stripped = _without_thinking(text).strip()
    if stripped.startswith("```json"):
        stripped = stripped[len("```json"):]
    elif stripped.startswith("```"):
        stripped = stripped[len("```"):]
    if stripped.endswith("```"):
        stripped = stripped[: -len("```")]
    stripped = stripped.strip()
    if not stripped:
        raise RuntimeError("The model returned no response.")
    return stripped


def _without_thinking(text: str) -> str:
    """`text` with a leading `<think>...</think>` block removed, closed or not - a call cut short
    mid-thought leaves it unclosed, and either way there is no answer in it."""
    stripped = text.lstrip()
    if not stripped.startswith("<think>"):
        return text
    closed_at = stripped.find("</think>")
    return "" if closed_at < 0 else stripped[closed_at + len("</think>"):]


def strip_sql_fence(text: str) -> str:
    """Strips a leading/trailing ```sql fence, in case the model wraps `sqlText` in one despite
    instructions, then raises if nothing but the fence (or nothing at all) is left - a "Success"
    result with no usable SQL is a backend error, not something to hand to the caller silently."""
    stripped = text.strip()
    if stripped.startswith("```sql"):
        stripped = stripped[len("```sql"):]
    elif stripped.startswith("```"):
        stripped = stripped[len("```"):]
    if stripped.endswith("```"):
        stripped = stripped[: -len("```")]
    stripped = stripped.strip()
    if not stripped:
        raise RuntimeError("The model reported success but returned no SQL.")
    return stripped
