"""The API layer: that a call's progress, its result and how it ended all reach the client as
server-sent events, and that a request rejected before the stream starts is still a plain error.

The real `api` module is exercised, not a stand-in - which means getting past the two things that
normally only exist inside a running Superset: the `@api` decorator (replaced at startup by the
host) and Flask-AppBuilder's auth decorators. Both are neutralised below, before importing the
module under test, since neither has anything to do with what these tests check.
"""

import json
from unittest.mock import patch

import flask_appbuilder.api as fab_api
import pytest
import superset_core.rest_api.decorators as superset_decorators
from flask import Flask

superset_decorators.api = lambda **_kwargs: (lambda cls: cls)
fab_api.protect = lambda *_args, **_kwargs: (lambda handler: handler)
fab_api.safe = lambda handler: handler

from streams import FakeLlm, fake_schema_preamble  # noqa: E402
from waxwing.nltosqlandback import api as extension_api  # noqa: E402
from waxwing.nltosqlandback.cancellation import CancelledError  # noqa: E402

SUCCESS = json.dumps({"status": "Success", "sqlText": "SELECT 1", "sqlDetailedDescription": "desc"})


@pytest.fixture(name="resource")
def _resource():
    with patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider") as resolve:
        resolve.return_value.model = "test-model"
        yield extension_api.NlToSqlAndBackAPI()


def _call(resource, handler_name, body, *responses: str):
    """Runs one endpoint against a fake model and returns the events it streamed back."""
    app = Flask(__name__)
    llm = FakeLlm(*responses)
    with app.test_request_context("/", json=body), \
            patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        response = getattr(resource, handler_name)()
        if response.status_code != 200:
            return response, []
        return response, _events(response.get_data(as_text=True))


def _events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data:"):].strip())
        for line in body.split("\n\n")
        if line.startswith("data:")
    ]


def _of_type(events: list[dict], event_type: str) -> list[dict]:
    return [event for event in events if event["type"] == event_type]


def test_a_call_streams_its_stages_its_text_and_finally_its_result(resource):
    response, events = _call(resource, "generate_sql", {"naturalLanguage": "how many orders?"}, SUCCESS)
    assert response.mimetype == "text/event-stream"
    assert response.headers["X-Accel-Buffering"] == "no"
    assert _of_type(events, "plan")
    assert _of_type(events, "stage")
    assert "".join(
        event["text"] for event in _of_type(events, "delta") if event["field"] == "sqlText"
    ) == "SELECT 1"
    trace = events[-1].pop("trace")
    assert len(trace["traceId"]) == 32 and len(trace["spanId"]) == 16  # what feedback is filed under
    assert events[-1] == {"type": "result", "result": json.loads(SUCCESS) | {
        "tablesUsed": [], "suggestionsForRefinement": [], "assumptionsMade": [],
        "specificClarificationsNeeded": [], "generalClarificationsNeeded": [],
        "failureMessage": "", "whatWasTriedOnFailure": [], "suggestionsForFixingOnFailure": [], "changes": [],
    }}


def test_the_description_endpoints_stream_their_text_and_return_it_under_its_own_key(resource):
    _response, events = _call(resource, "generate_nl", {"sql": "SELECT 1"}, "Counts the rows.")
    assert "".join(event["text"] for event in _of_type(events, "delta")) == "Counts the rows."
    assert events[-1]["result"] == {"naturalLanguage": "Counts the rows."}


def test_a_failure_part_way_through_is_reported_as_an_error_event(resource):
    app = Flask(__name__)

    def explode(*_args, **_kwargs):
        raise RuntimeError("the model is on fire")

    with app.test_request_context("/", json={"naturalLanguage": "x"}), \
            patch("waxwing.nltosqlandback.llm_client.stream_complete", explode), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        response = resource.generate_sql()
        events = _events(response.get_data(as_text=True))
    # A 200 with an error event, not an error status: by the time this is known the response's
    # status line has long since been sent.
    assert response.status_code == 200
    assert events[-1]["type"] == "error"
    assert "the model is on fire" in events[-1]["message"]


