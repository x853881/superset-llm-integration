"""Reading the structure from the catalog views a group of schemas at a time: the same result as
reading it table by table, in a handful of queries."""

import re
from unittest.mock import patch

import pytest
from flask import Flask

from streams import run
from test_db_interrogation import FakeModel, _with_table
from waxwing.nltosqlandback import bulk_structure, db_interrogation, request_context, schema_knowledge, superset_db
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.superset_db import DatabaseInfo

ORACLE = DatabaseInfo(id=7, name="Warehouse", backend="oracle", dialect="oracle", model=None)
POSTGRES = DatabaseInfo(id=8, name="examples", backend="postgresql", dialect="postgres", model=None)


@pytest.mark.parametrize(
    "setting, database, mode",
    [
        (None, ORACLE, "per_table"),
        ("per_schema", ORACLE, "per_schema"),
        ("PER_DATABASE", ORACLE, "per_database"),
        ({"default": "per_database", "Warehouse": "per_schema"}, ORACLE, "per_schema"),
        ({"default": "per_schema"}, ORACLE, "per_schema"),
        ("per_database", POSTGRES, "per_table"),
        ("all_at_once", ORACLE, "per_table"),
    ],
)
def test_the_setting_chooses_how_the_structure_is_read(setting, database, mode):
    app = Flask(__name__)
    if setting is not None:
        app.config["NLSQL_STRUCTURE_READ"] = setting
    with app.app_context():
        assert bulk_structure.mode_for(database) == mode


def test_per_schema_reads_each_schema_on_its_own_and_per_database_reads_them_together():
    assert bulk_structure.groups("per_schema", ["a", "b"]) == [["a"], ["b"]]
    assert bulk_structure.groups("per_database", ["a", "b"]) == [["a", "b"]]
    many = [f"s{index}" for index in range(bulk_structure.SCHEMAS_PER_QUERY + 1)]
    assert [len(group) for group in bulk_structure.groups("per_database", many)] == [bulk_structure.SCHEMAS_PER_QUERY, 1]


class OracleCatalog:
    """Answers the Oracle catalog queries from a `FakeModel`'s tables, stored as Oracle stores
    case-insensitive names: in upper case."""

    TABLES = {"PUBLIC": ["ORDERS", "CUSTOMERS", "ORDER_SUMMARY"], "ARCHIVE": ["OLD_ORDERS"]}

    def __init__(self, model: FakeModel, truncate_groups: bool = False, fail: bool = False):
        self.model = model
        self.truncate_groups = truncate_groups
        self.fail = fail
        self.queries: list[str] = []

    def __call__(self, sql: str):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("ORA-00942: table or view does not exist")
        owners = re.search(r"owner IN \(([^)]*)\)", sql).group(1)
        owners = [owner.strip().strip("'") for owner in owners.split(",")]
        rows = []
        for owner in owners:
            for table in self.TABLES.get(owner, []):
                rows.extend(self._rows(sql, owner, table))
        return rows, self.truncate_groups and len(owners) > 1

    def _rows(self, sql, owner, table):
        name = table.lower()
        if "FROM all_tab_columns" in sql:
            return [
                (owner, table, column["column_name"].upper(), str(column["type"].compile()), None, None, None, None,
                 "N" if not column["nullable"] else "Y", position)
                for position, column in enumerate(self.model.columns[name], start=1)
            ]
        if "FROM all_col_comments" in sql:
            return [(owner, table, column["column_name"].upper(), column["comment"])
                    for column in self.model.columns[name] if column.get("comment")]
        if "FROM all_tab_comments" in sql:
            return [(owner, table, "Customer orders")] if name == "orders" else []
        if "constraint_type = 'P'" in sql:
            return [] if name == "order_summary" else [(owner, table, f"{table}_PK", "ID", 1)]
        if "constraint_type = 'R'" in sql:
            return [(owner, table, "ORDERS_CUST_FK", "CUST_ID", 1, owner, "CUSTOMERS", "ID")] if name == "orders" else []
        if "FROM all_indexes" in sql:
            if name != "orders":
                return []
            return [
                (owner, table, "ORDERS_PK", "UNIQUE", "ID", 1),             # the primary key's own index
                (owner, table, "ORDERS_CUST", "NONUNIQUE", "CUST_ID", 1),
                (owner, table, "ORDERS_EXPR", "NONUNIQUE", "SYS_NC00005$", 1),  # a function-based index
            ]
        raise AssertionError(f"unexpected query: {sql}")


