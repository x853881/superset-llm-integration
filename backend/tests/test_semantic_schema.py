from waxwing.nltosqlandback.semantic_schema import load_semantic_schema, semantic_schema_from_json


def test_the_bundled_schema_still_loads():
    schema = load_semantic_schema()
    assert schema.tables


def test_sql_helpers_multi_schema_shape_is_flattened_with_each_table_keeping_its_schema():
    raw = {
        "databaseName": "warehouse",
        "schemas": [
            {"schemaName": "sales", "description": "Orders", "tables": [{"name": "orders", "columns": []}]},
            {"schemaName": "hr", "tables": [{"name": "staff", "columns": []}]},
        ],
    }
    schema = semantic_schema_from_json(raw)
    assert [(table.schema, table.name) for table in schema.tables] == [("sales", "orders"), ("hr", "staff")]
    assert "TABLE sales.orders" in schema.to_prompt_text()
