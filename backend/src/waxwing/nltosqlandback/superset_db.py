"""Everything this extension asks of Superset about a database - and the only module that asks.

The extension has no database connections of its own: it reaches a database only through the
Superset ``Database`` the user picked in SQL Lab, with Superset's own permission checks in front of
it. And it never runs a user's SQL for data. What it does ask for:

- **Which database, and may this user use it** (`resolve_database`): through ``superset_core``'s
  ``DatabaseDAO``, whose base filter is Superset's own "databases this user can access" filter.
- **Its dialect** (`dialect_for`): ``superset_core``'s ``get_sqlglot_dialect``, the same mapping
  Superset parses SQL Lab's queries with, so a generated query is parsed the way Superset will.
- **What Superset's SQL validator makes of a query** (`validate`): the engine-specific validator
  configured in ``SQL_VALIDATORS_BY_ENGINE`` - the same one behind the editor's gutter markers.
- **A query's plan** (`explain`): the engine's ``EXPLAIN`` (see ``query_plan``), run through
  ``Database.execute`` - Superset's own execution path, with its security checks (disallowed
  functions and tables, DML permission), RLS and audit record. Planning is refused up front for
  anything but a single read-only query, and ``EXPLAIN ANALYZE`` is never used.
- **A database's metadata** (`list_schemas`, `list_tables`, `schema_reader`): schema, table and view
  names, columns and their types and comments, keys and indexes - through the ``Database`` model's
  own methods and its inspector, which read the database's catalog, never a table's rows. Filtered
  to what this user may see.
- **Roughly how many rows each table has** (`estimate_rows`): the planner's own estimate, from one
  ``EXPLAIN`` of a query touching sixty tables at a time - planned, never run.

Every import of ``superset`` itself is inside a function: this module is loaded by tests and tools
that have no Superset, and an extension's backend is imported before every part of the host is.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Generator, Iterator, Optional

from flask import current_app

from . import query_plan, request_context, telemetry
from .query_plan import NotPlannable, QueryPlan

logger = logging.getLogger(__name__)


@dataclass
class DatabaseInfo:
    id: int
    name: str
    backend: str
    #: sqlglot's name for the dialect ("postgres", "trino", ...), or None if it couldn't be told.
    dialect: Optional[str]
    model: Any  # the host's Database model


class DatabaseUnavailable(Exception):
    """The database doesn't exist, or this user may not use it."""


def resolve_database(database_id: Optional[int]) -> Optional[DatabaseInfo]:
    """The database `database_id` names, if this user can access it. None when no id was given;
    raises `DatabaseUnavailable` when one was given but can't be used."""
    if database_id is None:
        return None
    try:
        from superset_core.common.daos import DatabaseDAO

        database = DatabaseDAO.find_one_or_none(id=database_id)
    except Exception as ex:  # noqa: BLE001 - no host (tests, tools), or the DAO isn't injected
        raise DatabaseUnavailable(f"Superset's database API isn't available: {ex}") from ex
    if database is None:
        raise DatabaseUnavailable(f"Database {database_id} doesn't exist or you don't have access to it.")
    _check_access(database)
    backend = str(getattr(database, "backend", "") or "")
    info = DatabaseInfo(
        id=database_id,
        name=str(getattr(database, "database_name", None) or getattr(database, "name", "") or database_id),
        backend=backend,
        dialect=dialect_for(database, backend),
        model=database,
    )
    context = request_context.current()
    if context is not None and context.database_id == database_id:
        context.dialect = info.dialect or backend
    return info


def _check_access(database: Any) -> None:
    """Belt and braces on top of the DAO's base filter: Superset's own database-access check."""
    try:
        from superset import security_manager
    except Exception:  # noqa: BLE001
        return
    can_access = getattr(security_manager, "can_access_database", None)
    if can_access is not None and not can_access(database):
        raise DatabaseUnavailable("You don't have access to this database.")


def dialect_for(database: Any, backend: str) -> Optional[str]:
    """sqlglot's dialect name for `database`, from Superset's own mapping where the host provides
    it, else from the backend name."""
    try:
        from superset_core.queries.query import get_sqlglot_dialect

        dialect = get_sqlglot_dialect(database)
        name = getattr(dialect, "value", dialect)
        if name:
            return str(name)
    except Exception:  # noqa: BLE001 - not injected (tests) or an unknown engine
        pass
    return query_plan.sqlglot_dialect_for_backend(backend)


