"""Draft schema_metadata.json from the live database, through SQL Lab.

Reads every table and view in SUPERSET_SCHEMA: columns, their types and which
are NOT NULL, approximate row count and indexes. No columns are marked
sensitive: users may view any data their account allows. To have the suite
fail SQL that reads a column, list it in that table's sensitive_columns by
hand. Descriptions and preferred_for are left for you to fill in, since only
you know what each table is for.

    python evals/generate_schema_metadata.py            # all tables
    python evals/generate_schema_metadata.py users flights

Updates evals/schema_metadata.json, which the suite's metrics and the
judge read. Only the tables it reads are replaced:
other schemas' tables stay, and what you've written by hand (a table's
description, preferred_for, sensitive_columns, rollup_of and
partition_column) is kept, so run it once per schema (SUPERSET_SCHEMA) to
describe tables in several.
"""

import json
import sys
from pathlib import Path

from plugin.client import SupersetClient
from suite.config import SuiteConfig

OUTPUT = Path(__file__).parent / "schema_metadata.json"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def main(only: list) -> None:
    config = SuiteConfig.from_env()
    client = SupersetClient(config)
    schema = _quote(config.schema)

    tables = client.execute_sql(
        "SELECT c.relname AS name, c.reltuples::bigint AS approx_rows "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {schema} AND c.relkind IN ('r', 'p', 'v', 'm') "
        "ORDER BY 1"
    )
    # information_schema leaves out materialized views, so theirs come from
    # the catalog.
    columns = client.execute_sql(
        "SELECT table_name, column_name, data_type, is_nullable FROM ("
        "SELECT table_name::text, column_name::text, data_type::text, "
        "is_nullable::text, ordinal_position::int AS n "
        "FROM information_schema.columns "
        f"WHERE table_schema = {schema} "
        "UNION ALL "
        "SELECT c.relname, a.attname, format_type(a.atttypid, NULL), "
        "CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END, a.attnum "
        "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {schema} AND c.relkind = 'm' "
        "AND a.attnum > 0 AND NOT a.attisdropped"
        ") cols ORDER BY table_name, n"
    )
    # One row per key column of each index, in order. Given a column number,
    # pg_get_indexdef returns just that column or expression: no DESC,
    # operator class or INCLUDE columns to parse out. A partial index's
    # condition comes separately.
    indexes = client.execute_sql(
        "SELECT t.relname AS tablename, i.relname AS indexname, am.amname AS kind, "
        "pg_get_expr(x.indpred, x.indrelid) AS condition, "
        "pg_get_indexdef(x.indexrelid, k.n, true) AS key "
        "FROM pg_index x "
        "JOIN pg_class i ON i.oid = x.indexrelid "
        "JOIN pg_class t ON t.oid = x.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN pg_am am ON am.oid = i.relam "
        "CROSS JOIN LATERAL generate_series(1, x.indnkeyatts) AS k(n) "
        f"WHERE n.nspname = {schema} ORDER BY t.relname, x.indexrelid, k.n"
    )

    wanted = {t.lower() for t in only}
    existing = json.loads(OUTPUT.read_text()) if OUTPUT.exists() else []
    written = {(t.get("schema"), t["name"]): t for t in existing}
    out = []
    for name, approx_rows in tables.itertuples(index=False):
        if wanted and name.lower() not in wanted:
            continue
        if approx_rows is None or approx_rows < 0:  # never analysed
            quoted = '"' + name.replace('"', '""') + '"'
            approx_rows = int(
                client.execute_sql(
                    f'SELECT COUNT(*) AS n FROM "{config.schema}".{quoted}'
                ).iloc[0, 0]
            )
        cols = columns[columns.table_name == name]
        by_name = {}
        for _, idx_name, kind, condition, key in indexes[
            indexes.tablename == name
        ].itertuples(index=False):
            index = by_name.setdefault(
                idx_name, {"name": idx_name, "kind": kind, "columns": []}
            )
            index["columns"].append(key.strip('"'))
            if isinstance(condition, str) and condition:
                index["note"] = f"partial: only rows where {condition}"
        table_indexes = list(by_name.values())
        before = written.get((config.schema, name), {})
        out.append(
            {
                "name": name,
                "description": before.get("description", ""),
                "approx_row_count": int(approx_rows),
                "columns": dict(zip(cols.column_name, cols.data_type)),
                "not_null": [
                    c for c, nullable in zip(cols.column_name, cols.is_nullable)
                    if nullable == "NO"
                ],
                "schema": config.schema,
                "indexes": table_indexes,
                "preferred_for": before.get("preferred_for", []),
                "sensitive_columns": before.get("sensitive_columns", []),
                **{
                    k: before[k]
                    for k in ("rollup_of", "partition_column")
                    if before.get(k)
                },
            }
        )
        print(
            f"{name}: {len(cols)} columns, ~{int(approx_rows):,} rows, {len(table_indexes)} indexes"
        )

    # Keep every table this run didn't read: other schemas, and the rest of
    # this one when only some tables were asked for.
    kept = [
        t
        for t in existing
        if t.get("schema") != config.schema
        or (wanted and t["name"].lower() not in wanted)
    ]
    OUTPUT.write_text(json.dumps(kept + out, indent=2) + "\n")
    print(f"\nWrote {len(out)} tables to {OUTPUT} (kept {len(kept)} others)")


if __name__ == "__main__":
    main(sys.argv[1:])
