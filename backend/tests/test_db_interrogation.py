"""Interrogate DB: reading a database's structure, optionally describing it, keeping it, and prompts using it."""

import contextlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from streams import FakeLlm, final_stage, planned_keys, run
from waxwing.nltosqlandback import db_interrogation, nlsql_backend, progress, request_context, schema_knowledge, superset_db
from waxwing.nltosqlandback.query_plan import PlanNode, QueryPlan
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.superset_db import ColumnMeta, DatabaseInfo, ForeignKeyMeta, TableMeta


class FakeType:
    def __init__(self, name, enums=None):
        self.name = name
        self.enums = enums

    def compile(self):
        return self.name


class FakeModel:
    """The handful of `Database` model methods interrogation reads - metadata only. Two schemas of
    data ("public", "archive") and one of the database's own ("information_schema")."""

    def __init__(self):
        self.columns = {
            "orders": [
                {"column_name": "id", "type": FakeType("INTEGER"), "nullable": False},
                {"column_name": "cust_id", "type": FakeType("INTEGER"), "nullable": True},
                {"column_name": "status", "type": FakeType("order_status", ["new", "shipped"]), "nullable": True},
                {"column_name": "amt", "type": FakeType("NUMERIC(10, 2)"), "nullable": True, "comment": "order value"},
            ],
            "customers": [
                {"column_name": "id", "type": FakeType("INTEGER"), "nullable": False},
                {"column_name": "name", "type": FakeType("TEXT"), "nullable": True},
            ],
            "order_summary": [{"column_name": "total", "type": FakeType("NUMERIC"), "nullable": True}],
            "old_orders": [{"column_name": "id", "type": FakeType("INTEGER"), "nullable": False}],
        }
        self.forced = []
        self.comment_reads = 0

    def get_all_schema_names(self, catalog=None, force=False):
        self.forced.append(("schemas", force))
        return {"public", "archive", "information_schema"}

    def get_all_table_names_in_schema(self, catalog, schema, force=False):
        self.forced.append((schema, force))
        if schema == "archive":
            return {("old_orders", schema, catalog)}
        return {("orders", schema, catalog), ("customers", schema, catalog)}

    def get_all_view_names_in_schema(self, catalog, schema, force=False):
        return {("order_summary", schema, catalog)} if schema == "public" else set()

    def get_columns(self, table):
        return self.columns[table.table]

    def get_table_comment(self, table):
        self.comment_reads += 1
        return "Customer orders" if table.table == "orders" else None

    def get_pk_constraint(self, table):
        return {"constrained_columns": ["id"]} if table.table != "order_summary" else {}

    def get_foreign_keys(self, table):
        if table.table == "orders":
            return [{"constrained_columns": ["cust_id"], "referred_table": "customers", "referred_columns": ["id"], "referred_schema": None}]
        return []

    def get_indexes(self, table):
        return [{"column_names": ["cust_id"], "unique": False}] if table.table == "orders" else []

    def get_default_schema(self, catalog):
        return "public"


def _database():
    return DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=FakeModel())


@pytest.fixture(autouse=True)
def _clean_store():
    schema_knowledge.store.clear_memory()
    yield
    schema_knowledge.store.clear_memory()


def _with_table(monkeypatch):
    """`superset.sql.parse.Table` without a Superset."""
    import sys
    import types

    parse = types.ModuleType("superset.sql.parse")
    parse.Table = lambda table, schema=None, catalog=None: SimpleNamespace(table=table, schema=schema, catalog=catalog)
    monkeypatch.setitem(sys.modules, "superset", types.ModuleType("superset"))
    monkeypatch.setitem(sys.modules, "superset.sql", types.ModuleType("superset.sql"))
    monkeypatch.setitem(sys.modules, "superset.sql.parse", parse)


def test_read_table_reads_structure_not_data(monkeypatch):
    _with_table(monkeypatch)
    meta = superset_db.read_table(_database(), None, "public", "orders")
    assert [column.name for column in meta.columns] == ["id", "cust_id", "status", "amt"]
    assert meta.primary_key == ["id"]
    assert meta.comment == "Customer orders"
    assert meta.columns[2].enum_values == ["new", "shipped"]
    assert meta.columns[3].data_type == "NUMERIC(10, 2)"
    assert meta.indexes == [(["cust_id"], False)]


def test_list_tables_includes_views(monkeypatch):
    _with_table(monkeypatch)
    assert superset_db.list_tables(_database(), None, "public") == [
        ("customers", "table"), ("order_summary", "view"), ("orders", "table"),
    ]


