"""SQLGlot safety gate for generated SQL, and the parsing helpers the other
metrics share. Safety violations fail a test outright.
"""

from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Set, Tuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

from suite.schema_metadata import SQL_DIALECT, TableMetadata


def strip_sql_fences(text: str) -> str:
    """Remove a ```sql ... ``` wrapper that LLMs often add."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip().rstrip(";").strip()


def _node_types(*names: str) -> Tuple[type, ...]:
    # Class names move between sqlglot releases (e.g. AlterTable -> Alter).
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


WRITE_NODES = _node_types(
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "AlterTable",
    "TruncateTable",
    "Grant",
    "Command",
    "Into",
    "Lock",
    "Copy",
)

DANGEROUS_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "load_file",
        "sleep",
        "benchmark",
        "xp_cmdshell",
        "pg_sleep_for",
        "pg_sleep_until",
        # Change state from inside a SELECT.
        "setval",
        "nextval",
        "set_config",
        "pg_notify",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_switch_wal",
        "pg_promote",
        "pg_drop_replication_slot",
        "pg_logical_emit_message",
        "pg_file_write",
        # Run SQL passed to them as text, out of reach of the safety gate.
        "query_to_xml",
        "query_to_xml_and_xmlschema",
        "query_to_xmlschema",
    }
)

# Whole families: advisory locks (held until the session ends, so they can
# block other queries), large objects (create, change and delete stored
# data), dblink (runs SQL on any server), and statistics and backup controls.
DANGEROUS_PREFIXES = (
    "pg_advisory_",
    "pg_try_advisory_",
    "lo_",
    "dblink",
    "pg_stat_reset",
    "pg_create_",
    "pg_backup_",
)
# A denylist is never complete: the database account SQL Lab connects with
# should be read-only too.


@dataclass
class SafetyResult:
    sql: str
    violations: List[str] = field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        return not self.violations


def parse_one(sql: str, dialect: str) -> exp.Expression:
    statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    if len(statements) != 1:
        raise ParseError(f"expected 1 statement, got {len(statements)}")
    return statements[0]


def function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _is_dangerous(name: str) -> bool:
    return name in DANGEROUS_FUNCTIONS or name.startswith(DANGEROUS_PREFIXES)


def check_sql_safety(
    sql: Optional[str],
    dialect: str = SQL_DIALECT,
    metadata: Optional[Mapping[str, TableMetadata]] = None,
) -> SafetyResult:
    """Read-only, single-statement SELECT with no side-effecting functions and
    no access to columns marked sensitive in `metadata`."""
    sql = strip_sql_fences(sql or "")
    result = SafetyResult(sql=sql)
    if not sql:
        result.violations.append("empty SQL")
        return result

    try:
        statements = [
            s for s in sqlglot.parse(sql, read=dialect) if s is not None
        ]
    except SqlglotError as e:  # ParseError, or TokenError for an unclosed quote
        result.violations.append(f"unparseable SQL: {e}")
        return result

    if len(statements) != 1:
        result.violations.append(
            f"expected exactly one statement, got {len(statements)}"
        )
    for stmt in statements:
        if not isinstance(stmt, exp.Query):
            result.violations.append(
                f"top-level statement is {type(stmt).__name__}, not a SELECT"
            )
        for node in stmt.find_all(*WRITE_NODES):
            result.violations.append(f"forbidden {type(node).__name__} clause")
        for fn in stmt.find_all(exp.Func):
            if _is_dangerous(function_name(fn)):
                result.violations.append(
                    f"dangerous function {function_name(fn)}()"
                )
        if metadata:
            for col in sorted(referenced_sensitive_columns(stmt, metadata)):
                result.violations.append(f"references sensitive column {col}")
    return result


def referenced_tables(tree: exp.Expression) -> Set[str]:
    """Physical tables (lowercase, unqualified) referenced, excluding CTE names."""
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    return {
        t.name.lower()
        for t in tree.find_all(exp.Table)
        if t.name and t.name.lower() not in cte_names
    }


def tables_in_sql(sql: str, dialect: str = SQL_DIALECT) -> Set[str]:
    return referenced_tables(parse_one(strip_sql_fences(sql), dialect))


def same_query(a: str, b: str, dialect: str = SQL_DIALECT) -> bool:
    """Whether two SQL texts are the same query apart from layout, letter
    case and a trailing semicolon."""
    try:
        return parse_one(a, dialect).sql(dialect=dialect, normalize=True) == (
            parse_one(b, dialect).sql(dialect=dialect, normalize=True)
        )
    except SqlglotError:
        def flat(sql: str) -> str:
            return " ".join(sql.strip().rstrip(";").split()).lower()

        return flat(a) == flat(b)


def referenced_sensitive_columns(
    tree: exp.Expression, metadata: Mapping[str, TableMetadata]
) -> Set[str]:
    tables = [metadata[t] for t in referenced_tables(tree) if t in metadata]
    sensitive = {
        f"{t.name}.{c}": c.lower() for t in tables for c in t.sensitive_columns
    }
    if not sensitive:
        return set()
    used = {c.name.lower() for c in tree.find_all(exp.Column)}
    hits = {fq for fq, col in sensitive.items() if col in used}
    # SELECT * on a table with sensitive columns exposes them too.
    if any(
        is_star(p) for s in tree.find_all(exp.Select) for p in s.expressions
    ):
        hits |= set(sensitive)
    return hits


def is_star(projection: exp.Expression) -> bool:
    return isinstance(projection, exp.Star) or (
        isinstance(projection, exp.Column)
        and isinstance(projection.this, exp.Star)
    )
