"""Which tables a query reads, and which of their columns it joins and filters on - read from the
query itself with sqlglot, no model and no database.

Two uses, both for prompts and panels that start from SQL rather than from a question:

- **A smaller prompt.** Describing a query (Break it down, Describe) only needs the tables it reads,
  not the whole database - ``tables_used`` names them, and ``nlsql_backend.query_schema_preamble``
  keeps just those from what Interrogate DB found. A typical prompt shrinks from tens of thousands
  of characters to a couple of thousand.
- **Facts per section of a breakdown** (``section_facts``): each section's tables with their
  estimated size, and a hint where the query joins or filters a large table on a column no index
  starts with - the usual reason a query over a big table is slow. All of it from the stored
  structure, so it costs nothing to compute.

Lines come from sqlglot's own token positions (1-based), the same numbering the breakdown's
sections use. Anything sqlglot can't parse yields nothing rather than an error: these are extras.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import sqlglot
from sqlglot import exp

from .semantic_schema import TableDescription

logger = logging.getLogger(__name__)

#: A table at least this big (by the planner's estimate) is worth a hint when the query joins or
#: filters it on a column no index starts with.
DEFAULT_LARGE_TABLE_ROWS = 1_000_000


@dataclass(frozen=True)
class TableUse:
    """One mention of a table in the query: as written (`schema` None when unqualified), the alias
    it goes by, and the line it's on."""

    schema: Optional[str]
    name: str
    alias: Optional[str]
    line: Optional[int]


@dataclass(frozen=True)
class ColumnUse:
    """A column the query joins (`role` "join") or filters (`role` "filter") on, with the table use
    it belongs to (None when that can't be told) and its line."""

    table: Optional[TableUse]
    column: str
    role: str
    line: Optional[int]


def parse(sql: str, dialect: Optional[str]) -> list[exp.Expression]:
    try:
        return [statement for statement in sqlglot.parse(sql, read=dialect or None) if statement is not None]
    except Exception as ex:  # noqa: BLE001 - unparseable: no extras, not an error
        logger.debug("could not parse the query for its tables: %s", ex)
        return []


def table_uses(statements: list[exp.Expression]) -> list[TableUse]:
    """Every table the statements read, in the order the query mentions them - not their CTEs,
    which are names the query made up."""
    uses: list[tuple[tuple[int, int, int], TableUse]] = []
    for number, statement in enumerate(statements):
        ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        for table in statement.find_all(exp.Table):
            if not table.name:
                continue
            if not table.db and table.name.lower() in ctes:
                continue
            meta = getattr(table.this, "meta", None) or {}
            uses.append(((number, meta.get("line") or 0, meta.get("col") or 0), TableUse(
                schema=table.db or None,
                name=table.name,
                alias=table.alias or None,
                line=_line(table.this),
            )))
    uses.sort(key=lambda pair: pair[0])
    return [use for _position, use in uses]


def tables_used(sql: str, dialect: Optional[str]) -> list[tuple[Optional[str], str]]:
    """The distinct (schema, name) pairs `sql` reads, schema None where it isn't written."""
    seen: dict[tuple[Optional[str], str], None] = {}
    for use in table_uses(parse(sql, dialect)):
        seen.setdefault((use.schema.lower() if use.schema else None, use.name.lower()), None)
    return list(seen)


def column_uses(statements: list[exp.Expression]) -> list[ColumnUse]:
    """The columns each SELECT joins on (its JOIN ... ON conditions) and filters on (its WHERE),
    each tied to the table it belongs to: by its qualifier when it has one, otherwise to the only
    table that SELECT reads."""
    uses: list[ColumnUse] = []
    for statement in statements:
        ctes = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
        for select in statement.find_all(exp.Select):
            scope = _select_tables(select, ctes)
            for join in select.args.get("joins") or []:
                on = join.args.get("on")
                if on is not None:
                    uses.extend(_columns_in(on, scope, "join"))
            where = select.args.get("where")
            if where is not None:
                uses.extend(_columns_in(where, scope, "filter"))
    return uses


def _select_tables(select: exp.Select, ctes: set[str]) -> dict[str, Optional[TableUse]]:
    """The tables a SELECT reads directly (its FROM and JOINs, not those of its subqueries), keyed by
    the names a column could qualify them by. A CTE maps to None: it isn't a stored table."""
    scope: dict[str, Optional[TableUse]] = {}
    sources = []
    from_ = select.args.get("from") or select.args.get("from_")
    if from_ is not None:
        sources.append(from_.this)
    sources.extend(join.this for join in select.args.get("joins") or [])
    for source in sources:
        if not isinstance(source, exp.Table) or not source.name:
            continue
        is_cte = not source.db and source.name.lower() in ctes
        use = None if is_cte else TableUse(source.db or None, source.name, source.alias or None, _line(source.this))
        for key in filter(None, (source.alias, source.name)):
            scope[key.lower()] = use
    return scope


