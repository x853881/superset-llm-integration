"""The test tables, as the metrics and the judge see them.

`load_schema_metadata()` reads schema_metadata.json (or SCHEMA_METADATA_FILE),
a JSON list of objects with the TableMetadata fields; generate_schema_metadata.py
drafts it from the database. The judge is shown the notes on the tables
involved (judge/context.py), so it can grade table choice and index usage
against facts about the warehouse rather than guessing.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Optional, Set, Tuple

DEFAULT_METADATA_FILE = Path(__file__).resolve().parents[1] / "schema_metadata.json"
SCHEMA_NAME = os.getenv("SUPERSET_SCHEMA", "YOUR_SCHEMA_NAME")
SQL_DIALECT = os.getenv("SQL_DIALECT", "postgres")
# Low enough that ACLED's global_events (~1M rows) counts as large.
LARGE_TABLE_ROWS = int(os.getenv("LARGE_TABLE_ROWS", "500000"))


@dataclass(frozen=True)
class IndexHint:
    name: str
    columns: Tuple[str, ...]
    kind: str = "btree"  # btree | brin | hash | sort_key | cluster_key
    note: str = ""


@dataclass(frozen=True)
class TableMetadata:
    name: str
    description: str
    approx_row_count: int
    columns: Mapping[str, str]
    schema: str = SCHEMA_NAME
    indexes: Tuple[IndexHint, ...] = ()
    partition_column: Optional[str] = None
    # Set on pre-aggregated tables: the raw table this one summarises.
    rollup_of: Optional[str] = None
    # Lowercase keywords of questions this table is the right source for.
    preferred_for: Tuple[str, ...] = ()
    sensitive_columns: Tuple[str, ...] = ()
    # Columns declared NOT NULL; any other column may hold NULLs.
    not_null: Tuple[str, ...] = ()

    @property
    def fqn(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def is_large(self) -> bool:
        return self.approx_row_count >= LARGE_TABLE_ROWS

    @property
    def indexed_columns(self) -> Set[str]:
        """Columns a filter can use to avoid a full scan: the leading column of
        each index plus the partition column."""
        cols = {i.columns[0].lower() for i in self.indexes if i.columns}
        if self.partition_column:
            cols.add(self.partition_column.lower())
        return cols

    def to_context(self) -> str:
        not_null = {c.lower() for c in self.not_null}
        lines = [
            f"Table {self.fqn} (~{self.approx_row_count:,} rows"
            f"{', LARGE' if self.is_large else ''}): {self.description}",
            "  Columns: "
            + ", ".join(
                f"{c} {t}{' NOT NULL' if c.lower() in not_null else ''}"
                for c, t in self.columns.items()
            ),
        ]
        if self.partition_column:
            lines.append(
                f"  Partitioned by {self.partition_column}; always filter on it "
                "to prune partitions."
            )
        for idx in self.indexes:
            note = f" ({idx.note})" if idx.note else ""
            lines.append(
                f"  Index {idx.name}: {idx.kind} on ({', '.join(idx.columns)}){note}"
            )
        if self.rollup_of:
            lines.append(
                f"  Pre-aggregated rollup of {self.rollup_of}; prefer it over the raw "
                "table whenever its grain can answer the question."
            )
        if self.preferred_for:
            lines.append(
                "  Preferred for questions about: "
                + ", ".join(self.preferred_for)
            )
        if self.sensitive_columns:
            lines.append(
                "  Sensitive columns, never select or filter on: "
                + ", ".join(self.sensitive_columns)
            )
        return "\n".join(lines)


def load_schema_metadata() -> Dict[str, TableMetadata]:
    """Tables keyed by lowercase name, from SCHEMA_METADATA_FILE if set, else
    schema_metadata.json in evals/."""
    path = Path(os.getenv("SCHEMA_METADATA_FILE") or DEFAULT_METADATA_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"No schema metadata: {path} not found. Draft it from the database "
            "with python evals/generate_schema_metadata.py, or set "
            "SCHEMA_METADATA_FILE to one."
        )
    with open(path) as f:
        raw = json.load(f)
    tables = tuple(
        TableMetadata(
            **{
                **t,
                "indexes": tuple(
                    IndexHint(**{**i, "columns": tuple(i["columns"])})
                    for i in t.get("indexes", ())
                ),
                "preferred_for": tuple(t.get("preferred_for", ())),
                "sensitive_columns": tuple(t.get("sensitive_columns", ())),
                "not_null": tuple(t.get("not_null", ())),
            }
        )
        for t in raw
    )
    return {t.name.lower(): t for t in tables}
