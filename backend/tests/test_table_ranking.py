"""How deep in each ordered table list the tables an answer used were: ranks only, per stage."""

import json
from unittest.mock import patch

import pytest
from flask import Flask

from streams import FakeLlm, run
from test_db_interrogation import _knowledge
from waxwing.nltosqlandback import nlsql_backend, request_context, schema_knowledge, table_ranking
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.superset_db import DatabaseInfo

EXAMPLES = DatabaseInfo(id=7, name="examples", backend="postgresql", dialect="postgres", model=None)


def _thirty_then_eight():
    """The ticket's example: 30 tables by embedding, of which the 2nd, 3rd, 5th... went in the prompt."""
    listing = table_ranking.Listing(strategy=table_ranking.EMBEDDING)
    ranked = [("s", f"t{rank}") for rank in range(1, 31)]
    listing.add("embedding", ranked)
    listing.add("prompt", [ranked[rank - 1] for rank in (2, 3, 5, 6, 7, 8, 12, 14)])
    return listing


def test_each_stage_records_the_ranks_the_next_kept_and_the_last_the_ranks_used():
    sql = "SELECT * FROM s.t2 JOIN s.t3 ON t2.id = t3.id JOIN s.t6 ON t3.id = t6.id"
    assert table_ranking.record(_thirty_then_eight(), sql, "postgres", "unknown") == {
        "strategy": "embedding",
        "stages": [
            {"stage": "embedding", "listed": 30, "kept": [2, 3, 5, 6, 7, 8, 12, 14]},
            {"stage": "prompt", "listed": 8, "used": [1, 2, 4]},
        ],
        "usedNotListed": 0,
        "outcome": "unknown",
    }


def test_a_table_no_list_offered_is_counted_and_an_unqualified_one_found_by_name():
    sql = "WITH recent AS (SELECT * FROM t5) SELECT * FROM recent JOIN s.t99 ON 1 = 1"
    ranking = table_ranking.record(_thirty_then_eight(), sql, "postgres", "unknown")
    assert ranking["stages"][-1]["used"] == [3]
    assert ranking["usedNotListed"] == 1


def test_the_record_names_no_table():
    ranking = table_ranking.record(_thirty_then_eight(), "SELECT * FROM s.t2", "postgres", "unknown")
    assert "t2" not in json.dumps(ranking)


def test_no_sql_records_the_lists_and_a_failure():
    ranking = table_ranking.record(_thirty_then_eight(), "", "postgres", "failed")
    assert ranking["stages"][-1]["used"] == [] and ranking["outcome"] == "failed"


def _listing(knowledge, max_chars=schema_knowledge.DEFAULT_MAX_PROMPT_CHARS, rank=None):
    listing = table_ranking.Listing()
    schema_knowledge.prompt_text(knowledge, "what is in table_2", max_chars=max_chars, rank=rank, listing=listing)
    return listing


def test_a_prompt_with_every_table_lists_them_in_one_stage():
    listing = _listing(_knowledge(tables=4))
    assert listing.strategy == table_ranking.WHOLE
    assert [(stage, len(tables)) for stage, tables in listing.stages] == [("prompt", 4)]


def test_a_prompt_cut_down_by_embedding_lists_the_ranking_then_what_fitted():
    knowledge = _knowledge(tables=4, described=True)
    order = [("sales", "table_3"), ("public", "table_0"), ("sales", "table_1"), ("public", "table_2")]
    listing = _listing(knowledge, max_chars=len(schema_knowledge._header(knowledge, knowledge.tables)) + 300, rank=lambda: order)
    assert listing.strategy == table_ranking.EMBEDDING
    (first, ranked), (last, listed) = listing.stages
    assert (first, ranked, last) == ("embedding", order, "prompt")
    assert 0 < len(listed) < 4 and listed == order[:len(listed)]


def test_a_prompt_cut_down_by_words_lists_the_word_ranking_then_what_fitted():
    knowledge = _knowledge(tables=4)
    head = len(schema_knowledge._header(knowledge, knowledge.tables)) + len("\n(Column types and indexes left out for space.)")
    listing = _listing(knowledge, max_chars=head + 150, rank=lambda: None)
    assert listing.strategy == table_ranking.WORDS
    assert [stage for stage, _ in listing.stages] == ["words", "prompt"]


@pytest.mark.no_semantic_schema
def test_an_answer_records_its_table_ranking_on_the_request():
    schema_knowledge.store.put(_knowledge())
    context = CallContext(endpoint="generate_sql", database_id=7)
    answer = json.dumps({"status": "Success", "sqlText": "SELECT * FROM sales.table_1", "sqlDetailedDescription": "d"})
    try:
        with Flask(__name__).app_context(), request_context.bound(context), \
                patch.object(nlsql_backend.sql_checks, "current_database", return_value=EXAMPLES), \
                patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider"), \
                patch("waxwing.nltosqlandback.llm_client.stream_complete", FakeLlm(answer)):
            run(nlsql_backend.generate_sql("what is in table 1"))
    finally:
        schema_knowledge.store.delete(7, None)
    assert context.attributes["tableRanking"] == {
        "strategy": "whole",
        "stages": [{"stage": "prompt", "listed": 3, "used": [3]}],  # kept by schema: public.table_0, public.table_2, sales.table_1
        "usedNotListed": 0,
        "outcome": "unknown",
        "option": "embedding",
    }
