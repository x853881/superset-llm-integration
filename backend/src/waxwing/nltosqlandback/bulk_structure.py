"""Reading a database's structure with a few catalog queries per group of schemas, instead of
several inspector calls per table.

The inspector (SQLAlchemy 1.4, which Superset 6.1 pins) reads one table at a time: its columns,
comment, primary key, foreign keys and indexes are a query each, so hundreds of tables are
thousands of round trips - minutes, and enough connections at once for Oracle to start refusing
them. The same facts sit in a handful of catalog views, which can be read for many schemas at once.

``NLSQL_STRUCTURE_READ`` says how much each query covers, for every database or per Superset
database name::

    NLSQL_STRUCTURE_READ = "per_database"
    NLSQL_STRUCTURE_READ = {"default": "per_database", "Warehouse": "per_table"}

- ``per_table`` (the default): the inspector, as before.
- ``per_schema``: each catalog view once per schema.
- ``per_database``: each catalog view once for all the schemas being read (in groups of
  ``SCHEMAS_PER_QUERY``, which Oracle's limit on an ``IN`` list needs).

Only Oracle and Trino have the catalog queries; any other engine is read per table whatever the
setting. These queries read the catalog - names, types, keys - never anyone's rows, and run through
Superset's ``Database.execute`` like the data dictionary's. Superset caps a result at
``SQL_MAX_ROW``: a group whose result reaches it is split and read again, and a single schema that
still reaches it is read per table.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from flask import current_app

from . import telemetry
from .superset_db import ColumnMeta, DatabaseInfo, ForeignKeyMeta, TableMeta

logger = logging.getLogger(__name__)

MODES = ("per_table", "per_schema", "per_database")
DEFAULT_MODE = "per_table"
#: The most schemas one query names: Oracle allows 1,000 values in an IN list.
SCHEMAS_PER_QUERY = 500
STRUCTURE_SPAN = "db · structure"

#: A query's rows, and whether there were more than Superset would return.
Rows = tuple[list[tuple[Any, ...]], bool]
Runner = Callable[[str], Rows]


class TooManyRows(Exception):
    """A catalog query's result reached the row cap, so some of it is missing."""


def mode_for(database: DatabaseInfo) -> str:
    """How `database`'s structure is to be read: one of `MODES`. Falls back to per table for an
    engine without catalog queries, or a setting that isn't one of them."""
    try:
        configured = current_app.config.get("NLSQL_STRUCTURE_READ", DEFAULT_MODE)
    except RuntimeError:
        configured = DEFAULT_MODE
    if isinstance(configured, dict):
        configured = configured.get(database.name, configured.get("default", DEFAULT_MODE))
    mode = str(configured or DEFAULT_MODE).lower()
    if mode not in MODES:
        logger.warning("NLSQL_STRUCTURE_READ %r for %s isn't one of %s - reading per table", configured, database.name, MODES)
        return "per_table"
    if mode != "per_table" and not supports(database):
        return "per_table"
    return mode


def supports(database: DatabaseInfo) -> bool:
    return (database.dialect or "").lower() in _QUERIES


def groups(mode: str, schemas: list[str]) -> list[list[str]]:
    """The schemas each round of catalog queries covers."""
    if mode == "per_schema":
        return [[schema] for schema in schemas]
    return [schemas[start:start + SCHEMAS_PER_QUERY] for start in range(0, len(schemas), SCHEMAS_PER_QUERY)]


def read_group(
    dialect: str,
    run: Runner,
    catalog: Optional[str],
    listed: dict[str, list[tuple[str, str]]],
    comments: bool,
) -> dict[str, list[TableMeta]]:
    """The structure of the `listed` tables - (name, "table" | "view") per schema - from the catalog
    views, in the order listed. A listed table the catalog has no columns for says so in `error`.
    Raises `TooManyRows` when a query's result was cut short."""
    queries = _QUERIES[dialect.lower()]
    schemas = list(listed)
    found = queries(run, catalog, schemas, comments)
    out: dict[str, list[TableMeta]] = {}
    for schema, tables in listed.items():
        out[schema] = []
        for name, kind in tables:
            parts = found.get((schema, name))
            if parts is None or not parts.columns:
                out[schema].append(TableMeta(name=name, kind=kind, columns=[], error="the catalog has no columns for it"))
                continue
            out[schema].append(parts.meta(name, kind, comments))
    return out


