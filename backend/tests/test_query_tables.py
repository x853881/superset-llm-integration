"""The tables a query reads and the facts drawn from them: smaller describe prompts, the breakdown's
per-section tables and hints, its sections streamed one at a time - and the trace names and the
validator call that came with them."""

import json
import sys
import types
from unittest.mock import patch

import pytest
from flask import Flask

from streams import FakeLlm, events_of, run
from waxwing.nltosqlandback import nlsql_backend, query_tables, request_context, schema_knowledge, sql_breakdown, superset_db, telemetry
from waxwing.nltosqlandback.request_context import CallContext
from waxwing.nltosqlandback.semantic_schema import ColumnDescription, TableDescription
from waxwing.nltosqlandback.streaming_json import complete_array_items
from waxwing.nltosqlandback.superset_db import DatabaseInfo

DATABASE = DatabaseInfo(id=3, name="films", backend="postgresql", dialect="postgres", model=None)

SQL = """WITH rated AS (
  SELECT movie_id, AVG(rating) AS score FROM movie_data.ratings WHERE user_id = 5 GROUP BY movie_id
)
SELECT m.title
FROM movie_data.movies m
JOIN rated r ON r.movie_id = m.id
WHERE m.year > 2000"""


def _table(schema, name, columns, rows, indexes=(), pk="id"):
    return TableDescription(
        schema=schema,
        name=name,
        columns=[ColumnDescription(name=column, data_type="INTEGER", is_primary_key=column == pk) for column in columns],
        row_count_estimate=rows,
        indexes=list(indexes),
    )


TABLES = [
    _table("movie_data", "ratings", ["movie_id", "user_id", "rating"], 25_000_000, [(("movie_id",), False)], pk=None),
    _table("movie_data", "movies", ["id", "title", "year"], 40_000),
    _table("crime_data", "incidents", ["id", "kind"], 9_000_000),
    _table("crime_data", "movies", ["id"], 10),
]


def _knowledge(tables=TABLES):
    return schema_knowledge.DatabaseKnowledge(database_id=3, database_name="films", catalog=None, tables=list(tables))


def test_the_tables_a_query_reads_leave_out_its_ctes():
    assert query_tables.tables_used(SQL, "postgres") == [("movie_data", "ratings"), ("movie_data", "movies")]
    assert query_tables.tables_used("select * from movies", "postgres") == [(None, "movies")]
    assert query_tables.tables_used("this is not sql (", "postgres") == []


def test_an_unqualified_name_matches_it_in_every_schema():
    matched = query_tables.match_tables([(None, "movies")], TABLES)
    assert sorted(f"{t.schema}.{t.name}" for t in matched) == ["crime_data.movies", "movie_data.movies"]
    assert [t.schema for t in query_tables.match_tables([("movie_data", "movies")], TABLES)] == ["movie_data"]


def test_each_section_gets_its_tables_and_a_hint_for_a_big_table_read_on_an_unindexed_column():
    facts = query_tables.section_facts(SQL, "postgres", [(1, 3), (4, 7)], TABLES)
    assert [t["name"] for t in facts[0]["tables"]] == ["ratings"]
    assert facts[0]["tables"][0]["rows"] == 25_000_000
    assert [t["name"] for t in facts[1]["tables"]] == ["movies"]
    assert len(facts[0]["hints"]) == 1 and "ratings" in facts[0]["hints"][0] and "user_id" in facts[0]["hints"][0]
    # movies is small, and joined on its primary key anyway
    assert facts[1]["hints"] == []


def test_no_hints_for_a_database_without_indexes():
    unindexed = [_table("movie_data", "ratings", ["movie_id", "user_id"], 25_000_000, pk=None)]
    facts = query_tables.section_facts(SQL, "postgres", [(1, 3)], unindexed)
    assert facts[0]["tables"] and facts[0]["hints"] == []


def test_a_column_an_index_starts_with_gets_no_hint():
    sql = "SELECT 1 FROM movie_data.ratings WHERE movie_id = 3"
    assert query_tables.section_facts(sql, "postgres", [(1, 1)], TABLES)[0]["hints"] == []


def test_finished_array_items_are_read_out_of_a_response_still_being_written():
    buffer = '{"summary": "x", "sections": [{"title": "a ]}", "n": [1]}, {"title": "b'
    assert complete_array_items(buffer, "sections") == [{"title": "a ]}", "n": [1]}]
    assert complete_array_items(buffer + '"}]}', "sections") == [{"title": "a ]}", "n": [1]}, {"title": "b"}]
    assert complete_array_items('{"summary": "x"', "sections") == []


