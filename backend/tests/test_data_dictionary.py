"""The database's own data dictionary: reading it through Superset, and what it says."""

from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from superset_core.queries.types import QueryStatus

from waxwing.nltosqlandback import data_dictionary, superset_db, telemetry
from waxwing.nltosqlandback.superset_db import DatabaseInfo


class Rows:
    """Enough of a DataFrame: `.values.tolist()`."""

    def __init__(self, rows):
        self.values = SimpleNamespace(tolist=lambda: [list(row) for row in rows])


class DictionaryModel:
    def __init__(self, rows, status=QueryStatus.SUCCESS, error=None):
        self.rows = rows
        self.status = status
        self.error = error
        self.executed = []

    def execute(self, sql, options):
        self.executed.append((sql, options))
        statement = SimpleNamespace(data=Rows(self.rows[: options.limit]))
        return SimpleNamespace(status=self.status, error_message=self.error, statements=[statement])


ROWS = [
    ("sales", None, None, "Everything sold, by the shop and online."),
    ("sales", "orders", None, "One row per order placed."),
    ("sales", "orders", "amt", "Order value in pounds, after discounts."),
    ("SALES", "ORDERS", "CUST_ID", "The customer who placed it."),
    ("sales", "orders", "status", None),
    (None, "orders", "id", "No schema, so it says nothing."),
    ("sales", "orders", "notes", float("nan")),
]


def _database(model, dialect="postgres"):
    return DatabaseInfo(id=7, name="shop", backend="postgresql", dialect=dialect, model=model)


def _app(**config):
    app = Flask(__name__)
    app.config.update(config)
    return app


def test_rows_say_what_they_describe_by_which_names_they_have():
    dictionary = data_dictionary.from_rows("metadata.data_dictionary", ROWS)
    assert dictionary.schema_description("sales") == "Everything sold, by the shop and online."
    assert dictionary.table_description("Sales", "Orders") == "One row per order placed."
    assert dictionary.column_description("sales", "orders", "AMT") == "Order value in pounds, after discounts."
    assert dictionary.column_description("sales", "orders", "cust_id") == "The customer who placed it."
    assert dictionary.column_description("sales", "orders", "status") is None
    assert dictionary.column_description("sales", "orders", "notes") is None
    assert dictionary.size == 4


def test_the_dictionary_table_is_recognised_whatever_the_case_or_quoting():
    assert data_dictionary.is_dictionary("metadata.data_dictionary", "METADATA", "DATA_DICTIONARY")
    assert data_dictionary.is_dictionary('"Metadata"."Data_Dictionary"', "metadata", "data_dictionary")
    assert not data_dictionary.is_dictionary("metadata.data_dictionary", "sales", "data_dictionary")
    # Without a schema, it's that table wherever it is.
    assert data_dictionary.is_dictionary("data_dictionary", "sales", "data_dictionary")
    assert not data_dictionary.is_dictionary(None, "metadata", "data_dictionary")


def test_nothing_is_read_when_no_dictionary_is_configured():
    model = DictionaryModel(ROWS)
    with _app().app_context():
        assert data_dictionary.read(_database(model), None) == (None, [])
    assert model.executed == []


def test_the_dictionary_is_read_with_a_fixed_select_of_the_configured_names():
    model = DictionaryModel(ROWS)
    config = {
        "NLSQL_DATA_DICTIONARY_TABLE": "meta.DICT",
        "NLSQL_DATA_DICTIONARY_COLUMNS": {"description": "comments"},
    }
    with _app(**config).app_context(), patch.object(telemetry, "record_tool_call") as record:
        dictionary, problems = data_dictionary.read(_database(model), "cat")
    (sql, options), = model.executed
    assert sql == 'SELECT schema_name, table_name, column_name, comments FROM meta."DICT"'
    assert (options.catalog, options.schema) == ("cat", None)
    assert problems == []
    assert dictionary.table == "meta.DICT"
    assert dictionary.table_description("sales", "orders") == "One row per order placed."
    assert record.call_args.args[0] == superset_db.DICTIONARY_SPAN


def test_a_dictionary_too_big_to_read_whole_says_so():
    with _app(NLSQL_DATA_DICTIONARY_TABLE="d", NLSQL_DATA_DICTIONARY_MAX_ROWS=2).app_context():
        dictionary, problems = data_dictionary.read(_database(DictionaryModel(ROWS)), None)
    assert dictionary.size == 2
    assert problems == ["The data dictionary (d) has more than 2 rows - only the first 2 were read."]


def test_a_dictionary_that_cant_be_read_is_a_problem_not_a_failure():
    model = DictionaryModel([], status=QueryStatus.FAILED, error="relation \"d\" does not exist")
    with _app(NLSQL_DATA_DICTIONARY_TABLE="d").app_context():
        dictionary, problems = data_dictionary.read(_database(model), None)
    assert dictionary is None
    assert problems == ['The data dictionary (d, for schemas, tables, columns) couldn\'t be read: relation "d" does not exist']


def test_names_are_quoted_only_where_they_need_it():
    database = _database(None, dialect="oracle")
    assert superset_db.dictionary_query(database, "data_dictionary", ["schema_name", "order"]) == (
        'SELECT schema_name, "order" FROM data_dictionary'
    )


SCHEMA_LEVEL = {
    "schemas": {"table": "META.SCHEMA_DESCRIPTIONS", "columns": {"schema": "SCHEMA_NAME", "description": "DESCRIPTION"}},
}


