"""Unit tests for everything that runs without an LLM: the SQL safety gate
and performance rules, schema metadata, result comparison, the SQL, timing
and prose metrics, the goldens, and reading the plugin's answers.

Network-free by construction: the Superset client is driven by a
`unittest.mock` session. The LLM judge's tests are in test_llm_judge.py.

Run: pytest -m unit
"""

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pandas as pd
import pytest
import requests
from deepeval import assert_test
from deepeval.metrics.utils import copy_metrics
from deepeval.test_case import LLMTestCase

import generate_schema_metadata
from metrics.clarification import (
    AskedForClarificationMetric,
    ClarificationCoversReadingsMetric,
    SaidItCantAnswerMetric,
    cant_answer_text,
    clarification_text,
)
from metrics.common import ForbiddenTextMetric, LatencyMetric, PluginAnsweredMetric
from metrics.expected_result import (
    SameRowsAsOriginalMetric,
    fingerprint_sql,
    tables_read,
)
from metrics.prose import (
    BreakdownCoverageMetric,
    DescriptionUnchangedMetric,
    TooltipTermsMetric,
)
from metrics.query_results import (
    UNSAFE,
    QueryResults,
    describe_results,
    frames_equivalent,
    run_queries,
)
from metrics.sql import (
    ExecutionMatchMetric,
    SqlRunsMetric,
    SqlSafetyMetric,
    StaticPerformanceMetric,
)
from metrics.sql_performance import analyze_query_performance
from metrics.sql_safety import check_sql_safety, same_query, strip_sql_fences, tables_in_sql
from metrics.timing import FasterThanOriginalMetric, SpeedVsReferenceMetric
from judge.metrics import (
    OneLineReasonTemplate,
    clarification_quality_metric,
    error_diagnosis_metric,
    missing_data_explanation_metric,
    prose_metric,
    query_performance_metric,
    sql_correctness_metric,
)
from judge.prompts import REASON_RULE
from plugin.endpoints import ENDPOINTS, endpoint_for, parse_latency_budgets
from plugin.internals import retries_in_stream
from suite import locks
from suite.config import SuiteConfig
from suite.dataset import SUITE_PARTS, golden_files, golden_id, load_dataset, select_part
from suite.endpoint_metrics import (
    ALREADY_FAST_METRICS,
    CANT_ANSWER_METRICS,
    CLARIFICATION_ANSWERED_METRICS,
    BUILDERS,
    CLARIFICATION_METRICS,
    METRICS,
    Answer,
    metrics_for,
)
from judge.context import build_performance_context
from suite.schema_metadata import load_schema_metadata
from unit.support import DAILY_REVENUE_PROMPT, ROLLUP_SQL, FakeJudge, sql_case

pytestmark = pytest.mark.unit

GENERATE = ENDPOINTS["generate-sql"]
UPDATE = ENDPOINTS["update-sql-from-nl"]


def rules(findings):
    return {f.rule for f in findings}


###############################################
# SQLGlot safety gate
###############################################


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        ROLLUP_SQL,
        "```sql\nSELECT region FROM YOUR_DIMENSION_TABLE;\n```",
        "WITH r AS (SELECT region FROM YOUR_DIMENSION_TABLE) SELECT region FROM r",
        "SELECT region FROM YOUR_DIMENSION_TABLE UNION SELECT 'unknown'",
        "SELECT COUNT(*) FROM YOUR_DIMENSION_TABLE",
        # Not the lo_ (large object) family.
        "SELECT lower(region), log(2) FROM YOUR_DIMENSION_TABLE",
    ],
)
def test_read_only_sql_is_safe(sql, metadata):
    result = check_sql_safety(sql, "postgres", metadata)
    assert result.is_safe, result.violations


@pytest.mark.parametrize(
    "sql, violation",
    [
        ("", "empty"),
        ("DROP TABLE YOUR_FACT_TABLE", "not a SELECT"),
        (
            "DELETE FROM YOUR_FACT_TABLE WHERE event_date < '2020-01-01'",
            "Delete",
        ),
        ("UPDATE YOUR_DIMENSION_TABLE SET segment = 'vip'", "Update"),
        ("SELECT 1; DROP TABLE YOUR_FACT_TABLE", "exactly one statement"),
        ("INSERT INTO t SELECT * FROM YOUR_FACT_TABLE", "Insert"),
        ("SELECT * INTO backup FROM YOUR_FACT_TABLE", "Into"),
        ("SELECT region FROM YOUR_DIMENSION_TABLE FOR UPDATE", "Lock"),
        ("SELECT pg_sleep(600)", "pg_sleep"),
        (
            "SELECT email FROM YOUR_DIMENSION_TABLE",
            "sensitive column YOUR_DIMENSION_TABLE.email",
        ),
        (
            "SELECT region FROM YOUR_DIMENSION_TABLE WHERE phone = '1'",
            "sensitive column YOUR_DIMENSION_TABLE.phone",
        ),
        ("SELECT * FROM YOUR_DIMENSION_TABLE", "sensitive column"),
        ("SELEC region FRM", "unparseable"),
        # sqlglot raises TokenError, not ParseError, for an unclosed quote.
        ("SELECT region FROM YOUR_DIMENSION_TABLE WHERE region = 'north", "unparseable"),
        ("SELECT setval('ids', 1)", "setval"),
        (
            "SELECT query_to_xml('DELETE FROM YOUR_FACT_TABLE', true, false, '')",
            "query_to_xml",
        ),
        ("SELECT pg_try_advisory_lock(1)", "pg_try_advisory_lock"),
        ("SELECT * FROM dblink('host=x', 'SELECT 1') AS t(a int)", "dblink"),
    ],
)
def test_unsafe_sql_is_rejected(sql, violation, metadata):
    result = check_sql_safety(sql, "postgres", metadata)
    assert not result.is_safe
    assert any(violation in v for v in result.violations), result.violations


def test_strip_sql_fences():
    assert strip_sql_fences("```sql\nSELECT 1;\n```") == "SELECT 1"
    assert strip_sql_fences("```\nSELECT 1\n```") == "SELECT 1"
    assert strip_sql_fences("  SELECT 1;  ") == "SELECT 1"


def test_tables_in_sql_ignores_ctes():
    sql = "WITH x AS (SELECT * FROM s.YOUR_FACT_TABLE) SELECT * FROM x JOIN s.YOUR_DIMENSION_TABLE d ON true"
    assert tables_in_sql(sql) == {"your_fact_table", "your_dimension_table"}


###############################################
# SQLGlot performance heuristics
###############################################


def test_preferred_rollup_query_has_no_findings(metadata):
    assert (
        analyze_query_performance(ROLLUP_SQL, metadata, DAILY_REVENUE_PROMPT)
        == []
    )


@pytest.mark.parametrize(
    "sql, rule, severity",
    [
        ("SELECT * FROM YOUR_DAILY_ROLLUP_TABLE", "select_star", "warning"),
        ("SELECT a FROM YOUR_MISSING_TABLE", "unknown_table", "error"),
        (
            "SELECT f.revenue FROM YOUR_FACT_TABLE f, YOUR_DIMENSION_TABLE d "
            "WHERE f.event_date = CURRENT_DATE LIMIT 5",
            "cartesian_join",
            "error",
        ),
        (
            # Under OR, rows that match the other side are paired with every row.
            "SELECT f.revenue FROM YOUR_FACT_TABLE f CROSS JOIN YOUR_DIMENSION_TABLE d "
            "WHERE f.customer_id = d.customer_id OR f.event_date = CURRENT_DATE LIMIT 5",
            "cartesian_join",
            "error",
        ),
        (
            "SELECT revenue FROM YOUR_FACT_TABLE WHERE quantity > 5 LIMIT 10",
            "missing_index_filter",
            "warning",
        ),
        (
            "SELECT event_id, revenue FROM YOUR_FACT_TABLE WHERE event_date = CURRENT_DATE",
            "unbounded_large_scan",
            "warning",
        ),
        (
            "SELECT SUM(revenue) FROM YOUR_FACT_TABLE "
            "WHERE DATE_TRUNC('month', event_date) = DATE '2026-01-01'",
            "non_sargable_predicate",
            "error",
        ),
        (
            "SELECT segment FROM YOUR_DIMENSION_TABLE WHERE region LIKE '%north%'",
            "leading_wildcard_like",
            "warning",
        ),
        ("SELECT region FROM YOUR_DIMENSION_TABLE WHERE region = 'north", "unparseable", "error"),
        (
            # One row per customer, paired with every customer.
            "SELECT d.region FROM YOUR_DIMENSION_TABLE d CROSS JOIN ("
            "SELECT customer_id, SUM(revenue) AS total FROM YOUR_FACT_TABLE "
            "GROUP BY customer_id) t",
            "cartesian_join",
            "error",
        ),
        (
            # A window keeps every row; only a plain aggregate makes one.
            "SELECT d.region FROM YOUR_DIMENSION_TABLE d CROSS JOIN ("
            "SELECT SUM(revenue) OVER () AS total FROM YOUR_FACT_TABLE) t",
            "cartesian_join",
            "error",
        ),
    ],
)
def test_performance_rule_fires(sql, rule, severity, metadata):
    findings = {f.rule: f for f in analyze_query_performance(sql, metadata)}
    assert rule in findings, findings
    assert findings[rule].severity == severity


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT f.revenue FROM YOUR_FACT_TABLE f, YOUR_DIMENSION_TABLE d "
        "WHERE f.customer_id = d.customer_id AND d.region = 'north' LIMIT 5",
        # A derived table joined in WHERE, as the accelerate endpoint writes it.
        "SELECT f.event_id, t.total FROM YOUR_FACT_TABLE f CROSS JOIN ("
        "SELECT customer_id, SUM(revenue) AS total FROM YOUR_FACT_TABLE "
        "GROUP BY customer_id) AS t "
        "WHERE (f.customer_id = t.customer_id) AND f.event_date = CURRENT_DATE",
    ],
)
def test_join_condition_in_where_is_not_cartesian(sql, metadata):
    assert "cartesian_join" not in rules(analyze_query_performance(sql, metadata))


@pytest.mark.parametrize(
    "sql",
    [
        # A one-row total for shares of it, inline or as a CTE.
        "SELECT d.region, t.total FROM YOUR_DIMENSION_TABLE d CROSS JOIN ("
        "SELECT SUM(revenue) AS total FROM YOUR_FACT_TABLE) t",
        "WITH t AS (SELECT SUM(total_revenue) AS total FROM YOUR_DAILY_ROLLUP_TABLE) "
        "SELECT r.event_date, r.total_revenue / t.total "
        "FROM YOUR_DAILY_ROLLUP_TABLE r CROSS JOIN t",
        "SELECT d.region, l.last FROM YOUR_DIMENSION_TABLE d CROSS JOIN LATERAL ("
        "SELECT MAX(f.event_date) AS last FROM YOUR_FACT_TABLE f "
        "WHERE f.customer_id = d.customer_id) l",
        "SELECT d.region, g.n FROM YOUR_DIMENSION_TABLE d "
        "CROSS JOIN generate_series(1, 3) AS g(n)",
    ],
)
def test_joins_that_need_no_condition_are_not_cartesian(sql, metadata):
    assert "cartesian_join" not in rules(analyze_query_performance(sql, metadata))


@pytest.mark.skipif("LARGE_TABLE_ROWS" in os.environ, reason="LARGE_TABLE_ROWS is set")
def test_only_the_big_acled_table_counts_as_large_by_default(monkeypatch):
    monkeypatch.delenv("SCHEMA_METADATA_FILE", raising=False)
    tables = load_schema_metadata()
    assert tables["global_events"].is_large
    assert not tables["political_violence_country_month"].is_large


def test_raw_table_for_rollup_question_is_flagged(metadata):
    sql = (
        "SELECT event_date, SUM(revenue) FROM YOUR_FACT_TABLE "
        "WHERE event_date >= CURRENT_DATE - 30 GROUP BY event_date"
    )
    assert "prefer_rollup" in rules(
        analyze_query_performance(sql, metadata, DAILY_REVENUE_PROMPT)
    )
    assert "prefer_rollup" not in rules(
        analyze_query_performance(sql, metadata, "List individual transactions")
    )


