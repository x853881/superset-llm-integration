"""Checks whether SQL text is syntactically valid, via `sqlglot <https://github.com/tobymao/sqlglot>`_
- already a Superset dependency, so this adds no new one.

Used to catch a "Success"-status LLM response that claims confidence but produced SQL that
doesn't actually parse, before it ever reaches the editor - see
``nlsql_backend.request_sql_generation``, which retries once (pointing out the exact parse error)
before giving up and reporting a "Failure" result instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot.errors import ParseError, TokenError


@dataclass
class SqlAnalysis:
    valid: bool
    error: Optional[str] = None


def analyze(sql: str, dialect: Optional[str] = None) -> SqlAnalysis:
    """Parses `sql` with sqlglot, in `dialect` (sqlglot's name for it - "postgres", "trino", ...)
    when the database is known, else its generic dialect. See ``sql_checks`` for the stricter checks
    that follow this one."""
    try:
        sqlglot.parse(sql, read=dialect or None)
        return SqlAnalysis(valid=True)
    except Exception as ex:  # noqa: BLE001 - any parse failure means "not valid SQL", not a bug here
        return SqlAnalysis(valid=False, error=str(ex))


@dataclass
class SyntaxProblem:
    """One place `sql` fails to parse, worded for the person who wrote it. `line` and `column` are
    1-based, as Superset's own validators report them; absent when sqlglot couldn't say where."""

    message: str
    line: Optional[int] = None
    column: Optional[int] = None


#: Jinja, which SQL Lab renders before a query runs - text no SQL parser can read as it stands.
_TEMPLATED = re.compile(r"{{|{%|{#")
_MISSING_FOR = re.compile(r"Required keyword: '\w+' missing for <class 'sqlglot\.expressions\.(\w+)'>")
#: sqlglot expression classes that are a clause of the same name, for "the WHERE clause is incomplete".
_CLAUSES = {"Where", "Having", "Group", "Order", "Limit", "Join", "From", "Qualify", "Window"}


def syntax_errors(sql: str, dialect: Optional[str]) -> list[SyntaxProblem]:
    """Where `sql` stops parsing in `dialect` - the editor's stand-in for an SQL validator on the
    engines Superset has none for. Never contacts the database, so it only knows syntax: an unknown
    table or column passes, and so does some text sqlglot is lenient about (a doubled comma, a
    missing one that reads as an alias). Empty when it parses, or when it's a Jinja template."""
    if not sql.strip() or _TEMPLATED.search(sql):
        return []
    try:
        sqlglot.parse(sql, read=dialect or None)
        return []
    except TokenError:
        return [SyntaxProblem("A quoted string, quoted name or comment is never closed.")]
    except ParseError as ex:
        problems = [_problem(error) for error in ex.errors]
        return problems or [SyntaxProblem(str(ex))]
    except Exception as ex:  # noqa: BLE001 - anything else sqlglot raises is still "doesn't parse"
        return [SyntaxProblem(str(ex))]


def _problem(error: dict) -> SyntaxProblem:
    description = str(error.get("description") or "").strip()
    token = str(error.get("highlight") or "").strip()
    near = f"'{token}'" if token else "the end"
    missing = _MISSING_FOR.fullmatch(description)
    if missing and missing.group(1) in _CLAUSES and token.upper().startswith(missing.group(1).upper()):
        message = f"The {token.upper()} clause is incomplete."
    elif missing:
        message = f"Something is missing before {near}."
    elif description.startswith("Expecting "):
        message = f"Expected {description[len('Expecting '):]} before {near}."
    elif description.startswith("Invalid expression") or description.startswith("Unexpected token"):
        message = f"Unexpected {near}."
    else:
        message = f"{description} (near {near})." if token else description
    line = error.get("line")
    column = error.get("col")
    # sqlglot's column is where the offending token ends; the validators report where it starts.
    if isinstance(column, int) and token:
        column = max(1, column - len(token) + 1)
    return SyntaxProblem(
        message=message,
        line=line if isinstance(line, int) else None,
        column=column if isinstance(column, int) else None,
    )
