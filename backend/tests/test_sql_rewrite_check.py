"""The checks an accelerate rewrite goes through before it is shown."""

from waxwing.nltosqlandback.query_plan import PlanWarning, QueryPlan
from waxwing.nltosqlandback.sql_rewrite_check import (
    VERDICT_INVALID,
    VERDICT_MAY_DIFFER,
    VERDICT_UNCHANGED,
    VERDICT_UNVERIFIED,
    check_rewrite,
)
from waxwing.nltosqlandback.superset_db import PlanOutcome


def _plan(cost, warnings=()):
    return QueryPlan(backend="postgresql", raw="", root=None, outline="", total_cost=cost, warnings=list(warnings))


def _planner(outcome):
    return lambda _sql: outcome


def test_whitespace_case_and_comments_are_unchanged():
    check = check_rewrite("select a from t;", "SELECT a\n  FROM t -- same", "postgres", None, None)
    assert check.verdict == VERDICT_UNCHANGED


def test_a_rewrite_the_database_rejects_is_invalid_when_the_original_planned():
    check = check_rewrite(
        "SELECT a FROM t", "SELECT a FROM nope", "postgres", _plan(10),
        _planner(PlanOutcome(error='relation "nope" does not exist', checked=True)),
    )
    assert check.verdict == VERDICT_INVALID
    assert "nope" in check.findings[0].message


def test_without_a_database_a_rewrite_that_does_not_parse_is_invalid():
    check = check_rewrite("SELECT a FROM t", "SELECT a FROM WHERE", "postgres", None, None)
    assert check.verdict == VERDICT_INVALID


def test_changing_the_output_columns_may_change_the_result():
    check = check_rewrite("SELECT a, b FROM t", "SELECT a FROM t", "postgres", None, None)
    assert check.verdict == VERDICT_MAY_DIFFER
    assert any("1 columns instead of 2" in finding.message for finding in check.findings)


def test_dropping_distinct_limit_or_order_is_reported():
    check = check_rewrite(
        "SELECT DISTINCT a FROM t ORDER BY a LIMIT 10", "SELECT a FROM t", "postgres", None, None
    )
    messages = " ".join(finding.message for finding in check.findings)
    assert check.verdict == VERDICT_MAY_DIFFER
    assert "drops DISTINCT" in messages and "row limit" in messages and "ORDER BY" in messages


def test_replacing_not_in_is_flagged_for_nulls():
    check = check_rewrite(
        "SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)",
        "SELECT a FROM t WHERE NOT EXISTS (SELECT 1 FROM u WHERE u.b = t.a)",
        "postgres", None, None,
    )
    assert check.verdict == VERDICT_MAY_DIFFER
    assert any("NOT IN" in finding.message and "NULL" in finding.message for finding in check.findings)


def test_an_equivalent_looking_rewrite_with_a_cheaper_plan_is_unverified_and_says_how_much_cheaper():
    original = "SELECT a FROM t WHERE a IN (SELECT b FROM u)"
    rewritten = "SELECT t.a FROM t JOIN (SELECT DISTINCT b FROM u) u ON u.b = t.a"
    check = check_rewrite(
        original, rewritten, "postgres",
        _plan(1000, [PlanWarning("warning", "large_seq_scan", "...")]),
        _planner(PlanOutcome(plan=_plan(250), checked=True)),
    )
    assert check.verdict == VERDICT_UNVERIFIED
    messages = " ".join(finding.message for finding in check.findings)
    assert "75.0% lower" in messages
    assert "No longer flagged" in messages
    assert check.rewritten_cost == 250


def test_a_rewrite_that_cannot_be_planned_for_other_reasons_is_compared_on_the_sql_alone():
    check = check_rewrite(
        "SELECT a FROM t", "SELECT t.a FROM t", "postgres", _plan(10),
        _planner(PlanOutcome(error="Planning isn't supported", checked=False)),
    )
    assert check.verdict == VERDICT_UNVERIFIED
    assert any("couldn't be planned" in finding.message for finding in check.findings)


def test_a_select_list_subquery_turned_into_an_inner_join_may_drop_rows():
    # The rewrite a real model suggested for a per-table column count over pg_catalog.
    original = (
        "SELECT c.relname, (SELECT count(*) FROM pg_attribute a WHERE a.attrelid = c.oid) AS columns "
        "FROM pg_class c WHERE c.relkind = 'r' ORDER BY 2 DESC"
    )
    rewritten = (
        "SELECT c.relname, count(a.attrelid) AS columns FROM pg_class c "
        "JOIN pg_attribute a ON a.attrelid = c.oid WHERE c.relkind = 'r' GROUP BY c.oid, c.relname ORDER BY 2 DESC"
    )
    check = check_rewrite(original, rewritten, "postgres", None, None)
    assert check.verdict == VERDICT_MAY_DIFFER
    assert any("LEFT JOIN would keep them" in finding.message for finding in check.findings)


def test_the_same_rewrite_with_a_left_join_is_not_flagged_for_it():
    original = "SELECT c.relname, (SELECT count(*) FROM pg_attribute a WHERE a.attrelid = c.oid) AS columns FROM pg_class c"
    rewritten = (
        "SELECT c.relname, count(a.attrelid) AS columns FROM pg_class c "
        "LEFT JOIN pg_attribute a ON a.attrelid = c.oid GROUP BY c.oid, c.relname"
    )
    check = check_rewrite(original, rewritten, "postgres", None, None)
    assert not any("LEFT JOIN would keep them" in finding.message for finding in check.findings)


def test_an_in_subquery_turned_into_a_join_may_repeat_rows():
    check = check_rewrite(
        "SELECT o.id FROM orders o WHERE o.customer_id IN (SELECT id FROM vip)",
        "SELECT o.id FROM orders o JOIN vip v ON v.id = o.customer_id",
        "postgres", None, None,
    )
    assert check.verdict == VERDICT_MAY_DIFFER
    assert any("repeat once per match" in finding.message for finding in check.findings)