def test_a_cancelled_call_says_so_rather_than_looking_like_a_failure(resource):
    app = Flask(__name__)

    def cancelled(*_args, **_kwargs):
        raise CancelledError("The request was cancelled.")

    with app.test_request_context("/", json={"naturalLanguage": "x", "requestId": "r1"}), \
            patch("waxwing.nltosqlandback.llm_client.stream_complete", cancelled), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_preamble", fake_schema_preamble()):
        response = resource.generate_sql()
        events = _events(response.get_data(as_text=True))
    assert events[-1] == {"type": "cancelled"}


def test_the_cancel_token_is_released_once_the_stream_has_been_read(resource):
    from waxwing.nltosqlandback import cancellation

    _response, _events_seen = _call(
        resource, "generate_sql", {"naturalLanguage": "x", "requestId": "r2"}, SUCCESS
    )
    # Nothing is in flight under that id any more, so a late /cancel for it is a miss, not a hit on
    # a token left behind forever.
    assert cancellation.cancel("r2") is False


def test_a_request_rejected_before_the_stream_starts_is_a_plain_error_response(resource):
    response, _events_seen = _call(resource, "generate_sql", {"naturalLanguage": "   "})
    assert response.status_code == 400
    assert response.mimetype == "application/json"


def test_explaining_an_error_needs_both_the_sql_and_at_least_one_message(resource):
    assert _call(resource, "explain_sql_error", {"sql": "", "errors": [{"message": "x"}]})[0].status_code == 400
    assert _call(resource, "explain_sql_error", {"sql": "SELECT 1", "errors": []})[0].status_code == 400
    assert _call(resource, "explain_sql_error", {"sql": "SELECT 1", "errors": [{"message": "  "}]})[0].status_code == 400


def test_explaining_an_error_streams_the_explanation_and_returns_the_fix(resource):
    fix = json.dumps({
        "status": "Fixed",
        "problemSummary": "No such column.",
        "cause": "It is misspelled.",
        "fixExplanation": "Corrects the spelling.",
        "fixedSql": "SELECT customer_name FROM orders",
        "confidence": "High",
    })
    _response, events = _call(
        resource,
        "explain_sql_error",
        {
            "sql": "SELECT customer_nmae FROM orders",
            "errors": [{"message": "no such column: customer_nmae", "line": 1, "source": "sql-validator"}],
            "dialect": "postgresql",
        },
        fix,
    )
    streamed = {
        field: "".join(event["text"] for event in _of_type(events, "delta") if event["field"] == field)
        for field in ("problemSummary", "fixedSql")
    }
    assert streamed["problemSummary"] == "No such column."
    assert streamed["fixedSql"] == "SELECT customer_name FROM orders"
    assert events[-1]["result"]["status"] == "Fixed"
    assert events[-1]["result"]["fixedSql"] == "SELECT customer_name FROM orders"


def test_every_log_line_carries_the_superset_user_and_the_tab_s_database(resource, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="waxwing.nltosqlandback.llm_calls"), \
            patch("waxwing.nltosqlandback.request_context.current_user", return_value=(7, "bob")), \
            patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=None):
        _call(resource, "generate_nl", {"sql": "SELECT 1", "databaseId": "5", "schema": "public", "tabId": "t1",
                                        "requestId": "rq"}, "Counts.")
    lines = {record.getMessage().split(" ", 1)[0]: json.loads(record.getMessage().split(" ", 1)[1])
             for record in caplog.records}
    # The fake model stands in for llm_client, so the request line is the one to check here (the
    # llm_call line itself is covered in test_telemetry).
    request_line = lines["nlsql_request"]
    assert request_line["user.id"] == 7 and request_line["user.name"] == "bob"
    assert request_line["databaseId"] == 5 and "schema" not in request_line  # the tab's schema is ignored
    assert request_line["endpoint"] == "generate_nl" and request_line["outcome"] == "ok"
    assert request_line["tabId"] == "t1"


def test_the_new_endpoints_validate_their_input(resource):
    assert _call(resource, "accelerate", {"sql": " "})[0].status_code == 400
    assert _call(resource, "explain_plan", {"sql": "SELECT 1"})[0].status_code == 400  # no database
    assert _call(resource, "describe_breakdown", {"sql": ""})[0].status_code == 400
    assert _call(resource, "query_plan", {"sql": "SELECT 1"})[0].status_code == 400



