"""Query plans: what to ask a database for, and how to read what it says back.

The pure half of planning - nothing here touches a database (that is ``superset_db``). It decides:

- **whether a query may be planned at all** (`check_plannable`): exactly one statement, and a
  read-only query. This extension never runs SQL for data; planning is the one thing it asks a
  database to do with a user's query, and even that is refused for anything that isn't a plain
  query - an ``EXPLAIN`` of a ``DELETE`` doesn't delete anything, but there's no reason to find out
  which engines treat ``EXPLAIN`` how.
- **how to ask** (`explain_statement`): each engine's own ``EXPLAIN`` - never ``EXPLAIN ANALYZE``,
  which on Postgres *runs* the query.
- **how to read the answer** (`parse_plan`): each engine's plan into one `PlanNode` tree, and from
  that an indented outline for a model to read (`plan_outline`, after waxwing-sql's
  ``PlanOutline``) and the handful of things worth flagging about it (`plan_warnings`).

Postgres is the engine this is built and tested against. Trino and SQLite are read from their
JSON/row shapes; Oracle's two-step ``EXPLAIN PLAN`` + ``DBMS_XPLAN`` is supported but untested.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlglot
from sqlglot import exp

#: Large enough for any plan a person would read; small enough not to crowd out the rest of a prompt.
PLAN_OUTLINE_MAX_CHARS = 16_000
_MAX_DETAIL_CHARS = 200

# sqlglot dialect names for the engines planning knows about, keyed by Superset's backend name.
_SQLGLOT_DIALECTS = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "redshift": "redshift",
    "trino": "trino",
    "presto": "presto",
    "oracle": "oracle",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "mssql": "tsql",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
    "databricks": "databricks",
    "hive": "hive",
    "spark": "spark",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse",
}

_MUTATING = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.TruncateTable, exp.Alter)


def sqlglot_dialect_for_backend(backend: Optional[str]) -> Optional[str]:
    """The sqlglot dialect for a Superset backend name ("postgresql" -> "postgres"), if known."""
    return _SQLGLOT_DIALECTS.get((backend or "").lower())


class NotPlannable(ValueError):
    """The SQL isn't something this extension will ask a database to plan."""


def check_plannable(sql: str, dialect: Optional[str]) -> str:
    """`sql` without its trailing semicolon, if it is exactly one read-only query; raises
    `NotPlannable` (saying why) otherwise."""
    text = sql.strip().rstrip(";").strip()
    if not text:
        raise NotPlannable("There is no SQL to plan.")
    try:
        statements = [statement for statement in sqlglot.parse(text, read=dialect) if statement is not None]
    except Exception as ex:  # noqa: BLE001 - any parse failure
        raise NotPlannable(f"The SQL doesn't parse: {ex}") from ex
    if len(statements) != 1:
        raise NotPlannable(f"Only a single statement can be planned; this is {len(statements)}.")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise NotPlannable("Only a query (SELECT / WITH / UNION ...) can be planned.")
    if any(statement.find(node) for node in _MUTATING):
        raise NotPlannable("The query modifies data, so it won't be planned.")
    return text


def explain_statement(sql: str, backend: str) -> Optional[list[str]]:
    """The statement(s) that ask `backend` for `sql`'s plan without running it, or None for an
    engine this doesn't know how to plan. More than one statement only for Oracle, which writes the
    plan to PLAN_TABLE first and reads it back second."""
    backend = (backend or "").lower()
    if backend in ("postgresql", "postgres", "redshift"):
        return [f"EXPLAIN (FORMAT JSON) {sql}"] if backend != "redshift" else [f"EXPLAIN {sql}"]
    if backend in ("trino", "presto"):
        return [f"EXPLAIN (FORMAT JSON) {sql}"]
    if backend in ("mysql", "mariadb"):
        return [f"EXPLAIN FORMAT=JSON {sql}"]
    if backend == "sqlite":
        return [f"EXPLAIN QUERY PLAN {sql}"]
    if backend == "oracle":
        statement_id = "nlsql"
        return [
            f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {sql}",
            f"SELECT PLAN_TABLE_OUTPUT FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', '{statement_id}', 'TYPICAL'))",
        ]
    return None


