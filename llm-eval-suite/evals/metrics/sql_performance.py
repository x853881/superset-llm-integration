"""SQLGlot performance heuristics for generated SQL. Findings of "error"
severity fail a test; the others are passed to the judge as evidence.
"""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional

from sqlglot import exp
from sqlglot.errors import SqlglotError

from metrics.sql_safety import (
    function_name,
    is_star,
    parse_one,
    referenced_tables,
    strip_sql_fences,
)
from suite.schema_metadata import SQL_DIALECT, TableMetadata


@dataclass(frozen=True)
class PerformanceFinding:
    rule: str
    severity: str  # "error" | "warning"
    message: str


def _outer_select(tree: exp.Expression) -> Optional[exp.Select]:
    return tree if isinstance(tree, exp.Select) else tree.find(exp.Select)


def _wrapping_function(
    col: exp.Column, stop: exp.Expression
) -> Optional[exp.Func]:
    """Nearest function call between `col` and `stop`. AND/OR and comparison
    predicates subclass Func in sqlglot, so they are skipped."""
    node = col.parent
    while node is not None and node is not stop:
        if isinstance(node, exp.Func) and not isinstance(
            node, (exp.Connector, exp.Predicate)
        ):
            return node
        node = node.parent
    return None


def _compared_bare(col: exp.Column, select: exp.Select) -> bool:
    """Whether `select`'s WHERE or a JOIN's ON also uses `col` with no
    function around it, as in `WHERE week >= DATE '2025-01-01' AND
    EXTRACT(DOW FROM week) = 1`: the index serves that predicate, and the
    wrapped one only filters what it finds."""
    conditions = [select.args.get("where")] + [
        join.args.get("on") for join in select.args.get("joins") or []
    ]
    for condition in filter(None, conditions):
        for other in condition.find_all(exp.Column):
            if (
                other is not col
                and other.name.lower() == col.name.lower()
                and other.table.lower() == col.table.lower()
                and other.parent_select is select
                and not isinstance(other.parent, exp.Is)
                and _wrapping_function(other, stop=condition) is None
            ):
                return True
    return False


def _joined_in_where(join: exp.Join) -> bool:
    """Whether WHERE links the joined table to another table of the same
    SELECT, as in `FROM a CROSS JOIN b WHERE a.id = b.id`. Only comparisons
    ANDed at the top level count, and only with table-qualified columns."""
    select = join.parent_select
    where = select.args.get("where") if select else None
    if where is None:
        return False
    sources = {
        node.this.alias_or_name.lower()
        for node in select.iter_expressions()
        if isinstance(node, (exp.From, exp.Join))
    }
    joined = join.this.alias_or_name.lower()
    condition = where.this
    parts = condition.flatten() if isinstance(condition, exp.And) else [condition]
    for part in parts:
        if not isinstance(part, exp.Predicate):
            continue
        tables = {
            c.table.lower()
            for c in part.find_all(exp.Column)
            if c.table and c.parent_select is select
        }
        if joined in tables and tables & (sources - {joined}):
            return True
    return False


def _one_row(query: Optional[exp.Expression]) -> bool:
    """Whether a query returns at most one row: LIMIT 1, an aggregate with no
    GROUP BY, or a SELECT with no FROM."""
    if isinstance(query, exp.Subquery):
        query = query.this
    if not isinstance(query, exp.Select):
        return False
    limit = query.args.get("limit")
    if isinstance(limit, exp.Limit) and limit.expression.sql() == "1":
        return True
    if query.args.get("group"):
        return False
    if not query.args.get("from"):
        return True
    # Only this SELECT's own aggregates: not windowed, not in a subquery.
    return any(
        agg.find_ancestor(exp.Window, exp.Select) is query
        for agg in query.find_all(exp.AggFunc)
    )


def _needs_no_condition(join: exp.Join) -> bool:
    """Joins that are not accidental cartesian products without a condition:
    LATERAL, which is correlated with the tables before it; a set-returning
    function such as generate_series, which multiplies rows on purpose; and a
    subquery or CTE that returns one row, which adds columns but no rows."""
    source = join.this
    if isinstance(source, (exp.Lateral, exp.Unnest)):
        return True
    if isinstance(source, exp.Table):
        if isinstance(source.this, exp.Func):
            return True
        if not source.db:
            name = source.name.lower()
            cte = next(
                (c for c in join.root().find_all(exp.CTE) if c.alias_or_name.lower() == name),
                None,
            )
            source = cte.this if cte else source
    return _one_row(source)


def _own_table(
    col: exp.Column, select: exp.Select, metadata: Mapping[str, TableMetadata]
) -> Optional[TableMetadata]:
    """The table `col` belongs to among `select`'s own FROM and JOINs, found
    through its alias. None for a column of an outer query (which a correlated
    subquery can refer to), of a subquery or CTE, or one it can't tell apart."""
    ctes = {c.alias_or_name.lower() for c in select.root().find_all(exp.CTE)}
    sources: Dict[str, Optional[TableMetadata]] = {}
    for node in select.iter_expressions():
        if not isinstance(node, (exp.From, exp.Join)):
            continue
        source = node.this
        physical = isinstance(source, exp.Table) and not (
            not source.db and source.name.lower() in ctes
        )
        sources[source.alias_or_name.lower()] = (
            metadata.get(source.name.lower()) if physical else None
        )
    if col.table:
        return sources.get(col.table.lower())
    owners = [
        t
        for t in sources.values()
        if t is not None and col.name.lower() in {c.lower() for c in t.columns}
    ]
    return owners[0] if len(owners) == 1 else None