def test_the_database_s_own_schemas_are_left_out():
    database = _database()
    assert superset_db.list_schemas(database, None, refresh=True) == (["archive", "public"], 1)
    assert ("schemas", True) in database.model.forced


def test_the_schema_reader_uses_one_inspector_when_the_host_has_one(monkeypatch):
    _with_table(monkeypatch)
    opened = []

    class Spec:
        @staticmethod
        def get_columns(inspector, table, options=None):
            return [{"column_name": "id", "type": FakeType("INTEGER"), "nullable": False}]

        @staticmethod
        def get_table_comment(inspector, table):
            raise AssertionError("comments weren't asked for")

        @staticmethod
        def get_indexes(model, inspector, table):
            return [{"column_names": ["id"], "unique": True}]

    class Inspector:
        def get_pk_constraint(self, name, schema):
            return {"constrained_columns": ["id"]}

        def get_foreign_keys(self, name, schema):
            return []

    class Model:
        db_engine_spec = Spec
        schema_options = {}

        @contextlib.contextmanager
        def get_inspector(self, catalog=None, schema=None):
            opened.append(schema)
            yield Inspector()

    database = DatabaseInfo(id=1, name="d", backend="postgresql", dialect="postgres", model=Model())
    with superset_db.schema_reader(database, None, "public", comments=False) as read:
        metas = [read("a", "table"), read("b", "table")]
    assert opened == ["public"]
    assert [meta.primary_key for meta in metas] == [["id"], ["id"]]
    assert metas[0].indexes == [(["id"], True)]


def _plan(*nodes):
    return QueryPlan(backend="postgresql", raw="", root=PlanNode("Append", children=list(nodes)), outline="")


def test_row_estimates_come_from_one_plan_across_schemas():
    database = _database()
    plans = []

    def fake_explain(_database, sql, _catalog, schema):
        plans.append((sql, schema))
        return superset_db.PlanOutcome(plan=_plan(
            PlanNode("Seq Scan", object_name="public.orders", estimated_rows=2800),
            PlanNode("Seq Scan", object_name="archive.old_orders", estimated_rows=90),
            PlanNode("Seq Scan", object_name="public.customers", estimated_rows=120),
        ), checked=True)

    with patch.object(superset_db, "explain", fake_explain):
        sized = superset_db.estimate_rows(
            database, None, [("public", "orders"), ("public", "customers"), ("archive", "old_orders")],
        )
    assert sized.estimates == {("public", "orders"): 2800, ("public", "customers"): 120, ("archive", "old_orders"): 90}
    assert sized.error is None
    assert plans == [(
        "SELECT 1 FROM public.orders\nUNION ALL\nSELECT 1 FROM public.customers\nUNION ALL\nSELECT 1 FROM archive.old_orders",
        None,
    )]


def test_tables_sharing_a_name_are_planned_apart():
    # Oracle names a plan step by the table alone, so two "orders" in one plan couldn't be told apart.
    batches = superset_db._estimate_batches([("a", "orders"), ("b", "ORDERS"), ("a", "items")])
    assert batches == [[("a", "orders"), ("a", "items")], [("b", "ORDERS")]]


def test_a_batch_the_database_refuses_is_split_down_to_the_table_it_refuses():
    database = _database()

    def fake_explain(_database, sql, _catalog, _schema):
        if "secret" in sql:
            return superset_db.PlanOutcome(error="permission denied for table secret\nLINE 1: ...", checked=True)
        nodes = [PlanNode("Seq Scan", object_name=f"public.{name}", estimated_rows=10) for name in ("a", "b", "c")]
        return superset_db.PlanOutcome(plan=_plan(*[node for node in nodes if f"public.{node.object_name[7:]}" in sql]), checked=True)

    with patch.object(superset_db, "explain", fake_explain):
        sized = superset_db.estimate_rows(database, None, [("public", n) for n in ("a", "b", "secret", "c")])
    assert set(sized.estimates) == {("public", "a"), ("public", "b"), ("public", "c")}
    assert sized.refused == {("public", "secret"): "permission denied for table secret"}
    assert sized.error is None