def test_a_dictionary_of_schemas_alone_reads_just_those_two_columns():
    model = DictionaryModel([("SALES", "Everything sold.")])
    with _app(NLSQL_DATA_DICTIONARY=SCHEMA_LEVEL).app_context():
        dictionary, problems = data_dictionary.read(_database(model, dialect="oracle"), None)
    (sql, _options), = model.executed
    assert sql == 'SELECT "SCHEMA_NAME", "DESCRIPTION" FROM "META"."SCHEMA_DESCRIPTIONS"'
    assert problems == []
    assert dictionary.schema_description("sales") == "Everything sold."
    assert dictionary.tables == {} and dictionary.columns == {}


def test_each_level_can_have_a_table_of_its_own():
    tables = {"table": "meta.table_descriptions", "columns": {"schema": "s", "table": "t", "description": "d"}}
    executed = []

    def read(_database, _catalog, table, names, _limit):
        executed.append((table, names))
        if table == "META.SCHEMA_DESCRIPTIONS":
            return superset_db.DictionaryRows(rows=[("SALES", "Everything sold.")])
        return superset_db.DictionaryRows(rows=[("sales", "orders", "One row per order.")])

    with _app(NLSQL_DATA_DICTIONARY={**SCHEMA_LEVEL, "tables": tables}).app_context(), patch.object(superset_db, "read_dictionary", read):
        dictionary, problems = data_dictionary.read(_database(None), None)
    assert executed == [("META.SCHEMA_DESCRIPTIONS", ["SCHEMA_NAME", "DESCRIPTION"]), ("meta.table_descriptions", ["s", "t", "d"])]
    assert problems == []
    assert dictionary.table == "META.SCHEMA_DESCRIPTIONS, meta.table_descriptions"
    assert dictionary.schema_description("sales") == "Everything sold."
    assert dictionary.table_description("sales", "orders") == "One row per order."


def test_levels_sharing_a_table_read_it_once_and_rows_say_which_level_they_are():
    shared = {"table": "d", "columns": {"schema": "schema_name", "table": "table_name", "description": "description"}}
    settings = {"schemas": {"table": "d", "columns": {"schema": "schema_name", "description": "description"}}, "tables": shared}
    model = DictionaryModel([(schema, table, description) for schema, table, column, description in ROWS if column is None])
    with _app(NLSQL_DATA_DICTIONARY=settings).app_context():
        dictionary, problems = data_dictionary.read(_database(model), None)
    assert len(model.executed) == 1
    assert problems == []
    assert dictionary.schema_description("sales") == "Everything sold, by the shop and online."
    assert dictionary.table_description("sales", "orders") == "One row per order placed."
    assert dictionary.columns == {}


def test_a_level_missing_what_it_needs_is_skipped_and_reported():
    settings = {**SCHEMA_LEVEL, "columns": {"table": "meta.column_descriptions", "columns": {"schema": "s", "description": "d"}}}
    with _app(NLSQL_DATA_DICTIONARY=settings).app_context():
        config = data_dictionary.configured()
    assert [source.levels for source in config.sources] == [["schemas"]]
    assert config.problems == [
        'The data dictionary\'s columns level is skipped: it needs a table and "schema", "table", "column", '
        '"description" columns (missing table, column).'
    ]


def test_the_dictionary_can_be_read_from_another_database():
    elsewhere = _database(DictionaryModel([("sales", "Everything sold.")]), dialect="oracle")
    elsewhere.name = "Warehouse"
    trino = DatabaseInfo(id=9, name="Lake", backend="trino", dialect="trino", model=DictionaryModel([]))
    with _app(NLSQL_DATA_DICTIONARY={**SCHEMA_LEVEL, "database": "Warehouse"}).app_context(), \
            patch.object(superset_db, "resolve_database_by_name", lambda name: elsewhere):
        dictionary, problems = data_dictionary.read(trino, "lake_catalog")
        config = data_dictionary.configured()
    assert trino.model.executed == [] and len(elsewhere.model.executed) == 1
    assert elsewhere.model.executed[0][1].catalog is None
    assert dictionary.table == "Warehouse: META.SCHEMA_DESCRIPTIONS"
    assert dictionary.schema_description("SALES") == "Everything sold."
    # Only when reading the database the dictionary is in is its table left out.
    assert data_dictionary.tables_in(config, trino) == []
    assert data_dictionary.tables_in(config, elsewhere) == ["META.SCHEMA_DESCRIPTIONS"]


def test_a_dictionary_database_the_user_cant_use_is_a_problem_not_a_failure():
    def unavailable(name):
        raise superset_db.DatabaseUnavailable(f"There's no database called {name}, or you don't have access to it.")

    with _app(NLSQL_DATA_DICTIONARY={**SCHEMA_LEVEL, "database": "Warehouse"}).app_context(), \
            patch.object(superset_db, "resolve_database_by_name", unavailable):
        dictionary, problems = data_dictionary.read(_database(DictionaryModel([])), None)
    assert dictionary is None
    assert problems == [
        "The data dictionary's database, Warehouse, couldn't be used: There's no database called Warehouse, or you don't have access to it."
    ]


def test_coverage_counts_what_is_described_and_what_matches_nothing():
    dictionary = data_dictionary.from_rows("d", ROWS + [("gone", None, None, "A schema that isn't there.")])
    coverage = data_dictionary.coverage(dictionary, {"sales": [("orders", ["amt", "cust_id", "status"]), ("returns", ["id"])]})
    assert coverage.to_json() == {
        "schemas": 1, "schemasDescribed": 1, "tables": 2, "tablesDescribed": 1,
        "columns": 4, "columnsDescribed": 2, "unmatched": 1,
    }
    assert coverage.summary() == "describes 1 of 1 schemas, 1 of 2 tables and 2 of 4 columns; 1 of its entries match nothing read"


def test_any_of_several_dictionary_tables_is_recognised():
    assert data_dictionary.is_dictionary(["meta.schemas", "meta.tables"], "META", "TABLES")
    assert not data_dictionary.is_dictionary(["meta.schemas"], "meta", "tables")
