"""Accelerate, explain-plan and the section breakdown, against a fake model and a fake database."""

import json
from unittest.mock import patch

import pytest

from streams import FakeLlm, final_stage, planned_keys, run, streamed_text
from waxwing.nltosqlandback import performance, sql_breakdown
from waxwing.nltosqlandback.query_plan import PlanWarning, QueryPlan
from waxwing.nltosqlandback.superset_db import DatabaseInfo, PlanOutcome

DATABASE = DatabaseInfo(id=1, name="examples", backend="postgresql", dialect="postgres", model=None)
ORIGINAL = "SELECT a FROM t WHERE a IN (SELECT b FROM u)"
FASTER = "SELECT t.a FROM t JOIN (SELECT DISTINCT b FROM u) u ON u.b = t.a"


def _plan(cost, warnings=()):
    return QueryPlan(backend="postgresql", raw="", root=None, outline=f"Seq Scan on t  (cost {cost})", total_cost=cost,
                     warnings=list(warnings))


def _rewrite(sql=FASTER, explanation="Joins instead of a subquery."):
    return json.dumps({"rewrittenSql": sql, "explanation": explanation, "changes": ["subquery to join"]})


@pytest.fixture(name="provider")
def _provider():
    with patch("waxwing.nltosqlandback.nlsql_backend.resolve_llm_provider") as resolve:
        resolve.return_value.model = "test-model"
        yield resolve


def _accelerate(*responses, run_plan=True, database=DATABASE, plans=None):
    """Runs accelerate with `responses` as the model's answers and `plans` as what the database says
    about each query it is asked to plan, keyed by SQL."""
    plans = plans or {}
    llm = FakeLlm(*responses)

    def explain(_database, sql, _catalog, _schema):
        return plans.get(sql, PlanOutcome(error="not in the fake", checked=False))

    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=database), \
            patch("waxwing.nltosqlandback.superset_db.explain", explain):
        result, events = run(performance.accelerate(ORIGINAL, run_plan))
    return result, events, llm


def test_accelerate_plans_first_gives_the_model_the_plan_and_checks_the_rewrite(provider):
    flagged = _plan(1000, [PlanWarning("warning", "large_seq_scan", "Full scan of t")])
    result, events, llm = _accelerate(
        _rewrite(),
        plans={ORIGINAL: PlanOutcome(plan=flagged, checked=True), FASTER: PlanOutcome(plan=_plan(200), checked=True)},
    )
    assert planned_keys(events) == ["read", "plan", "draft", "check"]
    assert "Seq Scan on t" in llm.prompts[0] and "Full scan of t" in llm.prompts[0]
    assert "PostgreSQL" in llm.prompts[0]
    assert streamed_text(events, "rewrittenSql") == FASTER
    assert result["rewrittenSql"] == FASTER
    assert result["basedOn"] == "plan"
    assert result["plan"]["totalCost"] == 1000
    assert result["check"]["verdict"] == "Unverified"
    assert result["check"]["rewrittenCost"] == 200
    assert final_stage(events, "check")["status"] == "done"


def test_without_run_plan_the_model_gets_the_sql_alone(provider):
    result, events, llm = _accelerate(
        _rewrite(), run_plan=False, plans={FASTER: PlanOutcome(plan=_plan(200), checked=True)}
    )
    assert planned_keys(events) == ["read", "draft", "check"]
    assert "work from the SQL alone" in llm.prompts[0]
    assert result["basedOn"] == "sql" and result["plan"] is None


def test_a_plan_the_database_refuses_is_reported_and_accelerate_carries_on(provider):
    result, events, _llm = _accelerate(
        _rewrite(), plans={ORIGINAL: PlanOutcome(error="permission denied for table t", checked=True)}
    )
    assert final_stage(events, "plan")["status"] == "failed"
    assert result["planError"] == "permission denied for table t"
    assert result["rewrittenSql"] == FASTER


def test_a_rewrite_the_database_rejects_is_redrafted_once_with_the_reason(provider):
    broken = "SELECT t.a FROM t JOIN nope ON nope.b = t.a"
    result, _events, llm = _accelerate(
        _rewrite(broken), _rewrite(),
        plans={
            ORIGINAL: PlanOutcome(plan=_plan(1000), checked=True),
            broken: PlanOutcome(error='relation "nope" does not exist', checked=True),
            FASTER: PlanOutcome(plan=_plan(300), checked=True),
        },
    )
    assert llm.call_count == 2
    assert 'relation "nope" does not exist' in llm.prompts[1]
    assert result["rewrittenSql"] == FASTER


