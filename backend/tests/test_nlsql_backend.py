import json
from unittest.mock import patch

import pytest

from streams import FakeLlm, fake_schema_preamble, final_stage, planned_keys, run, stage_keys, streamed_text
from waxwing.nltosqlandback import nlsql_backend, progress
from waxwing.nltosqlandback.sql_generation_result import STATUS_CLARIFICATIONS_REQUIRED, STATUS_FAILURE, STATUS_SUCCESS


def _success(sql: str) -> str:
    return json.dumps({"status": "Success", "sqlText": sql, "sqlDetailedDescription": "desc"})


@pytest.fixture(name="provider")
def _provider():
    with patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider") as resolve:
        resolve.return_value.model = "test-model"
        yield resolve


def _generate(*responses: str, label: str = "Drafting SQL", reasoning: str = ""):
    """Runs one `_request_sql_generation` with `responses` as the model's successive answers."""
    llm = FakeLlm(*responses, reasoning=reasoning)
    steps = nlsql_backend._sql_steps(label, "Drafted the SQL")
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm):
        result, events = run(nlsql_backend._request_sql_generation("prompt", steps))
    return result, events, llm


def test_valid_sql_is_returned_without_a_retry(provider):
    result, _events, llm = _generate(_success("SELECT 1"))
    assert result.status == STATUS_SUCCESS
    assert result.sql_text == "SELECT 1"
    assert llm.call_count == 1


def test_invalid_sql_is_retried_and_fixed(provider):
    result, _events, llm = _generate(_success("SELECT * FROM"), _success("SELECT 1"))
    assert result.status == STATUS_SUCCESS
    assert result.sql_text == "SELECT 1"
    assert llm.call_count == 2
    # The retry prompt should point out the original invalid SQL and its parse error.
    retry_prompt = llm.prompts[1]
    assert "SELECT * FROM" in retry_prompt
    assert "Problem:" in retry_prompt


def test_invalid_sql_still_invalid_after_retry_reports_failure(provider):
    result, _events, _llm = _generate(_success("SELECT * FROM"), _success("SELECT * FROM WHERE"))
    assert result.status == STATUS_FAILURE
    assert result.what_was_tried_on_failure == ["SELECT * FROM", "SELECT * FROM WHERE"]
    assert result.suggestions_for_fixing_on_failure


def test_clarifications_required_is_returned_as_is_without_sql_validation(provider):
    raw = json.dumps({
        "status": "ClarificationsRequired",
        "generalClarificationsNeeded": ["What do you mean by revenue?"],
    })
    result, events, llm = _generate(raw)
    assert result.status == STATUS_CLARIFICATIONS_REQUIRED
    assert result.general_clarifications_needed == ["What do you mean by revenue?"]
    assert llm.call_count == 1
    assert "parse" not in stage_keys(events)


def test_sql_fence_is_stripped_from_a_successful_result(provider):
    raw = json.dumps({"status": "Success", "sqlText": "```sql\nSELECT 1\n```"})
    result, _events, _llm = _generate(raw)
    assert result.sql_text == "SELECT 1"


def test_json_fence_around_the_whole_response_is_stripped(provider):
    result, _events, _llm = _generate("```json\n" + _success("SELECT 1") + "\n```")
    assert result.status == STATUS_SUCCESS
    assert result.sql_text == "SELECT 1"


def test_the_sql_is_streamed_out_as_the_model_writes_it(provider):
    sql = "SELECT customer_name,\n  SUM(amount)\nFROM orders\nGROUP BY 1"
    _result, events, _llm = _generate(_success(sql))
    # Streamed in pieces, and the pieces add up to exactly the SQL that was generated - no fence,
    # no surrounding JSON, nothing from the fields either side of it.
    assert len([event for event in events if event["type"] == "delta"]) > 1
    assert streamed_text(events, "sqlText") == sql


def test_a_repair_attempt_restarts_the_streamed_sql_rather_than_appending_to_it(provider):
    _result, events, _llm = _generate(_success("SELECT * FROM"), _success("SELECT 1"))
    # What a viewer is left looking at is the SQL that was actually kept, not the two concatenated.
    assert streamed_text(events, "sqlText") == "SELECT 1"


def test_each_step_of_the_call_is_reported_in_order(provider):
    _result, events, _llm = _generate(_success("SELECT 1"), label="Applying your refinement")
    assert stage_keys(events) == ["draft", "summary", "parse"]
    # The step is named for what it did once it's done, not for what it was doing.
    assert final_stage(events, "draft")["label"] == "Drafted the SQL"
    assert final_stage(events, "draft")["status"] == "done"
    assert final_stage(events, "parse")["status"] == "done"
    assert "test-model" in final_stage(events, "summary")["detail"]