def superset_runner(database: DatabaseInfo, catalog: Optional[str]) -> Runner:
    """Runs a catalog query through Superset's ``Database.execute``, as the data dictionary is read."""

    def run(sql: str) -> Rows:
        from superset_core.queries.types import CacheOptions, QueryOptions, QueryStatus

        cap = int(current_app.config.get("SQL_MAX_ROW") or 100_000)
        timeout = int(current_app.config.get("NLSQL_STRUCTURE_TIMEOUT_SECONDS", 120))
        options = QueryOptions(catalog=catalog, schema=None, limit=cap, timeout_seconds=timeout, cache=CacheOptions(force_refresh=True))
        start_ns = telemetry.now_ns()
        error: Optional[str] = None
        rows: list[tuple[Any, ...]] = []
        try:
            result = database.model.execute(sql, options)
            if getattr(result, "status", None) != QueryStatus.SUCCESS:
                error = getattr(result, "error_message", None) or "The database didn't return the catalog rows."
            else:
                statements = getattr(result, "statements", None) or []
                data = statements[-1].data if statements else None
                rows = [tuple(row) for row in (data.values.tolist() if hasattr(data, "values") else list(data or []))]
        except Exception as ex:  # noqa: BLE001 - recorded below, then raised for the caller to fall back
            error = str(ex)
        telemetry.record_tool_call(
            STRUCTURE_SPAN,
            start_ns,
            telemetry.now_ns(),
            input_text=sql,
            output_text=f"{len(rows)} rows",
            error=error,
            attributes={"databaseId": database.id, "backend": database.backend},
        )
        if error is not None:
            raise RuntimeError(error)
        return rows, len(rows) >= cap

    return run


# --- What the catalog says about one table -----------------------------------------------------


@dataclass
class _Parts:
    columns: list[tuple[int, ColumnMeta]] = field(default_factory=list)
    comment: Optional[str] = None
    column_comments: dict[str, str] = field(default_factory=dict)
    primary_key: list[tuple[int, str]] = field(default_factory=list)
    primary_key_name: Optional[str] = None
    #: constraint name -> [(position, column, referred schema, referred table, referred column)]
    foreign_keys: dict[str, list[tuple[int, str, str, str, str]]] = field(default_factory=lambda: defaultdict(list))
    #: index name -> (unique, [(position, column)])
    indexes: dict[str, tuple[bool, list[tuple[int, str]]]] = field(default_factory=dict)

    def meta(self, name: str, kind: str, comments: bool) -> TableMeta:
        columns = []
        for _position, column in sorted(self.columns, key=lambda item: item[0]):
            if column.name in self.column_comments:
                column.comment = self.column_comments[column.name]
            columns.append(column)
        meta = TableMeta(name=name, kind=kind, columns=columns, comment=self.comment if comments else None)
        if kind == "view":
            return meta
        meta.primary_key = [column for _position, column in sorted(self.primary_key)]
        for parts in sorted(self.foreign_keys.values(), key=lambda rows: [row[1] for row in sorted(rows)]):
            parts = sorted(parts)
            referred_schema, referred_table = parts[0][2], parts[0][3]
            meta.foreign_keys.append(ForeignKeyMeta(
                columns=[row[1] for row in parts],
                referred_table=referred_table,
                referred_columns=[row[4] for row in parts],
                referred_schema=referred_schema,
            ))
        names = {column.name for column in meta.columns}
        for index_name, (unique, index_columns) in sorted(self.indexes.items()):
            if index_name == self.primary_key_name:
                continue
            ordered = [column for _position, column in sorted(index_columns)]
            if ordered and all(column in names for column in ordered):
                meta.indexes.append((ordered, unique))
        return meta


# --- Oracle --------------------------------------------------------------------------------------


def _oracle_name(name: str) -> str:
    """A name as Oracle's catalog stores it, from SQLAlchemy's spelling: case-insensitive names are
    reported in lower case and stored in upper case; anything else is stored as written."""
    return name.upper() if name == name.lower() else name


def _sqlalchemy_name(name: str) -> str:
    """The reverse of `_oracle_name`: what SQLAlchemy (and so Superset) calls an Oracle name."""
    return name.lower() if name == name.upper() else name