#: What the questions put to the database are called in the trace - named for what they ask, and
#: unlike any endpoint's name (``explain_plan`` is one), so the three kinds of span stay apart.
EXPLAIN_SPAN = "db · explain"
VALIDATE_SPAN = "db · validate"


@dataclass
class ValidationMessage:
    message: str
    line: Optional[int] = None
    column: Optional[int] = None


def validate(database: DatabaseInfo, sql: str, catalog: Optional[str], schema: Optional[str]) -> Optional[list[ValidationMessage]]:
    """Superset's configured SQL validator's verdict on `sql` (an empty list meaning "valid"), or
    None when there is no validator configured for this engine - which is the default."""
    validators = current_app.config.get("SQL_VALIDATORS_BY_ENGINE") or {}
    name = validators.get(database.backend)
    if not name:
        return None
    try:
        from superset.sql_validators import get_validator_by_name
    except Exception:  # noqa: BLE001
        return None
    validator = get_validator_by_name(name)
    if validator is None:
        return None
    start_ns = telemetry.now_ns()
    try:
        try:
            annotations = validator.validate(sql, catalog, schema, database.model)
        except TypeError:
            annotations = validator.validate(sql, schema, database.model)  # pre-catalog signature
    except Exception as ex:  # noqa: BLE001 - the validator itself failing isn't the SQL being wrong
        logger.warning("SQL validator %s failed: %s", name, ex)
        _record_validation(database, name, sql, start_ns, None, str(ex))
        return None
    messages = [
        ValidationMessage(
            message=str(getattr(annotation, "message", "") or ""),
            line=getattr(annotation, "line_number", None),
            column=getattr(annotation, "start_column", None),
        )
        for annotation in annotations or []
        if getattr(annotation, "message", None)
    ]
    _record_validation(database, name, sql, start_ns, messages)
    return messages


def _record_validation(
    database: DatabaseInfo,
    validator: str,
    sql: str,
    start_ns: int,
    messages: Optional[list[ValidationMessage]],
    error: Optional[str] = None,
) -> None:
    telemetry.record_tool_call(
        VALIDATE_SPAN,
        start_ns,
        telemetry.now_ns(),
        input_text=sql,
        output_text="\n".join(_located_message(message) for message in messages or []) or ("" if error else "valid"),
        error=error,
        attributes={
            "databaseId": database.id,
            "backend": database.backend,
            "validator": validator,
            "messages": len(messages or []),
        },
    )


def _located_message(message: ValidationMessage) -> str:
    return f"line {message.line}: {message.message}" if message.line is not None else message.message


@dataclass
class PlanOutcome:
    """What asking for a plan came to. `plan` on success; otherwise `error`, and `checked` says
    whether the database actually looked at the query (so the error is about the SQL) or the plan
    was never asked for (an engine planning doesn't support, a query that isn't plannable, no
    access) - the difference between "the database rejected this" and "couldn't check"."""

    plan: Optional[QueryPlan] = None
    error: Optional[str] = None
    checked: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.plan is not None


def explain(database: DatabaseInfo, sql: str, catalog: Optional[str], schema: Optional[str]) -> PlanOutcome:
    """`sql`'s plan on `database`, via its own EXPLAIN. Never runs the query."""
    start_ns = telemetry.now_ns()
    try:
        text = query_plan.check_plannable(sql, database.dialect)
    except NotPlannable as ex:
        return PlanOutcome(error=str(ex), checked=False)
    statements = query_plan.explain_statement(text, database.backend)
    if statements is None:
        return PlanOutcome(error=f"Planning isn't supported for {database.backend} databases yet.", checked=False)

    outcome = _run_explain(database, statements, catalog, schema)
    end_ns = telemetry.now_ns()
    outcome.duration_ms = int((end_ns - start_ns) / 1_000_000)
    telemetry.record_tool_call(
        EXPLAIN_SPAN,
        start_ns,
        end_ns,
        input_text=";\n".join(statements),
        output_text=outcome.plan.outline if outcome.plan else "",
        error=outcome.error,
        attributes={"databaseId": database.id, "backend": database.backend, "checked": outcome.checked},
    )
    return outcome