@pytest.mark.parametrize(
    "sql",
    [
        # AND/OR subclass Func in sqlglot; they must not count as wrappers.
        "SELECT SUM(revenue) FROM YOUR_FACT_TABLE "
        "WHERE event_date >= CURRENT_DATE - INTERVAL '30 days' AND customer_id = 5",
        # The function lives in the subquery, not around the outer predicate.
        "SELECT SUM(revenue) FROM YOUR_FACT_TABLE "
        "WHERE event_date = (SELECT MAX(event_date) FROM YOUR_DAILY_ROLLUP_TABLE)",
    ],
)
def test_sargable_predicates_are_not_flagged(sql, metadata):
    assert "non_sargable_predicate" not in rules(
        analyze_query_performance(sql, metadata)
    )


def _non_sargable(findings):
    return [f.severity for f in findings if f.rule == "non_sargable_predicate"]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(revenue) FROM YOUR_FACT_TABLE "
        "WHERE DATE_TRUNC('month', event_date) = DATE '2026-01-01'",
        # Through an alias, in a correlated subquery, as NOT EXISTS rewrites are.
        "SELECT d.region FROM YOUR_DIMENSION_TABLE d WHERE NOT EXISTS ("
        "SELECT 1 FROM YOUR_FACT_TABLE f WHERE f.customer_id = d.customer_id "
        "AND EXTRACT(YEAR FROM f.event_date) = 2026)",
        "WITH recent AS (SELECT customer_id FROM YOUR_FACT_TABLE "
        "WHERE EXTRACT(YEAR FROM event_date) = 2026) SELECT COUNT(*) FROM recent",
        # IS NOT NULL on the bare column doesn't give the index a range.
        "SELECT COUNT(*) FROM YOUR_FACT_TABLE "
        "WHERE event_date IS NOT NULL AND EXTRACT(YEAR FROM event_date) = 2026",
    ],
)
def test_a_faster_rewrite_fails_on_a_function_around_an_indexed_column(sql, metadata):
    assert _non_sargable(
        analyze_query_performance(sql, metadata, fail_non_sargable=True)
    ) == ["error"]


@pytest.mark.parametrize(
    "sql",
    [
        # The outer query's column: it doesn't stop the subquery's table
        # using an index.
        "SELECT d.customer_id FROM YOUR_DIMENSION_TABLE d WHERE EXISTS ("
        "SELECT 1 FROM YOUR_FACT_TABLE f WHERE f.customer_id = d.customer_id "
        "AND LOWER(d.region) = 'north')",
        # Joining on a computed month is how a per-month total is joined back.
        "SELECT f.event_id, m.revenue FROM YOUR_FACT_TABLE f JOIN ("
        "SELECT customer_id, DATE_TRUNC('month', event_date) AS month, "
        "SUM(revenue) AS revenue FROM YOUR_FACT_TABLE GROUP BY 1, 2) m "
        "ON f.customer_id = m.customer_id "
        "AND DATE_TRUNC('month', f.event_date) = m.month WHERE f.customer_id = 5",
        # The bare range uses the index; the wrapped column only filters
        # what it finds.
        "SELECT COUNT(*) FROM YOUR_FACT_TABLE WHERE event_date >= DATE '2026-01-01' "
        "AND EXTRACT(DOW FROM event_date) = 1",
        "SELECT d.region, COUNT(*) FROM YOUR_DIMENSION_TABLE d JOIN YOUR_FACT_TABLE f "
        "ON f.event_date = d.signup_date WHERE EXTRACT(DOW FROM f.event_date) = 1 "
        "GROUP BY d.region",
    ],
)
def test_a_faster_rewrite_may_wrap_other_columns(sql, metadata):
    assert "error" not in _non_sargable(
        analyze_query_performance(sql, metadata, fail_non_sargable=True)
    )


def test_a_function_around_a_small_tables_column_stays_a_warning(metadata):
    small = {
        **metadata,
        "your_dimension_table": replace(
            metadata["your_dimension_table"], approx_row_count=1_000
        ),
    }
    findings = analyze_query_performance(
        "SELECT customer_id FROM YOUR_DIMENSION_TABLE WHERE LOWER(region) = 'north'",
        small,
        fail_non_sargable=True,
    )
    assert _non_sargable(findings) == ["warning"]


###############################################
# Performance context for the judge
###############################################


def test_context_carries_index_and_table_preferences(metadata):
    findings = analyze_query_performance(
        "SELECT * FROM YOUR_DAILY_ROLLUP_TABLE", metadata
    )
    context = build_performance_context(metadata, findings=findings)
    text = "\n".join(context)

    assert context[0].startswith("Performance policy:")
    assert "Partitioned by event_date" in text
    assert (
        "Index ix_fact_customer_date: btree on (customer_id, event_date)"
        in text
    )
    assert "Pre-aggregated rollup of YOUR_FACT_TABLE" in text
    assert "never select or filter on: email, phone" in text
    assert "[warning] select_star" in context[-1]


def test_context_can_be_limited_to_relevant_tables(metadata):
    context = build_performance_context(
        metadata, tables=["YOUR_DAILY_ROLLUP_TABLE"]
    )
    assert len(context) == 2
    assert "YOUR_DAILY_ROLLUP_TABLE" in context[1]


def test_schema_metadata_loads_from_json(tmp_path, monkeypatch):
    path = tmp_path / "schema.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "orders",
                    "description": "Orders",
                    "approx_row_count": 50_000_000,
                    "columns": {
                        "order_id": "bigint",
                        "created_at": "timestamp",
                    },
                    "indexes": [
                        {"name": "ix_orders_created", "columns": ["created_at"]}
                    ],
                    "sensitive_columns": ["card_number"],
                    "not_null": ["order_id"],
                }
            ]
        )
    )
    monkeypatch.setenv("SCHEMA_METADATA_FILE", str(path))

    loaded = load_schema_metadata()

    assert set(loaded) == {"orders"}
    assert loaded["orders"].is_large
    assert loaded["orders"].indexed_columns == {"created_at"}
    assert (
        "Columns: order_id bigint NOT NULL, created_at timestamp\n"
        in loaded["orders"].to_context()
    )


def test_missing_schema_metadata_stops_the_suite(tmp_path, monkeypatch):
    # Rather than grading against tables that don't exist.
    monkeypatch.setenv("SCHEMA_METADATA_FILE", str(tmp_path / "gone.json"))
    with pytest.raises(FileNotFoundError, match="generate_schema_metadata.py"):
        load_schema_metadata()


def test_schema_metadata_drafts_not_null_and_keeps_what_you_wrote(tmp_path, monkeypatch):
    out = tmp_path / "schema_metadata.json"
    out.write_text(json.dumps([{
        "name": "events", "schema": "acled", "description": "Weekly events",
        "approx_row_count": 1, "columns": {}, "rollup_of": "raw_events",
    }]))
    answers = {
        "pg_class c JOIN": pd.DataFrame({"name": ["events"], "approx_rows": [1_000_000]}),
        "information_schema.columns": pd.DataFrame({
            "table_name": ["events", "events"], "column_name": ["id", "week"],
            "data_type": ["bigint", "date"], "is_nullable": ["NO", "YES"],
        }),
        "FROM pg_index": pd.DataFrame({
            "tablename": ["events"], "indexname": ["events_pkey"], "kind": ["btree"],
            "condition": [None], "key": ["id"],
        }),
    }
    client = mock.Mock()
    client.execute_sql.side_effect = lambda sql: next(v for k, v in answers.items() if k in sql)
    monkeypatch.setattr(generate_schema_metadata, "OUTPUT", out)
    monkeypatch.setattr(generate_schema_metadata, "SupersetClient", lambda config: client)
    monkeypatch.setattr(
        generate_schema_metadata, "SuiteConfig",
        mock.Mock(from_env=lambda: mock.Mock(schema="acled")),
    )

    generate_schema_metadata.main([])

    (table,) = json.loads(out.read_text())
    assert table["columns"] == {"id": "bigint", "week": "date"}
    assert table["not_null"] == ["id"]
    assert table["description"] == "Weekly events" and table["rollup_of"] == "raw_events"


###############################################
# Result comparison
###############################################


def test_frames_equivalent_ignores_row_order_and_aliases():
    expected = pd.DataFrame(
        {"region": ["north", "south"], "revenue": [10.0, 20.0]}
    )
    reordered = pd.DataFrame(
        {"REVENUE": [Decimal("20.0000001"), 10], "Region": ["south", "north"]}
    )
    aliased = pd.DataFrame({"r": ["south", "north"], "total": [20.0, 10.0]})

    assert frames_equivalent(reordered, expected) == (True, "")
    assert frames_equivalent(aliased, expected) == (True, "")


def test_frames_equivalent_reports_differences():
    expected = pd.DataFrame(
        {"region": ["north", "south"], "revenue": [10.0, 20.0]}
    )

    same, diff = frames_equivalent(expected.head(1), expected)
    assert not same and "shape" in diff

    same, diff = frames_equivalent(
        pd.DataFrame({"region": ["north", "south"], "revenue": [10.0, 99.0]}),
        expected,
    )
    assert not same and "('south', 20.0)" in diff


def test_frames_equivalent_counts_null_as_zero():
    expected = pd.DataFrame({"country": ["Albania", "Chad"], "deaths": [None, 12]})
    zero = pd.DataFrame({"country": ["Albania", "Chad"], "deaths": [0, 12]})
    assert frames_equivalent(zero, expected) == (True, "")

    # A dropped row still fails, NULL or not.
    same, diff = frames_equivalent(zero.tail(1), expected)
    assert not same and "shape" in diff


###############################################
# DeepEval SQL metrics
###############################################


def test_safety_metric(metadata):
    metric = SqlSafetyMetric(metadata)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 1.0

    metric.measure(sql_case("DROP TABLE YOUR_FACT_TABLE"))
    assert not metric.is_successful() and "not a SELECT" in metric.reason

    metric.measure(sql_case())
    assert metric.reason == "plugin returned no SQL"


def test_static_performance_metric_checks_tables(metadata):
    metric = StaticPerformanceMetric(metadata)
    tables = {
        "must_use": ["YOUR_DAILY_ROLLUP_TABLE"],
        "must_not_use": ["YOUR_FACT_TABLE"],
    }
    assert metric.measure(sql_case(ROLLUP_SQL, **tables)) == 1.0

    raw = (
        "SELECT event_date, SUM(revenue) FROM YOUR_FACT_TABLE "
        "WHERE event_date >= CURRENT_DATE - 30 GROUP BY event_date"
    )
    assert metric.measure(sql_case(raw, **tables)) == 0.0
    assert "expected tables not used" in metric.reason
    assert "should not query" in metric.reason
    assert "warning prefer_rollup" in metric.reason


def test_static_performance_warnings_do_not_fail(metadata):
    metric = StaticPerformanceMetric(metadata)
    assert metric.measure(sql_case("SELECT * FROM YOUR_DAILY_ROLLUP_TABLE")) == 1.0
    assert "warning select_star" in metric.reason


def test_static_performance_fails_a_function_around_an_indexed_column(metadata):
    sql = (
        "SELECT SUM(revenue) FROM YOUR_FACT_TABLE "
        "WHERE DATE_TRUNC('month', event_date) = DATE '2026-01-01'"
    )
    # On every endpoint, as the Query Performance judge does.
    metric = StaticPerformanceMetric(metadata)
    assert metric.measure(sql_case(sql)) == 0.0
    assert "compare the raw column with a range" in metric.reason

    lenient = StaticPerformanceMetric(metadata, fail_non_sargable=False)
    assert lenient.measure(sql_case(sql)) == 1.0
    assert "warning non_sargable_predicate" in lenient.reason


def test_execution_metric_compares_against_reference(metadata):
    client = mock.Mock()
    client.execute_sql.side_effect = [
        pd.DataFrame({"a": [1, 2]}),
        pd.DataFrame({"a": [2, 1]}),
    ]
    metric = ExecutionMatchMetric(client, metadata)

    assert metric.measure(sql_case(ROLLUP_SQL)) == 1.0
    assert client.execute_sql.call_args_list[1].args == ("SELECT 1",)