def _literals(values: list[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _oracle_type(data_type: str, length: Any, char_length: Any, precision: Any, scale: Any) -> str:
    base = str(data_type or "?")
    precision = int(precision) if precision not in (None, "") else None
    scale = int(scale) if scale not in (None, "") else None
    if base == "NUMBER":
        if precision is None:
            return "NUMBER"
        return f"NUMBER({int(precision)})" if not scale else f"NUMBER({int(precision)},{int(scale)})"
    if base == "FLOAT" and precision is not None:
        return f"FLOAT({int(precision)})"
    if base in ("VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR"):
        size = char_length or length
        return f"{base}({int(size)})" if size else base
    if base == "RAW" and length:
        return f"RAW({int(length)})"
    return base


def _oracle_queries(run: Runner, _catalog: Optional[str], schemas: list[str], comments: bool) -> dict[tuple[str, str], _Parts]:
    owners = _literals([_oracle_name(schema) for schema in schemas])
    found: dict[tuple[str, str], _Parts] = defaultdict(_Parts)

    def rows(sql: str) -> list[tuple[Any, ...]]:
        result, truncated = run(sql)
        if truncated:
            raise TooManyRows(sql.split("FROM", 1)[1].split()[0])
        return result

    def key(owner: str, table: str) -> tuple[str, str]:
        return _sqlalchemy_name(str(owner)), _sqlalchemy_name(str(table))

    for owner, table, column, data_type, length, char_length, precision, scale, nullable, position in rows(
        "SELECT owner, table_name, column_name, data_type, data_length, char_length, data_precision, data_scale, "
        f"nullable, column_id FROM all_tab_columns WHERE owner IN ({owners})"
    ):
        found[key(owner, table)].columns.append((int(position or 0), ColumnMeta(
            name=_sqlalchemy_name(str(column)),
            data_type=_oracle_type(data_type, length, char_length, precision, scale),
            nullable=nullable != "N",
        )))
    for owner, table, column, text in rows(
        f"SELECT owner, table_name, column_name, comments FROM all_col_comments WHERE owner IN ({owners}) AND comments IS NOT NULL"
    ):
        if str(text).strip():
            found[key(owner, table)].column_comments[_sqlalchemy_name(str(column))] = str(text).strip()
    if comments:
        for owner, table, text in rows(
            f"SELECT owner, table_name, comments FROM all_tab_comments WHERE owner IN ({owners}) AND comments IS NOT NULL"
        ):
            if str(text).strip():
                found[key(owner, table)].comment = str(text).strip()
    for owner, table, name, column, position in rows(
        "SELECT c.owner, c.table_name, c.constraint_name, cc.column_name, cc.position "
        "FROM all_constraints c JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
        f"WHERE c.constraint_type = 'P' AND c.owner IN ({owners})"
    ):
        parts = found[key(owner, table)]
        parts.primary_key.append((int(position or 0), _sqlalchemy_name(str(column))))
        parts.primary_key_name = str(name)
    for owner, table, name, column, position, r_owner, r_table, r_column in rows(
        "SELECT c.owner, c.table_name, c.constraint_name, cc.column_name, cc.position, "
        "r.owner, r.table_name, rc.column_name "
        "FROM all_constraints c "
        "JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
        "JOIN all_constraints r ON r.owner = c.r_owner AND r.constraint_name = c.r_constraint_name "
        "JOIN all_cons_columns rc ON rc.owner = r.owner AND rc.constraint_name = r.constraint_name AND rc.position = cc.position "
        f"WHERE c.constraint_type = 'R' AND c.owner IN ({owners})"
    ):
        found[key(owner, table)].foreign_keys[str(name)].append((
            int(position or 0), _sqlalchemy_name(str(column)),
            _sqlalchemy_name(str(r_owner)), _sqlalchemy_name(str(r_table)), _sqlalchemy_name(str(r_column)),
        ))
    for owner, table, name, uniqueness, column, position in rows(
        "SELECT i.table_owner, i.table_name, i.index_name, i.uniqueness, ic.column_name, ic.column_position "
        "FROM all_indexes i JOIN all_ind_columns ic ON ic.index_owner = i.owner AND ic.index_name = i.index_name "
        f"WHERE i.table_owner IN ({owners}) AND i.index_type <> 'LOB'"
    ):
        parts = found[key(owner, table)]
        unique, columns = parts.indexes.get(str(name), (uniqueness == "UNIQUE", []))
        columns.append((int(position or 0), _sqlalchemy_name(str(column))))
        parts.indexes[str(name)] = (unique, columns)
    for (schema, _table), parts in found.items():
        for constraint in parts.foreign_keys.values():
            for index, row in enumerate(constraint):
                if row[2] == schema:
                    constraint[index] = (row[0], row[1], None, row[3], row[4])  # type: ignore[assignment]
    return found


# --- Trino ---------------------------------------------------------------------------------------


def _trino_queries(run: Runner, catalog: Optional[str], schemas: list[str], comments: bool) -> dict[tuple[str, str], _Parts]:
    """Trino has no keys or indexes to read - only columns and table comments. Its
    ``information_schema.columns`` has no column comments (only ``DESCRIBE``, per table, has them),
    so a Trino column's comment is only read per table."""
    names = _literals(schemas)
    quoted = '"' + catalog.replace('"', '""') + '"' if catalog else None
    info = f"{quoted}.information_schema.columns" if quoted else "information_schema.columns"
    found: dict[tuple[str, str], _Parts] = defaultdict(_Parts)

    def rows(sql: str) -> list[tuple[Any, ...]]:
        result, truncated = run(sql)
        if truncated:
            raise TooManyRows("information_schema")
        return result

    for schema, table, column, data_type, nullable, position in rows(
        "SELECT table_schema, table_name, column_name, data_type, is_nullable, ordinal_position "
        f"FROM {info} WHERE table_schema IN ({names})"
    ):
        parts = found[(str(schema), str(table))]
        parts.columns.append((int(position or 0), ColumnMeta(name=str(column), data_type=str(data_type), nullable=nullable != "NO")))
    if comments:
        where = f"schema_name IN ({names}) AND catalog_name = " + (_literals([catalog]) if catalog else "current_catalog")
        for schema, table, text in rows(f"SELECT schema_name, table_name, comment FROM system.metadata.table_comments WHERE {where}"):
            if text and str(text).strip():
                found[(str(schema), str(table))].comment = str(text).strip()
    return found


_QUERIES: dict[str, Callable[[Runner, Optional[str], list[str], bool], dict[tuple[str, str], _Parts]]] = {
    "oracle": _oracle_queries,
    "trino": _trino_queries,
}
