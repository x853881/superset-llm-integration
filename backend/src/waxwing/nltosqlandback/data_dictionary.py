"""A site's own descriptions of its schemas, tables and columns - its data dictionary.

Some sites keep these in tables of their own: written by the people who own the data, so better
than anything a model could guess from the structure. Interrogate DB reads them (see
``db_interrogation``) and their descriptions go on the schemas, tables and columns it keeps, ahead
of the database's comments and anything the model wrote.

``NLSQL_DATA_DICTIONARY`` says where they are, a section per level. Every level is optional - a
site may only describe its schemas - and the dictionary can live in any of Superset's databases::

    NLSQL_DATA_DICTIONARY = {
        "database": "Warehouse",   # the Superset database it's in; default: the one being read
        "schemas": {"table": "META.SCHEMA_DESCRIPTIONS",
                    "columns": {"schema": "SCHEMA_NAME", "description": "DESCRIPTION"}},
        "tables": None,            # {"table": ..., "columns": {"schema", "table", "description"}}
        "columns": None,           # {"table": ..., "columns": {"schema", "table", "column", "description"}}
        "max_rows": 200_000,
    }

A level's ``columns`` maps each part it needs to that table's column. Two levels may name the same
table: it's read once, and a row says which level it belongs to by which names it has - one with a
column describes that column, one with a table but no column that table, one with neither its
schema. So a shared table that also holds column rows needs its column column named (a ``columns``
level), or those rows would read as describing their tables. A level whose table or columns aren't
there is reported in the result and skipped.

The older single-table settings are still read when ``NLSQL_DATA_DICTIONARY`` isn't set - one
table at every level, ``NLSQL_DATA_DICTIONARY_TABLE`` with ``NLSQL_DATA_DICTIONARY_COLUMNS`` and
``NLSQL_DATA_DICTIONARY_MAX_ROWS``.

Names are written as they'd be written in SQL Lab: plain lower-case ones are left unquoted (so
they match an upper-case Oracle name too), anything else is quoted. Matching a row to a table or
column ignores case, for the same reason.

Each table is read with one fixed ``SELECT`` of the configured names, through Superset's
``Database.execute`` and so with the user's own access to that database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from flask import current_app

from . import superset_db
from .superset_db import DatabaseInfo

DEFAULT_COLUMNS = {
    "schema": "schema_name",
    "table": "table_name",
    "column": "column_name",
    "description": "description",
}
DEFAULT_MAX_ROWS = 200_000
_ROLES = ("schema", "table", "column", "description")
LEVELS = ("schemas", "tables", "columns")
#: The parts of a row each level needs.
_LEVEL_ROLES = {
    "schemas": ("schema", "description"),
    "tables": ("schema", "table", "description"),
    "columns": ("schema", "table", "column", "description"),
}


@dataclass
class Source:
    """One dictionary table, and the levels it describes."""

    table: str
    levels: list[str]
    #: Role ("schema", "table", "column", "description") -> that table's column, for the roles read.
    columns: dict[str, str]

    @property
    def roles(self) -> list[str]:
        return [role for role in _ROLES if role in self.columns]


@dataclass
class Config:
    sources: list[Source]
    #: The Superset database the dictionary is in; None for the one being read.
    database: Optional[str] = None
    max_rows: int = DEFAULT_MAX_ROWS
    problems: list[str] = field(default_factory=list)

    @property
    def tables(self) -> list[str]:
        return [source.table for source in self.sources]


@dataclass
class DataDictionary:
    """What the dictionary says, looked up by lower-cased name."""

    #: Where it was read from, for prompts and the summary: its tables, and its database if not the
    #: one being read.
    table: str
    schemas: dict[str, str] = field(default_factory=dict)
    tables: dict[tuple[str, str], str] = field(default_factory=dict)
    columns: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def schema_description(self, schema: str) -> Optional[str]:
        return self.schemas.get(schema.lower())

    def table_description(self, schema: str, table: str) -> Optional[str]:
        return self.tables.get((schema.lower(), table.lower()))

    def column_description(self, schema: str, table: str, column: str) -> Optional[str]:
        return self.columns.get((schema.lower(), table.lower(), column.lower()))

    @property
    def size(self) -> int:
        return len(self.schemas) + len(self.tables) + len(self.columns)


def configured() -> Optional[Config]:
    """Where the dictionary is, from ``NLSQL_DATA_DICTIONARY`` or the older single-table settings;
    None when neither is set."""
    try:
        config = current_app.config
    except RuntimeError:
        return None
    settings = config.get("NLSQL_DATA_DICTIONARY")
    if settings:
        return _from_settings(dict(settings))
    table = str(config.get("NLSQL_DATA_DICTIONARY_TABLE") or "").strip()
    if not table:
        return None
    columns = {**DEFAULT_COLUMNS, **(config.get("NLSQL_DATA_DICTIONARY_COLUMNS") or {})}
    return Config(
        sources=[Source(table=table, levels=list(LEVELS), columns=columns)],
        max_rows=int(config.get("NLSQL_DATA_DICTIONARY_MAX_ROWS", DEFAULT_MAX_ROWS)),
    )


def _from_settings(settings: dict[str, Any]) -> Config:
    database = str(settings.get("database") or "").strip()
    config = Config(sources=[], database=database or None, max_rows=int(settings.get("max_rows") or DEFAULT_MAX_ROWS))
    by_table: dict[str, Source] = {}
    for level in LEVELS:
        section = settings.get(level)
        if not section:
            continue
        table = str(section.get("table") or "").strip() if isinstance(section, dict) else ""
        columns = dict(section.get("columns") or {}) if isinstance(section, dict) else {}
        missing = [role for role in _LEVEL_ROLES[level] if not columns.get(role)]
        if not table or missing:
            config.problems.append(
                f"The data dictionary's {level} level is skipped: it needs a table and "
                + ", ".join(f'"{role}"' for role in _LEVEL_ROLES[level]) + " columns"
                + (f" (missing {', '.join(missing)})" if table and missing else "") + "."
            )
            continue
        source = by_table.get(table.lower())
        if source is None:
            source = by_table[table.lower()] = Source(table=table, levels=[], columns={})
            config.sources.append(source)
        for role in _LEVEL_ROLES[level]:
            named = str(columns[role])
            if source.columns.setdefault(role, named) != named:
                config.problems.append(
                    f"The data dictionary's {level} level names {named} as {table}'s {role}, but another level "
                    f"names {source.columns[role]} - {source.columns[role]} is used."
                )
        source.levels.append(level)
    return config


def is_dictionary(configured_tables: Iterable[str] | str | None, schema: str, table: str) -> bool:
    """Whether `schema`.`table` is one of the dictionary's own tables - which describe the data, so
    aren't data to query. A configured name without a schema matches that table in any schema."""
    if not configured_tables:
        return False
    names = [configured_tables] if isinstance(configured_tables, str) else list(configured_tables)
    for configured_name in names:
        parts = [part.strip().strip('"`[]').lower() for part in configured_name.split(".")]
        if parts[-1] == table.lower() and (len(parts) == 1 or parts[-2] == schema.lower()):
            return True
    return False


