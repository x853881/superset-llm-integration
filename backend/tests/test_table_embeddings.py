"""Matching an interrogated database's tables to a request by embedding: what's embedded, how it's
kept, how close each table is, and the prompt that results when not every table fits."""

import zlib
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from streams import final_stage, planned_keys, run
from test_db_interrogation import _interrogate, _knowledge
from waxwing.nltosqlandback import cancellation, nlsql_backend, progress, request_context, schema_knowledge, table_embeddings
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.superset_db import DatabaseInfo
from waxwing.nltosqlandback.semantic_schema import ColumnDescription, RelationshipDescription, TableDescription

DIMENSIONS = 64
PROVIDER = SimpleNamespace(model="embed-m", document_prefix="", query_prefix="")

EXAMPLES = DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=None)


def _vector(text):
    """A bag of words, hashed into `DIMENSIONS` buckets: texts sharing words point the same way."""
    vector = [0.0] * DIMENSIONS
    for word in text.lower().replace(".", " ").replace("_", " ").split():
        vector[zlib.crc32(word.encode()) % DIMENSIONS] += 1.0
    return vector


class FakeEmbedder:
    def __init__(self):
        self.calls = []

    def documents(self, provider, texts):
        self.calls.append(list(texts))
        return [_vector(text) for text in texts]

    def query(self, provider, text, cache=None):
        return _vector(text)


def _new_process():
    """Forgets what this process has read, as another process would."""
    for memory in (table_embeddings._memory, table_embeddings._vectors, table_embeddings._absent):
        memory.clear()


@pytest.fixture(autouse=True)
def _clean():
    schema_knowledge.store.clear_memory()
    _new_process()
    yield
    schema_knowledge.store.clear_memory()
    _new_process()


@pytest.fixture
def embedder(monkeypatch):
    fake = FakeEmbedder()
    monkeypatch.setattr(table_embeddings, "embed_documents", fake.documents)
    monkeypatch.setattr(table_embeddings, "embed_query", fake.query)
    monkeypatch.setattr(table_embeddings, "resolve_embedding_provider", lambda: PROVIDER)
    monkeypatch.setattr(table_embeddings, "enabled", lambda: True)
    return fake


def _table(schema, name, description=None, columns=("id",), joins=()):
    return TableDescription(
        schema=schema,
        name=name,
        description=description,
        columns=[ColumnDescription(name=column, data_type="INTEGER") for column in columns],
        relationships=[RelationshipDescription(f"id -> {to}.id", "id", to, "id") for to in joins],
    )


def _shop():
    return schema_knowledge.DatabaseKnowledge(
        database_id=7,
        database_name="shop",
        catalog=None,
        schemas=["sales", "hr"],
        tables=[
            _table("hr", "staff", "employees and their salaries", ("staff_id", "salary")),
            _table("sales", "orders", "customer orders", ("order_id", "customer_id"), joins=("sales.customers",)),
            _table("sales", "customers", "customer names and addresses", ("customer_id", "name", "address")),
            _table("hr", "holidays", "staff holiday bookings", ("staff_id", "starts")),
        ],
    )


def _embed(knowledge, provider=PROVIDER, cancel_token=None):
    """Embeds `knowledge`'s tables, as Interrogate DB does, and records their keys in it."""
    steps = progress.Steps([("embed", "Embedding", "Embedded")])
    (keys, reused), events = run(table_embeddings.embed(knowledge.tables, knowledge.schema_descriptions, provider, steps, cancel_token))
    knowledge.embedding_model, knowledge.embedding_keys = provider.model, keys
    return reused, events


def test_a_table_is_embedded_as_its_names_and_descriptions_without_types():
    table = TableDescription(
        schema="sales",
        name="orders",
        description="Customer orders",
        synonyms=["purchases"],
        columns=[ColumnDescription(name="amt", data_type="NUMERIC(10, 2)", description="order value"), ColumnDescription(name="id", data_type="INTEGER")],
    )
    text = table_embeddings.embedding_text(table, "Everything sold")
    assert text == "sales.orders\nIn sales: Everything sold\nCustomer orders\nAlso called: purchases\namt - order value\nid"


