"""What the extension knows about a database after interrogating it - kept, and given to prompts.

An interrogation (see ``db_interrogation``) reads the structure of every schema of a database
through Superset: tables and views, their columns and types, keys, indexes, and the planner's
estimate of each table's size. Optionally (``NLSQL_INTERROGATE_DESCRIPTIONS``) it also has the model
write a short description of each table and of the columns whose purpose isn't obvious from their
name - off by default, because across a database of many schemas that's a lot of model calls and a
lot of prompt. The result is a ``DatabaseDescription`` in sql-helper's multi-schema shape
(``{"databaseName", "schemas": [{"schemaName", "description", "tables": [...]}]}``), the same shape
``semantic_schema`` reads, wrapped in a record of when and how it was made.

**Where it's kept.** In Superset's own key-value store (the metadata database), keyed by database
and catalog - so every web worker sees the same one, and it survives a restart. Outside a Superset
(tests, tools) an in-process dictionary stands in. Each process also holds what it last read for a
minute, so a prompt doesn't cost a metadata-database round trip.

**How prompts use it.** Whole: every table the user may see, in every schema, is put in the prompt
- no embedding search. Without descriptions a table is a line or three (names, types, keys,
indexes, size), so a few hundred tables fit; past ``NLSQL_SCHEMA_MAX_CHARS`` the column types go,
and past that the tables sharing the fewest words with the request are left out (and the prompt
says so).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .semantic_schema import (
    ColumnDescription,
    RelationshipDescription,
    TableDescription,
    semantic_schema_from_json,
)

logger = logging.getLogger(__name__)

FORMAT_VERSION = 2
_NAMESPACE = uuid.UUID("6f1d3b8e-2a57-4c1e-9d0b-7a4e5c3f2b10")
#: How long a process trusts what it last read from the store before reading it again.
_MEMORY_TTL_SECONDS = 60.0
DEFAULT_MAX_PROMPT_CHARS = 120_000

SchemaTable = tuple[str, str]  # (schema, table)


@dataclass
class DatabaseKnowledge:
    """One interrogated database (or one catalog of it)."""

    database_id: int
    database_name: str
    catalog: Optional[str]
    tables: list[TableDescription]
    #: The schemas read, including any found empty.
    schemas: list[str] = field(default_factory=list)
    #: What the model made of each schema as a whole - only when descriptions were asked for.
    schema_descriptions: dict[str, str] = field(default_factory=dict)
    #: Whether the model described the tables, or this is the structure alone.
    described: bool = False
    created_at: float = field(default_factory=time.time)
    created_by: Optional[str] = None
    #: The model that wrote the descriptions, if any did.
    model: Optional[str] = None
    dialect: Optional[str] = None
    #: How many of the database's schemas were left out as the database's own (system schemas).
    skipped_schemas: int = 0
    #: Tables that couldn't be read, sized or described, and why.
    problems: list[str] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def table_keys(self) -> list[SchemaTable]:
        return [(table.schema, table.name) for table in self.tables]

    def visible_tables(self, visible: Optional[set[SchemaTable]] = None) -> list[TableDescription]:
        return [table for table in self.tables if visible is None or (table.schema, table.name) in visible]

    def to_json(self) -> dict[str, Any]:
        by_schema: dict[str, list[TableDescription]] = {schema: [] for schema in self.schemas}
        for table in self.tables:
            by_schema.setdefault(table.schema, []).append(table)
        return {
            "version": FORMAT_VERSION,
            "databaseId": self.database_id,
            "catalog": self.catalog,
            "described": self.described,
            "createdAt": self.created_at,
            "createdBy": self.created_by,
            "model": self.model,
            "dialect": self.dialect,
            "skippedSchemas": self.skipped_schemas,
            "problems": self.problems,
            "durationMs": self.duration_ms,
            "description": {
                "databaseName": self.database_name,
                "schemas": [
                    {
                        "schemaName": schema,
                        "description": self.schema_descriptions.get(schema),
                        "tables": [table_to_json(table) for table in tables],
                    }
                    for schema, tables in by_schema.items()
                ],
            },
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "DatabaseKnowledge":
        if int(raw.get("version") or 1) != FORMAT_VERSION:
            raise ValueError(f"format version {raw.get('version')}, not {FORMAT_VERSION}")
        description = raw["description"]
        schemas = description.get("schemas") or []
        parsed = semantic_schema_from_json(description)
        return cls(
            database_id=int(raw["databaseId"]),
            database_name=description.get("databaseName") or "",
            catalog=raw.get("catalog"),
            tables=parsed.tables,
            schemas=[str(schema.get("schemaName") or "") for schema in schemas],
            schema_descriptions={
                str(schema.get("schemaName")): str(schema["description"]) for schema in schemas if schema.get("description")
            },
            described=bool(raw.get("described")),
            created_at=float(raw.get("createdAt") or 0),
            created_by=raw.get("createdBy"),
            model=raw.get("model"),
            dialect=raw.get("dialect"),
            skipped_schemas=int(raw.get("skippedSchemas") or 0),
            problems=list(raw.get("problems") or []),
            duration_ms=int(raw.get("durationMs") or 0),
        )

    def summary(self, visible: Optional[set[SchemaTable]] = None) -> dict[str, Any]:
        """What the panel shows about it: when, by whom, how much - without the tables."""
        tables = self.visible_tables(visible)
        schemas: dict[str, int] = {}
        for table in tables:
            schemas[table.schema] = schemas.get(table.schema, 0) + 1
        return {
            "databaseId": self.database_id,
            "databaseName": self.database_name,
            "catalog": self.catalog,
            "schemas": [{"name": name, "tableCount": count} for name, count in sorted(schemas.items())],
            "schemaCount": len(schemas),
            "skippedSchemas": self.skipped_schemas,
            "tableCount": sum(1 for table in tables if table.kind != "view"),
            "viewCount": sum(1 for table in tables if table.kind == "view"),
            "columnCount": sum(len(table.columns) for table in tables),
            "indexCount": sum(len(table.indexes) for table in tables),
            "sizedCount": sum(1 for table in tables if table.row_count_estimate is not None),
            "relationshipCount": sum(len(table.relationships) for table in tables),
            "described": self.described,
            "describedCount": sum(1 for table in tables if table.description),
            "createdAt": self.created_at,
            "createdBy": self.created_by,
            "model": self.model,
            "problems": self.problems,
            "durationMs": self.duration_ms,
        }


def table_to_json(table: TableDescription) -> dict[str, Any]:
    """A table in the semantic-schema JSON shape (camelCase, empty parts left out)."""
    out: dict[str, Any] = {"name": table.name, "schema": table.schema}
    if table.kind != "table":
        out["kind"] = table.kind
    if table.description:
        out["description"] = table.description
    if table.synonyms:
        out["synonyms"] = table.synonyms
    if table.row_count_estimate is not None:
        out["rowCountEstimate"] = table.row_count_estimate
    if table.usage_notes:
        out["usageNotes"] = table.usage_notes
    out["columns"] = [column_to_json(column) for column in table.columns]
    if table.indexes:
        out["indexes"] = [{"columns": list(columns), "unique": unique} for columns, unique in table.indexes]
    if table.relationships:
        out["relationships"] = [
            {
                "description": relationship.description,
                "fromColumn": relationship.from_column,
                "toTable": relationship.to_table,
                "toColumn": relationship.to_column,
                "cardinality": relationship.cardinality,
            }
            for relationship in table.relationships
        ]
    return out


def column_to_json(column: ColumnDescription) -> dict[str, Any]:
    out: dict[str, Any] = {"name": column.name, "dataType": column.data_type, "nullable": column.nullable}
    if column.description:
        out["description"] = column.description
    if column.synonyms:
        out["synonyms"] = column.synonyms
    if column.enum_values is not None:
        out["enumValues"] = column.enum_values
    if column.is_primary_key:
        out["isPrimaryKey"] = True
    if column.is_foreign_key:
        out["isForeignKey"] = True
    return out


# --- The store ----------------------------------------------------------------------------------


def _key(database_id: int, catalog: Optional[str]) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, f"nltosqlandback:database:{database_id}:{catalog or ''}")


class _Store:
    """Superset's key-value store when there is one, a dictionary when there isn't; either way with
    a short-lived per-process copy in front."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._memory: dict[uuid.UUID, tuple[float, Optional[dict[str, Any]]]] = {}
        #: Stands in for Superset's store outside a Superset.
        self._fallback: dict[uuid.UUID, dict[str, Any]] = {}

    def get(self, database_id: int, catalog: Optional[str]) -> Optional[DatabaseKnowledge]:
        key = _key(database_id, catalog)
        with self._lock:
            cached = self._memory.get(key)
        if cached is not None and time.monotonic() - cached[0] < _MEMORY_TTL_SECONDS:
            raw = cached[1]
        else:
            raw = self._read(key)
            with self._lock:
                self._memory[key] = (time.monotonic(), raw)
        if raw is None:
            return None
        try:
            return DatabaseKnowledge.from_json(raw)
        except Exception as ex:  # noqa: BLE001 - a record from a future/older version
            logger.warning("unreadable database description %s: %s", key, ex)
            return None

    def put(self, knowledge: DatabaseKnowledge) -> None:
        key = _key(knowledge.database_id, knowledge.catalog)
        raw = knowledge.to_json()
        self._write(key, raw)
        with self._lock:
            self._memory[key] = (time.monotonic(), raw)

    def delete(self, database_id: int, catalog: Optional[str]) -> bool:
        key = _key(database_id, catalog)
        existed = self._remove(key)
        with self._lock:
            self._memory[key] = (time.monotonic(), None)
        return existed

    def clear_memory(self) -> None:
        with self._lock:
            self._memory.clear()
            self._fallback.clear()

    # Superset's key-value store, imported lazily: it doesn't exist in tests and tools.
    def _superset(self):  # type: ignore[no-untyped-def]
        try:
            from superset.daos.key_value import KeyValueDAO
            from superset.extensions import db
            from superset.key_value.types import JsonKeyValueCodec, KeyValueResource
        except Exception:  # noqa: BLE001
            return None
        return KeyValueDAO, db, JsonKeyValueCodec(), KeyValueResource.APP

    def _read(self, key: uuid.UUID) -> Optional[dict[str, Any]]:
        superset = self._superset()
        if superset is None:
            return self._fallback.get(key)
        dao, _db, codec, resource = superset
        try:
            return dao.get_value(resource, key, codec)
        except Exception as ex:  # noqa: BLE001 - e.g. no app context
            logger.warning("could not read a schema description: %s", ex)
            return self._fallback.get(key)

    def _write(self, key: uuid.UUID, raw: dict[str, Any]) -> None:
        superset = self._superset()
        if superset is None:
            self._fallback[key] = raw
            return
        dao, db, codec, resource = superset
        try:
            dao.upsert_entry(resource, raw, codec, key)
            db.session.commit()
        except Exception as ex:  # noqa: BLE001 - keep it for this process at least
            logger.warning("could not save the schema description: %s", ex)
            try:
                db.session.rollback()
            except Exception:  # noqa: BLE001
                pass
            self._fallback[key] = raw

    def _remove(self, key: uuid.UUID) -> bool:
        existed = self._fallback.pop(key, None) is not None
        superset = self._superset()
        if superset is None:
            return existed
        dao, db, _codec, resource = superset
        try:
            removed = dao.delete_entry(resource, key)
            db.session.commit()
            return bool(removed) or existed
        except Exception as ex:  # noqa: BLE001
            logger.warning("could not delete the schema description: %s", ex)
            return existed