def test_execution_metric_never_runs_unsafe_sql(metadata):
    client = mock.Mock()
    metric = ExecutionMatchMetric(client, metadata)

    assert metric.measure(sql_case("DELETE FROM YOUR_FACT_TABLE")) == 0.0
    assert metric.measure(sql_case()) == 0.0
    client.execute_sql.assert_not_called()


def test_execution_metric_reports_broken_generated_sql(metadata):
    client = mock.Mock()
    client.execute_sql.side_effect = requests.HTTPError(
        "column x does not exist"
    )
    metric = ExecutionMatchMetric(client, metadata)

    assert metric.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert "failed to execute: column x does not exist" in metric.reason


def test_execution_metric_uses_rows_the_test_already_fetched(metadata):
    client = mock.Mock()
    fetched = QueryResults(
        actual=pd.DataFrame({"a": [1, 2]}), expected=pd.DataFrame({"a": [2, 1]})
    )
    assert ExecutionMatchMetric(client, metadata, results=fetched).measure(
        sql_case(ROLLUP_SQL)
    ) == 1.0

    broken = ExecutionMatchMetric(
        client, metadata, results=QueryResults(actual_problem="failed to execute: x")
    )
    assert broken.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert broken.reason == "failed to execute: x"
    client.execute_sql.assert_not_called()


def test_run_queries_skips_unsafe_sql_and_blames_a_broken_reference(metadata):
    client = mock.Mock()
    unsafe = run_queries(client, "DELETE FROM YOUR_FACT_TABLE", "SELECT 1", metadata)
    assert unsafe.actual is None and unsafe.actual_problem == UNSAFE
    client.execute_sql.assert_not_called()

    client.execute_sql.side_effect = [
        pd.DataFrame({"a": [1]}),
        requests.HTTPError("relation x does not exist"),
    ]
    with pytest.raises(RuntimeError, match="reference SQL failed to run"):
        run_queries(client, ROLLUP_SQL, "SELECT a FROM x", metadata)


def _cut_off(frame):
    """A result SQL Lab stopped at its row limit, as execute_sql marks it."""
    frame.attrs["cut_off"] = True
    return frame


def test_sql_cut_off_at_the_row_limit_never_matches(metadata):
    # The rows SQL Lab fetched match, but the SQL returns more than that.
    results = QueryResults(
        actual=_cut_off(pd.DataFrame({"a": [1, 2]})), expected=pd.DataFrame({"a": [2, 1]})
    )
    metric = ExecutionMatchMetric(mock.Mock(), metadata, results=results)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert metric.reason == (
        "the SQL returns more than 2 rows (SQL Lab's row limit); the reference returns 2 rows"
    )
    text = describe_results(results)
    assert "Running the actual output returned more than 2 rows (SQL Lab's row limit)" in text
    assert "they return different rows" in text

    runs = SqlRunsMetric(mock.Mock(), metadata, results=QueryResults(actual=results.actual))
    assert runs.measure(sql_case(ROLLUP_SQL)) == 1.0
    assert runs.reason == "ran successfully, more than 2 rows (SQL Lab's row limit)"


def test_a_reference_cut_off_at_the_row_limit_is_the_goldens_problem(metadata):
    client = mock.Mock()
    client.execute_sql.side_effect = [
        pd.DataFrame({"a": [1]}),
        _cut_off(pd.DataFrame({"a": [1, 2]})),
    ]
    with pytest.raises(RuntimeError, match="reference SQL returns more than 2 rows"):
        run_queries(client, ROLLUP_SQL, "SELECT a FROM x", metadata)


def test_describe_results_shows_the_judge_rows_in_the_order_returned():
    reordered = QueryResults(
        actual=pd.DataFrame({"month": ["April", "August"], "events": [3.0, 5.0]}),
        expected=pd.DataFrame({"month": ["August", "April"], "events": [5.0, 3.0]}),
    )
    text = describe_results(reordered)
    assert "Running the actual output returned 2 rows; all of them" in text
    assert "  month | events\n  April | 3\n  August | 5" in text
    assert text.endswith("With row order ignored, both return the same 2 rows.")

    empty = describe_results(
        QueryResults(actual=pd.DataFrame({"n": []}), expected=pd.DataFrame({"n": [93]}))
    )
    assert "returned 0 rows, with columns n." in empty
    assert "they return different rows" in empty

    long = describe_results(QueryResults(actual=pd.DataFrame({"n": range(25)})), rows=10)
    assert "returned 25 rows; the first 10" in long and "\n  9" in long
    assert "\n  10" not in long

    assert describe_results(QueryResults(actual_problem=UNSAFE)).endswith(f"({UNSAFE}).")


def test_latency_metric():
    metric = LatencyMetric(budget_s=5)
    assert metric.measure(sql_case("SELECT 1", completion_time=4.9)) == 1.0
    assert metric.measure(sql_case("SELECT 1", completion_time=5.1)) == 0.0


def test_sql_runs_metric(metadata):
    client = mock.Mock()
    client.execute_sql.return_value = pd.DataFrame({"a": [1, 2, 3]})
    metric = SqlRunsMetric(client, metadata)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 1.0
    assert metric.reason == "ran successfully, 3 rows"

    client.execute_sql.side_effect = requests.HTTPError('type "Small"')
    assert metric.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert metric.reason == 'failed to execute: type "Small"'

    client.reset_mock()
    assert metric.measure(sql_case("DROP TABLE YOUR_FACT_TABLE")) == 0.0
    client.execute_sql.assert_not_called()


def _timer(**times):
    """A fake client.time_query: milliseconds per SQL, or None for a timeout."""
    client = mock.Mock()
    client.time_query.side_effect = lambda sql, timeout_s: times[sql]
    return client


def test_faster_than_original_metric_times_both_queries(metadata):
    timer = _timer(**{ROLLUP_SQL: 40.0, "SELECT slow": 100.0})
    metric = FasterThanOriginalMetric(timer, metadata, threshold=0.5, timeout_s=25)
    case = sql_case(ROLLUP_SQL, original_sql="SELECT slow")
    assert metric.measure(case) == pytest.approx(0.6)
    assert metric.is_successful()
    assert metric.reason == (
        "original 100.0 ms -> rewrite 40.0 ms (median of 3 runs) "
        "(60% of the time saved, 2.5x as fast; needs at least 50% saved, 2.0x as fast)"
    )
    # A warm-up plus 3 timed runs of each query, all under the time limit.
    assert timer.time_query.call_count == 8
    assert {c.args[1] for c in timer.time_query.call_args_list} == {25}


@pytest.mark.parametrize(
    "rewrite_ms, original_ms, score, passed, expected",
    [
        # The score is the share of the original's time saved.
        (180_000.0, 3_600_000.0, 0.95, True, "95% of the time saved, 20.0x as fast"),
        (50.0, 100.0, 0.5, True, "50% of the time saved, 2.0x as fast; needs at least 50% saved"),
        # Faster, but short of the pass mark: scored, and fails.
        (480_000.0, 600_000.0, 0.2, False, "20% of the time saved"),
        (100.0, 100.0, 0.0, False, "0% of the time saved, 1.0x as fast"),
        # Slower scores 0.
        (120.0, 100.0, 0.0, False, "20% slower"),
        # An original that hit the time limit is compared against the limit.
        (400.0, None, 0.984, True, "original did not finish within 25s; rewrite 400.0 ms (at least 98% of the time saved"),
        (None, 100.0, 0.0, False, "the rewrite did not finish within 25s"),
        # The rewrite hitting the limit fails whatever the original does.
        (None, None, 0.0, False, "the rewrite did not finish within 25s"),
    ],
)
def test_faster_than_original_metric_outcomes(
    metadata, rewrite_ms, original_ms, score, passed, expected
):
    timer = _timer(**{ROLLUP_SQL: rewrite_ms, "SELECT slow": original_ms})
    metric = FasterThanOriginalMetric(timer, metadata, timeout_s=25)
    assert metric.measure(sql_case(ROLLUP_SQL, original_sql="SELECT slow")) == pytest.approx(score)
    assert metric.is_successful() is passed
    assert expected in metric.reason


def test_faster_than_original_metric_compares_medians_after_a_warm_up(metadata):
    client = mock.Mock()
    client.time_query.side_effect = [
        900.0, 40.0, 60.0, 50.0,  # rewrite: slow cold run ignored, median 50
        100.0, 100.0, 100.0, 100.0,  # original
    ]
    metric = FasterThanOriginalMetric(client, metadata)
    metric.measure(sql_case(ROLLUP_SQL, original_sql="SELECT slow"))
    assert "rewrite 50.0 ms (median of 3 runs) (50% of the time saved" in metric.reason


def test_faster_than_original_metric_does_not_rerun_a_query_that_timed_out(metadata):
    timer = _timer(**{ROLLUP_SQL: 10.0, "SELECT slow": None})
    FasterThanOriginalMetric(timer, metadata).measure(
        sql_case(ROLLUP_SQL, original_sql="SELECT slow")
    )
    slow_runs = [c for c in timer.time_query.call_args_list if c.args[0] == "SELECT slow"]
    assert len(slow_runs) == 1


def test_faster_than_original_metric_runs_nothing_without_safe_sql(metadata):
    client = mock.Mock()
    metric = FasterThanOriginalMetric(client, metadata)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert metric.reason == "no original query (original_sql) to compare with"
    assert metric.measure(sql_case("DROP TABLE YOUR_FACT_TABLE", original_sql="x")) == 0.0
    assert metric.reason == "not run: SQL failed the safety gate"
    assert metric.measure(sql_case(None, original_sql="x")) == 0.0
    client.time_query.assert_not_called()


def test_speed_vs_reference_metric_times_against_the_reference(metadata):
    # The reference is the test case's expected_output.
    timer = _timer(**{ROLLUP_SQL: 110.0, "SELECT 1": 100.0})
    metric = SpeedVsReferenceMetric(timer, metadata, tolerance=0.2)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 1.0
    assert metric.reason == (
        "SQL 110.0 ms vs reference 100.0 ms (10% slower; allowed up to 20% slower), "
        "median of 3 runs"
    )


@pytest.mark.parametrize(
    "sql_ms, reference_ms, passed, expected",
    [
        (50.0, 100.0, True, "50% faster"),
        # Within the percentage: passes without the second test.
        (11000.0, 10000.0, True, "10% slower; allowed up to 20% slower), median"),
        # Over the percentage but not meaningfully slower in time: passes.
        (500.0, 50.0, True, "but only 450.0 ms slower"),
        (0.3, 0.1, True, "but only 0.2 ms slower"),
        # Over both: fails.
        (3000.0, 1000.0, False, "2,000.0 ms slower, more than the 500 ms"),
        (None, 100.0, False, "the SQL did not finish within 25s"),
        (100.0, None, True, "reference did not finish within 25s"),
    ],
)
def test_speed_vs_reference_metric_outcomes(
    metadata, sql_ms, reference_ms, passed, expected
):
    timer = _timer(**{ROLLUP_SQL: sql_ms, "SELECT 1": reference_ms})
    metric = SpeedVsReferenceMetric(timer, metadata, timeout_s=25, min_slowdown_ms=500)
    assert metric.measure(sql_case(ROLLUP_SQL)) == float(passed)
    assert expected in metric.reason


def test_timed_metrics_fail_untimed_when_the_rows_are_wrong(metadata):
    from metrics.query_results import QueryResults

    wrong = QueryResults(actual=pd.DataFrame({"a": []}), expected=pd.DataFrame({"a": [1, 2]}))
    right = QueryResults(actual=pd.DataFrame({"a": [2, 1]}), expected=pd.DataFrame({"a": [1, 2]}))
    failed = QueryResults(actual_problem="failed to execute: syntax error")
    for results, passed in ((wrong, False), (failed, False), (right, True)):
        timer = _timer(**{ROLLUP_SQL: 1.0, "SELECT 1": 100.0, "x": 100.0})
        speed = SpeedVsReferenceMetric(timer, metadata, results=results)
        faster = FasterThanOriginalMetric(timer, metadata, results=results)
        assert speed.measure(sql_case(ROLLUP_SQL)) == float(passed)
        assert faster.measure(sql_case(ROLLUP_SQL, original_sql="x")) == pytest.approx(
            0.99 if passed else 0.0
        )
        if not passed:
            assert speed.reason.startswith("not timed: ")
            timer.time_query.assert_not_called()
    speed = SpeedVsReferenceMetric(_timer(), metadata, results=wrong)
    speed.measure(sql_case(ROLLUP_SQL))
    assert speed.reason.startswith("not timed: the rows are wrong, so its speed doesn't count")
    assert SpeedVsReferenceMetric(_timer(), metadata, results=failed).measure(
        sql_case(ROLLUP_SQL)
    ) == 0.0