def test_each_finished_step_reports_how_long_it_took(provider):
    _result, events, _llm = _generate(_success("SELECT 1"))
    finished = [event for event in events if event["type"] == "stage" and event["status"] == "done"]
    assert finished
    assert all(event["seconds"] is not None for event in finished)


def test_the_summary_step_starts_when_the_model_moves_on_from_the_sql(provider):
    # The model writes `sqlDetailedDescription` straight after `sqlText`, so that field starting is
    # what ends the drafting step - the one handover the call can actually see.
    _result, events, _llm = _generate(_success("SELECT 1"))
    order = [(event["key"], event["status"]) for event in events if event["type"] == "stage"]
    assert order.index(("draft", "done")) < order.index(("summary", "active"))
    assert streamed_text(events, "sqlDetailedDescription") == "desc"


def test_a_models_thinking_is_streamed_while_it_thinks_and_kept_out_of_the_answer(provider):
    result, events, _llm = _generate(_success("SELECT 1"), reasoning="The users table has has_2fa.")
    # The whole of the thinking reaches the panel...
    assert streamed_text(events, progress.REASONING_FIELD) == "The users table has has_2fa."
    # ...the step says so while it's happening...
    thinking = [
        event for event in events
        if event["type"] == "stage" and event["key"] == "draft" and "thinking" in (event["detail"] or "")
    ]
    assert thinking
    # ...and none of it ends up in the JSON that was parsed.
    assert result.sql_text == "SELECT 1"
    assert "has_2fa" not in result.sql_text


def test_a_parse_failure_is_reported_as_a_second_attempt_at_the_same_step(provider):
    _result, events, _llm = _generate(_success("SELECT * FROM"), _success("SELECT 1"))
    # No extra rows in the plan for the retry - the same steps run again, and the drafting step says
    # which attempt this is.
    assert stage_keys(events) == ["draft", "summary", "parse"]
    assert any(
        event["key"] == "draft" and "Second attempt" in (event["detail"] or "")
        for event in events
        if event["type"] == "stage"
    )
    parse_outcomes = [
        event["status"] for event in events if event["type"] == "stage" and event["key"] == "parse"
    ]
    assert "failed" in parse_outcomes
    assert parse_outcomes[-1] == "done"


def test_generate_sql_reports_which_tables_it_used_and_returns_the_result_payload(provider):
    llm = FakeLlm(_success("SELECT 1"))
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        payload, events = run(nlsql_backend.generate_sql("how many orders?"))
    assert payload["status"] == STATUS_SUCCESS
    assert payload["sqlText"] == "SELECT 1"
    # The whole plan is announced before any of it runs, so the panel can show what is still to come.
    assert planned_keys(events) == ["read", "tables", "draft", "summary", "parse"]
    assert stage_keys(events)[0] == "read"
    assert "how many orders?" in (final_stage(events, "read")["detail"] or "")
    assert "Database schema:" in llm.prompts[0]
    assert "how many orders?" in llm.prompts[0]


def test_generate_nl_streams_the_description_and_returns_it_stripped(provider):
    llm = FakeLlm("  Counts the orders placed last quarter.\n")
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        payload, events = run(nlsql_backend.generate_nl("SELECT 1"))
    assert payload == {"naturalLanguage": "Counts the orders placed last quarter."}
    # The whole completion is the answer here, so it streams verbatim - whitespace and all.
    assert streamed_text(events, "naturalLanguage") == "  Counts the orders placed last quarter.\n"
    # Nothing to parse and no summary of its own, so this flow says so up front.
    assert planned_keys(events) == ["read", "tables", "draft"]


def test_sql_the_database_refuses_to_plan_is_retried_with_its_complaint(provider):
    from waxwing.nltosqlandback.superset_db import DatabaseInfo, PlanOutcome

    database = DatabaseInfo(id=1, name="examples", backend="postgresql", dialect="postgres", model=None)

    def explain(_database, sql, _catalog, _schema):
        if "nope" in sql:
            return PlanOutcome(error='relation "nope" does not exist', checked=True)
        return PlanOutcome(plan=None, error="fine but unread", checked=False)

    with patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=database), \
            patch("waxwing.nltosqlandback.sql_checks._explain_enabled", return_value=True), \
            patch("waxwing.nltosqlandback.superset_db.validate", return_value=None), \
            patch("waxwing.nltosqlandback.superset_db.explain", explain):
        result, events, llm = _generate(_success("SELECT * FROM nope"), _success("SELECT 1"))
    assert result.status == STATUS_SUCCESS and result.sql_text == "SELECT 1"
    assert 'relation "nope" does not exist' in llm.prompts[1]
    assert "parsed as postgres" in final_stage(events, "parse")["detail"]