@dataclass
class PlanNode:
    """One step of a plan, in a shape common to every engine."""

    operation: str
    object_name: Optional[str] = None
    estimated_rows: Optional[float] = None
    cost: Optional[float] = None
    startup_cost: Optional[float] = None
    details: dict[str, str] = field(default_factory=dict)
    children: list["PlanNode"] = field(default_factory=list)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "objectName": self.object_name,
            "estimatedRows": self.estimated_rows,
            "cost": self.cost,
            "details": self.details,
            "children": [child.to_dict() for child in self.children],
        }


@dataclass
class PlanWarning:
    severity: str  # "warning" or "info"
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"severity": self.severity, "code": self.code, "message": self.message}


@dataclass
class QueryPlan:
    """A plan as read back: the tree (when the engine's output could be parsed into one), its
    outline for a model, the raw text (for Oracle, the outline *is* the raw text), and the planner's
    own total cost estimate where it gives one."""

    backend: str
    raw: str
    root: Optional[PlanNode]
    outline: str
    total_cost: Optional[float] = None
    cost_unit: str = "planner cost units"
    warnings: list[PlanWarning] = field(default_factory=list)
    #: The planner's own estimate of the run time, for the engines that give one (Oracle).
    estimated_seconds: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "outline": self.outline,
            "totalCost": self.total_cost,
            "costUnit": self.cost_unit,
            "warnings": [warning.to_dict() for warning in self.warnings],
            "steps": sum(1 for _ in self.root.walk()) if self.root else None,
            "estimatedSeconds": self.estimated_seconds,
        }


def parse_plan(backend: str, rows: list[list[Any]]) -> QueryPlan:
    """Reads the rows an engine's EXPLAIN answered with into a `QueryPlan`. `rows` is the result
    set as a list of rows (each a list of column values)."""
    backend = (backend or "").lower()
    if backend in ("postgresql", "postgres"):
        return _postgres_plan(rows)
    if backend in ("trino", "presto"):
        return _trino_plan(backend, rows)
    if backend == "sqlite":
        return _sqlite_plan(rows)
    if backend in ("mysql", "mariadb"):
        return _mysql_plan(backend, rows)
    # Oracle's DBMS_XPLAN output, Redshift's text plan, or anything else: one line of text per row,
    # which reads well enough as it is - and, for Oracle, its table read into a tree as well.
    raw = "\n".join(str(row[0]) for row in rows if row)
    plan = QueryPlan(backend=backend, raw=raw, root=None, outline=_truncate_lines(raw), total_cost=_oracle_cost(raw))
    if backend == "oracle":
        plan.root, plan.estimated_seconds = _oracle_tree(raw)
        if plan.root is not None:
            plan.warnings = _oracle_warnings(plan.root)
    return plan


def plan_outline(root: PlanNode, max_chars: int = PLAN_OUTLINE_MAX_CHARS) -> str:
    """The plan as an indented outline: one line per step with what it reads, the estimated rows and
    cost, and the details that explain the cost (filters, join and index conditions, sort and group
    keys). An engine's own JSON is several times longer for the same facts, so a size cap on it cuts
    real plans off mid-document; this keeps every step and cuts only whole lines, saying how many."""
    lines: list[str] = []

    def visit(node: PlanNode, depth: int) -> None:
        lines.append("  " * depth + _step_line(node))
        for child in node.children:
            visit(child, depth + 1)

    visit(root, 0)
    out: list[str] = []
    size = 0
    for index, line in enumerate(lines):
        if size + len(line) + 1 > max_chars:
            out.append(f"… {len(lines) - index} more steps not shown")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _step_line(node: PlanNode) -> str:
    text = node.operation
    if node.object_name:
        text += f" on {node.object_name}"
    numbers = []
    if node.estimated_rows is not None:
        numbers.append(f"rows ≈ {_compact(node.estimated_rows)}")
    if node.cost is not None:
        numbers.append(f"cost {_compact(node.cost)}")
    if numbers:
        text += f"  ({', '.join(numbers)})"
    details = [f"{key}: {value[:_MAX_DETAIL_CHARS]}" for key, value in node.details.items() if value and value != "[]"]
    if details:
        text += "  " + "; ".join(details)
    return text