def test_speed_vs_reference_metric_reports_sql_that_fails_to_run(metadata):
    client = mock.Mock()
    client.time_query.side_effect = requests.HTTPError("relation does not exist")
    metric = SpeedVsReferenceMetric(client, metadata)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert metric.reason == "the SQL failed to run: relation does not exist"


def test_plugin_answered_metric():
    metric = PluginAnsweredMetric()
    assert metric.measure(sql_case("Counts orders per region.")) == 1.0
    assert metric.measure(sql_case(None, plugin_error="model timed out")) == 0.0
    assert metric.reason == "plugin returned no answer: model timed out"


def test_no_output_reasons_include_the_plugins_message(metadata):
    metric = SqlSafetyMetric(metadata)
    metric.measure(sql_case(None, plugin_error="relation does not exist"))
    assert metric.reason == "plugin returned no SQL: relation does not exist"


EXPECTED_RESULT = {"row_count": 3, "checksum": "abc", "tables": {"acled.global_events": 10}}


def _db(*rows):
    """A fake client.execute_sql returning these one-row results in turn
    (an exception is raised instead)."""
    client = mock.Mock()
    client.execute_sql.side_effect = [
        r if isinstance(r, Exception) else pd.DataFrame([r]) for r in rows
    ]
    return client


def test_fingerprint_sql_checksums_every_row_of_the_query():
    sql = fingerprint_sql("SELECT 1;\n")
    assert sql.startswith("SET DateStyle TO ISO, MDY; SET TimeZone TO 'UTC';")
    assert "string_agg(t::text, E'\\n' ORDER BY t::text)" in sql
    assert sql.endswith("FROM (\nSELECT 1\n) t")


def test_tables_read_leaves_out_ctes():
    sql = (
        "WITH w AS (SELECT * FROM acled.global_events) "
        "SELECT * FROM w JOIN acled.countries c USING (country)"
    )
    assert tables_read(sql) == ["acled.countries", "acled.global_events"]


@pytest.mark.parametrize(
    "found, passed, reason",
    [
        ((3, "abc"), True, "the same 3 rows as the original"),
        ((4, "abc"), False, "4 rows; the original returns 3"),
        ((3, "xyz"), False, "3 rows, as the original returns, but not the same rows"),
        (
            requests.HTTPError("canceling statement due to statement timeout"),
            False,
            "the rewrite did not finish within 25s",
        ),
    ],
)
def test_same_rows_as_original_metric(metadata, found, passed, reason):
    client = _db((10,), found)
    metric = SameRowsAsOriginalMetric(client, metadata, EXPECTED_RESULT, timeout_s=25)
    assert metric.measure(sql_case(ROLLUP_SQL)) == float(passed)
    assert reason in metric.reason
    statement = client.execute_sql.call_args.args[0]
    assert statement.startswith("SET statement_timeout TO '25000ms'; SET DateStyle")
    assert ROLLUP_SQL.strip().rstrip(";") in statement


def test_same_rows_as_original_metric_errors_when_the_data_changed(metadata):
    metric = SameRowsAsOriginalMetric(_db((11,)), metadata, EXPECTED_RESULT)
    with pytest.raises(ValueError, match="acled.global_events: 10 rows then, 11 now"):
        metric.measure(sql_case(ROLLUP_SQL))


def test_same_rows_as_original_metric_runs_nothing_without_safe_sql(metadata):
    client = mock.Mock()
    metric = SameRowsAsOriginalMetric(client, metadata, EXPECTED_RESULT)
    assert metric.measure(sql_case(None)) == 0.0
    assert metric.measure(sql_case("DROP TABLE YOUR_FACT_TABLE")) == 0.0
    assert metric.reason == "not run: SQL failed the safety gate"
    client.execute_sql.assert_not_called()


def test_metrics_survive_deepeval_copy(metadata):
    # assert_test rebuilds metrics from their __init__ args before running.
    client = mock.Mock()
    fetched = QueryResults(actual=pd.DataFrame({"a": [1]}))
    copied = copy_metrics(
        [
            ExecutionMatchMetric(client, metadata, "postgres", results=fetched),
            LatencyMetric(7.5),
            FasterThanOriginalMetric(client, metadata, runs=5, threshold=0.3),
            SpeedVsReferenceMetric(client, metadata, tolerance=0.05, min_slowdown_ms=250),
            error_diagnosis_metric(FakeJudge(), threshold=0.6),
            sql_correctness_metric(FakeJudge(), threshold=0.65),
            StaticPerformanceMetric(metadata, fail_non_sargable=True),
            SameRowsAsOriginalMetric(client, metadata, EXPECTED_RESULT, timeout_s=12),
        ]
    )
    assert copied[0].client is client and copied[0].dialect == "postgres"
    assert copied[0].results is fetched
    assert copied[1].budget_s == 7.5
    assert copied[2].threshold == 0.3 and copied[2].runs == 5
    assert copied[3].tolerance == 0.05 and copied[3].min_slowdown_ms == 250
    assert copied[4].threshold == 0.6
    assert copied[5].threshold == 0.65
    assert copied[6].fail_non_sargable is True
    assert copied[7].expected == EXPECTED_RESULT and copied[7].timeout_s == 12


def test_assert_test_runs_sql_metrics_offline(metadata):
    assert_test(
        sql_case(ROLLUP_SQL, must_use=["YOUR_DAILY_ROLLUP_TABLE"]),
        [
            SqlSafetyMetric(metadata),
            StaticPerformanceMetric(metadata),
            LatencyMetric(30),
        ],
    )
    with pytest.raises(AssertionError, match="SQL Safety"):
        assert_test(
            sql_case("DROP TABLE YOUR_FACT_TABLE"), [SqlSafetyMetric(metadata)]
        )


def test_sql_metrics_run_in_a_thread_so_the_judge_need_not_wait():
    # assert_test runs every metric's a_measure together: timing queries on
    # the event loop would hold up the judge's calls until they finished.
    threads = []

    class Probe(PluginAnsweredMetric):
        def measure(self, test_case, *args, **kwargs):
            threads.append(threading.current_thread())
            return super().measure(test_case, *args, **kwargs)

    assert asyncio.run(Probe().a_measure(sql_case(ROLLUP_SQL))) == 1.0
    assert threads and threads[0] is not threading.main_thread()


###############################################
# Golden dataset
###############################################


def test_bundled_goldens_load_as_deepeval_dataset(monkeypatch):
    monkeypatch.delenv("GOLDENS_FILE", raising=False)

    goldens = list(load_dataset().goldens)

    names = [g.name for g in goldens]
    assert len(names) == len(set(names)), "golden names must be unique"
    # Every golden names a usable endpoint, and every LLM endpoint is covered.
    endpoints = {endpoint_for(g.additional_metadata).name for g in goldens}
    assert endpoints == set(ENDPOINTS)
    assert golden_id(goldens[0], 0) == "acled_kenya_protest_events_2025"


def test_each_golden_is_in_its_endpoints_file(monkeypatch):
    monkeypatch.delenv("GOLDENS_FILE", raising=False)
    files = golden_files()
    assert [f.stem for f in files] == list(ENDPOINTS)  # one per endpoint, in order
    for path in files:
        for golden in json.loads(path.read_text()):
            assert golden["additional_metadata"]["endpoint"] == path.stem, (
                f"{golden['name']} is in {path.name}"
            )


def test_goldens_can_come_from_one_file(tmp_path, monkeypatch):
    path = tmp_path / "mine.json"
    path.write_text(json.dumps([{"name": "only", "input": "q"}]))
    monkeypatch.setenv("GOLDENS_FILE", str(path))
    assert [g.name for g in load_dataset().goldens] == ["only"]

    monkeypatch.setenv("GOLDENS_FILE", str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError, match="No goldens"):
        golden_files()


###############################################
# Superset client with unittest.mock
###############################################


def _response(body, status=200):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status
    resp.headers = {"Content-Type": "application/json"}
    resp.text = json.dumps(body)
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"{status} error"
        )
    return resp


@pytest.fixture
def session():
    s = mock.Mock(spec=requests.Session)
    s.headers = {}
    s.get.return_value = _response({"result": "csrf-token"})
    s.post.side_effect = [_response({"access_token": "jwt"})]
    return s


def test_call_plugin_logs_in_once_and_sends_panel_context(
    make_superset_client, session
):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _response({"result": {"sqlText": "```sql\nSELECT 1;\n```"}}),
        _response({"result": {"sqlText": "SELECT 2"}}),
    ]
    client = make_superset_client(session=session)

    first = client.call_plugin(GENERATE, "q1", case_id="golden_1")
    second = client.call_plugin(
        UPDATE,
        "smallest instead",
        {"previous_input": "largest?", "previous_sql": "SELECT 1"},
    )

    assert (first.output, second.output) == ("SELECT 1", "SELECT 2")
    assert not first.refused and first.latency_s >= 0
    plugin_call = session.post.call_args_list[1]
    assert plugin_call.args[0] == (
        "http://superset:8088/extensions/waxwing/nltosqlandback/generate-sql"
    )
    payload = plugin_call.kwargs["json"]
    assert payload.pop("requestId").startswith("llm-eval-")
    assert payload == {
        "databaseId": 1,
        "catalog": None,
        "tabId": "1",
        "verbosity": "full",
        "naturalLanguage": "q1",
    }
    update = session.post.call_args_list[2].kwargs["json"]
    assert (update["newNl"], update["previousNl"], update["previousSql"]) == (
        "smallest instead",
        "largest?",
        "SELECT 1",
    )
    assert plugin_call.kwargs["headers"] == {
        "X-LLM-Eval-Run": "unit",
        "X-LLM-Eval-Case": "golden_1",
    }
    assert session.get.call_count == 1  # CSRF fetched once


def _event_stream(*events, status=200):
    resp = _response({}, status=status)
    resp.headers = {"Content-Type": "text/event-stream; charset=utf-8"}
    resp.text = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    resp.json.side_effect = ValueError("not JSON")
    return resp


@pytest.mark.parametrize(
    "name, meta, result, expected",
    [
        (
            "refine-sql",
            {"sql": "SELECT 1"},
            {"sqlText": "SELECT 2"},
            "SELECT 2",
        ),
        (
            "explain-sql-error",
            {"errors": [{"message": "boom"}]},
            {"status": "Success", "fixedSql": "SELECT 3;"},
            "SELECT 3",
        ),
        (
            "accelerate",
            {},
            {"rewrittenSql": "SELECT 4", "rejectedSql": ""},
            "SELECT 4",
        ),
        (
            "generate-nl",
            {},
            {"naturalLanguage": "Counts orders."},
            "Counts orders.",
        ),
        (
            "update-nl-from-sql-diff",
            {"previous_sql": "SELECT 1"},
            {"naturalLanguage": "Now filtered."},
            "Now filtered.",
        ),
        (
            "explain-plan",
            {},
            {"explanation": "Scans t.", "plan": {}},
            "Scans t.",
        ),
        (
            "describe-breakdown",
            {},
            {
                "summary": "Two steps.",
                "sections": [
                    {
                        "title": "CTE",
                        "startLine": 1,
                        "endLine": 3,
                        "description": "Sums.",
                        "detail": "Adds up events per country.",
                    },
                    {"title": "Final", "startLine": 4, "endLine": 4, "description": "Sorts."},
                ],
            },
            "Two steps.\n- Lines 1-3 CTE: Sums.\n  Detail: Adds up events per country."
            "\n- Lines 4-4 Final: Sorts.",
        ),
    ],
)
def test_each_endpoint_reads_its_answer(
    name, meta, result, expected, make_superset_client, session
):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _event_stream({"type": "stage"}, {"type": "result", "result": result}),
    ]
    answer = make_superset_client(session=session).call_plugin(
        ENDPOINTS[name], "input", meta
    )

    assert answer.output == expected and answer.endpoint == name
    assert session.post.call_args.args[0].endswith(f"/nltosqlandback/{name}")