def _run_explain(database: DatabaseInfo, statements: list[str], catalog: Optional[str], schema: Optional[str]) -> PlanOutcome:
    try:
        from superset_core.queries.types import CacheOptions, QueryOptions, QueryStatus
    except Exception as ex:  # noqa: BLE001
        return PlanOutcome(error=f"Superset's query API isn't available: {ex}", checked=False)
    timeout = int(current_app.config.get("NLSQL_EXPLAIN_TIMEOUT_SECONDS", 30))
    options = QueryOptions(
        catalog=catalog,
        schema=schema,
        limit=None,
        timeout_seconds=timeout,
        # A plan depends on the statistics as they are now, so never answer from a cached one.
        cache=CacheOptions(force_refresh=True),
    )
    try:
        result = database.model.execute(";\n".join(statements), options)
    except Exception as ex:  # noqa: BLE001 - e.g. an OAuth2 redirect, or a host without execute()
        return PlanOutcome(error=f"Couldn't ask the database for a plan: {ex}", checked=False)
    status = getattr(result, "status", None)
    if status != QueryStatus.SUCCESS:
        message = getattr(result, "error_message", None) or "The database didn't return a plan."
        # A security refusal (disallowed function/table, DML) or a timeout says nothing about the
        # SQL being wrong; anything else is the database's verdict on it.
        security = any(
            word in message.lower()
            for word in ("disallowed", "not allowed", "permission", "access", "privilege", "ora-01031", "ora-01039")
        )
        return PlanOutcome(error=message, checked=status == QueryStatus.FAILED and not security)
    statements_out = getattr(result, "statements", None) or []
    data = statements_out[-1].data if statements_out else None
    if data is None:
        return PlanOutcome(error="The database returned no plan.", checked=False)
    rows = data.values.tolist() if hasattr(data, "values") else list(data)
    try:
        plan = query_plan.parse_plan(database.backend, rows)
    except Exception as ex:  # noqa: BLE001 - an engine answering in a shape we don't read
        logger.warning("could not read a %s plan: %s", database.backend, ex)
        raw = "\n".join(str(row[0]) for row in rows if row)
        plan = QueryPlan(backend=database.backend, raw=raw, root=None, outline=raw[: query_plan.PLAN_OUTLINE_MAX_CHARS])
    return PlanOutcome(plan=plan, checked=True)


# --- Schema metadata ----------------------------------------------------------------------------


@dataclass
class ColumnMeta:
    name: str
    data_type: str
    nullable: bool = True
    comment: Optional[str] = None
    #: The allowed values when the column's type is an enum - part of the type, not the data.
    enum_values: Optional[list[str]] = None


@dataclass
class ForeignKeyMeta:
    columns: list[str]
    referred_table: str
    referred_columns: list[str]
    referred_schema: Optional[str] = None


@dataclass
class TableMeta:
    name: str
    kind: str  # "table" or "view"
    columns: list[ColumnMeta]
    comment: Optional[str] = None
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKeyMeta] = field(default_factory=list)
    #: Each index as its column list, unique ones marked - which columns are cheap to filter or join on.
    indexes: list[tuple[list[str], bool]] = field(default_factory=list)
    error: Optional[str] = None




#: Schemas that belong to the database itself rather than to anyone's data - never interrogated.
#: Compared case-insensitively; ``NLSQL_INTERROGATE_EXCLUDE_SCHEMAS`` replaces the list.
DEFAULT_EXCLUDED_SCHEMAS = frozenset(
    name.lower()
    for name in (
        # The SQL standard, Postgres, MySQL/MariaDB, Trino, SQL Server
        "information_schema", "pg_catalog", "pg_toast", "mysql", "performance_schema", "sys",
        "guest", "db_owner", "db_accessadmin", "db_securityadmin", "db_ddladmin", "db_backupoperator",
        "db_datareader", "db_datawriter", "db_denydatareader", "db_denydatawriter",
        # Oracle's own users
        "anonymous", "appqossys", "audsys", "ctxsys", "dbsfwuser", "dbsnmp", "dgpdb_int", "dip", "dvf",
        "dvsys", "ggsharedcap", "ggsys", "gsmadmin_internal", "gsmcatuser", "gsmrootuser", "gsmuser",
        "lbacsys", "mddata", "mdsys", "ojvmsys", "olapsys", "oracle_ocm", "orddata", "ordplugins",
        "ordsys", "outln", "remote_scheduler_agent", "si_informtn_schema", "sys$umf", "sysbackup",
        "sysdg", "syskm", "sysrac", "system", "wmsys", "xdb", "xs$null", "baasys", "pdbadmin",
        "apex_public_user", "flows_files", "ords_metadata", "ords_public_user", "opstool",
    )
)
_EXCLUDED_PATTERN = re.compile(r"^(pg_temp_\d+|pg_toast_temp_\d+|apex_\d+)$", re.IGNORECASE)