def _interrogate_oracle(monkeypatch, mode, catalog_reader=None, describe=False):
    _with_table(monkeypatch)
    model = FakeModel()
    database = DatabaseInfo(id=7, name="Warehouse", backend="oracle", dialect="oracle", model=model)
    reader = catalog_reader(model) if catalog_reader else OracleCatalog(model)
    context = CallContext(endpoint="interrogate_db", database_id=7, username="alice")
    monkeypatch.setattr(db_interrogation, "_config", lambda name, default: False if name == "NLSQL_INTERROGATE_ROW_ESTIMATES" else default)
    with request_context.bound(context), \
            patch.object(superset_db, "resolve_database", lambda _id: database), \
            patch.object(bulk_structure, "mode_for", lambda _database: mode), \
            patch.object(bulk_structure, "superset_runner", lambda _database, _catalog: reader), \
            patch("waxwing.nltosqlandback.db_interrogation.resolve_llm_provider", return_value=None):
        result, _events = run(db_interrogation.interrogate(describe=describe))
    kept = schema_knowledge.store.get(7, None)
    schema_knowledge.store.delete(7, None)
    return result, kept, reader


def _comparable(kept):
    def column(c):
        return (c.name, c.data_type, c.description)

    return sorted(
        (t.schema, t.name, t.kind, t.description, [column(c) for c in t.columns], t.indexes,
         [r.description for r in t.relationships])
        for t in kept.tables
    )


def _without_enums(monkeypatch):
    """Oracle has no enum types, so the fake's one enum column isn't something the catalog would report."""
    monkeypatch.setattr(superset_db, "_column_meta", lambda raw, real=superset_db._column_meta: _plain(real(raw)))


def _plain(meta):
    meta.enum_values = None
    return meta


@pytest.mark.parametrize("mode", ["per_schema", "per_database"])
@pytest.mark.parametrize("describe", [False, True])
def test_reading_from_the_catalog_gives_the_same_knowledge_as_reading_table_by_table(monkeypatch, mode, describe):
    _without_enums(monkeypatch)
    if describe:
        monkeypatch.setattr(db_interrogation, "_describe_all", lambda *a, **k: iter(()), raising=False)
    _result, by_table, _ = _interrogate_oracle(monkeypatch, "per_table")
    _result, in_bulk, reader = _interrogate_oracle(monkeypatch, mode)
    assert _comparable(in_bulk) == _comparable(by_table)
    assert len(reader.queries) == {"per_schema": 2 * 5, "per_database": 5}[mode]


def test_a_group_too_big_for_one_result_is_split_and_read_again(monkeypatch):
    _without_enums(monkeypatch)
    _result, by_table, _ = _interrogate_oracle(monkeypatch, "per_table")
    result, in_bulk, reader = _interrogate_oracle(monkeypatch, "per_database", lambda model: OracleCatalog(model, truncate_groups=True))
    assert _comparable(in_bulk) == _comparable(by_table)
    assert not result.get("problems")
    assert len({re.search(r"owner IN \(([^)]*)\)", sql).group(1) for sql in reader.queries}) == 3  # both, then each


def test_a_catalog_the_database_refuses_is_read_table_by_table_and_says_so(monkeypatch):
    _without_enums(monkeypatch)
    _result, by_table, _ = _interrogate_oracle(monkeypatch, "per_table")
    result, in_bulk, _reader = _interrogate_oracle(monkeypatch, "per_database", lambda model: OracleCatalog(model, fail=True))
    assert _comparable(in_bulk) == _comparable(by_table)
    assert any("couldn't be read from the catalog" in problem for problem in result["problems"])


def test_oracle_types_are_written_the_way_oracle_writes_them():
    assert bulk_structure._oracle_type("NUMBER", 22, None, "19", "0") == "NUMBER(19)"
    assert bulk_structure._oracle_type("NUMBER", 22, None, 10, 2) == "NUMBER(10,2)"
    assert bulk_structure._oracle_type("NUMBER", 22, None, None, None) == "NUMBER"
    assert bulk_structure._oracle_type("VARCHAR2", 400, 100, None, None) == "VARCHAR2(100)"
    assert bulk_structure._oracle_type("CLOB", 4000, None, None, None) == "CLOB"
    assert bulk_structure._oracle_type("TIMESTAMP(6)", 11, None, None, 6) == "TIMESTAMP(6)"


def test_oracle_names_are_matched_the_way_sqlalchemy_spells_them():
    assert bulk_structure._oracle_name("orders") == "ORDERS"
    assert bulk_structure._oracle_name("MixedCase") == "MixedCase"
    assert bulk_structure._sqlalchemy_name("ORDERS") == "orders"
    assert bulk_structure._sqlalchemy_name("MixedCase") == "MixedCase"


def test_a_listed_table_the_catalog_has_nothing_for_says_so():
    found = bulk_structure.read_group("oracle", lambda _sql: ([], False), None, {"public": [("gone", "table")]}, comments=False)
    assert found["public"][0].error == "the catalog has no columns for it"


def test_a_cut_short_result_is_reported_rather_than_used():
    with pytest.raises(bulk_structure.TooManyRows):
        bulk_structure.read_group("oracle", lambda _sql: ([], True), None, {"public": [("orders", "table")]}, comments=False)
