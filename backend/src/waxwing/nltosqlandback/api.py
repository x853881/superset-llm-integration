"""REST API for the nltosqlandback extension.

Exposes the endpoints implementing bidirectional, grounded NL<->SQL conversion, plus the
explain-and-fix call behind the panel's SQL-error workflow. Calls run server-side rather than from
the browser so the configured LLM's API key never ships to the client, and so calling a local
Ollama server isn't subject to browser CORS restrictions.

Every LLM-backed endpoint answers with a **server-sent event stream** rather than one JSON body,
because these calls take tens of seconds against a local model and a spinner of unknown duration
is not feedback. Each event is a JSON object on a ``data:`` line, of one of five types (see
``progress``):

- ``stage``: which step of the pipeline the call has reached, and what it found. Re-sent with the
  same ``key`` to update a step in place; a new key implies the earlier steps are done.
- ``reset``/``delta``: the text of the answer as the model writes it, field by field - what makes
  the panel show SQL appearing line by line instead of all at once at the end.
- ``result``: the finished payload, exactly the object the non-streaming version of these
  endpoints used to return as ``result``.
- ``error`` / ``cancelled``: how the call ended if it didn't produce a result. Sent as events
  rather than as an HTTP status, since the response's status line is long gone by then.

``/cancel`` is the exception and stays an ordinary JSON endpoint: it's a second, concurrent request
*about* a call in flight, not a call of its own - as is ``/query-plan``, which asks the database for
a plan and involves no model at all.

**Context.** Every request may also carry the SQL Lab tab's ``databaseId`` and ``catalog`` (and its
``tabId``). They say which dialect to write and check SQL in, which database to validate and plan it
against (see ``sql_checks``), and are on every log line and trace the request produces. A
``schema`` is ignored - queries here qualify their tables, see ``_context`` - along with the Superset user who made it, which is read from the session here, never
from the request body. See ``request_context`` and ``telemetry``.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Iterator, Optional

from flask import Response, request, stream_with_context
from flask_appbuilder.api import expose, protect, safe
from superset_core.rest_api.api import RestApi
from superset_core.rest_api.decorators import api

from . import (
    cancellation,
    db_interrogation,
    nlsql_backend,
    performance,
    progress,
    prose,
    request_context,
    schema_knowledge,
    sql_analyzer,
    sql_breakdown,
    sql_format,
    sql_error_fix,
    superset_db,
    telemetry,
)
from .cancellation import CancelledError, CancelToken
from .progress import EventStream
from .request_context import CallContext
from .sql_error_fix import SqlError

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which would hold every event back until the
    # call finished - defeating the whole point of streaming them.
    "X-Accel-Buffering": "no",
}


@api(
    id="nltosqlandback_api",
    name="NL to SQL and back API",
    description="Bidirectional, grounded natural-language <-> SQL conversion",
)
class NlToSqlAndBackAPI(RestApi):
    openapi_spec_tag = "NL to SQL and back"
    # Own permission scope for this extension (declared in extension.json),
    # rather than borrowing an unrelated Superset resource's permissions.
    class_permission_name = "nltosqlandback"

    @expose("/generate-sql", methods=("POST",))
    @protect()
    @safe
    def generate_sql(self) -> Response:
        """Generate SQL from a natural-language request, with no previous SQL to ground against.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [naturalLanguage]
                  properties:
                    naturalLanguage:
                      type: string
                    requestId:
                      type: string
                      description: Opaque id the caller may later pass to /cancel to abort this call.
          responses:
            200:
              description: An event stream ending in a SqlGenerationResult
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        natural_language = (body.get("naturalLanguage") or "").strip()
        if not natural_language:
            return self.response(400, message="naturalLanguage must not be empty")
        return self._stream(
            lambda token: nlsql_backend.generate_sql(natural_language, token),
            _context("generate_sql", body, natural_language),
        )

    @expose("/update-sql-from-nl", methods=("POST",))
    @protect()
    @safe
    def update_sql_from_nl(self) -> Response:
        """Update previously-generated SQL to match a changed request, grounded in the previous SQL.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [previousNl, previousSql, newNl]
                  properties:
                    previousNl:
                      type: string
                    previousSql:
                      type: string
                    newNl:
                      type: string
                    requestId:
                      type: string
                      description: Opaque id the caller may later pass to /cancel to abort this call.
          responses:
            200:
              description: An event stream ending in a SqlGenerationResult
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        previous_nl = prose.strip_terms(body.get("previousNl") or "")
        previous_sql = body.get("previousSql") or ""
        new_nl = (body.get("newNl") or "").strip()
        if not new_nl:
            return self.response(400, message="newNl must not be empty")
        return self._stream(
            lambda token: nlsql_backend.update_sql_from_nl(previous_nl, previous_sql, new_nl, token),
            _context("update_sql_from_nl", body, new_nl),
        )

    @expose("/refine-sql", methods=("POST",))
    @protect()
    @safe
    def refine_sql(self) -> Response:
        """Apply a free-text instruction to the current SQL, independent of whatever
        natural-language request (if any) produced it.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [command, sql]
                  properties:
                    command:
                      type: string
                    sql:
                      type: string
                    requestId:
                      type: string
                      description: Opaque id the caller may later pass to /cancel to abort this call.
          responses:
            200:
              description: An event stream ending in a SqlGenerationResult
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        command = prose.strip_terms(body.get("command") or "").strip()
        sql = body.get("sql") or ""
        if not command:
            return self.response(400, message="command must not be empty")
        if not sql.strip():
            return self.response(400, message="sql must not be empty")
        return self._stream(
            lambda token: nlsql_backend.refine_sql(command, sql, token),
            _context("refine_sql", body, f"{command}\n\n{sql}"),
        )

    @expose("/generate-nl", methods=("POST",))
    @protect()
    @safe
    def generate_nl(self) -> Response:
        """Describe a SQL query in natural language, with no previous description to ground against.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql]
                  properties:
                    sql:
                      type: string
                    requestId:
                      type: string
                      description: Opaque id the caller may later pass to /cancel to abort this call.
          responses:
            200:
              description: An event stream ending in the generated description
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        sql = (body.get("sql") or "").strip()
        if not sql:
            return self.response(400, message="sql must not be empty")
        return self._stream(
            lambda token: nlsql_backend.generate_nl(sql, token),
            _context("generate_nl", body, sql),
        )

    @expose("/update-nl-from-sql-diff", methods=("POST",))
    @protect()
    @safe
    def update_nl_from_sql_diff(self) -> Response:
        """Update a previous description to reflect a SQL edit, grounded in the previous
        description and the diff between old and new SQL - not a fresh description of the new SQL.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [previousNl, previousSql, newSql]
                  properties:
                    previousNl:
                      type: string
                    previousSql:
                      type: string
                    newSql:
                      type: string
                    requestId:
                      type: string
                      description: Opaque id the caller may later pass to /cancel to abort this call.
          responses:
            200:
              description: An event stream ending in the updated description
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        previous_nl = prose.strip_terms(body.get("previousNl") or "")
        previous_sql = body.get("previousSql") or ""
        new_sql = (body.get("newSql") or "").strip()
        if not new_sql:
            return self.response(400, message="newSql must not be empty")
        return self._stream(
            lambda token: nlsql_backend.update_nl_from_sql_diff(previous_nl, previous_sql, new_sql, token),
            _context("update_nl_from_sql_diff", body, new_sql),
        )

    @expose("/explain-sql-error", methods=("POST",))
    @protect()
    @safe
    def explain_sql_error(self) -> Response:
        """Explain the errors the editor is showing against a query, and propose a corrected
        version of it. `errors` are the gutter annotations as Superset reports them (1-based line
        and column), or the message from a run the engine rejected.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql, errors]
                  properties:
                    sql:
                      type: string
                    errors:
                      type: array
                      items:
                        type: object
                        required: [message]
                        properties:
                          message:
                            type: string
                          line:
                            type: integer
                            description: 1-based line the error points at, if it has a position.
                          column:
                            type: integer
                            description: 1-based column the error points at, if it has a position.
                          source:
                            type: string
                            description: What produced it, e.g. "sql-validator", "parser" or "query".
                    dialect:
                      type: string
                      description: The database's engine name, where the caller knows it.
                    requestId:
                      type: string
                      description: Opaque id the caller may later pass to /cancel to abort this call.
          responses:
            200:
              description: An event stream ending in a SqlFixResult
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        sql = body.get("sql") or ""
        if not sql.strip():
            return self.response(400, message="sql must not be empty")
        raw_errors = body.get("errors")
        if not isinstance(raw_errors, list) or not raw_errors:
            return self.response(400, message="errors must be a non-empty array")
        errors = [SqlError.from_dict(raw) for raw in raw_errors if isinstance(raw, dict)]
        errors = [error for error in errors if error.message]
        if not errors:
            return self.response(400, message="every error must have a message")
        dialect = (body.get("dialect") or "").strip()
        return self._stream(
            lambda token: sql_error_fix.explain_and_fix(sql, errors, dialect, token),
            _context("explain_sql_error", body, sql),
        )

    @expose("/accelerate", methods=("POST",))
    @protect()
    @safe
    def accelerate(self) -> Response:
        """Suggest a faster rewrite of a query that returns the same result, checked before it is
        returned. With `runPlan` (the default) the database plans the original first - never runs
        it - and the model gets the plan as well as the SQL.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql]
                  properties:
                    sql:
                      type: string
                    runPlan:
                      type: boolean
                      description: Ask the database for the original's plan first. Defaults to true.
                    databaseId:
                      type: integer
                    catalog:
                      type: string
                    schema:
                      type: string
                    tabId:
                      type: string
                    requestId:
                      type: string
          responses:
            200:
              description: >
                An event stream ending in {rewrittenSql, rejectedSql, explanation, changes, basedOn,
                runPlan, plan, planError, rewrittenPlan, check}
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        sql = (body.get("sql") or "").strip()
        if not sql:
            return self.response(400, message="sql must not be empty")
        run_plan = body.get("runPlan", True) is not False
        context = _context("accelerate", body, sql)
        context.attributes["runPlan"] = run_plan
        return self._stream(lambda token: performance.accelerate(sql, run_plan, token), context)

    @expose("/explain-plan", methods=("POST",))
    @protect()
    @safe
    def explain_plan(self) -> Response:
        """Ask the database for a query's plan (it isn't run) and explain it in plain English.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql, databaseId]
                  properties:
                    sql:
                      type: string
                    databaseId:
                      type: integer
                    catalog:
                      type: string
                    schema:
                      type: string
                    tabId:
                      type: string
                    requestId:
                      type: string
          responses:
            200:
              description: An event stream ending in {explanation, plan}
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        sql = (body.get("sql") or "").strip()
        if not sql:
            return self.response(400, message="sql must not be empty")
        if _database_id(body) is None:
            return self.response(400, message="databaseId is required to plan a query")
        return self._stream(lambda token: performance.explain_plan(sql, token), _context("explain_plan", body, sql))

    @expose("/describe-breakdown", methods=("POST",))
    @protect()
    @safe
    def describe_breakdown(self) -> Response:
        """Describe a query section by section, each section with the line range of SQL it covers.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql]
                  properties:
                    sql:
                      type: string
                    databaseId:
                      type: integer
                    tabId:
                      type: string
                    requestId:
                      type: string
          responses:
            200:
              description: An event stream ending in {summary, sections[{title, kind, startLine, endLine, description}]}
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        sql = (body.get("sql") or "").strip("\n")
        if not sql.strip():
            return self.response(400, message="sql must not be empty")
        format_sql = bool(body.get("formatSql"))
        return self._stream(
            lambda token: sql_breakdown.describe_breakdown(sql, token, format_sql=format_sql),
            _context("describe_breakdown", body, sql),
        )

    @expose("/validate-sql", methods=("POST",))
    @protect()
    @safe
    def validate_sql(self) -> Response:
        """Superset's own SQL validator's verdict on the editor's SQL - the check behind the
        error markers in SQL Lab's gutter - asked from here rather than from the browser so that,
        like every other call the panel makes, it is on the trace (a ``validate_sql`` request with
        a ``db · validate`` span under it). No model, and the query isn't run.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql, databaseId]
                  properties:
                    sql:
                      type: string
                    databaseId:
                      type: integer
                    catalog:
                      type: string
                    tabId:
                      type: string
          responses:
            200:
              description: "result: [{message, line, column}], empty when nothing was found; checkedBy:
                validator (Superset's SQL validator) or parser (sqlglot, where there's no validator)"
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
            422:
              description: No SQL validator is configured and the database's dialect is unknown, so it can't be parsed either
        """
        body = request.json or {}
        sql = body.get("sql") or ""
        if not sql.strip():
            return self.response(400, message="sql must not be empty")
        context = _context("validate_sql", body, sql)
        if context.database_id is None:
            return self.response(400, message="databaseId is required")
        start_ns = telemetry.now_ns()
        with request_context.bound(context):
            try:
                database = superset_db.resolve_database(context.database_id)
            except superset_db.DatabaseUnavailable as ex:
                telemetry.record_request(context, start_ns, telemetry.now_ns(), "error", str(ex))
                return self.response(403, message=str(ex))
            assert database is not None
            messages = superset_db.validate(database, sql, context.catalog, None)
            checked_by = "validator"
            if messages is None:
                # No validator for this engine (the usual case - Superset has them for Postgres and
                # Presto only), or it fell over: parse it here instead, so a syntax error still
                # shows before the query is run. Without a dialect a generic parse would flag the
                # engine's own syntax, so that's the one case left with no check at all.
                if database.dialect is None:
                    telemetry.record_request(context, start_ns, telemetry.now_ns(), "ok")
                    return self.response(422, message=f"No SQL validator is configured for {database.backend}")
                messages = sql_analyzer.syntax_errors(sql, database.dialect)
                checked_by = "parser"
            result = [{"message": m.message, "line": m.line, "column": m.column} for m in messages]
            context.attributes["output"] = result
            context.attributes["checkedBy"] = checked_by
            telemetry.record_request(context, start_ns, telemetry.now_ns(), "ok")
        return self.response(200, result=result, checkedBy=checked_by)

    @expose("/format-sql", methods=("POST",))
    @protect()
    @safe
    def format_sql(self) -> Response:
        """The query laid out one clause per line, in the tab's database's dialect. No model.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql]
                  properties:
                    sql:
                      type: string
                    databaseId:
                      type: integer
          responses:
            200:
              description: "{sql, formatted}: formatted is false when it couldn't be parsed"
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        sql = body.get("sql") or ""
        if not sql.strip():
            return self.response(400, message="sql must not be empty")
        context = _context("format_sql", body)
        with request_context.bound(context):
            database = _database_quietly()
            formatted = sql_format.format_sql(sql, database.dialect if database else None, database.backend if database else None)
        return self.response(200, result={"sql": formatted or sql, "formatted": formatted is not None})

    @expose("/interrogate-db", methods=("POST",))
    @protect()
    @safe
    def interrogate_db(self) -> Response:
        """Read the structure of every schema of the tab's database - tables, columns, keys, indexes,
        row estimates - and keep it for every later prompt about that database.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [databaseId]
                  properties:
                    databaseId:
                      type: integer
                    catalog:
                      type: string
                    tabId:
                      type: string
                    requestId:
                      type: string
          responses:
            200:
              description: An event stream ending in the description's summary
              content:
                text/event-stream: {}
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        if _database_id(body) is None:
            return self.response(400, message="databaseId is required to interrogate a database")
        return self._stream(db_interrogation.interrogate, _context("interrogate_db", body))

    @expose("/db-knowledge", methods=("GET",))
    @protect()
    @safe
    def db_knowledge(self) -> Response:
        """Whether the tab's database has been interrogated: when, by whom, how many schemas and
        tables - and with ``full``, the tables themselves (only those this user may see).
        ---
        get:
          parameters:
          - in: query
            name: databaseId
            required: true
            schema:
              type: integer
          - in: query
            name: catalog
            schema:
              type: string
          - in: query
            name: full
            schema:
              type: boolean
          responses:
            200:
              description: "{status: none | ready, ...summary, tables?}"
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
        """
        args = request.args
        context = _context("db_knowledge", dict(args))
        if context.database_id is None:
            return self.response(400, message="databaseId is required")
        with request_context.bound(context):
            try:
                database = superset_db.resolve_database(context.database_id)
            except superset_db.DatabaseUnavailable as ex:
                return self.response(403, message=str(ex))
            assert database is not None
            knowledge = schema_knowledge.store.get(database.id, context.catalog)
            if knowledge is None:
                return self.response(200, result={"status": "none", "databaseName": database.name})
            visible = set(superset_db.accessible_tables(database, context.catalog, knowledge.table_keys))
            result = {"status": "ready", **knowledge.summary(visible)}
            if str(args.get("full") or "").lower() in ("1", "true", "yes"):
                result["tables"] = [schema_knowledge.table_to_json(table) for table in knowledge.visible_tables(visible)]
        return self.response(200, result=result)

    @expose("/db-knowledge/forget", methods=("POST",))
    @protect()
    @safe
    def forget_db_knowledge(self) -> Response:
        """Throw away what was found in the tab's database; prompts go back to working from the SQL
        alone.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [databaseId]
                  properties:
                    databaseId:
                      type: integer
                    catalog:
                      type: string
          responses:
            200:
              description: "{forgotten}"
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
        """
        body = request.json or {}
        context = _context("forget_db_knowledge", body)
        if context.database_id is None:
            return self.response(400, message="databaseId is required")
        with request_context.bound(context):
            try:
                database = superset_db.resolve_database(context.database_id)
            except superset_db.DatabaseUnavailable as ex:
                return self.response(403, message=str(ex))
            assert database is not None
            forgotten = schema_knowledge.store.delete(database.id, context.catalog)
        return self.response(200, result={"forgotten": forgotten})

    @expose("/query-plan", methods=("POST",))
    @protect()
    @safe
    def query_plan(self) -> Response:
        """The database's plan for a query, without running it and without involving a model.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [sql, databaseId]
                  properties:
                    sql:
                      type: string
                    databaseId:
                      type: integer
                    catalog:
                      type: string
                    schema:
                      type: string
          responses:
            200:
              description: "{plan} on success, {error, checked} if the database couldn't plan it"
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
            403:
              $ref: '#/components/responses/403'
        """
        body = request.json or {}
        sql = (body.get("sql") or "").strip()
        if not sql:
            return self.response(400, message="sql must not be empty")
        context = _context("query_plan", body, sql)
        if context.database_id is None:
            return self.response(400, message="databaseId is required to plan a query")
        start_ns = telemetry.now_ns()
        with request_context.bound(context):
            try:
                database = superset_db.resolve_database(context.database_id)
            except superset_db.DatabaseUnavailable as ex:
                telemetry.record_request(context, start_ns, telemetry.now_ns(), "error", str(ex))
                return self.response(403, message=str(ex))
            outcome = superset_db.explain(database, sql, context.catalog, context.schema)
            telemetry.record_request(context, start_ns, telemetry.now_ns(), "ok" if outcome.ok else "error", outcome.error)
        if outcome.ok:
            plan = outcome.plan.to_dict()
            if body.get("includeTree") and outcome.plan.root is not None:
                plan["tree"] = outcome.plan.root.to_dict()
            plan["durationMs"] = outcome.duration_ms
            return self.response(200, result={"plan": plan})
        return self.response(200, result={"error": outcome.error, "checked": outcome.checked})

    @expose("/cancel", methods=("POST",))
    @protect()
    @safe
    def cancel(self) -> Response:
        """Cancels an in-flight call by the `requestId` it was started with, forcibly closing its
        connection to the LLM provider server-side rather than just abandoning the client's wait.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [requestId]
                  properties:
                    requestId:
                      type: string
          responses:
            200:
              description: Whether a call matching requestId was still in flight and got cancelled
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        request_id = (body.get("requestId") or "").strip()
        if not request_id:
            return self.response(400, message="requestId must not be empty")
        return self.response(200, result={"cancelled": cancellation.cancel(request_id)})

    @expose("/feedback", methods=("POST",))
    @protect()
    @safe
    def feedback(self) -> Response:
        """What the user thought of a result: a thumbs up or down, for a thumbs down optionally one
        of the reasons the panel offered or their own words. Logged at the ``FEEDBACK`` level and
        sent as a ``feedback`` span into the trace of the request that produced the result (see
        `telemetry.record_feedback`). Nothing else is done with it: no model, no database.
        ---
        post:
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  type: object
                  required: [traceId, spanId, feedbackId, rating, target]
                  properties:
                    traceId:
                      type: string
                      description: 32 hex characters - the rated result's `trace.traceId`
                    spanId:
                      type: string
                      description: 16 hex characters - the rated result's `trace.spanId`
                    feedbackId:
                      type: string
                      description: the same for every change of mind about one result; the latest counts
                    rating:
                      type: string
                      enum: [up, down]
                    target:
                      type: string
                      description: what was rated - sql, sql-failure, fix, accelerate, explain-plan, describe, breakdown
                    reason:
                      type: string
                    label:
                      type: string
                    comment:
                      type: string
                    variant:
                      type: object
                      description: the panel's options at the time - layout, verbosity
          responses:
            200:
              description: Recorded
            400:
              $ref: '#/components/responses/400'
            401:
              $ref: '#/components/responses/401'
        """
        body = request.json or {}
        trace_id = str(body.get("traceId") or "").lower()
        span_id = str(body.get("spanId") or "").lower()
        if not (_is_hex(trace_id, 32) and _is_hex(span_id, 16)):
            return self.response(400, message="traceId and spanId must be the hex ids of a result's trace")
        rating = body.get("rating")
        if rating not in ("up", "down"):
            return self.response(400, message='rating must be "up" or "down"')
        feedback_id = _short_text(body.get("feedbackId"), 64)
        target = _short_text(body.get("target"), 40)
        if not feedback_id or not target:
            return self.response(400, message="feedbackId and target are required")
        variant = body.get("variant") if isinstance(body.get("variant"), dict) else {}
        user_id, username = request_context.current_user()
        telemetry.record_feedback(telemetry.Feedback(
            trace_id=trace_id,
            span_id=span_id,
            feedback_id=feedback_id,
            rating=rating,
            target=target,
            reason=_short_text(body.get("reason"), 60),
            label=_short_text(body.get("label"), 120),
            comment=_short_text(body.get("comment"), MAX_FEEDBACK_COMMENT),
            variant={str(key)[:40]: _short_text(value, 40) for key, value in list(variant.items())[:10]},
            user_id=user_id,
            username=username,
        ))
        return self.response(200, result={"recorded": True})

    def _stream(
        self,
        action: Callable[[Optional[CancelToken]], EventStream],
        context: CallContext,
    ) -> Response:
        """Runs `action` - passing it a `CancelToken` registered under the request's id if it gave
        one, so a concurrent call to `/cancel` with the same id can abort it - and forwards the
        progress events it yields to the client as they happen, followed by the payload it
        returned as a final `result` event.

        The whole pipeline runs inside `stream_with_context`, on this request's own thread and
        inside its app context, so the work reads Flask config and the reporting of that work are
        the same call stack (see `progress`). `context` is bound for the duration (see
        `request_context`), which is how every LLM call made below knows whose request it is; once
        the call ends, however it ends, the request itself is recorded (see `telemetry`). A client
        that hangs up simply stops pulling: the generator is closed, which closes the LLM
        connection it was reading from.
        """
        request_id = context.request_id
        token = cancellation.create(request_id) if request_id else None

        def events() -> Iterator[str]:
            start_ns = telemetry.now_ns()
            outcome, error = "cancelled", "The client stopped reading."
            with request_context.bound(context):
                stream = action(token)
                try:
                    while True:
                        try:
                            event = next(stream)
                        except StopIteration as stop:
                            context.attributes["output"] = stop.value
                            outcome, error = "ok", None
                            yield _sse(progress.result(stop.value, {"traceId": context.trace_id, "spanId": context.span_id}))
                            return
                        yield _sse(event)
                except CancelledError:
                    outcome, error = "cancelled", None
                    yield _sse(progress.cancelled())
                except Exception as ex:  # noqa: BLE001 - surface the error to the frontend
                    logger.exception("nlsql request failed")
                    outcome, error = "error", str(ex)
                    yield _sse(progress.error(f"Request failed: {ex}"))
                finally:
                    stream.close()
                    if request_id:
                        cancellation.release(request_id)
                    telemetry.record_request(context, start_ns, telemetry.now_ns(), outcome, error)

        return Response(stream_with_context(events()), mimetype="text/event-stream", headers=SSE_HEADERS)


def _database_quietly() -> Optional[superset_db.DatabaseInfo]:
    """The current request's database, or None when there's no database to ask."""
    from . import sql_checks

    return sql_checks.current_database()


def _database_id(body: dict) -> Optional[int]:
    value = body.get("databaseId")
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text or None


def _context(endpoint: str, body: dict, input_text: Optional[str] = None) -> CallContext:
    """The `CallContext` for this request: the user from the session (never the body), the tab's
    database/catalog, request id and verbosity (the panel's "Brief" checkbox - see `prose`) from the
    body. SQL Lab's selected schema is deliberately
    ignored: queries here qualify every table with its schema, so nothing - prompts, plans,
    interrogation - should depend on which one the tab happens to have selected."""
    user_id, username = request_context.current_user()
    context = CallContext(
        endpoint=endpoint,
        user_id=user_id,
        username=username,
        request_id=_optional_text(body.get("requestId")),
        database_id=_database_id(body),
        catalog=_optional_text(body.get("catalog")),
        schema=None,
        verbosity=prose.verbosity(body.get("verbosity")),
    )
    tab_id = _optional_text(body.get("tabId"))
    if tab_id:
        context.attributes["tabId"] = tab_id
    if input_text is not None:
        context.attributes["input"] = input_text
    return context


#: How much of someone's own words a feedback record keeps.
MAX_FEEDBACK_COMMENT = 2000


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(c in "0123456789abcdef" for c in value)


def _short_text(value: object, limit: int) -> Optional[str]:
    """`value` as trimmed text of at most `limit` characters, or None when there's none."""
    text = _optional_text(value)
    return text[:limit] if text else None


def _sse(event: dict[str, object]) -> str:
    """One server-sent event: the JSON of `event` on a `data:` line, terminated by a blank line."""
    return f"data: {json.dumps(event)}\n\n"
