from waxwing.nltosqlandback.sql_generation_result import (
    STATUS_CLARIFICATIONS_REQUIRED,
    STATUS_SUCCESS,
    SqlGenerationResult,
)


def test_success_round_trips_through_dict():
    result = SqlGenerationResult(
        status=STATUS_SUCCESS,
        sql_text="SELECT 1",
        sql_detailed_description="Returns the number one.",
        tables_used=["dual"],
        suggestions_for_refinement=["Add a WHERE clause"],
        assumptions_made=["Assumed dual exists"],
    )
    raw = result.to_dict()
    assert raw["status"] == "Success"
    assert raw["sqlText"] == "SELECT 1"
    assert SqlGenerationResult.from_dict(raw) == result


def test_clarifications_round_trip_through_dict():
    result = SqlGenerationResult(
        status=STATUS_CLARIFICATIONS_REQUIRED,
        specific_clarifications_needed=[],
        general_clarifications_needed=["What do you mean by revenue?"],
    )
    raw = {
        "status": "ClarificationsRequired",
        "specificClarificationsNeeded": [{"question": "Which revenue?", "options": ["Gross", "Net"]}],
        "generalClarificationsNeeded": ["What do you mean by revenue?"],
    }
    decoded = SqlGenerationResult.from_dict(raw)
    assert decoded.status == STATUS_CLARIFICATIONS_REQUIRED
    assert decoded.specific_clarifications_needed[0].question == "Which revenue?"
    assert decoded.specific_clarifications_needed[0].options == ["Gross", "Net"]
    assert decoded.general_clarifications_needed == result.general_clarifications_needed


def test_missing_fields_default_to_empty():
    decoded = SqlGenerationResult.from_dict({"status": "Failure"})
    assert decoded.failure_message == ""
    assert decoded.what_was_tried_on_failure == []
    assert decoded.suggestions_for_fixing_on_failure == []