def analyze_query_performance(
    sql: str,
    metadata: Mapping[str, TableMetadata],
    question: Optional[str] = None,
    dialect: str = SQL_DIALECT,
    fail_non_sargable: bool = True,
) -> List[PerformanceFinding]:
    """Heuristic SQLGlot rules against the metadata. Most rules map columns
    to tables by name, ignoring aliases, so treat warnings as evidence for the
    judge rather than verdicts; "error" findings are reserved for unambiguous
    problems.

    With `fail_non_sargable` (the default, for every endpoint: the
    performance policy forbids it, and the Query Performance judge fails it),
    a function around an indexed column in WHERE is an error when the column
    belongs, through its alias, to a large table that indexes it; otherwise
    it is only a warning. Test data may be too small to make it slow. Join
    conditions aren't checked: joining on a computed year, say, is the normal
    way to join to a per-year total, and the timing metrics cover its speed."""
    findings: List[PerformanceFinding] = []
    try:
        tree = parse_one(strip_sql_fences(sql), dialect)
    except SqlglotError as e:
        return [PerformanceFinding("unparseable", "error", str(e))]

    tables = referenced_tables(tree)
    for name in sorted(tables - set(metadata)):
        findings.append(
            PerformanceFinding(
                "unknown_table",
                "error",
                f"{name} is not in the schema metadata",
            )
        )
    known = [metadata[t] for t in sorted(tables) if t in metadata]

    for select in tree.find_all(exp.Select):
        if any(is_star(p) for p in select.expressions):
            findings.append(
                PerformanceFinding(
                    "select_star", "warning", "query uses SELECT *"
                )
            )
            break

    for join in tree.find_all(exp.Join):
        if (
            not join.args.get("on")
            and not join.args.get("using")
            and not _joined_in_where(join)
            and not _needs_no_condition(join)
        ):
            findings.append(
                PerformanceFinding(
                    "cartesian_join",
                    "error",
                    f"join to {join.this.sql(dialect=dialect)} has no join "
                    "condition in ON, USING or WHERE",
                )
            )

    filter_nodes = list(tree.find_all(exp.Where)) + [
        j.args["on"] for j in tree.find_all(exp.Join) if j.args.get("on")
    ]
    filtered_cols = {
        c.name.lower()
        for node in filter_nodes
        for c in node.find_all(exp.Column)
    }
    outer = _outer_select(tree)
    bounded = bool(tree.args.get("limit")) or (
        outer is not None
        and bool(
            outer.args.get("limit")
            or outer.args.get("group")
            or any(p.find(exp.AggFunc) for p in outer.expressions)
        )
    )
    for t in known:
        if t.is_large and not (t.indexed_columns & filtered_cols):
            findings.append(
                PerformanceFinding(
                    "missing_index_filter",
                    "warning",
                    f"{t.name} (~{t.approx_row_count:,} rows) is not filtered on any "
                    f"of {sorted(t.indexed_columns)}",
                )
            )
        if t.is_large and not bounded:
            findings.append(
                PerformanceFinding(
                    "unbounded_large_scan",
                    "warning",
                    f"{t.name} is read without LIMIT or aggregation",
                )
            )

    indexed_everywhere = set().union(*(t.indexed_columns for t in known))
    for where in tree.find_all(exp.Where):
        for col in where.find_all(exp.Column):
            # Columns inside a subquery belong to that subquery's predicates.
            if (
                col.name.lower() not in indexed_everywhere
                or col.find_ancestor(exp.Where) is not where
                or col.parent_select is not where.parent_select
            ):
                continue
            wrapper = _wrapping_function(col, stop=where)
            if wrapper is None:
                continue
            message = (
                f"indexed column {col.name} is wrapped in "
                f"{function_name(wrapper)}() in WHERE"
            )
            table = (
                _own_table(col, where.parent_select, metadata)
                if fail_non_sargable and where.parent_select
                else None
            )
            if (
                table
                and table.is_large
                and col.name.lower() in table.indexed_columns
                and not _compared_bare(col, where.parent_select)
            ):
                findings.append(
                    PerformanceFinding(
                        "non_sargable_predicate",
                        "error",
                        f"{message}: {table.name} has ~{table.approx_row_count:,} "
                        "rows, so compare the raw column with a range",
                    )
                )
            else:
                findings.append(
                    PerformanceFinding("non_sargable_predicate", "warning", message)
                )
        for like in where.find_all(exp.Like, exp.ILike):
            pattern = like.expression
            if (
                isinstance(like.this, exp.Column)
                and like.this.name.lower() in indexed_everywhere
                and isinstance(pattern, exp.Literal)
                and str(pattern.this).startswith("%")
            ):
                findings.append(
                    PerformanceFinding(
                        "leading_wildcard_like",
                        "warning",
                        f"LIKE on indexed column {like.this.name} starts with %",
                    )
                )

    if question:
        q = question.lower()
        for rollup in metadata.values():
            if (
                rollup.rollup_of
                and rollup.rollup_of.lower() in tables
                and rollup.name.lower() not in tables
                and any(k in q for k in rollup.preferred_for)
            ):
                findings.append(
                    PerformanceFinding(
                        "prefer_rollup",
                        "warning",
                        f"question matches {rollup.name}, a rollup of "
                        f"{rollup.rollup_of}, but the raw table was queried",
                    )
                )
    return findings