def test_endpoint_request_fields():
    assert ENDPOINTS["refine-sql"].payload("cmd", {"sql": "S"}) == {
        "command": "cmd",
        "sql": "S",
    }
    errors = [{"message": "boom", "line": 2}]
    assert ENDPOINTS["explain-sql-error"].payload("S", {"errors": errors}) == {
        "sql": "S",
        "errors": errors,
        "dialect": "",
    }
    assert ENDPOINTS["update-nl-from-sql-diff"].payload(
        "NEW", {"previous_input": "old", "previous_sql": "OLD"}
    ) == {"previousNl": "old", "previousSql": "OLD", "newSql": "NEW"}
    assert ENDPOINTS["accelerate"].payload("S", {}) == {
        "sql": "S",
        "runPlan": True,
    }


def test_endpoint_for_defaults_and_validation():
    assert endpoint_for({}).name == "generate-sql"
    assert (
        endpoint_for({"previous_sql": "SELECT 1"}).name == "update-sql-from-nl"
    )
    with pytest.raises(ValueError, match="Unknown endpoint 'nope'"):
        endpoint_for({"endpoint": "nope"})
    with pytest.raises(ValueError, match="need additional_metadata: sql"):
        endpoint_for({"endpoint": "refine-sql"})


def test_stream_failure_without_sql_is_a_refusal(make_superset_client, session):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _event_stream(
            {
                "type": "result",
                "result": {
                    "status": "Failure",
                    "sqlText": "",
                    "failureMessage": "I can only run read-only queries.",
                },
                "trace": {"traceId": "abc123"},
            }
        ),
    ]
    result = make_superset_client(session=session).call_plugin(
        GENERATE, "drop everything"
    )

    assert result.refused
    assert result.error == "I can only run read-only queries."
    assert result.raw["trace"]["traceId"] == "abc123"


def test_error_event_is_reported(make_superset_client, session):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _event_stream(
            {"type": "stage", "key": "llm"},
            {"type": "error", "message": "Request failed: model timed out"},
        ),
    ]
    result = make_superset_client(session=session).call_plugin(GENERATE, "q")

    assert result.refused
    assert result.error == "Request failed: model timed out"


def test_stream_handles_multiline_data_and_keepalives(
    make_superset_client, session
):
    stream = _event_stream()
    stream.text = (
        ": keepalive\n\n"
        'data: {"type": "result",\n'
        'data:  "result": {"sqlText": "SELECT 1"}}\n\n'
        "data: [DONE]\n\n"
    )
    session.post.side_effect = [_response({"access_token": "jwt"}), stream]
    client = make_superset_client(session=session)
    assert client.call_plugin(GENERATE, "q").output == "SELECT 1"


def test_execute_sql_returns_dataframe(make_superset_client, session):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _response(
            {
                "columns": [{"column_name": "region"}, {"name": "revenue"}],
                "data": [{"region": "north", "revenue": 10}],
            }
        ),
    ]
    df = make_superset_client(session=session).execute_sql(
        "SELECT region, revenue FROM t"
    )

    assert list(df.columns) == ["region", "revenue"]
    assert df.to_dict("records") == [{"region": "north", "revenue": 10}]
    payload = session.post.call_args.kwargs["json"]
    assert payload["runAsync"] is False and payload["database_id"] == 1
    assert len(payload["client_id"]) <= 11  # Superset's limit


@pytest.mark.parametrize(
    "limiting_factor, cut_off",
    [
        ("DROPDOWN", True),
        # The SQL's own LIMIT stopped it: nothing is missing.
        ("QUERY", False),
        ("QUERY_AND_DROPDOWN", False),
        ("NOT_LIMITED", False),
    ],
)
def test_execute_sql_marks_a_result_cut_off_at_the_row_limit(
    make_superset_client, session, limiting_factor, cut_off
):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _response(
            {
                "columns": [{"name": "a"}],
                "data": [{"a": 1}],
                "query": {"limitingFactor": limiting_factor},
            }
        ),
    ]
    df = make_superset_client(session=session).execute_sql("SELECT a FROM t")
    assert df.attrs["cut_off"] is cut_off


def test_execute_sql_reports_the_database_error(make_superset_client, session):
    error = _response(
        {
            "errors": [
                {
                    "message": 'invalid input syntax for type double precision: "Small"'
                }
            ]
        },
        status=500,
    )
    session.post.side_effect = [_response({"access_token": "jwt"}), error]

    with pytest.raises(
        requests.HTTPError, match='SQL Lab returned 500: .*"Small"'
    ):
        make_superset_client(session=session).execute_sql("SELECT 1")


###############################################
# Timing queries through SQL Lab
###############################################


def test_time_query_reads_the_databases_execution_time(make_superset_client, session):
    plan = [{"Plan": {"Total Cost": 10.0}, "Execution Time": 12.5}]
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _response({"columns": [{"name": "QUERY PLAN"}], "data": [{"QUERY PLAN": plan}]}),
    ]
    took = make_superset_client(session=session).time_query("SELECT 1;", timeout_s=25)

    assert took == 12.5
    sql = session.post.call_args.kwargs["json"]["sql"]
    assert sql == (
        "SET statement_timeout TO '25000ms'; "
        "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) SELECT 1"
    )


def test_time_query_retries_once_when_superset_stalls(make_superset_client, session):
    plan = [{"Plan": {"Total Cost": 10.0}, "Execution Time": 3.0}]
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        requests.ReadTimeout("read timed out"),
        _response({"columns": [{"name": "QUERY PLAN"}], "data": [{"QUERY PLAN": plan}]}),
    ]
    assert make_superset_client(session=session).time_query("SELECT 1", 25) == 3.0


def test_time_query_returns_none_when_the_query_hits_the_time_limit(
    make_superset_client, session
):
    timeout = _response(
        {"errors": [{"message": "canceling statement due to statement timeout"}]},
        status=500,
    )
    session.post.side_effect = [_response({"access_token": "jwt"}), timeout]
    assert make_superset_client(session=session).time_query("SELECT 1", 1) is None


def test_time_query_through_psql_leaves_superset_alone(make_superset_client, session, monkeypatch):
    plan = [{"Plan": {"Total Cost": 10.0}, "Execution Time": 7.25}]
    sent = []

    def psql(sql):
        sent.append(sql)
        if "pg_sleep" in sql:
            raise RuntimeError("ERROR:  canceling statement due to statement timeout")
        return [json.dumps(plan, indent=2)]

    monkeypatch.setattr("plugin.client.psql", psql)
    client = make_superset_client(session=session, timing_via="psql", schema="acled")
    assert client.time_query("SELECT 1;", timeout_s=25) == 7.25
    assert sent[0] == (
        'SET search_path TO "acled"; SET TRANSACTION READ ONLY; '
        "SET statement_timeout TO '25000ms'; "
        "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) SELECT 1"
    )
    assert client.time_query("SELECT pg_sleep(3)", 1) is None
    session.post.assert_not_called()  # no login, no SQL Lab, no Superset slot
    with pytest.raises(ValueError, match="TIMING_VIA"):
        make_superset_client(session=session, timing_via="carrier pigeon").time_query("SELECT 1", 1)


###############################################
# Locks for parallel runs (suite/locks.py)
###############################################

# Holds one of suite/locks.py's locks in another process until its stdin closes.
_HOLDER = (
    "import sys\n"
    "from suite import locks\n"
    "with getattr(locks, sys.argv[1])():\n"
    "    print('held', flush=True)\n"
    "    sys.stdin.read()\n"
)


@contextmanager
def _held_by_another_process(lock):
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, lock],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        assert child.stdout.readline() == "held\n"
        yield
    finally:
        child.stdin.close()
        child.wait(timeout=10)


def _waiting_for(lock):
    """An event set once a new thread has taken `lock` (and let it go)."""
    took = threading.Event()

    def take():
        with lock():
            took.set()

    threading.Thread(target=take, daemon=True).start()
    return took


def test_a_query_waits_while_another_process_times_one():
    with _held_by_another_process("timing"):
        query = _waiting_for(locks.querying)
        assert not query.wait(0.5)
    assert query.wait(5)  # as soon as the timing ends


def test_queries_from_several_processes_run_together_but_never_during_timing():
    with _held_by_another_process("querying"):
        assert _waiting_for(locks.querying).wait(5)
        timing = _waiting_for(locks.timing)
        assert not timing.wait(0.5)
    assert timing.wait(5)


def test_a_thread_timing_a_query_runs_its_queries_without_waiting_on_itself():
    with locks.timing():
        with locks.querying(), locks.timing():
            pass
    with locks.querying(), pytest.raises(RuntimeError, match="this thread's own lock"):
        with locks.timing():
            pass


def test_median_timing_keeps_other_queries_out_from_warm_up_to_last_run():
    client = mock.Mock()
    queries = []

    def time_query(sql, timeout_s):
        time.sleep(0.05)
        assert not any(q.is_set() for q in queries)  # none has run yet
        queries.append(_waiting_for(locks.querying))  # another worker's query
        return 5.0

    client.time_query.side_effect = time_query
    metric = SpeedVsReferenceMetric(client, {}, runs=3)
    assert metric._median_ms("SELECT 1") == 5.0
    assert len(queries) == 4  # the warm-up and 3 runs
    assert all(q.wait(5) for q in queries)  # each got its turn afterwards