store = _Store()


# --- Prompt text --------------------------------------------------------------------------------


def prompt_text(
    knowledge: DatabaseKnowledge,
    query_text: str,
    visible: Optional[set[SchemaTable]] = None,
    max_chars: int = DEFAULT_MAX_PROMPT_CHARS,
    only_used: bool = False,
) -> tuple[str, int, int]:
    """The schema section of a prompt: every table (of those in `visible`, when given) of every
    schema - with descriptions when the interrogation wrote some, else their structure; if that's
    longer than `max_chars`, with the column types and indexes dropped; if that's still too long,
    the tables sharing the most words with `query_text` that fit. Returns the text, how many tables
    it covers and how many were left out. `only_used` says `visible` is the tables a query reads
    rather than all the user may see, so the header doesn't present them as the whole database."""
    tables = knowledge.visible_tables(visible)
    header = _header(knowledge, tables, only_used)
    tiers = ([_described_text] if knowledge.described else []) + [_structure_text]
    for render in tiers:
        texts = [render(table) for table in tables]
        if sum(len(text) + 1 for text in texts) <= max_chars:
            return f"{header}\n\n" + "\n".join(texts), len(tables), 0
    names = [_names_text(table) for table in tables]
    if sum(len(text) + 1 for text in names) <= max_chars:
        return f"{header}\n(Column types and indexes left out for space.)\n\n" + "\n".join(names), len(tables), 0
    words = _words(query_text)
    ranked = sorted(range(len(tables)), key=lambda index: -len(words & _words(names[index])))
    kept: list[int] = []
    size = 0
    for index in ranked:
        if size + len(names[index]) + 1 > max_chars:
            continue
        kept.append(index)
        size += len(names[index]) + 1
    kept.sort()
    left_out = len(tables) - len(kept)
    note = f"\n\n({left_out} more tables are not listed here - too many to fit.)" if left_out else ""
    return (
        f"{header}\n(Column types and indexes left out for space.)\n\n" + "\n".join(names[index] for index in kept) + note,
        len(kept),
        left_out,
    )