def test_a_table_planned_from_its_index_is_sized_by_its_place_in_the_union():
    # Oracle answers SELECT 1 FROM t from t's primary-key index, and names only the index.
    database = DatabaseInfo(id=1, name="o", backend="oracle", dialect="oracle", model=None)
    plan = QueryPlan(backend="oracle", raw="", outline="", root=PlanNode("SELECT STATEMENT", estimated_rows=77430, children=[
        PlanNode("UNION-ALL", estimated_rows=77430, children=[
            PlanNode("INDEX FAST FULL SCAN", object_name="SYS_C008731", estimated_rows=5000),
            PlanNode("TABLE ACCESS FULL", object_name="MOVIES", estimated_rows=62423),
            PlanNode("INDEX FAST FULL SCAN", object_name="SYS_C008726", estimated_rows=10007),
        ]),
    ]))
    with patch.object(superset_db, "explain", lambda *_a: superset_db.PlanOutcome(plan=plan, checked=True)):
        sized = superset_db.estimate_rows(database, None, [("g", "sales"), ("m", "movies"), ("g", "friendships")])
    assert sized.estimates == {("g", "sales"): 5000, ("m", "movies"): 62423, ("g", "friendships"): 10007}


def test_only_names_that_need_it_are_quoted():
    database = _database()
    assert superset_db._qualified(database, "public", "orders") == "public.orders"
    assert superset_db._qualified(database, "public", "Order Items") == 'public."Order Items"'
    assert superset_db._qualified(database, "public", "order") == 'public."order"'
    oracle = DatabaseInfo(id=1, name="o", backend="oracle", dialect="oracle", model=None)
    assert superset_db._qualified(oracle, "movie_data", "movies") == "movie_data.movies"


DESCRIBED = json.dumps({
    "schemaSummary": "A small shop's orders.",
    "tables": [
        {
            "name": "orders",
            "description": "One row per order placed.",
            "synonyms": ["purchases"],
            "columns": {"cust_id": "the customer who ordered", "amt": "probably in dollars", "nope": "x"},
            "joins": [
                {"fromColumn": "status", "toTable": "customers", "toColumn": "id", "note": "a guess"},
                {"fromColumn": "cust_id", "toTable": "missing", "toColumn": "id"},
                {"fromColumn": "status", "toTable": "customers", "toColumn": "nope"},
            ],
        },
        {"name": "customers", "description": "One row per customer."},
        {"name": "order_summary", "description": "Totals over orders."},
    ],
})
ARCHIVE_DESCRIBED = json.dumps({"schemaSummary": "Old orders.", "tables": [{"name": "old_orders", "description": "Orders before 2020."}]})


def _interrogate(monkeypatch, *responses, describe=False):
    _with_table(monkeypatch)
    llm = FakeLlm(*responses)
    model = FakeModel()
    database = DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=model)
    context = CallContext(endpoint="interrogate_db", database_id=7, username="alice")
    plan = _plan(
        PlanNode("Seq Scan", object_name="public.orders", estimated_rows=2800),
        PlanNode("Seq Scan", object_name="archive.old_orders", estimated_rows=40),
    )
    with request_context.bound(context), \
            patch.object(superset_db, "resolve_database", lambda _id: database), \
            patch.object(superset_db, "explain", lambda *_a: superset_db.PlanOutcome(plan=plan, checked=True)), \
            patch("waxwing.nltosqlandback.db_interrogation.resolve_llm_provider", return_value=SimpleNamespace(model="m")), \
            patch("waxwing.nltosqlandback.llm_client.stream_complete", llm):
        result, events = run(db_interrogation.interrogate(describe=describe))
    return result, events, llm, model


def test_interrogate_reads_every_schema_s_structure_without_the_model(monkeypatch):
    result, events, llm, model = _interrogate(monkeypatch)
    assert planned_keys(events) == ["schemas", "read", "rows", "save"]
    assert llm.prompts == []
    assert model.comment_reads == 0
    assert ("public", True) in model.forced and ("archive", True) in model.forced
    assert result["schemaCount"] == 2
    assert result["skippedSchemas"] == 1
    assert (result["tableCount"], result["viewCount"]) == (3, 1)
    assert result["indexCount"] == 1
    assert result["sizedCount"] == 2
    assert result["described"] is False and result["describedCount"] == 0
    assert result["createdBy"] == "alice"

    kept = schema_knowledge.store.get(7, None)
    orders = next(table for table in kept.tables if table.name == "orders")
    assert orders.schema == "public"
    assert orders.row_count_estimate == 2800
    assert orders.indexes == [(("cust_id",), False)]
    assert orders.description is None
    assert [r.description for r in orders.relationships] == ["cust_id -> public.customers.id (foreign key)"]
    old = next(table for table in kept.tables if table.name == "old_orders")
    assert (old.schema, old.row_count_estimate) == ("archive", 40)
    view = next(table for table in kept.tables if table.name == "order_summary")
    assert view.kind == "view"