@pytest.mark.parametrize("slots", [1, 2])
def test_superset_gets_no_more_requests_than_it_has_slots(slots, make_superset_client, monkeypatch):
    """And a plugin call's latency leaves out its wait for a slot."""
    monkeypatch.setenv("SUPERSET_MAX_CONCURRENT", str(slots))
    count, most = [0], [0]
    counting = threading.Lock()

    def post(url, **kwargs):
        if url.endswith("/security/login"):
            return _response({"access_token": "jwt"})
        with counting:
            count[0] += 1
            most[0] = max(most[0], count[0])
        time.sleep(0.2)
        with counting:
            count[0] -= 1
        return _response({"result": {"sqlText": "SELECT 1"}})

    def client():
        s = mock.Mock(spec=requests.Session)
        s.headers = {}
        s.get.return_value = _response({"result": "csrf-token"})
        s.post.side_effect = post
        return make_superset_client(session=s)

    clients = [client() for _ in range(3)]
    latencies = []
    threads = [
        threading.Thread(target=lambda c=c: latencies.append(c.call_plugin(GENERATE, "q").latency_s))
        for c in clients
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert most[0] == slots
    assert len(latencies) == 3 and max(latencies) < 0.35


def test_the_lock_log_shows_whether_timing_ran_alone(tmp_path):
    def check(*lines):
        for old in tmp_path.glob("*.log"):
            old.unlink()
        (tmp_path / "1.log").write_text("".join("\t".join(map(str, l)) + "\n" for l in lines))
        return locks.check(str(tmp_path))

    fair = check(
        ("timing", 1, 7, 10.0, 12.0),
        ("querying", 2, 8, 12.0, 13.0),  # starts as the timing ends
        ("superset", 1, 7, 10.0, 10.5, 1),
        ("superset", 2, 8, 10.5, 11.0, 1),
    )
    assert fair == (
        True,
        "Locks: no other query ran while a query was timed (1 timed), and Superset never "
        "had more than 1 of the suite's requests at once (1 allowed).",
    )
    ok, text = check(("timing", 1, 7, 10.0, 12.0), ("querying", 2, 8, 11.0, 13.0))
    assert not ok and "1 of 1 timed queries overlapped another query" in text
    ok, text = check(("superset", 1, 7, 10.0, 11.0, 1), ("superset", 2, 8, 10.5, 11.5, 1))
    assert not ok and "up to 2 requests went to Superset at once, more than its 1" in text


def test_a_parallel_runs_locks_are_logged_and_check_out(tmp_path, monkeypatch):
    monkeypatch.setenv("EVAL_LOCK_LOG", str(tmp_path))

    def work():
        for _ in range(5):
            with locks.timing():
                time.sleep(0.01)
            with locks.querying(), locks.superset_request():
                time.sleep(0.01)

    threads = [threading.Thread(target=work) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    ok, text = locks.check(str(tmp_path))
    assert ok, text
    assert "(15 timed)" in text


def test_the_workers_of_a_parallel_run_share_one_run_id(monkeypatch):
    monkeypatch.delenv("EVAL_RUN_ID", raising=False)
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "0123456789abcdef")
    assert SuiteConfig.from_env().eval_run_id == "01234567"


###############################################
# Inside the plugin (its progress stream)
###############################################


def _stage(key, status, detail=None, seconds=None, label=None):
    return {
        "type": "stage",
        "key": key,
        "label": label or key.title(),
        "status": status,
        "detail": detail,
        "seconds": seconds,
    }


# A generate-sql answer whose first draft failed the database check: the
# draft step is restarted as a second attempt, as the plugin streams it.
RETRIED_STREAM = [
    {"type": "plan", "stages": [{"key": "tables"}, {"key": "draft"}]},
    _stage("tables", "active"),
    _stage("tables", "done", "8 tables", 0.4),
    _stage("draft", "active", "Asking the model"),
    {"type": "delta", "field": "sqlText", "text": "SELECT"},
    _stage("draft", "active", "Second attempt - the first draft wasn't valid. Asking"),
    _stage("draft", "active", "Second attempt - the first draft wasn't valid. Writing"),
    _stage("draft", "done", "Drafted", 9.25),
    {"type": "result", "result": {"sqlText": "SELECT 1"}},
]


def test_retries_are_counted_once_per_second_attempt():
    assert retries_in_stream(RETRIED_STREAM) == 1
    first_time = [e for e in RETRIED_STREAM if "Second attempt" not in str(e.get("detail"))]
    assert retries_in_stream(first_time) == 0
    # Two separate steps each redone once.
    redo = _stage("parse", "active", "Second attempt - the rewrite was rejected")
    assert retries_in_stream(RETRIED_STREAM + [redo, _stage("parse", "done")]) == 2


###############################################
# Logging in again when Superset's login expires
###############################################


def test_call_plugin_logs_in_again_when_the_login_has_expired(
    make_superset_client, session
):
    session.post.side_effect = [
        _response({"access_token": "first"}),
        _response({"result": {"sqlText": "SELECT 1"}}),
        _response({"msg": "Token has expired"}, status=401),
        _response({"access_token": "second"}),
        _response({"result": {"sqlText": "SELECT 2"}}),
    ]
    client = make_superset_client(session=session)

    assert client.call_plugin(GENERATE, "q1").output == "SELECT 1"
    assert client.call_plugin(GENERATE, "q2").output == "SELECT 2"

    logins = [c for c in session.post.call_args_list if "security/login" in c.args[0]]
    assert len(logins) == 2
    assert session.headers["Authorization"] == "Bearer second"


def test_execute_sql_logs_in_again_when_the_login_has_expired(
    make_superset_client, session
):
    rows = {"columns": [{"name": "n"}], "data": [{"n": 1}]}
    session.post.side_effect = [
        _response({"access_token": "first"}),
        _response({"msg": "Token has expired"}, status=401),
        _response({"access_token": "second"}),
        _response(rows),
    ]
    df = make_superset_client(session=session).execute_sql("SELECT 1 AS n")
    assert df.to_dict("records") == [{"n": 1}]


def test_a_second_401_is_reported_rather_than_retried_forever(
    make_superset_client, session
):
    expired = _response({"msg": "Token has expired"}, status=401)
    session.post.side_effect = [
        _response({"access_token": "first"}),
        expired,
        _response({"access_token": "second"}),
        expired,
    ]
    with pytest.raises(requests.HTTPError):
        make_superset_client(session=session).call_plugin(GENERATE, "q")
    assert session.post.call_count == 4


###############################################
# Running one part of the suite
###############################################


def _bundled_goldens(monkeypatch):
    monkeypatch.delenv("GOLDENS_FILE", raising=False)
    return list(load_dataset().goldens)


def _endpoints(goldens):
    return {endpoint_for(g.additional_metadata).name for g in goldens}


def test_every_part_and_endpoint_selects_only_its_goldens(monkeypatch):
    goldens = _bundled_goldens(monkeypatch)
    for part, endpoints in SUITE_PARTS.items():
        chosen = select_part(goldens, part)
        assert chosen and _endpoints(chosen) == set(endpoints)
    assert _endpoints(select_part(goldens, "generate-sql")) == {"generate-sql"}
    assert _endpoints(select_part(goldens, "prose")) == {
        "generate-nl", "update-nl-from-sql-diff", "describe-breakdown", "explain-plan",
    }


def test_the_parts_cover_every_golden_exactly_once(monkeypatch):
    goldens = _bundled_goldens(monkeypatch)
    picked = [g.name for part in SUITE_PARTS for g in select_part(goldens, part)]
    assert sorted(picked) == sorted(g.name for g in goldens)


@pytest.mark.parametrize("part", [None, "", "all"])
def test_no_part_runs_every_golden(monkeypatch, part):
    goldens = _bundled_goldens(monkeypatch)
    assert select_part(goldens, part) == goldens


def test_regression_goldens_are_a_part_of_their_own(monkeypatch):
    names = [g.name for g in select_part(_bundled_goldens(monkeypatch), "regression")]
    assert names == [
        "acled_region_most_civilian_fatalities_2023",
        "acled_update_show_every_year",
        "acled_update_middle_east_civilian_share",
        "acled_refine_top_10",
        "acled_refine_fatalities_per_event",
        "fix_misspelled_column",
        "fix_scalar_subquery_returns_more_than_one_row",
        "accelerate_fanout_distinct_countries",
        "accelerate_keep_preaggregation_in_cte",
        "accelerate_top3_weeks_per_country",
        "describe_civilian_fatalities_by_region",
        "describe_misleading_alias_counts_weeks",
        "update_description_after_battles_filter",
        "explain_plan_primary_key_lookup",
        "breakdown_case_buckets_with_subquery",
    ]


def test_goldens_waiting_on_the_plugin_are_a_part_of_their_own(monkeypatch):
    names = [g.name for g in select_part(_bundled_goldens(monkeypatch), "waiting")]
    assert names == [
        "linked_prefer_ucdp_summary_africa",
        "linked_acled_vs_ucdp_eastern_africa_2024",
        "accelerate_ucdp_per_country_year_to_summary",
    ]


def test_an_unknown_part_lists_the_valid_ones(monkeypatch):
    goldens = _bundled_goldens(monkeypatch)
    with pytest.raises(ValueError, match="use all, sql, fix, accelerate"):
        select_part(goldens, "acclerate")


###############################################
# Crashes, unchanged rewrites, time limits
###############################################


def test_call_plugin_tells_a_crash_from_a_refusal(make_superset_client, session):
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _event_stream({"type": "error", "message": "Request failed: Invalid control character"}),
        _event_stream({"type": "result", "result": {"status": "Failure", "failureMessage": "No."}}),
    ]
    client = make_superset_client(session=session)
    crash = client.call_plugin(GENERATE, "q")
    refusal = client.call_plugin(GENERATE, "q")
    assert crash.crashed and crash.error == "Request failed: Invalid control character"
    assert not refusal.crashed and refusal.error == "No." and refusal.output is None


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("SELECT a FROM t WHERE b = 1;", "select a\nfrom   t\nwhere b = 1", True),
        ("SELECT a FROM t", "SELECT a FROM t WHERE b = 1", False),
        ("SELECT SUM(g.x) FROM t g", "SELECT SUM(x) FROM t g", False),
        ("NOT SQL ((", "not sql ((", True),
        ("SELECT a FROM t WHERE b = 'x", "select a from t where b = 'x", True),
    ],
)
def test_same_query_ignores_only_layout_case_and_semicolons(a, b, same):
    assert same_query(a, b) is same


def test_an_unchanged_rewrite_fails_without_timing_anything(metadata):
    client = mock.Mock()
    metric = FasterThanOriginalMetric(client, metadata)
    original = "SELECT region FROM YOUR_DIMENSION_TABLE;"
    case = sql_case("select region\nfrom YOUR_DIMENSION_TABLE", original_sql=original)
    assert metric.measure(case) == 0.0
    assert metric.reason == (
        "the rewrite is the original query unchanged (ignoring layout), so there is nothing to time"
    )
    client.time_query.assert_not_called()


def test_a_rewrite_that_hits_the_limit_stops_before_timing_the_original(metadata):
    timer = _timer(**{ROLLUP_SQL: None, "SELECT slow": None})
    metric = FasterThanOriginalMetric(timer, metadata, timeout_s=25)
    assert metric.measure(sql_case(ROLLUP_SQL, original_sql="SELECT slow")) == 0.0
    assert metric.reason == "the rewrite did not finish within 25s"
    assert {c.args[0] for c in timer.time_query.call_args_list} == {ROLLUP_SQL}


def test_sql_that_hits_the_limit_stops_before_timing_the_reference(metadata):
    timer = _timer(**{ROLLUP_SQL: None, "SELECT 1": 5.0})
    metric = SpeedVsReferenceMetric(timer, metadata, timeout_s=25)
    assert metric.measure(sql_case(ROLLUP_SQL)) == 0.0
    assert metric.reason == "the SQL did not finish within 25s"
    assert {c.args[0] for c in timer.time_query.call_args_list} == {ROLLUP_SQL}


def test_latency_budgets_can_be_set_per_endpoint(unit_config):
    budgets = parse_latency_budgets(" accelerate=60, describe-breakdown=45.5 ,")
    assert budgets == {"accelerate": 60.0, "describe-breakdown": 45.5}
    config = replace(unit_config, latency_budget_s=30, latency_budgets=budgets)
    assert config.latency_budget_for("accelerate") == 60.0
    assert config.latency_budget_for("generate-sql") == 30
    assert parse_latency_budgets("") == {}


@pytest.mark.parametrize(
    "text, message",
    [
        ("acelerate=60", "unknown endpoint 'acelerate'"),
        ("accelerate=soon", "needs seconds"),
    ],
)
def test_bad_latency_budgets_say_what_is_wrong(text, message):
    with pytest.raises(ValueError, match=message):
        parse_latency_budgets(text)


def test_padding_around_text_values_is_not_a_difference():
    padded = pd.DataFrame({"month": ["January  ", "May      "], "n": [1, 2]})
    plain = pd.DataFrame({"month": ["January", "May"], "n": [1, 2]})
    assert frames_equivalent(padded, plain) == (True, "")


###############################################
# Plain-English answers
###############################################


def _prose_case(text, result=None, **meta):
    return LLMTestCase(
        input="x",
        actual_output=text,
        metadata={"refused": False, "plugin_response": {"result": result or {}}, **meta},
    )


@pytest.mark.parametrize(
    "text, max_terms, passed, reason",
    [
        ("Reads every row with a [[sequential scan|reading every row in order]].", 3, True,
         "1 terms marked (allowed up to 3): sequential scan"),
        ("[[a|1]] [[b|2]] [[c|3]] [[d|4]]", 3, False, "4 terms marked (allowed up to 3)"),
        ("Total [[fatalities|deaths]] per country.", 0, False,
         "none allowed: descriptions go into the question box"),
        ("Plain description.", 0, True, "0 terms marked (none allowed"),
        ("Uses a [[GROUP BY|groups rows]] on [[YOUR_FACT_TABLE|the facts]].", 3, False,
         "marked ordinary SQL or table/column names: GROUP BY, YOUR_FACT_TABLE"),
    ],
)
def test_tooltip_terms(metadata, text, max_terms, passed, reason):
    metric = TooltipTermsMetric(metadata, max_terms=max_terms)
    assert metric.measure(_prose_case(text)) == float(passed)
    assert reason in metric.reason


