"""Checks a suggested faster rewrite the way CI checks a pull request, before anyone sees it.

A port of waxwing-sql's ``RewriteChecker`` (its decision B102) to sqlglot: does the rewrite plan,
does its outside - output columns, DISTINCT, ORDER BY, LIMIT - match the original's, did it replace
a ``NOT IN`` whose NULL behaviour differs from what replaced it, does it read different tables, and
what does the planner think it costs. None of this runs either query and none of it proves the two
equivalent; it finds the differences code can see, and the verdict says how much that is worth:

- ``Invalid``: the database rejects the rewrite (when it accepted the original), or it doesn't parse.
- ``MayDiffer``: something found here changes the result.
- ``Unverified``: no difference found - which is not the same as none existing.
- ``Unchanged``: the same query, give or take whitespace, comments and keyword case.

Without a catalog this can't know which columns are nullable, so a replaced ``NOT IN`` is always
reported as "depends on NULLs" - waxwing-sql can say more there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import sqlglot
from sqlglot import exp
from sqlglot.tokens import TokenType

from .query_plan import QueryPlan
from .superset_db import PlanOutcome

VERDICT_INVALID = "Invalid"
VERDICT_MAY_DIFFER = "MayDiffer"
VERDICT_UNVERIFIED = "Unverified"
VERDICT_UNCHANGED = "Unchanged"


@dataclass
class Finding:
    severity: str  # "error", "warning" or "info"
    message: str
    #: About the result (which makes the verdict MayDiffer) rather than only performance.
    semantic: bool = False

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "message": self.message}


@dataclass
class RewriteCheck:
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    original_cost: Optional[float] = None
    rewritten_cost: Optional[float] = None
    cost_unit: Optional[str] = None
    rewritten_plan: Optional[QueryPlan] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "findings": [finding.to_dict() for finding in self.findings],
            "originalCost": self.original_cost,
            "rewrittenCost": self.rewritten_cost,
            "costUnit": self.cost_unit,
        }


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def check_rewrite(
    original: str,
    rewritten: str,
    dialect: Optional[str],
    original_plan: Optional[QueryPlan],
    plan: Optional[Callable[[str], PlanOutcome]],
) -> RewriteCheck:
    """`plan` asks the database for a query's plan (None when there's no database to ask);
    `original_plan` is the original's, when it was planned."""
    cost_unit = original_plan.cost_unit if original_plan else None
    original_cost = original_plan.total_cost if original_plan else None
    if _tokens(original, dialect) == _tokens(rewritten, dialect):
        return RewriteCheck(VERDICT_UNCHANGED, original_cost=original_cost, cost_unit=cost_unit)

    rewritten_plan: Optional[QueryPlan] = None
    planning_note: Optional[Finding] = None
    if plan is not None:
        outcome = plan(rewritten)
        if outcome.ok:
            rewritten_plan = outcome.plan
        elif outcome.checked and original_plan is not None:
            # The database wins: it rejecting the rewrite when it accepted the original settles it.
            return _invalid(f"The database rejected the rewrite: {outcome.error}", original_plan)
        else:
            planning_note = Finding("info", f"The rewrite couldn't be planned here ({outcome.error}), so only the SQL was compared.")
    if rewritten_plan is None:
        parse_error = _parse_error(rewritten, dialect)
        if parse_error:
            return _invalid(f"The rewrite doesn't parse: {parse_error}", original_plan)

    findings: list[Finding] = []
    if planning_note:
        findings.append(planning_note)
    elif plan is None:
        findings.append(Finding("info", "There was no database to plan the rewrite on, so only the SQL was compared."))
    findings += _shape_findings(original, rewritten, dialect)
    findings += _not_in_findings(original, rewritten, dialect)
    findings += _subquery_to_join_findings(original, rewritten, dialect)
    findings += _table_findings(original, rewritten, dialect)
    if original_plan is not None and rewritten_plan is not None:
        findings += _plan_findings(original_plan, rewritten_plan)
    verdict = VERDICT_MAY_DIFFER if any(f.semantic and f.severity == "warning" for f in findings) else VERDICT_UNVERIFIED
    findings.sort(key=lambda finding: _SEVERITY_ORDER.get(finding.severity, 3))
    return RewriteCheck(
        verdict=verdict,
        findings=findings,
        original_cost=original_cost,
        rewritten_cost=rewritten_plan.total_cost if rewritten_plan else None,
        cost_unit=(rewritten_plan or original_plan).cost_unit if (rewritten_plan or original_plan) else None,
        rewritten_plan=rewritten_plan,
    )