def _compact(number: float) -> str:
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.1f}M"
    if number >= 10_000:
        return f"{number / 1_000:.0f}k"
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _truncate_lines(text: str, max_chars: int = PLAN_OUTLINE_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    kept = text[:max_chars].rsplit("\n", 1)[0]
    return f"{kept}\n… {text.count(chr(10)) - kept.count(chr(10))} more lines not shown"


# --- Postgres -----------------------------------------------------------------------------------

# Details of a Postgres plan node worth showing: what explains its cost. Everything else (output
# column lists, parallel-awareness flags, widths) is noise at the length a model reads.
_PG_DETAILS = (
    "Join Type", "Strategy", "Index Name", "Index Cond", "Recheck Cond", "Filter", "Join Filter",
    "Hash Cond", "Merge Cond", "Sort Key", "Group Key", "Subplan Name", "CTE Name", "Partial Mode",
)


def _postgres_plan(rows: list[list[Any]]) -> QueryPlan:
    value = rows[0][0] if rows and rows[0] else None
    document = json.loads(value) if isinstance(value, str) else value
    if isinstance(document, list):
        document = document[0] if document else {}
    plan = (document or {}).get("Plan") or {}
    root = _pg_node(plan)
    return QueryPlan(
        backend="postgresql",
        raw=json.dumps(document),
        root=root,
        outline=plan_outline(root),
        total_cost=_as_float(plan.get("Total Cost")),
        warnings=_postgres_warnings(root),
    )


def _pg_node(plan: dict[str, Any]) -> PlanNode:
    relation = plan.get("Relation Name")
    if relation and plan.get("Schema"):
        relation = f"{plan['Schema']}.{relation}"
    alias = plan.get("Alias")
    if relation and alias and alias != plan.get("Relation Name"):
        relation = f"{relation} {alias}"
    details = {}
    for key in _PG_DETAILS:
        if key in plan:
            value = plan[key]
            details[key] = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    return PlanNode(
        operation=str(plan.get("Node Type") or "?"),
        object_name=relation or plan.get("CTE Name") or plan.get("Function Name"),
        estimated_rows=_as_float(plan.get("Plan Rows")),
        cost=_as_float(plan.get("Total Cost")),
        startup_cost=_as_float(plan.get("Startup Cost")),
        details=details,
        children=[_pg_node(child) for child in plan.get("Plans") or []],
    )


#: Above this many estimated rows, a sequential scan is worth mentioning.
LARGE_SCAN_ROWS = 100_000


def _postgres_warnings(root: PlanNode) -> list[PlanWarning]:
    """The few things about a Postgres plan worth pointing out unprompted - each one a common reason
    a query is slower than it needs to be. Estimates only: the query isn't run."""
    warnings: list[PlanWarning] = []
    for node in root.walk():
        rows = node.estimated_rows or 0
        if node.operation == "Seq Scan" and rows >= LARGE_SCAN_ROWS:
            what = f"Full scan of {node.object_name}"
            if "Filter" in node.details:
                what += f" filtered by {node.details['Filter'][:120]} - an index on the filtered column(s) could avoid it"
            warnings.append(PlanWarning("warning", "large_seq_scan", f"{what} (≈{_compact(rows)} rows)."))
        if node.operation == "Nested Loop" and len(node.children) == 2:
            outer = node.children[0].estimated_rows or 0
            inner = node.children[1]
            if outer >= 1_000 and inner.operation == "Seq Scan":
                warnings.append(PlanWarning(
                    "warning", "nested_loop_seq_scan",
                    f"Nested loop rescans {inner.object_name} for each of ≈{_compact(outer)} outer rows.",
                ))
        if node.operation == "Sort" and rows >= 1_000_000:
            warnings.append(PlanWarning("info", "large_sort", f"Sorts ≈{_compact(rows)} rows."))
        if node.operation == "Nested Loop" and len(node.children) == 2 and _joins_without_condition(node):
            left, right = (_scanned(child) for child in node.children)
            warnings.append(PlanWarning(
                "warning", "possible_cross_join",
                f"Nested loop without a join condition between {left} and {right}: every row of one is "
                f"paired with every row of the other (≈{_compact(rows)} rows).",
            ))
    if root.cost is not None and root.cost >= VERY_HIGH_COST:
        warnings.append(PlanWarning("warning", "very_high_cost", f"The planner puts this query's cost at {_compact(root.cost)} units."))
    return warnings


#: A total cost above this (Postgres' units: roughly sequential page reads) is a query that will
#: take a long time on any hardware.
VERY_HIGH_COST = 10_000_000


def _joins_without_condition(node: PlanNode) -> bool:
    """A nested loop whose inner side isn't driven by the outer row - no join filter on the loop,
    and no index or bitmap condition anywhere under its inner side to look rows up with."""
    if node.details.get("Join Filter"):
        return False
    # Pairing every row with a single row (an uncorrelated scalar subquery, a one-row CTE) is
    # what a cross join is for, not a mistake.
    if any((child.estimated_rows or 0) <= 1 for child in node.children):
        return False
    inner = node.children[1]
    return not any(
        step.details.get("Index Cond") or step.details.get("Recheck Cond") or step.details.get("Hash Cond")
        or step.operation in ("Result", "Function Scan", "Values Scan")
        for step in inner.walk()
    )


def _scanned(node: PlanNode) -> str:
    """The tables a plan subtree reads, for naming the two sides of a join."""
    names = [step.object_name for step in node.walk() if step.object_name]
    return ", ".join(dict.fromkeys(names)) or node.operation


# --- Trino / Presto -----------------------------------------------------------------------------


def _trino_plan(backend: str, rows: list[list[Any]]) -> QueryPlan:
    value = rows[0][0] if rows and rows[0] else None
    try:
        document = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        raw = "\n".join(str(row[0]) for row in rows if row)
        return QueryPlan(backend=backend, raw=raw, root=None, outline=_truncate_lines(raw))
    # A distributed plan is a map of fragment id -> fragment root; a logical one is a single root.
    if isinstance(document, dict) and "name" not in document:
        fragments = [_trino_node(fragment) for _, fragment in sorted(document.items(), key=lambda item: _fragment_key(item[0]))]
        root = fragments[0] if len(fragments) == 1 else PlanNode("Fragments", children=fragments)
    else:
        root = _trino_node(document or {})
    cost = None
    for node in root.walk():
        if node.cost is not None:
            cost = node.cost if cost is None else max(cost, node.cost)
    return QueryPlan(
        backend=backend,
        raw=json.dumps(document),
        root=root,
        outline=plan_outline(root),
        total_cost=cost,
        cost_unit="estimated CPU cost",
    )


def _fragment_key(key: str) -> tuple[int, str]:
    return (int(key), key) if str(key).isdigit() else (1 << 30, str(key))


_TRINO_NOISE = {"table", "sourceFragmentIds", "columnNames"}


def _trino_node(node: dict[str, Any]) -> PlanNode:
    descriptor = node.get("descriptor") or {}
    estimates = (node.get("estimates") or [{}])[0] if node.get("estimates") else {}
    details = {key: str(value) for key, value in descriptor.items() if key not in _TRINO_NOISE and value not in (None, "", "[]")}
    if node.get("details"):
        details["details"] = " ".join(map(str, node["details"]))[:_MAX_DETAIL_CHARS]
    return PlanNode(
        operation=str(node.get("name") or "?"),
        object_name=descriptor.get("table"),
        estimated_rows=_finite(estimates.get("outputRowCount")),
        cost=_finite(estimates.get("cpuCost")),
        details=details,
        children=[_trino_node(child) for child in node.get("children") or []],
    )


# --- SQLite / MySQL -------------------------------------------------------------------------------


def _sqlite_plan(rows: list[list[Any]]) -> QueryPlan:
    """`EXPLAIN QUERY PLAN` rows are (id, parent, notused, detail)."""
    nodes: dict[Any, PlanNode] = {}
    root = PlanNode("QUERY PLAN")
    for row in rows:
        if len(row) < 4:
            continue
        node_id, parent, _unused, detail = row[0], row[1], row[2], str(row[3])
        node = PlanNode(operation=detail)
        nodes[node_id] = node
        (nodes.get(parent) or root).children.append(node)
    warnings = [
        PlanWarning("warning", "full_scan", f"{node.operation} - a full table scan.")
        for node in root.walk()
        if node.operation.startswith("SCAN ") and "USING" not in node.operation
    ]
    raw = "\n".join(str(row[3]) for row in rows if len(row) >= 4)
    return QueryPlan(backend="sqlite", raw=raw, root=root, outline=plan_outline(root), warnings=warnings)


def _mysql_plan(backend: str, rows: list[list[Any]]) -> QueryPlan:
    value = rows[0][0] if rows and rows[0] else ""
    try:
        document = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raw = str(value)
        return QueryPlan(backend=backend, raw=raw, root=None, outline=_truncate_lines(raw))
    block = document.get("query_block") or {}
    cost = _as_float((block.get("cost_info") or {}).get("query_cost"))
    raw = json.dumps(document, indent=1)
    return QueryPlan(backend=backend, raw=raw, root=None, outline=_truncate_lines(raw), total_cost=cost)


# --- Oracle ---------------------------------------------------------------------------------------


def _oracle_cost(raw: str) -> Optional[float]:
    """The Cost column of the plan's first operation (Id 0) in DBMS_XPLAN's table."""
    match = re.search(r"\|\s*0\s*\|[^\n]*", raw)
    if not match:
        return None
    cells = [cell.strip() for cell in match.group(0).strip("|").split("|")]
    for cell in reversed(cells):
        number = re.match(r"^(\d+)(?:\s*\(\d+\))?$", cell)
        if number:
            return float(number.group(1))
    return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _finite(value: Any) -> Optional[float]:
    number = _as_float(value)
    if number is None or number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _oracle_tree(raw: str) -> tuple[Optional[PlanNode], Optional[float]]:
    """DBMS_XPLAN's plan table read into a tree - nesting from the Operation column's indent - with
    each step's predicates (from "Predicate Information") as its details, and the top step's Time
    estimate in seconds. (None, None) when there's no table to read."""
    lines = [line.expandtabs(8) for line in raw.splitlines()]
    header_index = next((i for i, line in enumerate(lines) if re.match(r"^\|\s*Id\s*\|", line)), None)
    if header_index is None:
        return None, None
    headers = [cell.strip().lower() for cell in lines[header_index].strip().strip("|").split("|")]

    def column(name: str) -> Optional[int]:
        return next((i for i, header in enumerate(headers) if header.startswith(name)), None)

    op_col, name_col, rows_col, cost_col, time_col = (column(n) for n in ("operation", "name", "rows", "cost", "time"))
    if op_col is None:
        return None, None
    predicates = _oracle_predicates(lines)
    root: Optional[PlanNode] = None
    stack: list[tuple[int, PlanNode]] = []
    base_indent: Optional[int] = None
    seconds: Optional[float] = None
    for line in lines[header_index + 1:]:
        match = re.match(r"^\|\s*\*?\s*(\d+)\s*\|", line)
        if not match:
            if root is not None and not line.startswith("|") and line.strip().startswith("-") is False and line.strip():
                break
            continue
        cells = line.strip().strip("|").split("|")
        if len(cells) <= op_col:
            continue
        operation = cells[op_col]
        indent = len(operation) - len(operation.lstrip())
        base_indent = indent if base_indent is None else base_indent
        node = PlanNode(
            operation=operation.strip(),
            object_name=(cells[name_col].strip() or None) if name_col is not None and name_col < len(cells) else None,
            estimated_rows=_oracle_number(cells[rows_col]) if rows_col is not None and rows_col < len(cells) else None,
            cost=_oracle_number(cells[cost_col].split("(")[0]) if cost_col is not None and cost_col < len(cells) else None,
            details=predicates.get(int(match.group(1)), {}),
        )
        if root is None:
            root = node
            if time_col is not None and time_col < len(cells):
                seconds = _oracle_seconds(cells[time_col])
            stack = [(indent, node)]
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        (stack[-1][1] if stack else root).children.append(node)
        stack.append((indent, node))
    return root, seconds


def _oracle_predicates(lines: list[str]) -> dict[int, dict[str, str]]:
    """ "Predicate Information": `   2 - access("A"."ID"="B"."ID")`, continued on indented lines."""
    out: dict[int, dict[str, str]] = {}
    start = next((i for i, line in enumerate(lines) if line.startswith("Predicate Information")), None)
    if start is None:
        return out
    current: Optional[tuple[int, str]] = None
    for line in lines[start + 1:]:
        if not line.strip() or set(line.strip()) == {"-"}:
            if current is not None and not line.strip():
                current = None
            continue
        match = re.match(r"^\s*(\d+)\s+-\s+(\w+)\((.*)$", line)
        if match:
            node_id, kind, text = int(match.group(1)), match.group(2).title(), match.group(3)
            text = text[:-1] if text.endswith(")") else text
            out.setdefault(node_id, {})
            out[node_id][kind] = (out[node_id].get(kind, "") + " " + text).strip()
            current = (node_id, kind)
        elif current is not None and line.startswith("  "):
            node_id, kind = current
            out[node_id][kind] += " " + line.strip().rstrip(")")
        elif re.match(r"^[A-Z]", line):
            break
    return out


def _oracle_number(cell: str) -> Optional[float]:
    match = re.match(r"^\s*([\d.]+)\s*([KMGTP]?)\s*$", cell or "")
    if not match:
        return None
    scale = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}[match.group(2)]
    return float(match.group(1)) * scale