def _header(knowledge: DatabaseKnowledge, tables: list[TableDescription], only_used: bool = False) -> str:
    schemas = sorted({table.schema for table in tables})
    listed = f": {', '.join(schemas)}" if len(schemas) <= 30 else ""
    lines = [
        f"Database {knowledge.database_name}: the {len(tables)} tables and views this query reads."
        if only_used
        else f"Database {knowledge.database_name}: {len(tables)} tables and views in {len(schemas)} "
        f"schema{'s' if len(schemas) != 1 else ''}{listed}.",
        "Every table is listed as schema.table - always qualify table names with their schema.",
        "Read from the database's catalog: each column's name and type; PK = primary key; ->t.c = foreign key; "
        "idx = the table's indexes (filtering or joining on an indexed column is cheap); "
        "≈N rows = the planner's estimate of its size.",
    ]
    for schema, description in sorted(knowledge.schema_descriptions.items()):
        if schema in schemas:
            lines.append(f"Schema {schema}: {description}")
    if knowledge.described:
        lines.append(
            "(Descriptions were written from the structure by a model that never saw the data: treat them as likely, not certain.)"
        )
    if any(_is_lob(column.data_type) for table in tables for column in table.columns):
        lines.append(
            "Columns marked [LOB] are large objects: they can't be used in GROUP BY, ORDER BY, DISTINCT or "
            "comparisons (in Oracle, use DBMS_LOB.SUBSTR(col, 4000, 1) or TO_CHAR(col) to get text out of one)."
        )
    return "\n".join(lines)