def _invalid(reason: str, original_plan: Optional[QueryPlan]) -> RewriteCheck:
    return RewriteCheck(
        VERDICT_INVALID,
        [Finding("error", reason)],
        original_cost=original_plan.total_cost if original_plan else None,
        cost_unit=original_plan.cost_unit if original_plan else None,
    )


def _parse_error(sql: str, dialect: Optional[str]) -> Optional[str]:
    try:
        sqlglot.parse_one(sql, read=dialect or None)
        return None
    except Exception as ex:  # noqa: BLE001
        return str(ex)


def _tokens(sql: str, dialect: Optional[str]) -> Optional[list[tuple[str, str]]]:
    """The SQL as the engine reads it: no whitespace or comments, keywords and unquoted names in one
    case, literals exactly as written."""
    try:
        tokens = sqlglot.tokenize(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return None
    out = [
        (token.token_type.name, token.text if token.token_type in (TokenType.STRING, TokenType.NATIONAL_STRING) else token.text.upper())
        for token in tokens
    ]
    while out and out[-1][0] == TokenType.SEMICOLON.name:
        out.pop()
    return out


@dataclass
class _Shape:
    columns: list[str]
    has_star: bool
    distinct: bool
    limit: Optional[str]
    order_by: Optional[str]


def _shape(sql: str, dialect: Optional[str]) -> Optional[_Shape]:
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(tree, exp.Query):
        return None
    leftmost = tree
    while isinstance(leftmost, exp.SetOperation):
        leftmost = leftmost.this
    if isinstance(leftmost, exp.Subquery):
        leftmost = leftmost.unnest()
    if not isinstance(leftmost, exp.Select):
        return None
    columns = [projection.alias_or_name.lower() for projection in leftmost.expressions]
    has_star = any(isinstance(projection, exp.Star) or projection.find(exp.Star) for projection in leftmost.expressions)
    limit = tree.args.get("limit") or tree.args.get("fetch")
    order = tree.args.get("order")
    return _Shape(
        columns=columns,
        has_star=has_star,
        distinct=bool(leftmost.args.get("distinct")),
        limit=_normalized(limit.sql(dialect=dialect or None)) if limit else None,
        order_by=_normalized(order.sql(dialect=dialect or None)) if order else None,
    )


def _shape_findings(original: str, rewritten: str, dialect: Optional[str]) -> list[Finding]:
    a = _shape(original, dialect)
    b = _shape(rewritten, dialect)
    if a is None or b is None:
        which = "the original" if a is None else "the rewrite"
        return [Finding("info", f"The parser can't read {which}'s outer query, so its columns, order and limit weren't compared.")]
    findings: list[Finding] = []
    if a.has_star or b.has_star:
        if a.columns != b.columns:
            findings.append(Finding("info", "A SELECT * means the output columns weren't compared."))
    elif len(a.columns) != len(b.columns):
        findings.append(Finding("warning", f"The rewrite returns {len(b.columns)} columns instead of {len(a.columns)}.", True))
    elif sorted(a.columns) != sorted(b.columns):
        findings.append(Finding("warning", f"Output columns change: {', '.join(a.columns)} → {', '.join(b.columns)}.", True))
    elif a.columns != b.columns:
        findings.append(Finding("warning", f"The output columns come back in a different order: {', '.join(b.columns)}.", True))
    if a.distinct != b.distinct:
        findings.append(Finding(
            "warning",
            "The rewrite adds DISTINCT, so duplicate rows collapse into one." if b.distinct
            else "The rewrite drops DISTINCT, so duplicate rows can appear.",
            True,
        ))
    if a.limit and not b.limit:
        findings.append(Finding("warning", f"The rewrite drops the row limit ({a.limit}).", True))
    elif b.limit and not a.limit:
        findings.append(Finding("warning", f"The rewrite adds a row limit ({b.limit}).", True))
    elif a.limit and b.limit and a.limit != b.limit:
        findings.append(Finding("warning", f"The row limit changes: {a.limit} → {b.limit}.", True))
    if a.order_by and not b.order_by:
        tail = " and the limit can keep different ones" if a.limit else ""
        findings.append(Finding("warning", f"The rewrite drops the ORDER BY, so rows can come back in any order{tail}.", True))
    elif b.order_by and not a.order_by:
        findings.append(Finding("info", f"The rewrite adds {b.order_by}."))
    elif a.order_by and b.order_by and _sort_key(a.order_by) != _sort_key(b.order_by):
        findings.append(Finding("warning", f"The sort changes: {a.order_by} → {b.order_by}.", bool(a.limit)))
    if a.order_by and a.limit and b.order_by and b.limit:
        findings.append(Finding("info", "With ORDER BY and a limit, rows that tie at the cut-off can differ between two correct queries."))
    return findings


def _not_in_subqueries(sql: str, dialect: Optional[str]) -> Optional[list[str]]:
    """Each `x NOT IN (SELECT ...)` in `sql`, as its outer column's text."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return None
    found = []
    for not_node in tree.find_all(exp.Not):
        inner = not_node.this
        if isinstance(inner, exp.In) and inner.args.get("query") is not None:
            found.append(inner.this.sql(dialect=dialect or None).lower())
    return found


def _not_in_findings(original: str, rewritten: str, dialect: Optional[str]) -> list[Finding]:
    """`x NOT IN (SELECT y ...)` returns nothing at all once `y` has a NULL, and drops rows where `x`
    is NULL; NOT EXISTS and anti-joins do neither. So replacing one is only equivalent when neither
    column can be NULL - which, without a catalog, this can't tell."""
    before = _not_in_subqueries(original, dialect)
    after = _not_in_subqueries(rewritten, dialect)
    if before is None or after is None:
        return []
    remaining = list(after)
    findings = []
    for column in before:
        if column in remaining:
            remaining.remove(column)
            continue
        findings.append(Finding(
            "warning",
            f"NOT IN (subquery) on {column} was replaced. If the subquery can return a NULL, the original returns no rows "
            "at all while the rewrite still returns some (and NULLs in the outer column are treated differently too); "
            "equivalent only if neither column is ever NULL.",
            True,
        ))
    return findings


def _parsed(sql: str, dialect: Optional[str]) -> Optional[exp.Expression]:
    try:
        return sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return None


def _outer_select(tree: exp.Expression) -> Optional[exp.Select]:
    while isinstance(tree, exp.SetOperation):
        tree = tree.this
    return tree if isinstance(tree, exp.Select) else None


def _scalar_subqueries_in_select_list(select: exp.Select) -> int:
    return sum(1 for projection in select.expressions for _ in projection.find_all(exp.Subquery))


def _inner_joins(select: exp.Select, skip_deduplicated: bool = False) -> int:
    """Joins that drop unmatched rows: plain/INNER joins (and comma joins), not LEFT/RIGHT/FULL.
    With `skip_deduplicated`, a join to a subquery that is DISTINCT or GROUP BY isn't counted - it
    can't repeat a row."""
    count = 0
    for join in select.args.get("joins") or []:
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if side or kind not in ("", "INNER", "CROSS"):
            continue
        target = join.this
        if skip_deduplicated and isinstance(target, exp.Subquery):
            inner = _outer_select(target.this)
            if inner is not None and (inner.args.get("distinct") or inner.args.get("group")):
                continue
        count += 1
    return count


def _in_subqueries(tree: exp.Expression) -> int:
    return sum(
        1 for node in tree.find_all(exp.In)
        if node.args.get("query") is not None and not isinstance(node.parent, exp.Not)
    )


def _subquery_to_join_findings(original: str, rewritten: str, dialect: Optional[str]) -> list[Finding]:
    """The two most common "faster" rewrites both have an edge where the result changes:

    - a subquery in the SELECT list (a per-row count, say) turned into a JOIN + GROUP BY drops the
      rows that had no match - the subquery gave them 0 or NULL - unless the join is a LEFT JOIN;
    - `x IN (SELECT y ...)` turned into a JOIN repeats a row once per match, unless `y` is unique.
    """
    a_tree, b_tree = _parsed(original, dialect), _parsed(rewritten, dialect)
    if a_tree is None or b_tree is None:
        return []
    a, b = _outer_select(a_tree), _outer_select(b_tree)
    if a is None or b is None:
        return []
    findings = []
    added_inner_joins = _inner_joins(b) > _inner_joins(a)
    if _scalar_subqueries_in_select_list(a) > _scalar_subqueries_in_select_list(b) and added_inner_joins:
        findings.append(Finding(
            "warning",
            "A subquery in the SELECT list was replaced by an inner join: rows with no matching row are now dropped, "
            "where the subquery gave them 0 or NULL. A LEFT JOIN would keep them - check whether any can have no match.",
            True,
        ))
    added_repeating_joins = _inner_joins(b, skip_deduplicated=True) > _inner_joins(a, skip_deduplicated=True)
    if _in_subqueries(a_tree) > _in_subqueries(b_tree) and added_repeating_joins and not b.args.get("distinct"):
        findings.append(Finding(
            "warning",
            "An IN (subquery) was replaced by a join: if the joined column isn't unique, rows now repeat once per "
            "match. Equivalent only if it is unique (a primary key, say).",
            True,
        ))
    return findings


def _tables(sql: str, dialect: Optional[str]) -> Optional[set[str]]:
    try:
        tree = sqlglot.parse_one(sql, read=dialect or None)
    except Exception:  # noqa: BLE001
        return None
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    names = set()
    for table in tree.find_all(exp.Table):
        name = ".".join(part for part in (table.db, table.name) if part).lower()
        if name and table.name.lower() not in cte_names:
            names.add(name)
    return names


def _table_findings(original: str, rewritten: str, dialect: Optional[str]) -> list[Finding]:
    a = _tables(original, dialect)
    b = _tables(rewritten, dialect)
    if a is None or b is None:
        return []
    findings = []
    if b - a:
        findings.append(Finding("info", f"The rewrite also reads {', '.join(sorted(b - a))}."))
    if a - b:
        findings.append(Finding("info", f"The rewrite no longer reads {', '.join(sorted(a - b))}."))
    return findings


def _plan_findings(a: QueryPlan, b: QueryPlan) -> list[Finding]:
    findings = []
    before, after = a.total_cost, b.total_cost
    if before is not None and after is not None and before > 0 and a.cost_unit == b.cost_unit:
        change = (after - before) / before * 100
        direction = f"{_number(-change)}% lower" if change <= 0 else f"{_number(change)}% higher"
        findings.append(Finding(
            "warning" if after > before else "info",
            f"The planner's estimated cost goes from {_number(before)} to {_number(after)} {a.cost_unit} ({direction}).",
        ))
    elif before is None or after is None:
        findings.append(Finding("info", "The planner gave no total cost to compare."))
    cleared = {w.code for w in a.warnings} - {w.code for w in b.warnings}
    added = {w.code for w in b.warnings} - {w.code for w in a.warnings}
    if cleared:
        findings.append(Finding("info", f"No longer flagged in the plan: {', '.join(sorted(cleared))}."))
    if added:
        findings.append(Finding("warning", f"Newly flagged in the plan: {', '.join(sorted(added))}."))
    return findings


def _number(value: float) -> str:
    return f"{value:,.0f}" if abs(value) >= 100 else f"{value:.1f}"


def _normalized(sql: str) -> str:
    return " ".join(sql.strip().rstrip(";").split()).lower()


def _sort_key(order_by: str) -> str:
    """An ORDER BY with table qualifiers and the default ASC dropped: `m.movie_id ASC` sorts like
    `movie_id`."""
    import re

    text = re.sub(r"\b[a-z_][a-z0-9_$]*\.", "", order_by)
    return re.sub(r"\s+asc\b", "", text)