def test_interrogate_can_still_have_the_model_describe_the_tables(monkeypatch):
    result, events, llm, model = _interrogate(monkeypatch, ARCHIVE_DESCRIBED, DESCRIBED, describe=True)
    assert planned_keys(events) == ["schemas", "read", "rows", "describe", "save"]
    assert model.comment_reads == 4
    assert result["described"] is True and result["describedCount"] == 4
    assert result["model"] == "m"

    archive_prompt, public_prompt = llm.prompts
    assert '"archive"' in archive_prompt and "TABLE old_orders (≈40 rows, planner estimate)" in archive_prompt
    assert "TABLE orders (≈2,800 rows, planner estimate) -- comment: Customer orders" in public_prompt
    assert "status order_status one of: new, shipped" in public_prompt
    assert "cust_id INTEGER FK -> customers.id" in public_prompt
    assert "VIEW order_summary" in public_prompt
    assert "can't see any of the data" in public_prompt

    kept = schema_knowledge.store.get(7, None)
    assert kept.schema_descriptions == {"archive": "Old orders.", "public": "A small shop's orders."}
    orders = next(table for table in kept.tables if table.name == "orders")
    assert orders.description == "Customer orders (One row per order placed.)"
    assert orders.synonyms == ["purchases"]
    notes = {column.name: column.description for column in orders.columns}
    assert notes["cust_id"] == "the customer who ordered"
    assert notes["amt"] == "order value (probably in dollars)"
    # The declared key, and only the guessed join naming columns that exist.
    assert [r.description for r in orders.relationships] == [
        "cust_id -> public.customers.id (foreign key)",
        "status -> public.customers.id (likely, not declared): a guess",
    ]


def test_a_batch_the_model_garbles_keeps_the_structure(monkeypatch):
    result, events, _llm, _model = _interrogate(monkeypatch, "not json at all", "nor this", describe=True)
    assert result["tableCount"] == 3
    assert result["describedCount"] == 1  # orders, from its own comment
    assert any("couldn't be read" in problem for problem in result["problems"])
    assert final_stage(events, "save")["status"] == progress.STATUS_DONE


def test_interrogation_needs_a_database():
    with request_context.bound(CallContext(endpoint="interrogate_db")):
        with pytest.raises(db_interrogation.InterrogationError):
            run(db_interrogation.interrogate())


def _knowledge(tables=3, described=False):
    from waxwing.nltosqlandback.semantic_schema import ColumnDescription, TableDescription

    return schema_knowledge.DatabaseKnowledge(
        database_id=7,
        database_name="examples",
        catalog=None,
        schemas=["public", "sales"],
        tables=[
            TableDescription(
                schema="public" if index % 2 == 0 else "sales",
                name=f"table_{index}",
                description=f"Table number {index}" if described else None,
                columns=[ColumnDescription(name=f"col_{n}", data_type="INTEGER", is_primary_key=n == 0) for n in range(6)],
                row_count_estimate=1500,
                indexes=[(("col_0",), True), (("col_1", "col_2"), False)],
            )
            for index in range(tables)
        ],
        schema_descriptions={"public": "Numbered tables."} if described else {},
        described=described,
    )


def test_knowledge_round_trips_as_a_database_description():
    knowledge = _knowledge()
    raw = knowledge.to_json()
    assert [schema["schemaName"] for schema in raw["description"]["schemas"]] == ["public", "sales"]
    assert raw["description"]["schemas"][0]["tables"][0]["rowCountEstimate"] == 1500
    assert raw["description"]["schemas"][0]["tables"][0]["indexes"][1] == {"columns": ["col_1", "col_2"], "unique": False}
    again = schema_knowledge.DatabaseKnowledge.from_json(json.loads(json.dumps(raw)))
    assert sorted(again.table_keys) == sorted(knowledge.table_keys)
    assert again.tables[0].indexes == knowledge.tables[0].indexes
    assert again.schemas == ["public", "sales"]


def test_an_old_per_schema_record_is_not_read():
    raw = _knowledge().to_json()
    raw["version"] = 1
    with pytest.raises(ValueError):
        schema_knowledge.DatabaseKnowledge.from_json(raw)