def tables_in(config: Optional[Config], database: DatabaseInfo) -> list[str]:
    """The dictionary's tables that are in `database` - the ones to leave out when reading it."""
    if config is None or (config.database is not None and config.database != database.name):
        return []
    return config.tables


def read(database: DatabaseInfo, catalog: Optional[str]) -> tuple[Optional[DataDictionary], list[str]]:
    """The configured dictionary and any problems reading it. None when there's no dictionary
    configured, or none of it could be read."""
    config = configured()
    if config is None:
        return None, []
    problems = list(config.problems)
    if not config.sources:
        return None, problems
    source_database, source_catalog = database, catalog
    if config.database is not None and config.database != database.name:
        try:
            source_database = superset_db.resolve_database_by_name(config.database)
            source_catalog = None
        except superset_db.DatabaseUnavailable as ex:
            return None, problems + [f"The data dictionary's database, {config.database}, couldn't be used: {ex}"]
    where = f"{config.database}: " if source_database is not database else ""
    dictionary = DataDictionary(table=where + ", ".join(config.tables))
    read_any = False
    for source in config.sources:
        names = [source.columns[role] for role in source.roles]
        result = superset_db.read_dictionary(source_database, source_catalog, source.table, names, config.max_rows)
        if result.error:
            problems.append(
                f"The data dictionary ({source.table}, for {', '.join(source.levels)}) couldn't be read: {result.error[:300]}"
            )
            continue
        read_any = True
        if result.truncated:
            problems.append(
                f"The data dictionary ({source.table}) has more than {config.max_rows} rows - only the first "
                f"{config.max_rows} were read."
            )
        _add_rows(dictionary, source, result.rows)
    return (dictionary if read_any else None), problems


