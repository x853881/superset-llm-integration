"""Loads the semantic-schema JSON bundled as this extension's own resource
(``resources/semantic_schema.json``, embedded as a Python literal in
``resources/semantic_schema_data.py`` - see that file for why) and renders it
as compact prompt text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .resources.semantic_schema_data import SEMANTIC_SCHEMA_JSON


@dataclass(frozen=True)
class ColumnDescription:
    name: str
    data_type: str
    description: Optional[str] = None
    synonyms: list[str] = field(default_factory=list)
    nullable: bool = True
    role: str = "UNKNOWN"
    unit: Optional[str] = None
    sample_values: list[str] = field(default_factory=list)
    enum_values: Optional[list[str]] = None
    is_primary_key: bool = False
    is_foreign_key: bool = False

    def to_prompt_text(self) -> str:
        text = f"{self.name} {self.data_type}"
        if not self.nullable:
            text += " NOT NULL"
        if self.is_primary_key:
            text += " PK"
        if self.is_foreign_key:
            text += " FK"
        if self.description:
            text += f" -- {self.description}"
        if self.synonyms:
            text += f" (aka {', '.join(self.synonyms)})"
        if self.enum_values is not None:
            text += f" values: {', '.join(self.enum_values)}"
        elif self.sample_values:
            text += f" e.g. {', '.join(self.sample_values)}"
        if self.unit:
            text += f" [{self.unit}]"
        return text


@dataclass(frozen=True)
class RelationshipDescription:
    description: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: str = "MANY_TO_ONE"


@dataclass(frozen=True)
class TableDescription:
    schema: str
    name: str
    columns: list[ColumnDescription]
    description: Optional[str] = None
    synonyms: list[str] = field(default_factory=list)
    relationships: list[RelationshipDescription] = field(default_factory=list)
    row_count_estimate: Optional[int] = None
    usage_notes: Optional[str] = None
    #: "table" or "view" - set by Interrogate DB, which reads it from the database.
    kind: str = "table"
    #: Each index's columns, and whether it is unique - set by Interrogate DB.
    indexes: list[tuple[tuple[str, ...], bool]] = field(default_factory=list)

    def to_prompt_text(self) -> str:
        header = f"TABLE {self.schema}.{self.name}"
        if self.synonyms:
            header += f" (aka {', '.join(self.synonyms)})"
        if self.description:
            header += f" -- {self.description}"
        lines = [header]
        for column in self.columns:
            lines.append(f"  {column.to_prompt_text()}")
        if self.relationships:
            lines.append("  Relationships:")
            for relationship in self.relationships:
                lines.append(f"    - {relationship.description}")
        if self.usage_notes:
            lines.append(f"  Note: {self.usage_notes}")
        return "\n".join(lines)


@dataclass(frozen=True)
class SemanticSchema:
    database_name: str
    schema_name: str
    tables: list[TableDescription]

    def to_prompt_text(self) -> str:
        return "\n\n".join(table.to_prompt_text() for table in self.tables)


def _column_from_json(raw: dict[str, Any]) -> ColumnDescription:
    return ColumnDescription(
        name=raw["name"],
        data_type=raw["dataType"],
        description=raw.get("description"),
        synonyms=raw.get("synonyms") or [],
        nullable=raw.get("nullable", True),
        role=raw.get("role", "UNKNOWN"),
        unit=raw.get("unit"),
        sample_values=raw.get("sampleValues") or [],
        enum_values=raw.get("enumValues"),
        is_primary_key=raw.get("isPrimaryKey", False),
        is_foreign_key=raw.get("isForeignKey", False),
    )


def _relationship_from_json(raw: dict[str, Any]) -> RelationshipDescription:
    return RelationshipDescription(
        description=raw["description"],
        from_column=raw["fromColumn"],
        to_table=raw["toTable"],
        to_column=raw["toColumn"],
        cardinality=raw.get("cardinality", "MANY_TO_ONE"),
    )


def _table_from_json(raw: dict[str, Any], schema_name: Optional[str] = None) -> TableDescription:
    return TableDescription(
        schema=raw.get("schema") or schema_name or "",
        name=raw["name"],
        columns=[_column_from_json(c) for c in raw.get("columns", [])],
        description=raw.get("description"),
        synonyms=raw.get("synonyms") or [],
        relationships=[_relationship_from_json(r) for r in raw.get("relationships", [])],
        row_count_estimate=raw.get("rowCountEstimate"),
        usage_notes=raw.get("usageNotes"),
        kind=raw.get("kind") or "table",
        indexes=[
            (tuple(str(column) for column in index.get("columns") or []), bool(index.get("unique")))
            for index in raw.get("indexes") or []
        ],
    )


def load_semantic_schema() -> SemanticSchema:
    return semantic_schema_from_json(json.loads(SEMANTIC_SCHEMA_JSON))


def semantic_schema_from_json(raw: dict[str, Any]) -> SemanticSchema:
    """Reads either shape of semantic schema file: the original single-schema one
    (``{"databaseName", "schemaName", "tables": [...]}``), or sql-helper's multi-schema
    ``DatabaseDescription`` (``{"databaseName", "schemas": [{"schemaName", "description",
    "tables": [...]}]}``). Every table carries its own schema name either way, which is all the
    schema filter and the prompt text need, so the second is simply flattened into the first."""
    if "schemas" in raw:
        tables = [
            _table_from_json(table, schema.get("schemaName"))
            for schema in raw.get("schemas") or []
            for table in schema.get("tables") or []
        ]
        names = sorted({table.schema for table in tables})
        return SemanticSchema(database_name=raw["databaseName"], schema_name=", ".join(names), tables=tables)
    return SemanticSchema(
        database_name=raw["databaseName"],
        schema_name=raw["schemaName"],
        tables=[_table_from_json(t, raw.get("schemaName")) for t in raw.get("tables", [])],
    )