def test_scores_are_the_same_with_and_without_numpy(monkeypatch):
    rows = b"".join(table_embeddings.quantise(_vector(text)) for text in ("staff salaries", "customer orders", "customer names"))

    def scores():
        vectors = table_embeddings.TableVectors("m", DIMENSIONS, [("s", "a"), ("s", "b"), ("s", "c")], ["", "", ""], rows)
        return vectors.scores(table_embeddings._signed(table_embeddings.quantise(_vector("customer orders"))))

    with_numpy = scores()
    monkeypatch.setattr(table_embeddings, "numpy", None)
    without = scores()
    assert without.index(max(without)) == 1
    assert with_numpy == without


def test_a_key_is_a_short_hash_of_the_model_the_prefix_and_the_text():
    key = table_embeddings.key_for(PROVIDER, "sales.orders")
    assert len(key) == 32
    assert table_embeddings.key_for(PROVIDER, "sales.orders") == key
    assert table_embeddings.key_for(SimpleNamespace(model="another", document_prefix=""), "sales.orders") != key
    assert table_embeddings.key_for(SimpleNamespace(model="embed-m", document_prefix="doc: "), "sales.orders") != key
    assert table_embeddings.key_for(PROVIDER, "sales.order") != key


def test_only_tables_whose_text_has_changed_are_embedded_again(embedder):
    knowledge = _shop()
    reused, events = _embed(knowledge)
    assert reused == 0
    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 4
    assert final_stage(events, "embed")["detail"] == "4 of 4 tables with embed-m"
    first = dict(knowledge.embedding_keys)

    knowledge.tables[0] = _table("hr", "staff", "employees, salaries and grades", ("staff_id", "salary"))
    reused, _ = _embed(knowledge)
    assert reused == 3
    assert embedder.calls[1] == [table_embeddings.embedding_text(knowledge.tables[0])]
    assert knowledge.embedding_keys[("hr", "staff")] != first[("hr", "staff")]
    assert knowledge.embedding_keys[("sales", "orders")] == first[("sales", "orders")]

    reused, _ = _embed(knowledge, SimpleNamespace(model="another", document_prefix="", query_prefix=""))
    assert reused == 0


def test_a_table_described_the_same_way_in_two_databases_is_embedded_once(embedder):
    _embed(_shop())
    other = _shop()
    other.database_id = 8
    _new_process()
    reused, _ = _embed(other)
    assert reused == 4 and len(embedder.calls) == 1


def test_embedding_goes_in_batches_and_stops_when_cancelled(embedder, monkeypatch):
    monkeypatch.setattr(table_embeddings, "BATCH", 3)
    _embed(_shop())
    assert [len(call) for call in embedder.calls] == [3, 1]

    token = cancellation.CancelToken()
    token.cancel()
    with pytest.raises(cancellation.CancelledError):
        _embed(_shop(), SimpleNamespace(model="another", document_prefix="", query_prefix=""), cancel_token=token)


def test_what_was_embedded_before_a_failure_is_kept(embedder, monkeypatch):
    monkeypatch.setattr(table_embeddings, "BATCH", 3)
    real = embedder.documents

    def fails_second_time(provider, texts):
        if embedder.calls:
            raise RuntimeError("rate limited")
        return real(provider, texts)

    monkeypatch.setattr(table_embeddings, "embed_documents", fails_second_time)
    with pytest.raises(RuntimeError):
        _embed(_shop())
    monkeypatch.setattr(table_embeddings, "embed_documents", real)
    _new_process()
    reused, _ = _embed(_shop())
    assert reused == 3