def from_rows(table: str, rows: list[tuple[Any, ...]]) -> DataDictionary:
    """A dictionary from (schema, table, column, description) rows, at every level."""
    dictionary = DataDictionary(table=table)
    _add_rows(dictionary, Source(table=table, levels=list(LEVELS), columns=dict(DEFAULT_COLUMNS)), rows)
    return dictionary


def _add_rows(dictionary: DataDictionary, source: Source, rows: list[tuple[Any, ...]]) -> None:
    """Rows with no description, or no schema, say nothing and are skipped, as is a row for a level
    this table isn't configured for; a later row for the same thing replaces an earlier."""
    roles = source.roles
    for row in rows:
        values = {role: _text(value) for role, value in zip(roles, row)}
        schema, table_name, column, description = (values.get(role) for role in _ROLES)
        if not schema or not description:
            continue
        if column and table_name:
            if "columns" in source.levels:
                dictionary.columns[(schema.lower(), table_name.lower(), column.lower())] = description
        elif table_name:
            if "tables" in source.levels:
                dictionary.tables[(schema.lower(), table_name.lower())] = description
        elif "schemas" in source.levels:
            dictionary.schemas[schema.lower()] = description


@dataclass
class Coverage:
    """How much of what was read the dictionary describes, and how many of its entries matched nothing."""

    schemas: int = 0
    schemas_described: int = 0
    tables: int = 0
    tables_described: int = 0
    columns: int = 0
    columns_described: int = 0
    unmatched: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "schemas": self.schemas, "schemasDescribed": self.schemas_described,
            "tables": self.tables, "tablesDescribed": self.tables_described,
            "columns": self.columns, "columnsDescribed": self.columns_described,
            "unmatched": self.unmatched,
        }

    def summary(self) -> str:
        return (
            f"describes {self.schemas_described} of {self.schemas} schemas, {self.tables_described} of {self.tables} "
            f"tables and {self.columns_described} of {self.columns} columns"
            + (f"; {self.unmatched} of its entries match nothing read" if self.unmatched else "")
        )


def coverage(dictionary: DataDictionary, read_tables: dict[str, list[tuple[str, list[str]]]]) -> Coverage:
    """`read_tables` is each schema read, with each of its tables and that table's columns."""
    out = Coverage(schemas=len(read_tables))
    seen_schemas = {schema.lower() for schema in read_tables}
    seen_tables: set[tuple[str, str]] = set()
    seen_columns: set[tuple[str, str, str]] = set()
    for schema, tables in read_tables.items():
        out.schemas_described += 1 if dictionary.schema_description(schema) else 0
        for table, columns in tables:
            seen_tables.add((schema.lower(), table.lower()))
            out.tables += 1
            out.tables_described += 1 if dictionary.table_description(schema, table) else 0
            for column in columns:
                seen_columns.add((schema.lower(), table.lower(), column.lower()))
                out.columns += 1
                out.columns_described += 1 if dictionary.column_description(schema, table, column) else 0
    out.unmatched = (
        sum(1 for schema in dictionary.schemas if schema not in seen_schemas)
        + sum(1 for key in dictionary.tables if key not in seen_tables)
        + sum(1 for key in dictionary.columns if key not in seen_columns)
    )
    return out


def _text(value: Any) -> Optional[str]:
    # A missing value comes back as None, or - through a DataFrame - as NaN.
    if value is None or (isinstance(value, float) and value != value):
        return None
    text = " ".join(str(value).split())
    return text or None
