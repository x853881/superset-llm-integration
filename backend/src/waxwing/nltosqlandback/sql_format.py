"""Lays a query out one clause per line - for generated SQL before it reaches the editor, and for the
breakdown, whose sections are line ranges and so read far better against a query that isn't all on
one line.

The layout is Superset's own: ``superset.sql.parse.SQLScript(...).format()`` - what SQL Lab's
"Format SQL" button does - when the host provides it, otherwise the same thing directly (sqlglot,
in the database's dialect, pretty-printed, comments kept). Deterministic, no model.

sqlglot formats by parsing the query and writing it out again, so besides whitespace it can change
how something is spelt (``!=`` becomes ``<>``, Postgres' ``x::int`` becomes ``CAST(x AS INT)``,
keywords and function names are upper-cased). Two guards keep that from ever changing what a query
*means*: anything sqlglot can't parse, or only keeps as an opaque command, is left exactly as
written; and the formatted query is parsed again and must come out as the same syntax tree as the
original, or the original is kept.
"""

from __future__ import annotations

import logging
from typing import Optional

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


def format_sql(sql: str, dialect: Optional[str], engine: Optional[str] = None) -> Optional[str]:
    """`sql` laid out one clause per line, or None when it can't be (it doesn't parse, or laying it
    out would change its syntax tree). `engine` is the database's backend name, for Superset's own
    formatter; `dialect` is sqlglot's name for it."""
    text = sql.strip()
    if not text:
        return None
    trailing_semicolon = text.endswith(";")
    statements = _parse(text, dialect)
    if statements is None:
        return None
    formatted = _superset_format(text, engine) if engine else None
    if formatted is None:
        try:
            parts = [statement.sql(dialect=dialect or None, pretty=True, comments=True) for statement in statements]
        except Exception as ex:  # noqa: BLE001
            logger.debug("could not format SQL: %s", ex)
            return None
        formatted = ";\n\n".join(part.strip() for part in parts if part.strip())
    formatted = formatted.strip().rstrip(";").rstrip()
    if not formatted:
        return None
    again = _parse(formatted, dialect)
    if again is None or len(again) != len(statements) or any(a != b for a, b in zip(again, statements)):
        logger.info("not formatting: the formatted query didn't parse back to the same query")
        return None
    return formatted + (";" if trailing_semicolon or len(statements) > 1 else "")


def _parse(text: str, dialect: Optional[str]) -> Optional[list[exp.Expression]]:
    try:
        statements = [statement for statement in sqlglot.parse(text, read=dialect or None) if statement is not None]
    except Exception as ex:  # noqa: BLE001 - any parse failure: leave it alone
        logger.debug("not formatting unparseable SQL: %s", ex)
        return None
    if not statements or any(isinstance(statement, exp.Command) for statement in statements):
        # A Command is sqlglot's "didn't understand this, kept it as text" - not safe to lay out.
        return None
    return statements


def _superset_format(text: str, engine: str) -> Optional[str]:
    """SQL Lab's own "Format SQL", when there's a Superset to ask."""
    try:
        from superset.sql.parse import SQLScript
    except Exception:  # noqa: BLE001 - no host (tests, tools)
        return None
    try:
        return SQLScript(text, engine).format()
    except Exception as ex:  # noqa: BLE001 - fall back to formatting it here
        logger.debug("Superset couldn't format the SQL: %s", ex)
        return None