def test_the_vectors_are_shared_by_every_process_and_outlive_the_knowledge(embedder):
    knowledge = _shop()
    _embed(knowledge)
    before = table_embeddings.for_knowledge(knowledge)
    buckets = {table_embeddings._bucket(key) for key in knowledge.embedding_keys.values()}
    assert all(schema_knowledge.store.get_record(table_embeddings._bucket_name(bucket)) for bucket in buckets)

    _new_process()
    again = table_embeddings.for_knowledge(knowledge)
    assert again.vectors == before.vectors and again.keys == before.keys

    schema_knowledge.store.delete(7, None)
    _new_process()
    assert len(table_embeddings.lookup(list(knowledge.embedding_keys.values()))) == 4


def test_a_newer_interrogation_of_the_same_tables_uses_the_same_vectors(embedder):
    knowledge = _shop()
    _embed(knowledge)
    newer = _shop()
    newer.created_at = knowledge.created_at + 1
    _embed(newer)
    assert len(embedder.calls) == 1
    assert table_embeddings.for_knowledge(newer).vectors == table_embeddings.for_knowledge(knowledge).vectors


def test_a_table_whose_vector_was_not_kept_ranks_last(embedder):
    knowledge = _shop()
    _embed(knowledge)
    lost = knowledge.embedding_keys[("sales", "customers")]
    schema_knowledge.store.delete_record(table_embeddings._bucket_name(table_embeddings._bucket(lost)))
    _new_process()
    assert ("sales", "customers") not in table_embeddings.for_knowledge(knowledge).keys
    assert table_embeddings.ranking(knowledge, "names and addresses")[-1] == ("sales", "customers")


def test_a_full_store_keeps_nothing_more_and_gives_no_ranking(embedder, monkeypatch):
    monkeypatch.setattr(table_embeddings, "BUCKET_BYTES", 0)
    knowledge = _shop()
    _embed(knowledge)
    _new_process()
    assert table_embeddings.lookup(list(knowledge.embedding_keys.values())) == {}
    assert table_embeddings.ranking(knowledge, "names and addresses") is None


def test_a_write_another_process_undid_is_made_again(embedder, monkeypatch):
    real_put = schema_knowledge.store.put_record
    lost = []

    def loses_the_first(name, raw):
        if not lost:
            lost.append(name)
            return
        real_put(name, raw)

    monkeypatch.setattr(schema_knowledge.store, "put_record", loses_the_first)
    knowledge = _shop()
    _embed(knowledge)
    _new_process()
    assert len(table_embeddings.lookup(list(knowledge.embedding_keys.values()))) == 4


def test_the_keys_are_kept_with_the_knowledge(embedder):
    knowledge = _shop()
    _embed(knowledge)
    again = schema_knowledge.DatabaseKnowledge.from_json(knowledge.to_json())
    assert again.embedding_model == "embed-m" and again.embedding_keys == knowledge.embedding_keys
    assert schema_knowledge.DatabaseKnowledge.from_json(_shop().to_json()).embedding_keys == {}


def test_tables_are_ranked_by_closeness_and_a_joined_table_comes_along(embedder):
    knowledge = _shop()
    _embed(knowledge)
    order = table_embeddings.ranking(knowledge, "names and addresses")
    assert order[0] == ("sales", "customers")
    # orders shares nothing with the request, but joins to customers, so it beats the hr tables.
    assert order[1] == ("sales", "orders")
    assert table_embeddings.ranking(knowledge, "staff salaries")[0] == ("hr", "staff")


def test_no_ranking_when_off_or_the_model_has_changed(embedder, monkeypatch):
    knowledge = _shop()
    assert table_embeddings.ranking(knowledge, "customers") is None  # nothing embedded yet
    _embed(knowledge)
    monkeypatch.setattr(table_embeddings, "resolve_embedding_provider", lambda: SimpleNamespace(model="another"))
    assert table_embeddings.ranking(knowledge, "customers") is None
    monkeypatch.setattr(table_embeddings, "resolve_embedding_provider", lambda: PROVIDER)
    monkeypatch.setattr(table_embeddings, "enabled", lambda: False)
    assert table_embeddings.ranking(knowledge, "customers") is None


