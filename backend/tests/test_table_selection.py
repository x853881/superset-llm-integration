"""Choosing how the tables for the SQL prompt are picked: the options, who may choose, the picking
call and how its answer is held to what it was offered, and the prompt it leads to."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, g

from streams import FakeLlm, run
from test_db_interrogation import _knowledge
from waxwing.nltosqlandback import nlsql_backend, request_context, schema_knowledge, table_selection
from waxwing.nltosqlandback.llm_provider import LlmProvider
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.superset_db import DatabaseInfo

EXAMPLES = DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=None)
ALL = list(table_selection.OPTIONS)


def _app(**config):
    app = Flask(__name__)
    app.config.update(config)
    return app


def _as(*roles):
    g.user = SimpleNamespace(roles=[SimpleNamespace(name=role) for role in roles])


def _asking(name):
    return request_context.bound(CallContext(endpoint="generate_sql", database_id=7, table_selection=name))


def test_a_request_gets_the_option_it_asks_for_only_if_allowed_and_its_user_may_choose():
    with _app(NLSQL_TABLE_SELECTION_ALLOWED=ALL).app_context():
        _as("Admin")
        with _asking("embedding-then-model"):
            assert table_selection.choice().name == "embedding-then-model"
        _as("Gamma")
        with _asking("embedding-then-model"):
            assert table_selection.choice().name == "embedding"
    with _app(NLSQL_TABLE_SELECTION="embedding-reasoning").app_context():
        _as("Admin")
        with _asking("embedding-then-model"):
            assert table_selection.choice().name == "embedding-reasoning"


def test_the_panel_is_offered_a_choice_only_when_there_is_one():
    with _app().app_context():
        _as("Admin")
        assert table_selection.for_panel()["canChoose"] is False
    with _app(NLSQL_TABLE_SELECTION_ALLOWED=["embedding", "embedding-then-model", "nonsense"]).app_context():
        _as("Admin")
        panel = table_selection.for_panel()
        assert panel["canChoose"] is True and [option["name"] for option in panel["options"]] == ["embedding", "embedding-then-model"]


def test_reasoning_is_turned_on_or_off_only_as_configured():
    provider = LlmProvider(model="m", base_url="http://x")
    with _app().app_context():
        assert table_selection.with_reasoning(provider, True) is provider
    with _app(NLSQL_LLM_REASONING_ON={"reasoning": {"enabled": True}}, NLSQL_LLM_REASONING_OFF={"reasoning": {"enabled": False}}).app_context():
        assert table_selection.with_reasoning(provider, True).extra_body == {"reasoning": {"enabled": True}}
        assert table_selection.with_reasoning(provider, False).extra_body == {"reasoning": {"enabled": False}}
        assert table_selection.with_reasoning(provider, None) is provider


def test_the_answer_is_held_to_the_tables_offered_and_their_columns():
    offered = _knowledge(tables=4).tables
    picked = table_selection.parse_selection(
        {
            "tables": [{"table": "sales.table_1", "why": "the sales"}, "table_2", "public.missing"],
            "joins": [
                {"left": "sales.table_1.col_1", "right": "public.table_2.col_0"},
                {"left": "sales.table_1.nope", "right": "public.table_2.col_0"},
                {"left": "sales.table_3.col_1", "right": "public.table_2.col_0"},
            ],
            "reason": "r",
        },
        offered,
        most=12,
    )
    assert [f"{t.schema}.{t.name}" for t in picked.tables] == ["sales.table_1", "public.table_2"]
    assert picked.why == {"sales.table_1": "the sales"}
    assert [(join.left, join.right) for join in picked.joins] == [("sales.table_1.col_1", "public.table_2.col_0")]
    advice = picked.advice(4)
    assert "from the 4 closest" in advice and "- sales.table_1: the sales" in advice


def test_nothing_offered_picked_is_a_failure_with_the_reason():
    picked = table_selection.parse_selection({"tables": ["other.thing"], "reason": "none hold sales"}, _knowledge().tables, 12)
    assert picked.tables == [] and picked.error == "none hold sales"


def _pick(option, *answers, visible=None):
    context = CallContext(endpoint="generate_sql", database_id=7)
    fake = FakeLlm(*answers)
    tables = _knowledge(tables=6).tables
    with _app().app_context(), request_context.bound(context), \
            patch("waxwing.nltosqlandback.table_selection.resolve_llm_provider"), \
            patch("waxwing.nltosqlandback.llm_client.stream_complete", fake), \
            patch.object(nlsql_backend.sql_checks, "current_dialect", return_value=None):
        picked = table_selection.pick(table_selection.OPTIONS[option], "sales by month", tables[:2], visible or tables)
    return picked, fake, context.attributes["tableSelection"]


def test_a_picking_call_that_fails_falls_back_and_says_so():
    picked, _fake, recorded = _pick("embedding-then-model", "not json at all")
    assert picked.tables == [] and picked.error
    assert recorded["fallback"] is True and recorded["offered"] == 2 and recorded["option"] == "embedding-then-model"


def test_with_tools_the_model_may_look_further_among_the_tables_the_user_may_see():
    picked, fake, recorded = _pick(
        "embedding-then-model-tools",
        json.dumps({"look": [{"describe": "sales.table_5"}, {"describe": "secret.table"}]}),
        json.dumps({"tables": ["sales.table_5", "public.table_0"], "joins": []}),
    )
    assert [t.name for t in picked.tables] == ["table_0", "table_5"]
    assert recorded["looks"] == 1 and fake.call_count == 2
    assert "describe sales.table_5:\nsales.table_5" in fake.prompts[1] and "describe secret.table:\n(no such table)" in fake.prompts[1]


def test_without_tools_a_look_is_not_answered():
    picked, fake, recorded = _pick("embedding-then-model", json.dumps({"look": [{"find": "x"}]}))
    assert fake.call_count == 1 and recorded["looks"] == 0 and recorded["fallback"] is True


def test_top_n_caps_the_closest_tables_listed():
    knowledge = _knowledge(tables=8)
    order = [(t.schema, t.name) for t in knowledge.tables]
    text, shown, left_out = schema_knowledge.prompt_text(knowledge, "q", max_chars=1200, rank=lambda: order, max_tables=2)
    assert (shown, left_out) == (2, 6)


@pytest.mark.no_semantic_schema
def test_a_model_s_pick_becomes_the_prompt_with_its_advice():
    schema_knowledge.store.put(_knowledge(tables=8))
    context = CallContext(endpoint="generate_sql", database_id=7, table_selection="embedding-then-model")
    choice = json.dumps({"tables": [{"table": "sales.table_3", "why": "holds it"}], "joins": [], "reason": "r"})
    answer = json.dumps({"status": "Success", "sqlText": "SELECT * FROM sales.table_3", "sqlDetailedDescription": "d"})
    fake = FakeLlm(choice, answer)
    app = _app(NLSQL_TABLE_SELECTION_ALLOWED=ALL, NLSQL_SCHEMA_MAX_CHARS=800)
    try:
        with app.app_context(), request_context.bound(context), \
                patch.object(nlsql_backend.sql_checks, "current_database", return_value=EXAMPLES), \
                patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider"), \
                patch("waxwing.nltosqlandback.table_selection.resolve_llm_provider"), \
                patch("waxwing.nltosqlandback.llm_client.stream_complete", fake):
            _as("Admin")
            run(nlsql_backend.generate_sql("what is in table 3"))
    finally:
        schema_knowledge.store.delete(7, None)
    assert fake.call_count == 2
    sql_prompt = fake.prompts[1]
    assert "the 1 tables and views picked for this request, out of 8" in sql_prompt
    assert "- sales.table_3: holds it" in sql_prompt
    assert "public.table_0" not in sql_prompt
    ranking = context.attributes["tableRanking"]
    assert ranking["strategy"] == "words-then-model" and ranking["option"] == "embedding-then-model"
    assert [stage["stage"] for stage in ranking["stages"]] == ["words", "model"]
    assert ranking["stages"][-1] == {"stage": "model", "listed": 1, "used": [1]}
    assert context.attributes["tableSelection"]["picked"] == 1