def default_schema(database: DatabaseInfo, catalog: Optional[str]) -> Optional[str]:
    """The schema a query with unqualified names would read, e.g. Postgres' ``public``."""
    getter = getattr(database.model, "get_default_schema", None)
    if getter is None:
        return None
    try:
        return getter(catalog)
    except Exception as ex:  # noqa: BLE001
        logger.info("could not read the default schema of %s: %s", database.name, ex)
        return None


def list_schemas(database: DatabaseInfo, catalog: Optional[str], refresh: bool = False) -> tuple[list[str], int]:
    """The schemas of `database` worth interrogating and that this user may see, sorted - and how
    many system schemas were passed over. `refresh` skips Superset's cached list."""
    method = getattr(database.model, "get_all_schema_names")
    names = method(catalog=catalog, **({"force": True} if refresh else {}))
    configured = _config("NLSQL_INTERROGATE_EXCLUDE_SCHEMAS", None)
    excluded = {str(name).lower() for name in configured} if configured is not None else DEFAULT_EXCLUDED_SCHEMAS
    wanted = sorted(
        str(name) for name in names or []
        if str(name).lower() not in excluded and not _EXCLUDED_PATTERN.match(str(name))
    )
    skipped = len(set(names or [])) - len(wanted)
    only = _config("NLSQL_INTERROGATE_SCHEMAS", None)
    if only:
        allowed = {str(name).lower() for name in only}
        wanted = [name for name in wanted if name.lower() in allowed]
    return accessible_schemas(database, catalog, wanted), skipped


def accessible_schemas(database: DatabaseInfo, catalog: Optional[str], names: list[str]) -> list[str]:
    """`names` narrowed to the schemas this user has any access to, by Superset's own rules."""
    try:
        from superset import security_manager
    except Exception:  # noqa: BLE001 - no host (tests)
        return names
    getter = getattr(security_manager, "get_schemas_accessible_by_user", None)
    if getter is None:
        return names
    try:
        allowed = getter(database.model, catalog, set(names))
    except Exception as ex:  # noqa: BLE001
        logger.warning("could not check schema access: %s", ex)
        return names
    return [name for name in names if name in allowed]


def _config(name: str, default: Any) -> Any:
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def list_tables(database: DatabaseInfo, catalog: Optional[str], schema: str, refresh: bool = False) -> list[tuple[str, str]]:
    """Every table and view in `schema` this user may see, as (name, "table" | "view"), sorted.
    `refresh` skips Superset's cached lists."""
    model = database.model
    names: dict[str, str] = {}
    extra = {"force": True} if refresh else {}
    for kind, getter in (("table", "get_all_table_names_in_schema"), ("view", "get_all_view_names_in_schema")):
        method = getattr(model, getter, None)
        if method is None:
            continue
        try:
            found = method(catalog=catalog, schema=schema, **extra)
        except Exception as ex:  # noqa: BLE001 - views can be unsupported where tables aren't
            if kind == "table":
                raise
            logger.info("could not list views in %s: %s", schema, ex)
            continue
        for entry in found or []:
            name = entry[0] if isinstance(entry, tuple) else getattr(entry, "table", entry)
            names.setdefault(str(name), kind)
    visible = {name for _schema, name in accessible_tables(database, catalog, [(schema, name) for name in names])}
    return [(name, names[name]) for name in sorted(names) if name in visible]