def _oracle_seconds(cell: str) -> Optional[float]:
    match = re.match(r"^\s*(\d+):(\d+):(\d+)\s*$", cell or "")
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def _oracle_warnings(root: PlanNode) -> list[PlanWarning]:
    warnings: list[PlanWarning] = []
    for node in root.walk():
        rows = node.estimated_rows or 0
        if node.operation.startswith("TABLE ACCESS FULL") and rows >= LARGE_SCAN_ROWS:
            what = f"Full scan of {node.object_name}"
            if node.details.get("Filter"):
                what += f" filtered by {node.details['Filter'][:120]} - an index on the filtered column(s) could avoid it"
            warnings.append(PlanWarning("warning", "large_seq_scan", f"{what} (≈{_compact(rows)} rows)."))
        if "CARTESIAN" in node.operation:
            sides = [_scanned(child) for child in node.children]
            warnings.append(PlanWarning(
                "warning", "possible_cross_join",
                f"{node.operation}: every row of {' and '.join(sides) or 'one input'} paired with every row of the other (≈{_compact(rows)} rows).",
            ))
        if node.operation.startswith("SORT ORDER BY") and rows >= 1_000_000:
            warnings.append(PlanWarning("info", "large_sort", f"Sorts ≈{_compact(rows)} rows."))
    if root.cost is not None and root.cost >= VERY_HIGH_COST:
        warnings.append(PlanWarning("warning", "very_high_cost", f"The planner puts this query's cost at {_compact(root.cost)} units."))
    return warnings
