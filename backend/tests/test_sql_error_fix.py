import json
from unittest.mock import patch

import pytest

from streams import FakeLlm, fake_schema_preamble, final_stage, planned_keys, run, stage_keys, streamed_text
from waxwing.nltosqlandback import sql_error_fix
from waxwing.nltosqlandback.sql_error_fix import SqlError
from waxwing.nltosqlandback.sql_fix_result import STATUS_EXPLAINED, STATUS_FAILURE, STATUS_FIXED

BROKEN_SQL = "SELECT customer_nmae\nFROM orders\nWHERE amount > 100"


def _fixed(sql: str, **extra) -> str:
    return json.dumps({
        "status": "Fixed",
        "problemSummary": "There is no column called customer_nmae.",
        "cause": "It looks like a misspelling of customer_name.",
        "fixExplanation": "Corrects the spelling.",
        "fixedSql": sql,
        "confidence": "High",
        **extra,
    })


@pytest.fixture(name="provider")
def _provider():
    with patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider") as resolve:
        resolve.return_value.model = "test-model"
        yield resolve


def _explain(*responses: str, errors=None, dialect: str = ""):
    llm = FakeLlm(*responses)
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        payload, events = run(
            sql_error_fix.explain_and_fix(
                BROKEN_SQL,
                errors if errors is not None else [SqlError(message='no such column: "customer_nmae"', line=1, column=8, source="sql-validator")],
                dialect,
            )
        )
    return payload, events, llm


def test_a_correction_that_parses_is_offered_as_a_fix(provider):
    payload, events, llm = _explain(_fixed("SELECT customer_name\nFROM orders\nWHERE amount > 100"))
    assert payload["status"] == STATUS_FIXED
    assert payload["fixedSql"].startswith("SELECT customer_name")
    assert payload["problemSummary"]
    assert payload["confidence"] == "High"
    assert llm.call_count == 1
    assert planned_keys(events) == ["read", "tables", "draft", "parse"]
    assert stage_keys(events) == ["read", "tables", "draft", "parse"]


def test_the_explanation_and_the_correction_are_both_streamed_as_they_are_written(provider):
    payload, events, _llm = _explain(_fixed("SELECT customer_name\nFROM orders"))
    assert streamed_text(events, "problemSummary") == payload["problemSummary"]
    assert streamed_text(events, "cause") == payload["cause"]
    assert streamed_text(events, "fixedSql") == payload["fixedSql"]


def test_a_correction_that_does_not_parse_is_redrafted_once(provider):
    payload, events, llm = _explain(_fixed("SELECT customer_name FROM"), _fixed("SELECT customer_name FROM orders"))
    assert payload["status"] == STATUS_FIXED
    assert payload["fixedSql"] == "SELECT customer_name FROM orders"
    assert llm.call_count == 2
    # The retry is a second attempt at the same steps, not two more rows in the plan.
    assert stage_keys(events) == ["read", "tables", "draft", "parse"]
    assert any(
        event["key"] == "draft" and "Second attempt" in (event["detail"] or "")
        for event in events
        if event["type"] == "stage"
    )
    assert final_stage(events, "parse")["status"] == "done"


def test_a_correction_that_still_does_not_parse_keeps_the_explanation_and_drops_the_sql(provider):
    payload, _events, _llm = _explain(_fixed("SELECT customer_name FROM"), _fixed("SELECT FROM WHERE"))
    assert payload["status"] == STATUS_EXPLAINED
    assert payload["fixedSql"] == ""
    assert payload["problemSummary"]
    assert "didn't parse" in payload["fixExplanation"]


def test_an_explained_result_is_passed_through_without_a_parse_check(provider):
    raw = json.dumps({
        "status": "Explained",
        "problemSummary": "The engine cannot compare a date to a number.",
        "cause": "order_date is a DATE and 2024 is an integer.",
        "fixExplanation": "Which date range you meant would decide the fix.",
    })
    payload, events, llm = _explain(raw)
    assert payload["status"] == STATUS_EXPLAINED
    assert payload["fixedSql"] == ""
    assert llm.call_count == 1
    assert "parse" not in stage_keys(events)


def test_a_failure_is_passed_through(provider):
    raw = json.dumps({"status": "Failure", "failureMessage": "That error doesn't relate to this query."})
    payload, _events, llm = _explain(raw)
    assert payload["status"] == STATUS_FAILURE
    assert payload["failureMessage"]
    assert llm.call_count == 1


def test_the_prompt_carries_the_numbered_query_the_error_positions_and_the_offending_line(provider):
    _payload, _events, llm = _explain(_fixed("SELECT customer_name FROM orders"), dialect="PostgreSQL")
    prompt = llm.prompts[0]
    assert "   1 | SELECT customer_nmae" in prompt  # numbered as the validator counts lines
    assert "line 1, column 8" in prompt
    assert "the line it points at: SELECT customer_nmae" in prompt
    assert "PostgreSQL" in prompt
    assert "Database schema:" in prompt


def test_an_error_without_a_position_is_still_usable(provider):
    _payload, _events, llm = _explain(
        _fixed("SELECT 1"),
        errors=[SqlError(message="syntax error at or near FROM", source="query")],
    )
    # Listed with no position at all, rather than with a made-up one.
    assert "- query: syntax error at or near FROM\n" in llm.prompts[0]


def test_the_errors_are_part_of_what_picks_the_tables(provider):
    seen = {}

    def schema_preamble(query_text, _steps):
        seen["query_text"] = query_text
        yield from ()
        return "Database schema:\n\n"

    llm = FakeLlm(_fixed("SELECT 1"))
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", schema_preamble):
        run(sql_error_fix.explain_and_fix(BROKEN_SQL, [SqlError(message="no such column: customer_nmae")]))
    assert "customer_nmae" in seen["query_text"]
    assert "FROM orders" in seen["query_text"]


def test_a_fix_says_what_it_changed_with_the_sql_either_side_of_it(provider):
    payload, _events, llm = _explain(_fixed(
        "SELECT customer_name\nFROM orders\nWHERE amount > 100",
        changes=[{"summary": "Spelt the column right", "why": "customer_nmae doesn't exist",
                  "before": "SELECT customer_nmae", "after": "select customer_name"}],
    ))
    assert payload["changes"] == [{"summary": "Spelt the column right", "why": "customer_nmae doesn't exist",
                                   "before": "SELECT customer_nmae", "after": "SELECT customer_name"}]
    assert "the query in the editor the change replaced" in llm.prompts[0]


def test_an_explanation_without_a_correction_has_no_changes(provider):
    payload, _events, _llm = _explain(json.dumps({
        "status": "Explained", "problemSummary": "p", "cause": "c", "fixExplanation": "f", "fixedSql": "",
        "changes": [{"summary": "x", "before": "SELECT customer_nmae", "after": ""}],
    }))
    assert payload["status"] == STATUS_EXPLAINED
    assert payload["changes"] == []