def accessible_tables(database: DatabaseInfo, catalog: Optional[str], tables: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """`tables` - (schema, name) pairs - narrowed to those this user has access to, by Superset's
    own rules (database, catalog, schema or table-level permission)."""
    try:
        from superset import security_manager
        from superset.sql.parse import Table
    except Exception:  # noqa: BLE001 - no host (tests)
        return tables
    getter = getattr(security_manager, "get_datasources_accessible_by_user", None)
    if getter is None:
        return tables
    try:
        allowed = getter(database.model, [Table(name, schema, catalog) for schema, name in tables], catalog)
    except Exception as ex:  # noqa: BLE001
        logger.warning("could not check table access: %s", ex)
        return tables
    allowed_pairs = {(getattr(table, "schema", None), getattr(table, "table", None) or str(table)) for table in allowed}
    return [(schema, name) for schema, name in tables if (schema, name) in allowed_pairs]


@contextmanager
def schema_reader(database: DatabaseInfo, catalog: Optional[str], schema: str, comments: bool = True) -> Iterator[Callable[[str, str], TableMeta]]:
    """A function reading one table of `schema` at a time, through a single inspector for the whole
    schema - the ``Database`` model's own per-table methods each open a new engine, which over
    hundreds of tables is most of the time taken. Falls back to those methods when the host has no
    ``get_inspector``. `comments` = False skips the table comment (a query of its own per table);
    column comments come with the columns either way."""
    getter = getattr(database.model, "get_inspector", None)
    if getter is None:
        yield lambda name, kind: read_table(database, catalog, schema, name, kind, comments)
        return
    with getter(catalog=catalog, schema=schema) as inspector:
        yield lambda name, kind: _read_with_inspector(database, inspector, catalog, schema, name, kind, comments)


def _read_with_inspector(database: DatabaseInfo, inspector: Any, catalog: Optional[str], schema: str, name: str, kind: str, comments: bool) -> TableMeta:
    try:
        from superset.sql.parse import Table
    except Exception as ex:  # noqa: BLE001
        return TableMeta(name=name, kind=kind, columns=[], error=f"Superset isn't available: {ex}")
    model = database.model
    spec = model.db_engine_spec
    table = Table(name, schema, catalog)
    return _assemble(
        name,
        kind,
        columns=lambda: spec.get_columns(inspector, table, getattr(model, "schema_options", None)),
        comment=(lambda: spec.get_table_comment(inspector, table)) if comments else None,
        pk=lambda: inspector.get_pk_constraint(name, schema),
        fks=lambda: inspector.get_foreign_keys(name, schema),
        indexes=lambda: spec.get_indexes(model, inspector, table),
    )


def read_table(database: DatabaseInfo, catalog: Optional[str], schema: str, name: str, kind: str = "table", comments: bool = True) -> TableMeta:
    """One table's structure, from the database's catalog, through the ``Database`` model's own
    methods. A part that can't be read (an engine without foreign keys, say) is left empty; a table
    that can't be read at all says why in `error`."""
    try:
        from superset.sql.parse import Table
    except Exception as ex:  # noqa: BLE001
        return TableMeta(name=name, kind=kind, columns=[], error=f"Superset isn't available: {ex}")
    table = Table(name, schema, catalog)
    model = database.model
    return _assemble(
        name,
        kind,
        columns=lambda: model.get_columns(table),
        comment=(lambda: model.get_table_comment(table)) if comments else None,
        pk=lambda: model.get_pk_constraint(table),
        fks=lambda: model.get_foreign_keys(table),
        indexes=lambda: model.get_indexes(table),
    )


def _assemble(name: str, kind: str, columns: Callable[[], Any], comment: Optional[Callable[[], Any]], pk: Callable[[], Any], fks: Callable[[], Any], indexes: Callable[[], Any]) -> TableMeta:
    try:
        raw_columns = columns()
    except Exception as ex:  # noqa: BLE001
        return TableMeta(name=name, kind=kind, columns=[], error=str(ex))
    meta = TableMeta(name=name, kind=kind, columns=[_column_meta(column) for column in raw_columns or []])
    if comment is not None:
        meta.comment = _optional(comment)
    if kind == "view":
        return meta  # views have no keys or indexes of their own
    meta.primary_key = [str(column) for column in (_optional(pk) or {}).get("constrained_columns") or []]
    for fk in _optional(fks) or []:
        if not fk.get("referred_table"):
            continue
        meta.foreign_keys.append(ForeignKeyMeta(
            columns=[str(column) for column in fk.get("constrained_columns") or []],
            referred_table=str(fk["referred_table"]),
            referred_columns=[str(column) for column in fk.get("referred_columns") or []],
            referred_schema=fk.get("referred_schema"),
        ))
    names = {column.name for column in meta.columns}
    for index in _optional(indexes) or []:
        index_columns = [str(column) for column in index.get("column_names") or [] if column]
        # Only indexes on the table's own columns: Trino reports an unpartitioned Iceberg table's
        # "$partitions" summary (record_count, file_count...) as one, and an expression index has
        # no plain columns to name.
        if index_columns and all(column in names for column in index_columns):
            meta.indexes.append((index_columns, bool(index.get("unique"))))
    return meta


def _optional(read: Any) -> Any:
    try:
        return read()
    except Exception as ex:  # noqa: BLE001 - one missing part of the metadata isn't worth failing for
        logger.debug("metadata read failed: %s", ex)
        return None


def _column_meta(raw: dict[str, Any]) -> ColumnMeta:
    sql_type = raw.get("type")
    enums = getattr(sql_type, "enums", None)
    comment = raw.get("comment")
    return ColumnMeta(
        name=str(raw.get("column_name") or raw.get("name")),
        data_type=_type_name(sql_type),
        nullable=bool(raw.get("nullable", True)),
        comment=str(comment).strip() if comment else None,
        enum_values=[str(value) for value in enums] if enums else None,
    )


def _type_name(sql_type: Any) -> str:
    if sql_type is None:
        return "?"
    if isinstance(sql_type, str):
        return sql_type
    try:
        return str(sql_type.compile())
    except Exception:  # noqa: BLE001 - a dialect-specific type without a default compiler
        return str(sql_type) if str(sql_type) else type(sql_type).__name__


#: How many tables one row-estimate plan covers - a query over hundreds of tables plans slowly.
ROW_ESTIMATE_BATCH = 60
#: How many plans one interrogation may ask for beyond one per batch, splitting batches the
#: database refused (one table it won't plan - a permission, a broken view - fails the whole query).
ROW_ESTIMATE_RETRIES = 40

SchemaTable = tuple[str, str]  # (schema, table)


@dataclass
class RowEstimates:
    #: The planner's row estimate per (schema, table).
    estimates: dict[SchemaTable, float] = field(default_factory=dict)
    #: Tables the database refused to plan a query on at all, and what it said - usually a
    #: permission it hasn't granted, which a query written against them would hit too.
    refused: dict[SchemaTable, str] = field(default_factory=dict)
    #: The last error, when nothing could be sized.
    error: Optional[str] = None


def estimate_rows(database: DatabaseInfo, catalog: Optional[str], tables: list[SchemaTable]) -> RowEstimates:
    """`estimate_rows_progressively`, without the progress."""
    batches = estimate_rows_progressively(database, catalog, tables)
    while True:
        try:
            next(batches)
        except StopIteration as stop:
            return stop.value


def estimate_rows_progressively(
    database: DatabaseInfo, catalog: Optional[str], tables: list[SchemaTable]
) -> Generator[tuple[int, int], None, RowEstimates]:
    """The planner's estimate of each table's row count, read from the plan of one
    ``SELECT 1 FROM a UNION ALL SELECT 1 FROM b ...`` per batch of tables - planned, never run, and
    one entry in the user's query history per batch rather than one per table. A batch the
    database won't plan is split in two and tried again, within ``ROW_ESTIMATE_RETRIES``, down to
    the tables it won't plan on their own.

    Yields (plans asked for, plans in all) as it goes."""
    result = RowEstimates()
    if query_plan.explain_statement("SELECT 1", database.backend) is None:
        result.error = f"Planning isn't supported for {database.backend} databases yet."
        return result
    pending = _estimate_batches(tables)
    total = len(pending)
    retries = ROW_ESTIMATE_RETRIES
    done = 0
    while pending:
        batch = pending.pop(0)
        sql = "\nUNION ALL\n".join(f"SELECT 1 FROM {_qualified(database, schema, name)}" for schema, name in batch)
        outcome = explain(database, sql, catalog, None)
        done += 1
        if not outcome.ok or outcome.plan is None or outcome.plan.root is None:
            result.error = outcome.error or "The plan couldn't be read."
            if len(batch) > 1 and retries >= 2:
                retries -= 2
                middle = len(batch) // 2
                pending[:0] = [batch[:middle], batch[middle:]]
                total += 2
            elif len(batch) == 1:
                result.refused[batch[0]] = result.error.strip().splitlines()[0][:300]
            yield done, total
            continue
        _read_estimates(outcome.plan.root, batch, result.estimates)
        yield done, total
    if result.estimates:
        result.error = None
    return result


def _read_estimates(root: Any, batch: list[SchemaTable], estimates: dict[SchemaTable, float]) -> None:
    """Each of `batch`'s row estimates from the plan of its ``UNION ALL``: by the name of the step
    that scans it where the plan names it - longest names first, so "orders" doesn't claim the scan
    of "orders_archive" - and otherwise by position, since the union's branches are the batch's
    tables in order (Oracle may answer ``SELECT 1 FROM t`` from an index, and name only that)."""
    wanted = sorted(batch, key=lambda pair: len(pair[1]), reverse=True)
    found: dict[SchemaTable, float] = {}
    for node in root.walk():
        if node.estimated_rows is None or not node.object_name:
            continue
        for schema, name in wanted:
            if (schema, name) not in found and _table_in(node.object_name, schema, [name]) is not None:
                found[(schema, name)] = node.estimated_rows
                break
    if len(found) < len(batch):
        if len(batch) == 1:
            branches = [root]
        else:
            union = next(
                (node for node in root.walk() if len(node.children) == len(batch) and _is_union(node.operation)),
                None,
            )
            branches = union.children if union is not None else []
        for pair, branch in zip(batch, branches):
            if pair not in found and branch.estimated_rows is not None:
                found[pair] = branch.estimated_rows
    estimates.update(found)


def _is_union(operation: str) -> bool:
    text = operation.upper().replace("-", " ").replace("_", " ")
    return "UNION" in text or text in ("APPEND", "MERGE APPEND")


def _estimate_batches(tables: list[SchemaTable]) -> list[list[SchemaTable]]:
    """`tables` in batches of at most ``ROW_ESTIMATE_BATCH``, no two in a batch sharing a name -
    some engines (Oracle) name a plan step by the table alone, not its schema."""
    batches: list[tuple[list[SchemaTable], set[str]]] = []
    for schema, name in tables:
        key = name.lower()
        for batch, names in batches:
            if len(batch) < ROW_ESTIMATE_BATCH and key not in names:
                batch.append((schema, name))
                names.add(key)
                break
        else:
            batches.append(([(schema, name)], {key}))
    return [batch for batch, _names in batches]


#: What Trino adds to a table's name in a plan: Iceberg's "$data" and the snapshot it read ("@123").
_TRINO_SUFFIX = re.compile(r"(\$(data|partitions|files|snapshots|history|manifests))?@\d+$|\$data$")


def _table_in(object_name: str, schema: Optional[str], names: list[str]) -> Optional[str]:
    """Which of `names` a plan step reads, from how the engine names it: "schema.table alias",
    "table", Trino's "catalog:schema.table$data@snapshot", Oracle's upper case - and names with
    spaces in them."""
    text = object_name.replace('"', "").replace("`", "").lower()
    text = _TRINO_SUFFIX.sub("", text).replace(":", ".")
    prefix = f"{schema.lower()}." if schema else ""
    for name in names:
        lowered = name.lower()
        for candidate in (f"{prefix}{lowered}", lowered):
            if text == candidate or text.startswith(candidate + " ") or text.endswith("." + candidate) \
                    or f".{candidate} " in text:
                return name
    return None


def _qualified(database: DatabaseInfo, schema: Optional[str], name: str) -> str:
    """`schema.name` as this database would read it. A plain lower-case name is left unquoted:
    SQLAlchemy reports case-insensitive names in lower case whatever the database stores them as
    (Oracle's are upper case), so quoting one would name a table that doesn't exist. Anything else
    - mixed case, spaces, a reserved word - is quoted the database's way."""
    from sqlglot import exp

    def identifier(part: str) -> exp.Identifier:
        plain = re.match(r"^[a-z_][a-z0-9_$]*$", part) is not None and part.upper() not in _RESERVED
        return exp.to_identifier(part, quoted=not plain)

    table = exp.Table(this=identifier(name), db=identifier(schema) if schema else None)
    return table.sql(dialect=database.dialect or None)


#: Words that can't be a bare table or schema name in the common engines.
_RESERVED = frozenset(
    "ALL AND ANY AS ASC BETWEEN BY CASE CHECK COLUMN CREATE CROSS CURRENT DEFAULT DELETE DESC DISTINCT "
    "DROP ELSE END EXCEPT EXISTS FALSE FETCH FOR FROM FULL GRANT GROUP HAVING IN INDEX INNER INSERT "
    "INTERSECT INTO IS JOIN LEFT LIKE LIMIT NOT NULL OF OFFSET ON OR ORDER OUTER RIGHT ROW ROWS SELECT "
    "SET TABLE THEN TO TRUE UNION UNIQUE UPDATE USER USING VALUES VIEW WHEN WHERE WITH".split()
)