def _title(table: TableDescription) -> str:
    title = f"{table.schema}.{table.name}"
    if table.kind == "view":
        title += " (view)"
    if table.row_count_estimate is not None:
        title += f" ≈{_compact_number(table.row_count_estimate)} rows"
    return title


def _column_text(column: ColumnDescription, keys: dict[str, str]) -> str:
    text = f"{column.name} {column.data_type}"
    if column.is_primary_key:
        text += " PK"
    if column.name in keys:
        text += f" ->{keys[column.name]}"
    if column.enum_values:
        text += " {" + "|".join(column.enum_values[:12]) + ("|…" if len(column.enum_values) > 12 else "") + "}"
    if _is_lob(column.data_type):
        text += " [LOB]"
    return text


def _keys(table: TableDescription) -> dict[str, str]:
    return {relationship.from_column: f"{relationship.to_table}.{relationship.to_column}" for relationship in table.relationships}


def _index_text(table: TableDescription) -> Optional[str]:
    primary = tuple(column.name for column in table.columns if column.is_primary_key)
    shown = [
        f"{'unique' if unique else ''}({', '.join(columns)})"
        for columns, unique in table.indexes
        if tuple(columns) != primary
    ]
    return f"idx: {'; '.join(shown)}" if shown else None


def _structure_text(table: TableDescription) -> str:
    keys = _keys(table)
    lines = [_title(table), "  " + ", ".join(_column_text(column, keys) for column in table.columns)]
    indexes = _index_text(table)
    if indexes:
        lines.append(f"  {indexes}")
    if table.usage_notes:
        lines.append(f"  Note: {table.usage_notes}")
    return "\n".join(lines)