def test_a_rewrite_still_invalid_after_the_retry_is_not_offered(provider):
    broken = "SELECT t.a FROM t JOIN nope ON nope.b = t.a"
    result, events, _llm = _accelerate(
        _rewrite(broken), _rewrite(broken),
        plans={
            ORIGINAL: PlanOutcome(plan=_plan(1000), checked=True),
            broken: PlanOutcome(error='relation "nope" does not exist', checked=True),
        },
    )
    assert result["rewrittenSql"] == ""
    assert result["rejectedSql"] == broken
    assert result["check"]["verdict"] == "Invalid"
    assert final_stage(events, "check")["status"] == "failed"


def test_each_change_comes_back_with_the_sql_either_side_of_it_found_in_the_two_queries(provider):
    rewrite = json.dumps({
        "rewrittenSql": FASTER,
        "explanation": "Joins instead of a subquery.",
        "changes": [
            {"summary": "IN subquery to a join", "why": "The subquery is read once",
             "before": "a in (select b from u)", "after": "JOIN (SELECT DISTINCT b FROM u) u ON u.b = t.a"},
            {"summary": "Invented", "why": "Not in either query", "before": "ORDER BY z", "after": "LIMIT 9"},
        ],
    })
    with patch("waxwing.nltosqlandback.request_context.current") as current:
        current.return_value.attributes = {}
        result, _events, llm = _accelerate(rewrite, run_plan=False, plans={FASTER: PlanOutcome(plan=_plan(200), checked=True)})
        attributes = current.return_value.attributes
    assert result["changes"] == [
        {"summary": "IN subquery to a join", "why": "The subquery is read once",
         "before": "a IN (SELECT b FROM u)", "after": "JOIN (SELECT DISTINCT b FROM u) u ON u.b = t.a"},
        {"summary": "Invented", "why": "Not in either query", "before": "", "after": ""},
    ]
    assert attributes["changes"] == 2 and attributes["changesAnchored"] == 1
    assert '"before": "..."' in llm.prompts[0] and "copied from it exactly" in llm.prompts[0]


def test_an_unchanged_query_says_so(provider):
    result, _events, _llm = _accelerate(_rewrite(ORIGINAL, "Already efficient."), run_plan=False)
    assert result["check"]["verdict"] == "Unchanged"
    assert result["explanation"] == "Already efficient."


def test_explain_plan_needs_a_plan_and_streams_its_explanation(provider):
    llm = FakeLlm("The engine scans t in full.")
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=DATABASE), \
            patch("waxwing.nltosqlandback.superset_db.explain", return_value=PlanOutcome(plan=_plan(10), checked=True)):
        result, events = run(performance.explain_plan(ORIGINAL))
    assert streamed_text(events, "explanation") == "The engine scans t in full."
    assert result["explanation"] == "The engine scans t in full."
    assert "Seq Scan on t" in llm.prompts[0]


def test_explain_plan_without_a_database_fails_rather_than_guessing(provider):
    with patch("waxwing.nltosqlandback.sql_checks.current_database", return_value=None):
        with pytest.raises(RuntimeError):
            run(performance.explain_plan(ORIGINAL))


def test_the_breakdown_numbers_the_query_and_returns_sections_safe_to_draw(provider):
    sql = "SELECT a,\n  b\nFROM t\nWHERE a > 1\nORDER BY a"
    answer = json.dumps({
        "summary": "Lists a and b.",
        "sections": [
            {"title": "Filter", "kind": "filter", "startLine": 4, "endLine": 4, "description": "Only big a."},
            {"title": "Columns", "kind": "select", "startLine": 1, "endLine": 2, "description": "a and b."},
            {"title": "Way off", "kind": "other", "startLine": 40, "endLine": 50, "description": "Nonsense."},
            {"title": "Backwards", "kind": "weird", "startLine": 5, "endLine": 3, "description": "Reversed."},
        ],
    })
    llm = FakeLlm(answer)
    with patch("waxwing.nltosqlandback.llm_client.stream_complete", llm), \
            patch("waxwing.nltosqlandback.nlsql_backend.schema_enabled", return_value=False):
        result, events = run(sql_breakdown.describe_breakdown(sql))
    assert "   4 | WHERE a > 1" in llm.prompts[0]
    assert planned_keys(events) == ["read", "draft"]
    assert streamed_text(events, "summary") == "Lists a and b."
    assert [(s["title"], s["startLine"], s["endLine"], s["kind"]) for s in result["sections"]] == [
        ("Columns", 1, 2, "select"),
        ("Backwards", 3, 5, "other"),
        ("Filter", 4, 4, "filter"),
    ]