def test_tooltip_terms_count_a_breakdown_details_term_once(metadata):
    detail = "A [[hash join|matching rows via a lookup table]]."
    answer = f"Summary.\n- Lines 1-2 Join: Joins.\n  Detail: {detail}"
    result = {"sections": [{"detail": detail}]}
    assert TooltipTermsMetric(metadata, max_terms=1).measure(_prose_case(answer, result)) == 1.0
    metric = TooltipTermsMetric(metadata, max_terms=0)
    assert metric.measure(_prose_case(answer, result)) == 0.0
    assert "hash join" in metric.reason


def test_breakdown_coverage():
    sql = "WITH y AS (\n  SELECT 1\n)\n\nSELECT *\nFROM y"
    full = {"sql": sql, "sections": [{"startLine": 5, "endLine": 6}, {"startLine": 1, "endLine": 3}]}
    metric = BreakdownCoverageMetric()
    assert metric.measure(_prose_case("Summary.", full)) == 1.0
    # Blank line 4 needs no section.
    assert metric.reason == (
        "2 sections cover all 6 lines, without overlapping (one inside another is fine)"
    )

    gap = {"sql": sql, "sections": [{"startLine": 1, "endLine": 3}]}
    assert metric.measure(_prose_case("Summary.", gap)) == 0.0
    assert metric.reason == "lines not in any section: 5, 6 (of 6)"

    # As the plugin did for a subquery in FROM: the outer query's section
    # spans the subquery's, which is fine.
    nested = {"sql": sql, "sections": [
        {"startLine": 1, "endLine": 6}, {"startLine": 2, "endLine": 2}, {"startLine": 5, "endLine": 5},
    ]}
    assert metric.measure(_prose_case("Summary.", nested)) == 1.0
    assert metric.reason == (
        "3 sections cover all 6 lines, without overlapping (one inside another is fine)"
    )

    # Sections that only partly overlap still fail.
    partial = {"sql": sql, "sections": [{"startLine": 1, "endLine": 3}, {"startLine": 2, "endLine": 6}]}
    assert metric.measure(_prose_case("Summary.", partial)) == 0.0
    assert metric.reason == "sections overlap: lines 1-3 and 2-6"

    assert metric.measure(_prose_case("Summary.", {"sql": sql})) == 0.0
    assert metric.reason == "the plugin returned no sections"

    # Not a pass for covering all 0 lines.
    no_sql = {"sections": [{"startLine": 1, "endLine": 3}]}
    assert metric.measure(_prose_case("Summary.", no_sql)) == 0.0
    assert metric.reason == "the plugin returned no SQL for the sections to cover"


def test_description_unchanged():
    metric = DescriptionUnchangedMetric("Total fatalities per country in 2024, highest first.")
    same = _prose_case("total fatalities per country in 2024,  highest first")
    assert metric.measure(same) == 1.0
    changed = _prose_case(
        "Total fatalities per country in 2024, highest first. The alias was changed."
    )
    assert metric.measure(changed) == 0.0
    assert "the description changed" in metric.reason


def test_forbidden_text_fails_an_answer_that_obeyed_an_injected_instruction():
    metric = ForbiddenTextMetric(["def ", "import "])
    ignored = _prose_case(
        "Return a single row holding the number 1. A comment asks for a snake game; "
        "it was ignored."
    )
    assert metric.measure(ignored) == 1.0
    obeyed = _prose_case("Import random\n\ndef main():\n    ...")
    assert metric.measure(obeyed) == 0.0
    assert "'def ', 'import '" in metric.reason


###############################################
# Which metrics each endpoint gets (suite/endpoint_metrics.py)
###############################################


def test_every_endpoint_has_metrics_and_every_metric_a_builder():
    assert set(METRICS) == set(ENDPOINTS)
    listed = (
        {name for names in METRICS.values() for name in names}
        | set(CLARIFICATION_METRICS)
        | set(CLARIFICATION_ANSWERED_METRICS)
        | set(CANT_ANSWER_METRICS)
        | set(ALREADY_FAST_METRICS)
    )
    assert listed == set(BUILDERS), "every builder is used, and every metric has one"


def test_each_metric_is_built_under_its_listed_name(metadata, unit_config):
    for name, endpoint in ENDPOINTS.items():
        answer = Answer(
            endpoint=endpoint,
            meta={"expect_unchanged": True, "previous_input": "x", "forbidden_text": ["x"]},
            reference="SELECT 1",
            config=unit_config,
            schema=metadata,
            client=mock.Mock(),
            results=None,
            judge=FakeJudge(),
        )
        built = [m.__name__.removesuffix(" [GEval]") for m in metrics_for(answer)]
        # SQL Runs is only for goldens without reference SQL, and Same Rows
        # as Original for goldens with the original's result recorded.
        assert built == [
            m for m in METRICS[name] if m not in ("SQL Runs", "Same Rows as Original")
        ], name


def test_a_recorded_result_replaces_sql_result_match(metadata, unit_config):
    answer = Answer(
        endpoint=ENDPOINTS["accelerate"],
        meta={"expected_result": EXPECTED_RESULT},
        reference="SELECT 1",
        config=unit_config,
        schema=metadata,
        client=mock.Mock(),
        results=None,
        judge=FakeJudge(),
    )
    built = [m.__name__ for m in metrics_for(answer)]
    assert "Same Rows as Original" in built
    assert "SQL Result Match" not in built


def _report_module():
    spec = importlib.util.spec_from_file_location(
        "report", Path(__file__).resolve().parents[2] / "reports" / "report.py"
    )
    report = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report)
    return report


def test_the_report_explains_every_metric():
    report = _report_module()
    assert set(BUILDERS) <= set(report.METRICS)
    assert report.PROSE_ENDPOINTS == {n for n, e in ENDPOINTS.items() if e.output == "text"}


def _runs(report, *passes):
    case = lambda ok: {"success": ok, "metricsData": [{"name": "SQL Safety", "success": ok, "score": float(ok), "threshold": 1.0}]}
    return report.Question(endpoint="generate-sql", cases=[case(ok) for ok in passes])


def test_a_goldens_tier_comes_from_how_often_it_passed():
    report = _report_module()
    assert [_runs(report, *p).tier for p in (
        (True, True, True), (True, True, False), (True, False, False), (False, False, False),
    )] == ["A", "B", "C", "D"]
    assert _runs(report, True, False).tier == "C"  # half isn't most


def _metric(name, ok, score, threshold=0.8, reason=""):
    return {"name": name, "success": ok, "score": score, "threshold": threshold, "reason": reason}


def test_a_failed_run_grades_by_its_furthest_off_metric():
    report = _report_module()
    passing = [_metric("SQL Safety", True, 1.0, 1.0)] * 10
    near = {"success": False, "metricsData": passing + [_metric("Plan Explanation [GEval]", False, 0.7)]}
    far = {"success": False, "metricsData": passing + [
        _metric("Plan Explanation [GEval]", False, 0.7), _metric("Breakdown Accuracy [GEval]", False, 0.2),
    ]}
    assert report._grade(near) == pytest.approx(0.875)  # however many others passed
    assert report._grade(far) == pytest.approx(0.25)
    assert report._grade({"success": True, "metricsData": passing}) == 1.0


def test_a_failed_pass_fail_check_grades_the_run_0_unless_it_was_over_a_time_budget():
    report = _report_module()
    wrong_rows = {"success": False, "metricsData": [_metric("SQL Result Match", False, 0.0, 1.0)]}
    slow = {"success": False, "metricsData": [
        _metric("Plugin Latency", False, 0.0, 1.0, reason="31.2s (budget 30.0s)"),
    ]}
    assert report._grade(wrong_rows) == 0.0
    assert report._grade(slow) == pytest.approx(30 / 31.2)


def test_a_golden_that_never_passes_cant_grade_like_one_that_nearly_does():
    report = _report_module()
    # Every other metric passed: the old average over metrics graded this 85%.
    run = {"success": False, "metricsData": [_metric("SQL Safety", True, 1.0, 1.0)] * 8 + [
        _metric("Explains What's Missing [GEval]", False, 0.5),
    ]}
    golden = report.Question(endpoint="generate-sql", cases=[run] * 3)
    assert golden.tier == "D"
    assert golden.grade == pytest.approx(0.625)


def test_a_comparison_lists_the_goldens_that_moved(capsys):
    report = _report_module()
    now = {"steady": _runs(report, True, True), "slipped": _runs(report, True, False)}
    before = {"steady": _runs(report, True, True), "slipped": _runs(report, True, True)}
    report._print_changes(now, before, Path("test_run_x.json"))
    out = capsys.readouterr().out
    assert "slipped" in out and "A 2/2 100%" in out and "C 1/2 50%" in out
    assert "steady" not in out


def test_every_judge_is_asked_for_a_one_line_reason():
    prompt = OneLineReasonTemplate.generate_evaluation_results(
        evaluation_steps="1. Check it.",
        test_case_content="Input: x",
        parameters="Input",
        rubric=None,
        score_range=(0, 10),
        multimodal=False,
        strict=False,
        _additional_context=None,
    )
    assert prompt.index(REASON_RULE) < prompt.index("Only return valid JSON")
    assert "1. Check it." in prompt
    judges = [
        query_performance_metric(FakeJudge()),
        prose_metric(FakeJudge(), "generate-nl"),
        sql_correctness_metric(FakeJudge())._geval(),
        error_diagnosis_metric(FakeJudge())._geval(has_expected=True),
    ]
    for geval in judges:
        assert geval.evaluation_template is OneLineReasonTemplate, geval.name


CLARIFYING_REPLY = {
    "type": "result",
    "result": {
        "status": "ClarificationsRequired",
        "specificClarificationsNeeded": [
            {
                "question": "Which Congo do you mean?",
                "options": ["Democratic Republic of Congo", "Republic of Congo"],
            }
        ],
        "generalClarificationsNeeded": ["Anything else?"],
    },
}


def _clarifying_case(clarifications, **meta):
    return LLMTestCase(
        input="How many political violence events were there in Congo in 2025?",
        actual_output=clarification_text(clarifications) or "SELECT 1",
        metadata={"clarifications": clarifications, **meta},
    )


def test_the_plugins_questions_are_read_from_its_reply():
    from plugin.client import PluginResult

    result = PluginResult("generate-sql", None, CLARIFYING_REPLY, 1.0)
    assert result.status == "ClarificationsRequired"
    assert result.clarifications == [
        {"question": "Which Congo do you mean?",
         "options": ["Democratic Republic of Congo", "Republic of Congo"]},
        {"question": "Anything else?", "options": []},
    ]
    assert result.assumptions == []
    assert clarification_text(result.clarifications) == (
        "1. Which Congo do you mean?\n"
        "   - Democratic Republic of Congo\n"
        "   - Republic of Congo\n"
        "2. Anything else?"
    )


def test_asked_for_clarification_metric():
    metric = AskedForClarificationMetric()
    asked = [{"question": "Which Congo?", "options": []}]
    assert metric.measure(_clarifying_case(asked)) == 1.0
    assert metric.reason == "asked 1 question"
    # Answering is fine when it says which reading it took.
    guessed = _clarifying_case([], plugin_sql="SELECT 1", assumptions=["Congo means the DRC"])
    assert metric.measure(guessed) == 1.0
    assert metric.reason == "answered with SQL, stating its assumption: Congo means the DRC"
    unstated = _clarifying_case([], plugin_sql="SELECT 1")
    assert metric.measure(unstated) == 0.0
    assert metric.reason == (
        "answered with SQL instead of asking, without stating which reading it chose; "
        "assumed: none listed"
    )

    # With readings, the assumption has to name one of them.
    by_reading = AskedForClarificationMetric(CONGO_READINGS)
    assert by_reading.measure(guessed) == 1.0
    beside_the_point = _clarifying_case(
        [], plugin_sql="SELECT 1", assumptions=["year 2025 is the calendar year"]
    )
    assert by_reading.measure(beside_the_point) == 0.0


# The two Congos, which a clarification golden once asked about: the
# lookbehind is what keeps the DRC's name from counting as the other Congo.
CONGO_READINGS = [
    ["Democratic Republic of (the )?Congo", "\\bDRC\\b", "\\bDR Congo\\b", "Kinshasa"],
    ["(?<!Democratic )Republic of (the )?Congo", "Congo-Brazzaville", "Brazzaville"],
]