def _described_text(table: TableDescription) -> str:
    header = _title(table)
    if table.synonyms:
        header += f" (aka {', '.join(table.synonyms)})"
    if table.description:
        header += f" -- {table.description}"
    keys = _keys(table)
    lines = [header]
    for column in table.columns:
        text = _column_text(column, keys)
        if column.description:
            text += f" -- {column.description}"
        lines.append(f"  {text}")
    indexes = _index_text(table)
    if indexes:
        lines.append(f"  {indexes}")
    joins = [relationship.description for relationship in table.relationships if "likely" in relationship.description]
    if joins:
        lines.append("  Likely joins: " + "; ".join(joins))
    if table.usage_notes:
        lines.append(f"  Note: {table.usage_notes}")
    return "\n".join(lines)


def _names_text(table: TableDescription) -> str:
    keys = _keys(table)
    columns = ", ".join(
        column.name + (f"->{keys[column.name]}" if column.name in keys else "") + (" [LOB]" if _is_lob(column.data_type) else "")
        for column in table.columns
    )
    note = f" (Note: {table.usage_notes})" if table.usage_notes else ""
    return f"{_title(table)}: {columns}{note}"


_LOB_TYPES = ("CLOB", "NCLOB", "BLOB", "LONG", "NTEXT", "IMAGE")


def _is_lob(data_type: str) -> bool:
    """Large-object columns can't be grouped, sorted, compared or made DISTINCT in Oracle (and
    similar engines) - a model writing GROUP BY on one is the most common way its SQL fails there."""
    return data_type.upper().split("(")[0].strip() in _LOB_TYPES


def _compact_number(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def _words(text: str) -> set[str]:
    """The words of `text` worth matching on: whole identifiers ("order_items") and their parts
    ("order", "items", "item"), ignoring very short ones."""
    words = set()
    for token in re.findall(r"[a-z0-9_]+", text.lower()):
        for word in {token, *token.split("_")}:
            if len(word) > 2 or (word == token and len(word) > 1):
                words.add(word)
                if word.endswith("s"):
                    words.add(word[:-1])
    return words


def relationship(from_column: str, to_table: str, to_column: str, declared: bool, note: Optional[str] = None) -> RelationshipDescription:
    """A join path, described the way prompts read it. `to_table` is schema-qualified."""
    text = f"{from_column} -> {to_table}.{to_column}"
    text += " (foreign key)" if declared else " (likely, not declared)"
    if note:
        text += f": {note}"
    return RelationshipDescription(description=text, from_column=from_column, to_table=to_table, to_column=to_column)


def dumps(knowledge: DatabaseKnowledge) -> str:
    return json.dumps(knowledge.to_json())
