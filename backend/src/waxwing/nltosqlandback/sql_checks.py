"""The checks any SQL the model writes has to pass before it is offered to the user.

Three, in order of cost, each stricter than the last:

1. **It parses** - with sqlglot, in the dialect of the database the tab points at (Superset's own
   mapping, see ``superset_db.dialect_for``), not sqlglot's generic one, which accepts things a
   given engine would reject and vice versa.
2. **Superset's SQL validator passes it** - when the deployment has one configured for the engine
   (``SQL_VALIDATORS_BY_ENGINE``); the same check behind the editor's gutter markers.
3. **The database can plan it** - its ``EXPLAIN``, which catches what no parser can: a table or
   column that doesn't exist, a type mismatch, a function the engine doesn't have. Planning never
   runs the query. On by default whenever the request names a database; turn it off with
   ``NLSQL_VALIDATE_WITH_EXPLAIN = False``.

A failure at any of them is reported the same way - the step fails with the reason, and the caller
retries once with that reason in the prompt (see ``nlsql_backend._request_sql_generation``). A check
that couldn't be made (no database, no access, an engine planning doesn't support) is skipped and
says so, rather than counting against the SQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from flask import current_app

from . import progress, request_context, sql_analyzer, superset_db
from .progress import EventStream
from .query_plan import QueryPlan
from .superset_db import DatabaseInfo, DatabaseUnavailable

logger = logging.getLogger(__name__)

_DATABASE_KEY = "_database"


@dataclass
class SqlCheck:
    valid: bool
    error: Optional[str] = None
    #: What was checked, for the step's detail: "parsed as postgres; planned by the database".
    summary: str = ""
    #: The plan, when the database was asked for one - worth keeping: accelerate compares it.
    plan: Optional[QueryPlan] = None


def current_database() -> Optional[DatabaseInfo]:
    """The database the current request is about, resolved once per request; None when the
    request named none or it can't be used (logged, not raised - most callers can do without)."""
    context = request_context.current()
    if context is None or context.database_id is None:
        return None
    if _DATABASE_KEY in context.attributes:
        return context.attributes[_DATABASE_KEY]  # type: ignore[return-value]
    try:
        database = superset_db.resolve_database(context.database_id)
    except DatabaseUnavailable as ex:
        logger.info("database %s unavailable for checks: %s", context.database_id, ex)
        database = None
    context.attributes[_DATABASE_KEY] = database
    return database


def current_dialect() -> Optional[str]:
    database = current_database()
    return database.dialect if database else None


def _explain_enabled() -> bool:
    try:
        return bool(current_app.config.get("NLSQL_VALIDATE_WITH_EXPLAIN", True))
    except RuntimeError:
        return False


def check(sql: str, steps: progress.Steps, key: str = "parse", running_detail: Optional[str] = None) -> EventStream:
    """Runs the checks above on `sql`, reported as step `key`, and returns a `SqlCheck`."""
    yield steps.start(key, running_detail or "Nothing is written into the editor until this passes")
    database = current_database()
    dialect = database.dialect if database else None
    context = request_context.current()

    analysis = sql_analyzer.analyze(sql, dialect)
    if not analysis.valid:
        yield steps.failed(key, analysis.error or "It didn't parse")
        return SqlCheck(valid=False, error=analysis.error or "It didn't parse")
    done = [f"{len(sql.splitlines())} lines, parsed as {dialect or 'generic SQL'}"]

    if database is not None:
        messages = superset_db.validate(database, sql, context.catalog if context else None, context.schema if context else None)
        if messages:
            error = "; ".join(_located(message) for message in messages)
            yield steps.failed(key, f"Superset's SQL validator: {error}")
            return SqlCheck(valid=False, error=f"Superset's SQL validator reported: {error}")
        if messages is not None:
            done.append("passed Superset's validator")

    plan = None
    if database is not None and _explain_enabled():
        yield steps.update(key, f"Asking {database.name} to plan it (the query isn't run)")
        outcome = superset_db.explain(database, sql, context.catalog if context else None, context.schema if context else None)
        if outcome.ok:
            plan = outcome.plan
            done.append(f"planned by {database.name}")
        elif outcome.checked:
            yield steps.failed(key, f"{database.name} rejected it: {outcome.error}")
            return SqlCheck(valid=False, error=f"The database rejected it when planning: {outcome.error}")
        else:
            done.append(f"not planned ({outcome.error})")

    summary = "; ".join(done)
    yield steps.done(key, summary)
    return SqlCheck(valid=True, summary=summary, plan=plan)


def _located(message: superset_db.ValidationMessage) -> str:
    if message.line is None:
        return message.message
    return f"line {message.line}: {message.message}"