def test_prompt_text_asks_for_a_ranking_only_when_not_every_table_fits():
    knowledge = _knowledge(tables=4, described=True)
    asked = []

    def rank():
        asked.append(True)
        return [("sales", "table_3"), ("public", "table_0"), ("sales", "table_1"), ("public", "table_2")]

    _text, shown, _ = schema_knowledge.prompt_text(knowledge, "x", rank=rank)
    assert shown == 4 and asked == []

    full = schema_knowledge._described_text(knowledge.tables[3])
    room = next(
        size for size in range(100, 10_000, 5)
        if all(name in schema_knowledge.prompt_text(knowledge, "x", max_chars=size, rank=rank)[0] for name in ("sales.table_3", "public.table_0"))
    )
    text, shown, left_out = schema_knowledge.prompt_text(knowledge, "x", max_chars=room, rank=rank)
    assert (shown, left_out) == (2, 2)
    assert text.index("sales.table_3") < text.index("public.table_0")
    assert full in text  # described, not cut down to names
    assert "Only 2 of the 4 tables fit here: the ones closest to the request, closest first." in text
    assert "2 more tables are not listed" in text

    text, _, _ = schema_knowledge.prompt_text(knowledge, "x", visible={("public", "table_0"), ("sales", "table_1")}, max_chars=room, rank=rank)
    assert "table_3" not in text and text.index("public.table_0") < text.index("sales.table_1")

    head = len(schema_knowledge._header(knowledge, knowledge.tables)) + len("\n(Column types and indexes left out for space.)")
    _text, _, left_out = schema_knowledge.prompt_text(knowledge, "what is in table_2", max_chars=head + 150, rank=lambda: None)
    assert left_out > 0  # no ranking to be had: as before


@pytest.mark.no_semantic_schema
def test_interrogation_embeds_the_tables_and_prompts_get_the_closest(embedder, monkeypatch):
    monkeypatch.setattr("waxwing.nltosqlandback.db_interrogation.resolve_embedding_provider", lambda: PROVIDER)
    _result, events, _llm, _model = _interrogate(monkeypatch)
    assert planned_keys(events) == ["schemas", "read", "rows", "embed", "save"]
    assert final_stage(events, "embed")["detail"] == "4 tables"
    knowledge = schema_knowledge.store.get(7, None)
    assert table_embeddings.for_knowledge(knowledge) is not None
    _result, events, _llm, _model = _interrogate(monkeypatch)
    assert final_stage(events, "embed")["detail"] == "4 tables, 4 already embedded"

    real_config = nlsql_backend._config
    budget = len(schema_knowledge._header(knowledge, knowledge.tables)) + 250
    monkeypatch.setattr(nlsql_backend, "_config", lambda name, default: budget if name == "NLSQL_SCHEMA_MAX_CHARS" else real_config(name, default))
    context = CallContext(endpoint="generate_sql", database_id=7)
    with request_context.bound(context), patch.object(nlsql_backend.sql_checks, "current_database", return_value=EXAMPLES):
        steps = progress.Steps(nlsql_backend.with_tables_step([nlsql_backend.TABLES_STEP]))
        preamble, events = run(nlsql_backend.schema_preamble("old orders", steps))
    assert "archive.old_orders" in preamble
    assert final_stage(events, "tables")["detail"].endswith("the closest to the request")


def test_interrogation_carries_on_when_the_tables_cannot_be_embedded(embedder, monkeypatch):
    monkeypatch.setattr("waxwing.nltosqlandback.db_interrogation.resolve_embedding_provider", lambda: PROVIDER)

    def failing(provider, texts):
        raise RuntimeError("no embedding model")

    monkeypatch.setattr(table_embeddings, "embed_documents", failing)
    result, events, _llm, _model = _interrogate(monkeypatch)
    assert final_stage(events, "embed")["status"] == progress.STATUS_FAILED
    assert any("couldn't be embedded" in problem for problem in result["problems"])
    assert table_embeddings.for_knowledge(schema_knowledge.store.get(7, None)) is None