def _columns_in(condition: exp.Expression, scope: dict[str, Optional[TableUse]], role: str) -> list[ColumnUse]:
    distinct_tables = {id(use): use for use in scope.values()}
    only = next(iter(distinct_tables.values())) if len(distinct_tables) == 1 else None
    out = []
    for column in condition.find_all(exp.Column):
        if not column.name:
            continue
        # A column inside a subquery belongs to that subquery's own SELECT, which is visited itself.
        if column.find_ancestor(exp.Select) is not condition.find_ancestor(exp.Select):
            continue
        qualifier = column.table.lower() if column.table else None
        table = scope.get(qualifier) if qualifier else only
        out.append(ColumnUse(table=table, column=column.name, role=role, line=_line(column.this)))
    return out


def _line(node: Any) -> Optional[int]:
    meta = getattr(node, "meta", None) or {}
    line = meta.get("line")
    return int(line) if isinstance(line, int) else None


def match_tables(
    wanted: list[tuple[Optional[str], str]],
    tables: list[TableDescription],
) -> list[TableDescription]:
    """The tables in `tables` that `wanted` names - by schema and name, or by name alone in any
    schema when the query didn't say which (so an unqualified name may match more than one)."""
    by_key = {(table.schema.lower(), table.name.lower()): table for table in tables}
    by_name: dict[str, list[TableDescription]] = {}
    for table in tables:
        by_name.setdefault(table.name.lower(), []).append(table)
    found: dict[tuple[str, str], TableDescription] = {}
    for schema, name in wanted:
        candidates = [by_key[(schema, name)]] if schema and (schema, name) in by_key else (
            [] if schema else by_name.get(name, [])
        )
        for table in candidates:
            found.setdefault((table.schema, table.name), table)
    return list(found.values())


def section_facts(
    sql: str,
    dialect: Optional[str],
    sections: list[tuple[int, int]],
    tables: list[TableDescription],
    large_rows: int = DEFAULT_LARGE_TABLE_ROWS,
) -> list[dict[str, Any]]:
    """For each section (its 1-based, inclusive line range), the stored tables it reads - schema,
    name, kind and estimated rows - and hints about its joins and filters. One dict per section,
    in order; empty ones where there is nothing to say.

    A hint is given for a column a section joins or filters a table of at least `large_rows`
    estimated rows on, when no index (or primary key) of that table starts with that column - and
    only for databases whose tables have indexes at all, so a lakehouse engine with none doesn't
    get one on every join."""
    statements = parse(sql, dialect)
    facts: list[dict[str, Any]] = [{"tables": [], "hints": []} for _ in sections]
    if not statements or not tables:
        return facts
    uses = table_uses(statements)
    resolved = {use: _resolve(use, tables) for use in uses}
    indexed_database = any(table.indexes for table in tables)

    for index, (start, end) in enumerate(sections):
        inside = lambda line: line is not None and start <= line <= end  # noqa: E731
        shown: dict[tuple[str, str], dict[str, Any]] = {}
        for use in uses:
            table = resolved[use]
            if table is None or not inside(use.line):
                continue
            shown.setdefault((table.schema, table.name), {
                "schema": table.schema,
                "name": table.name,
                "kind": table.kind,
                "rows": table.row_count_estimate,
            })
        facts[index]["tables"] = list(shown.values())

    if not indexed_database:
        return facts
    hinted: set[tuple[int, str, str, str]] = set()
    for use in column_uses(statements):
        table = resolved.get(use.table) if use.table is not None else None
        if table is None or (table.row_count_estimate or 0) < large_rows:
            continue
        if _leads_an_index(table, use.column):
            continue
        for index, (start, end) in enumerate(sections):
            if use.line is None or not start <= use.line <= end:
                continue
            key = (index, table.schema, table.name, use.column.lower())
            if key in hinted:
                continue
            hinted.add(key)
            verb = "Joins" if use.role == "join" else "Filters"
            facts[index]["hints"].append(
                f"{verb} {table.schema}.{table.name} (≈{_compact(table.row_count_estimate or 0)} rows) on "
                f"{use.column}, which no index starts with - this may read the whole table."
            )
    # The narrowest section a line belongs to is where a hint is read - drop copies from the
    # sections that merely contain it (the "Final query" around a subquery, say).
    _keep_narrowest(facts, sections)
    return facts


def _resolve(use: TableUse, tables: list[TableDescription]) -> Optional[TableDescription]:
    matches = match_tables([(use.schema.lower() if use.schema else None, use.name.lower())], tables)
    return matches[0] if len(matches) == 1 else None


def _leads_an_index(table: TableDescription, column: str) -> bool:
    wanted = column.lower()
    primary = [c.name.lower() for c in table.columns if c.is_primary_key]
    if primary and primary[0] == wanted:
        return True
    return any(columns and columns[0].lower() == wanted for columns, _unique in table.indexes)


def _keep_narrowest(facts: list[dict[str, Any]], sections: list[tuple[int, int]]) -> None:
    for index, (start, end) in enumerate(sections):
        narrower = [
            other for other, (s, e) in enumerate(sections)
            if other != index and start <= s and e <= end and (e - s) < (end - start)
        ]
        if not narrower:
            continue
        theirs = {hint for other in narrower for hint in facts[other]["hints"]}
        facts[index]["hints"] = [hint for hint in facts[index]["hints"] if hint not in theirs]


def _compact(value: float) -> str:
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= size:
            text = f"{value / size:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(int(value))