def test_validate_sql_answers_with_superset_s_validator_when_there_is_one(resource):
    from waxwing.nltosqlandback.superset_db import DatabaseInfo, ValidationMessage

    database = DatabaseInfo(id=3, name="films", backend="postgresql", dialect="postgres", model=None)
    body = {"sql": "SELECT x FROM t", "databaseId": 3, "tabId": "tab-1"}
    assert _call(resource, "validate_sql", {"sql": "SELECT 1"})[0].status_code == 400  # no database
    with patch("waxwing.nltosqlandback.superset_db.resolve_database", return_value=database):
        found = [ValidationMessage("no such column x", 1, 8)]
        with patch("waxwing.nltosqlandback.superset_db.validate", return_value=found) as validate:
            response, _events = _call(resource, "validate_sql", body)
    assert response.status_code == 200
    assert response.get_json()["result"] == [{"message": "no such column x", "line": 1, "column": 8}]
    assert response.get_json()["checkedBy"] == "validator"
    assert validate.call_args.args[3] is None  # never the tab's schema


def test_validate_sql_parses_the_sql_itself_where_there_is_no_validator(resource):
    from waxwing.nltosqlandback.superset_db import DatabaseInfo

    oracle = DatabaseInfo(id=4, name="hr", backend="oracle", dialect="oracle", model=None)
    unknown = DatabaseInfo(id=5, name="odd", backend="something", dialect=None, model=None)
    broken = {"sql": "SELECT *\nFROM hr.employees\nWHERE (salary > 1\nORDER BY 1", "databaseId": 4}
    with patch("waxwing.nltosqlandback.superset_db.validate", return_value=None):
        with patch("waxwing.nltosqlandback.superset_db.resolve_database", return_value=oracle):
            response, _events = _call(resource, "validate_sql", broken)
            assert response.status_code == 200
            assert response.get_json()["checkedBy"] == "parser"
            [problem] = response.get_json()["result"]
            assert problem["line"] == 4 and problem["column"] == 1 and ")" in problem["message"]
            fine = {"sql": "SELECT level FROM dual CONNECT BY level <= 3", "databaseId": 4}
            assert _call(resource, "validate_sql", fine)[0].get_json()["result"] == []
        with patch("waxwing.nltosqlandback.superset_db.resolve_database", return_value=unknown):
            # No dialect to parse it in: nothing can be checked, said the same way as before.
            assert _call(resource, "validate_sql", {**broken, "databaseId": 5})[0].status_code == 422


def _feedback(resource, body):
    app = Flask(__name__)
    with app.test_request_context("/", json=body), \
            patch("waxwing.nltosqlandback.telemetry.record_feedback") as record:
        response = resource.feedback()
    return response, record


FEEDBACK = {
    "traceId": "ab" * 16, "spanId": "cd" * 8, "feedbackId": "f1", "rating": "down", "target": "sql",
    "reason": "wrong-tables", "label": "Wrong tables or columns", "variant": {"layout": "tabs", "verbosity": "brief"},
}


def test_feedback_is_recorded_against_the_trace_it_names(resource):
    response, record = _feedback(resource, FEEDBACK)
    assert response.status_code == 200
    feedback = record.call_args.args[0]
    assert (feedback.trace_id, feedback.span_id, feedback.rating) == ("ab" * 16, "cd" * 8, "down")
    assert (feedback.reason, feedback.label, feedback.comment) == ("wrong-tables", "Wrong tables or columns", None)
    assert feedback.variant == {"layout": "tabs", "verbosity": "brief"}


def test_feedback_needs_real_trace_ids_a_rating_and_what_it_is_about(resource):
    for broken in (
        {"traceId": "not-hex"},
        {"spanId": "cd" * 4},
        {"rating": "meh"},
        {"target": ""},
        {"feedbackId": None},
    ):
        response, record = _feedback(resource, FEEDBACK | broken)
        assert response.status_code == 400, broken
        record.assert_not_called()


def test_a_long_comment_is_cut_rather_than_refused(resource):
    response, record = _feedback(resource, FEEDBACK | {"comment": "x" * 10_000})
    assert response.status_code == 200
    assert len(record.call_args.args[0].comment) == extension_api.MAX_FEEDBACK_COMMENT