@pytest.fixture(name="known_database")
def _known_database():
    with patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider") as resolve, \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_enabled", return_value=False), \
            patch("waxwing.nltosqlandback.nlsql_backend.interrogated_schema", return_value=_knowledge()), \
            patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=DATABASE), \
            patch("waxwing.nltosqlandback.superset_db.accessible_tables", side_effect=lambda _d, _c, tables: tables):
        resolve.return_value.model = "test-model"
        yield


def test_the_breakdown_prompt_has_only_the_tables_the_query_reads(known_database):
    answer = json.dumps({"summary": "Top films.", "sections": [
        {"title": "rated (CTE)", "kind": "cte", "startLine": 1, "endLine": 3, "description": "Scores films."},
        {"title": "Final query", "kind": "select", "startLine": 4, "endLine": 7, "description": "Lists them."},
    ]})
    llm = FakeLlm(answer)
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm):
        result, events = run(sql_breakdown.describe_breakdown(SQL))
    prompt = llm.prompts[0]
    assert "movie_data.ratings" in prompt and "movie_data.movies" in prompt
    assert "crime_data.incidents" not in prompt
    assert "the 2 tables and views this query reads" in prompt
    assert result["sections"][0]["tables"][0]["name"] == "ratings"
    assert result["sections"][0]["hints"]

    partials = events_of(events, "partial")
    assert partials[0] == {"type": "partial", "field": "sql", "value": {"sql": SQL, "formatted": False}}
    streamed = [event["value"] for event in partials if event["field"] == "sections"]
    # one event per finished section, each the whole list so far
    assert [len(sections) for sections in streamed] == [1, 2]
    assert streamed[0][0]["title"] == "rated (CTE)" and streamed[0][0]["tables"]


def test_a_query_whose_tables_were_never_interrogated_gets_the_whole_database(known_database):
    steps = sql_breakdown.progress.Steps([("tables", "Finding", "Found")])
    text, _events = run(nlsql_backend.query_schema_preamble("SELECT * FROM nowhere.at_all", steps))
    assert "crime_data.incidents" in text and "movie_data.ratings" in text


def test_model_spans_are_named_apart_from_the_request_they_belong_to():
    record = telemetry.LlmCallRecord(
        operation="chat", request_model="m", base_url="x", start_ns=0, end_ns=1, outcome="ok", purpose="describe_breakdown",
    )
    assert telemetry.span_name_for_call(record) == "llm · describe_breakdown"
    record.attempt = 2
    assert telemetry.span_name_for_call(record) == "llm · describe_breakdown (attempt 2)"
    record.operation = "embeddings"
    record.attempt = 1
    assert telemetry.span_name_for_call(record) == "embedding · describe_breakdown"


class _Annotation:
    def __init__(self, message, line_number):
        self.message = message
        self.line_number = line_number
        self.start_column = 1


def _with_validator(validator):
    module = types.ModuleType("superset.sql_validators")
    module.get_validator_by_name = lambda _name: validator
    return patch.dict(sys.modules, {"superset.sql_validators": module})


def test_superset_s_validator_is_traced():
    validator = types.SimpleNamespace(validate=lambda *_args: [_Annotation("no such column x", 2)])
    app = Flask(__name__)
    app.config["SQL_VALIDATORS_BY_ENGINE"] = {"postgresql": "PostgreSQLValidator"}
    with app.app_context(), _with_validator(validator), patch.object(telemetry, "record_tool_call") as record:
        messages = superset_db.validate(DATABASE, "SELECT\nx FROM t", None, None)
    assert [(m.message, m.line) for m in messages] == [("no such column x", 2)]
    name, *_ = record.call_args.args
    assert name == superset_db.VALIDATE_SPAN == "db · validate"
    assert record.call_args.kwargs["output_text"] == "line 2: no such column x"
    assert record.call_args.kwargs["attributes"]["validator"] == "PostgreSQLValidator"


def test_no_validator_configured_means_no_verdict_and_no_span():
    app = Flask(__name__)
    with app.app_context(), patch.object(telemetry, "record_tool_call") as record:
        assert superset_db.validate(DATABASE, "SELECT 1", None, None) is None
    record.assert_not_called()


def test_the_query_plan_span_is_named_for_what_it_asks():
    assert superset_db.EXPLAIN_SPAN == "db · explain"


def test_validate_runs_inside_the_request_it_was_asked_for():
    seen = []
    validator = types.SimpleNamespace(validate=lambda *_args: seen.append(request_context.current()) or [])
    app = Flask(__name__)
    app.config["SQL_VALIDATORS_BY_ENGINE"] = {"postgresql": "v"}
    context = CallContext(endpoint="validate_sql", database_id=3)
    with app.app_context(), _with_validator(validator), request_context.bound(context), \
            patch.object(telemetry, "_send_span"):
        assert superset_db.validate(DATABASE, "SELECT 1", None, None) == []
    assert seen == [context]