def test_prompt_text_is_structure_then_names_then_ranked():
    knowledge = _knowledge(tables=4)
    text, shown, left_out = schema_knowledge.prompt_text(knowledge, "anything")
    assert (shown, left_out) == (4, 0)
    assert "Database examples: 4 tables and views in 2 schemas: public, sales." in text
    assert "always qualify table names with their schema" in text
    assert "public.table_0 ≈1.5k rows\n  col_0 INTEGER PK, col_1 INTEGER" in text
    assert "sales.table_1 ≈1.5k rows" in text
    assert "  idx: (col_1, col_2)" in text  # the primary key's own index isn't repeated
    assert "never saw the data" not in text

    text, shown, left_out = schema_knowledge.prompt_text(knowledge, "anything", max_chars=400)
    assert (shown, left_out) == (4, 0)
    assert "public.table_0 ≈1.5k rows: col_0, col_1" in text
    assert "left out for space" in text

    text, shown, left_out = schema_knowledge.prompt_text(knowledge, "what is in table_2", max_chars=150)
    assert left_out > 0
    assert "table_2" in text
    assert "more tables are not listed" in text

    text, shown, _ = schema_knowledge.prompt_text(knowledge, "x", visible={("sales", "table_1")})
    assert shown == 1 and "table_3" not in text


def test_prompt_text_has_descriptions_when_they_were_written():
    text, _, _ = schema_knowledge.prompt_text(_knowledge(described=True), "x")
    assert "public.table_0 ≈1.5k rows -- Table number 0" in text
    assert "Schema public: Numbered tables." in text
    assert "never saw the data" in text


@pytest.mark.no_semantic_schema
def test_prompts_use_the_whole_interrogated_database_whatever_schema_the_tab_has():
    schema_knowledge.store.put(_knowledge())
    context = CallContext(endpoint="generate_sql", database_id=7, schema="something_else")
    with request_context.bound(context), patch.object(nlsql_backend.sql_checks, "current_database", return_value=None):
        steps = progress.Steps(nlsql_backend.with_tables_step([("read", "r", "r"), nlsql_backend.TABLES_STEP]))
        assert "tables" in [key for key, *_ in nlsql_backend.with_tables_step([nlsql_backend.TABLES_STEP])]
        preamble, events = run(nlsql_backend.schema_preamble("orders by month", steps))
    assert "public.table_0" in preamble and "sales.table_1" in preamble
    assert final_stage(events, "tables")["detail"] == "All 3 tables in 2 schemas of examples"


@pytest.mark.no_semantic_schema
def test_prompts_without_an_interrogation_are_unchanged():
    context = CallContext(endpoint="generate_sql", database_id=8)
    steps = progress.Steps([("read", "r", "r")])
    with request_context.bound(context), patch.object(nlsql_backend.sql_checks, "current_database", return_value=None):
        assert nlsql_backend.with_tables_step([nlsql_backend.TABLES_STEP]) == []
        preamble, events = run(nlsql_backend.schema_preamble("x", steps))
    assert preamble == ""
    assert events == []


def test_forgetting_removes_it():
    schema_knowledge.store.put(_knowledge())
    assert schema_knowledge.store.delete(7, None) is True
    assert schema_knowledge.store.get(7, None) is None


def test_structure_text_names_foreign_keys():
    meta = TableMeta(
        name="orders",
        kind="table",
        columns=[ColumnMeta("customer_id", "INTEGER")],
        foreign_keys=[ForeignKeyMeta(columns=["customer_id"], referred_table="customers", referred_columns=["id"], referred_schema="crm")],
    )
    assert "customer_id INTEGER FK -> crm.customers.id" in db_interrogation.structure_text(meta)


def test_large_object_columns_carry_a_warning():
    from waxwing.nltosqlandback.semantic_schema import ColumnDescription, TableDescription

    knowledge = _knowledge(tables=0)
    knowledge.tables = [TableDescription(schema="s", name="movies", columns=[ColumnDescription(name="title", data_type="CLOB")])]
    text, _, _ = schema_knowledge.prompt_text(knowledge, "x")
    assert "title CLOB [LOB]" in text
    assert "Columns marked [LOB] are large objects" in text


def test_plan_steps_are_matched_to_tables_however_the_engine_names_them():
    names = sorted(["orders", "orders_archive", "FCC 2018 Survey", "movies"], key=len, reverse=True)
    assert superset_db._table_in("public.orders o", "public", names) == "orders"
    assert superset_db._table_in("public.orders_archive", "public", names) == "orders_archive"
    assert superset_db._table_in("superset_data.FCC 2018 Survey", "superset_data", names) == "FCC 2018 Survey"
    assert superset_db._table_in("MOVIES", "movie_data", names) == "movies"
    assert superset_db._table_in("iceberg.sales.orders", "sales", names) == "orders"
    assert superset_db._table_in("iceberg:movie_data.movies$data@1397344821116344405", "movie_data", names) == "movies"
    assert superset_db._table_in("customers", "public", names) is None