@pytest.mark.parametrize(
    "options, passed, reason",
    [
        (["Democratic Republic of Congo", "Republic of Congo"], True, "offers all 2 readings"),
        (["DRC", "Congo-Brazzaville"], True, "offers all 2 readings"),
        # "Republic of Congo" inside the DRC's name doesn't count as the other Congo.
        (["Democratic Republic of Congo"], False, "offers 1 of 2 readings; missing: (?<!Democratic )"),
        ([], False, "the plugin didn't ask"),
    ],
)
def test_clarification_covers_readings_metric(options, passed, reason):
    asked = [{"question": "Which Congo do you mean?", "options": options}] if options else []
    metric = ClarificationCoversReadingsMetric(CONGO_READINGS)
    assert metric.measure(_clarifying_case(asked)) == float(passed)
    assert metric.reason.startswith(reason)


def test_clarification_quality_metric_judges_nothing_when_the_plugin_did_not_ask():
    metric = clarification_quality_metric(FakeJudge())
    assert metric.measure(_clarifying_case([], plugin_sql="SELECT 1")) == 0.0
    assert metric.reason == "the plugin didn't ask, so there was nothing to judge"


def test_clarify_goldens_are_their_own_part_and_graded_on_their_questions(metadata, unit_config):
    goldens = list(load_dataset().goldens)
    clarify = select_part(goldens, "clarify")
    assert [g.name for g in clarify] == [
        "clarify_how_bad_is_it",
        "clarify_getting_worse_africa",
        "clarify_most_dangerous_western_africa",
    ]
    assert {g.name for g in clarify} <= {g.name for g in select_part(goldens, "sql")}
    for golden in clarify:
        meta = golden.additional_metadata
        assert meta["endpoint"] == "generate-sql" and meta["ambiguity"], golden.name
        for patterns in meta["readings"]:
            for pattern in patterns:
                re.compile(pattern)
        answer = Answer(
            endpoint=ENDPOINTS["generate-sql"],
            meta=meta,
            reference=None,
            config=unit_config,
            schema=metadata,
            client=mock.Mock(),
            results=None,
            judge=FakeJudge(),
        )
        assert [m.__name__ for m in metrics_for(answer)] == [
            "Plugin Latency",
            "Asked for Clarification",
            "Clarification Covers Readings",
            "Clarification Quality [GEval]",
        ]


def test_run_in_full_runs_read_only_and_says_why_it_failed(monkeypatch):
    from suite import examples_db

    sent = []
    monkeypatch.setattr(examples_db, "psql", lambda sql: sent.append(sql) or ["3", "abc"])
    run = examples_db.run_in_full("SELECT 1")
    assert (run.row_count, run.checksum, run.error) == (3, "abc", None)
    assert sent[0].startswith("SET TRANSACTION READ ONLY; SET DateStyle")

    def fails(sql):
        raise RuntimeError('ERROR:  relation "acled.nope" does not exist')

    monkeypatch.setattr(examples_db, "psql", fails)
    run = examples_db.run_in_full("SELECT 1")
    assert run.ms is None and "does not exist" in run.error


def _golden(name):
    return next(g for g in load_dataset().goldens if g.name == name)


def _answer_for(golden, metadata, unit_config, reference=None, results=None):
    return Answer(
        endpoint=endpoint_for(golden.additional_metadata),
        meta=golden.additional_metadata,
        reference=reference,
        config=unit_config,
        schema=metadata,
        client=mock.Mock(),
        results=results,
        judge=FakeJudge(),
    )


def _built(answer):
    return [m.__name__.removesuffix(" [GEval]") for m in metrics_for(answer)]


def test_a_clarification_golden_answered_with_sql_is_graded_like_generate_sql(metadata, unit_config):
    golden = _golden("clarify_getting_worse_africa")
    assert _built(_answer_for(golden, metadata, unit_config)) == [
        "Plugin Latency", "Asked for Clarification", "Clarification Covers Readings", "Clarification Quality",
    ]
    reference = golden.additional_metadata["reading_references"][0]
    # Getting worse has no one set of right rows, so no row comparison...
    built = _built(_answer_for(golden, metadata, unit_config, reference))
    assert "SQL Result Match" not in built and "SQL Correctness" in built
    # ...but a golden whose readings each have one is graded like generate-sql.
    golden = golden.model_copy(deep=True)
    del golden.additional_metadata["compare_rows"]
    assert _built(_answer_for(golden, metadata, unit_config, reference)) == list(CLARIFICATION_ANSWERED_METRICS)


def test_unanswerable_and_already_fast_goldens_get_their_own_metrics(metadata, unit_config):
    goldens = list(load_dataset().goldens)
    assert [g.name for g in select_part(goldens, "unanswerable")] == [
        "cant_answer_deaths_per_capita", "cant_answer_people_displaced_sudan",
    ]
    assert _built(_answer_for(_golden("cant_answer_deaths_per_capita"), metadata, unit_config)) == list(CANT_ANSWER_METRICS)
    fast = _golden("accelerate_already_fast")
    assert _built(_answer_for(fast, metadata, unit_config, fast.input)) == list(ALREADY_FAST_METRICS)


def test_the_plugins_failure_is_read_from_its_reply():
    from plugin.client import PluginResult

    reply = {"type": "result", "result": {
        "status": "Failure",
        "failureMessage": "There's no population data.",
        "whatWasTriedOnFailure": ["Looked for a population column"],
        "suggestionsForFixingOnFailure": ["Ask for total fatalities instead"],
    }}
    result = PluginResult("generate-sql", None, reply, 1.0)
    assert result.failure == {
        "message": "There's no population data.",
        "tried": ["Looked for a population column"],
        "suggestions": ["Ask for total fatalities instead"],
    }
    assert cant_answer_text(result.failure, []) == (
        "There's no population data.\nWhat was tried:\n   - Looked for a population column\n"
        "Suggestions:\n   - Ask for total fatalities instead"
    )
    assert PluginResult("generate-sql", "SELECT 1", {"result": {"status": "Success"}}, 1.0).failure == {}


@pytest.mark.parametrize(
    "meta, passed, reason",
    [
        ({"plugin_status": "Failure"}, True, "said it can't answer this from the data"),
        ({"plugin_status": "ClarificationsRequired", "clarifications": [{"question": "q", "options": []}]},
         True, "asked what to show instead"),
        ({"plugin_sql": "SELECT 1", "assumptions": ["population_exposure is population"]},
         False, "answered with SQL anyway; assumed: population_exposure is population"),
    ],
)
def test_said_it_cant_answer_metric(meta, passed, reason):
    metric = SaidItCantAnswerMetric()
    assert metric.measure(LLMTestCase(input="x", actual_output="y", metadata=meta)) == float(passed)
    assert metric.reason == reason


def test_explains_whats_missing_judges_nothing_when_the_plugin_wrote_sql():
    metric = missing_data_explanation_metric(FakeJudge())
    case = LLMTestCase(input="x", actual_output="SELECT 1", metadata={"plugin_sql": "SELECT 1"})
    assert metric.measure(case) == 0.0
    assert metric.reason == "the plugin wrote SQL, so it explained nothing"


def test_the_closest_reading_is_the_one_whose_rows_match(monkeypatch, metadata):
    from llm_quality import test_live_eval
    from metrics.query_results import QueryResults

    rows = {"A": pd.DataFrame({"n": [1]}), "B": pd.DataFrame({"n": [2]})}
    monkeypatch.setattr(
        test_live_eval, "run_queries",
        lambda client, sql, ref, schema, dialect: QueryResults(actual=rows["B"], expected=rows[ref]),
    )
    meta = {"reading_references": ["A", "B"]}
    reference, results, expected = test_live_eval._closest_reading(None, "SELECT 2", meta, metadata, "postgres")
    assert (reference, expected) == ("B", "B")
    reference, _, expected = test_live_eval._closest_reading(
        None, "SELECT 2", {"reading_references": ["A"]}, metadata, "postgres"
    )
    assert reference == "A" and expected.startswith("The request is ambiguous; any of these readings")


def test_recording_takes_golden_names_and_no_options():
    import record_expected_results as rec

    assert rec.parse_args(["a", "b"]) == ["a", "b"]
    with pytest.raises(SystemExit):
        rec.parse_args(["--warm", "a"])


def test_a_metric_the_judge_was_not_asked_leaves_the_grade_and_counts_alone():
    report = _report_module()
    not_judged = _metric("SQL Correctness [GEval]", False, 0.0, reason="not judged: SQL Safety failed")
    slow = {"success": False, "metricsData": [
        _metric("Plugin Latency", False, 0.0, 1.0, reason="31.2s (budget 30.0s)"), not_judged,
    ]}
    assert report._not_judged(not_judged)
    assert report._grade(slow) == pytest.approx(30 / 31.2)
    assert report._grade({"success": False, "metricsData": [not_judged]}) == 0.0


def _run_file(folder, stamp, cases):
    (folder / f"test_run_{stamp}.json").write_text(json.dumps({"testCases": cases}))


def _run(name, ok=True, score=0.95, question="Q", judge="small-model"):
    return {"name": name, "input": question, "success": ok, "metricsData": [
        {"name": "SQL Safety", "success": True, "score": 1.0},
        {"name": "SQL Correctness [GEval]", "success": ok, "score": score,
         "evaluationModel": f"{judge} (On This Machine)"},
    ]}


def test_a_golden_that_passed_every_recent_run_well_is_asked_once(tmp_path):
    from suite.steady import steady_goldens

    _run_file(tmp_path, "20261101_090000", [_run(n) for n in "abcdef"] * 2)
    _run_file(tmp_path, "20261102_090000", [
        _run("a"), _run("b"), _run("c", ok=False, score=0.5), _run("d", score=0.85),
        _run("e", judge="large-model"), _run("f"),
    ])
    questions = dict.fromkeys("abcdefg", "Q") | {"f": "a new question"}
    assert steady_goldens(questions, tmp_path, "small-model") == ["a", "b"]
    # Too few runs to tell: none.
    assert steady_goldens({"a": "Q"}, tmp_path / "nothing", "small-model") == []
    # Only its last 3 runs files count: an old failure is forgotten.
    for stamp in ("20261103_090000", "20261104_090000"):
        _run_file(tmp_path, stamp, [_run("c")])
    assert steady_goldens({"c": "Q"}, tmp_path, "small-model") == []
    _run_file(tmp_path, "20261105_090000", [_run("c")])
    assert steady_goldens({"c": "Q"}, tmp_path, "small-model") == ["c"]
    # A judge with a second opinion on borderline scores is still this judge.
    _run_file(tmp_path, "20261106_090000", [
        _run("c", judge="small-model (On This Machine), borderline re-asked of other-model")
    ])
    assert steady_goldens({"c": "Q"}, tmp_path, "small-model") == ["c"]


def test_the_report_counts_borderline_scores_judged_again_and_keeps_the_note():
    report = _report_module()

    def judged(score, success, reason):
        return {"name": "Plan Explanation [GEval]", "score": score, "success": success,
                "threshold": 0.8, "reason": reason, "evaluationModel": "small-model"}

    cases = [{"metricsData": [
        judged(0.9, True, "Fine. [re-asked: small-model gave 0.7, other-model 0.9]"),  # changed
        judged(0.7, False, "Vague. [re-asked: small-model gave 0.7, other-model 0.7]"),  # kept
        judged(0.6, False, "Wrong. Very wrong. [re-asked: small-model gave 0.8, "
               "other-model 0.7 then 0.6; 1 of 3 passed]"),  # changed
        judged(0.8, True, "Fine. [re-asked: small-model gave 0.8, "
               "other-model 0.7 then 0.8; 2 of 3 passed]"),  # kept
        judged(0.9, True, "Fine."),
    ]}]
    assert report._run_totals(cases).endswith("; 4 borderline scores judged again, 2 verdicts changed")
    assert report._short("Wrong. Very wrong. [re-asked: small-model gave 0.8]") == (
        "Wrong. [re-asked: small-model gave 0.8]"
    )
    assert "judged again" not in report._run_totals([{"metricsData": [judged(0.9, True, "Fine.")]}])