def test_sql_superset_s_validator_rejects_is_retried(provider):
    from waxwing.nltosqlandback.superset_db import DatabaseInfo, ValidationMessage

    database = DatabaseInfo(id=1, name="examples", backend="postgresql", dialect="postgres", model=None)
    verdicts = iter([[ValidationMessage("syntax error at or near FORM", line=1)], []])
    with patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=database), \
            patch("waxwing.nltosqlandback.sql_checks._explain_enabled", return_value=False), \
            patch("waxwing.nltosqlandback.superset_db.validate", side_effect=lambda *_a: next(verdicts)):
        result, _events, llm = _generate(_success("SELECT 1"), _success("SELECT 2"))
    assert result.sql_text == "SELECT 2"
    assert "line 1: syntax error at or near FORM" in llm.prompts[1]


@pytest.mark.no_semantic_schema
def test_without_a_semantic_schema_there_is_no_tables_step_and_no_embedding(provider):
    llm = FakeLlm("Counts the orders.")
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend._get_schema_filter") as schema_filter:
        result, events = run(nlsql_backend.generate_nl("SELECT count(*) FROM orders"))
    schema_filter.assert_not_called()
    assert planned_keys(events) == ["read", "draft"]
    assert "Database schema" not in llm.prompts[0]
    assert result == {"naturalLanguage": "Counts the orders."}


def _run_with(llm, flow):
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        payload, _events = run(flow)
    return payload


def _changed(sql: str, changes: list) -> str:
    return json.dumps({"status": "Success", "sqlText": sql, "sqlDetailedDescription": "desc", "changes": changes})


def test_a_refinement_says_what_it_changed_with_the_sql_either_side_found_in_the_two_queries(provider):
    llm = FakeLlm(_changed("SELECT a FROM t WHERE a > 1 ORDER BY a LIMIT 5", [
        {"summary": "Kept the top 5", "why": "The instruction asks for five", "before": "", "after": "order by a limit 5"},
        {"summary": "Imagined", "why": "Not in either query", "before": "GROUP BY b", "after": "HAVING 1"},
    ]))
    payload = _run_with(llm, nlsql_backend.refine_sql("only the top 5", "SELECT a FROM t WHERE a > 1"))
    assert payload["changes"] == [
        {"summary": "Kept the top 5", "why": "The instruction asks for five", "before": "", "after": "ORDER BY a LIMIT 5"},
        {"summary": "Imagined", "why": "Not in either query", "before": "", "after": ""},
    ]
    assert '"changes": [{"summary"' in llm.prompts[0] and "the query in the editor the change replaced" in llm.prompts[0]


def test_an_update_places_its_changes_against_the_sql_it_was_grounded_in(provider):
    llm = FakeLlm(_changed("SELECT region, SUM(total) FROM orders GROUP BY region", [
        {"summary": "By region", "why": "The request now asks per region",
         "before": "select sum(total) from orders", "after": "SELECT region, SUM(total) FROM orders GROUP BY region"},
    ]))
    payload = _run_with(llm, nlsql_backend.update_sql_from_nl(
        "total sales", "SELECT SUM(total) FROM orders", "total sales by region"))
    assert payload["changes"][0]["before"] == "SELECT SUM(total) FROM orders"
    assert payload["changes"][0]["after"] == "SELECT region, SUM(total) FROM orders GROUP BY region"
    assert "the previous SQL the change replaced" in llm.prompts[0]


def test_sql_written_from_scratch_has_no_changes_and_isnt_asked_for_any(provider):
    llm = FakeLlm(_changed("SELECT 1", [{"summary": "Volunteered", "why": "", "before": "", "after": "SELECT 1"}]))
    payload = _run_with(llm, nlsql_backend.generate_sql("one"))
    assert payload["changes"] == []
    assert '"changes"' not in llm.prompts[0]


def test_no_changes_are_offered_without_sql_to_put_them_in(provider):
    llm = FakeLlm(json.dumps({"status": "Failure", "failureMessage": "no", "changes": [{"summary": "x", "after": "y"}]}))
    payload = _run_with(llm, nlsql_backend.refine_sql("do the impossible", "SELECT a FROM t"))
    assert payload["status"] == STATUS_FAILURE
    assert payload["changes"] == []


@pytest.mark.formats_sql
def test_changes_are_found_in_the_sql_as_laid_out_for_the_editor(provider):
    llm = FakeLlm(_changed("select a, count(*) from s.t group by a", [
        {"summary": "Counted per a", "why": "Asked for counts", "before": "select a from s.t",
         "after": "select a, count(*) from s.t group by a"},
    ]))
    payload = _run_with(llm, nlsql_backend.refine_sql("count them per a", "select a from s.t"))
    assert payload["sqlText"] == "SELECT\n  a,\n  COUNT(*)\nFROM s.t\nGROUP BY\n  a"
    assert payload["changes"][0]["after"] == payload["sqlText"]
