"""The LLM-quality suite for the nltosqlandback plugin.

Each golden from the DeepEval dataset (see suite/dataset.py) is sent to the
endpoint it names (see plugin/endpoints.py), once per run. The answer becomes
an LLMTestCase graded by every relevant metric at once, so each golden is one
row in the DeepEval report for each run.

Which metrics grade each endpoint's answers, and on which goldens, is set in
suite/endpoint_metrics.py. Before grading, the SQL and its reference are run
once: SQL Result Match and SQL Runs use those rows, and the correctness judge
gets a summary of them (row counts, the first rows in order, and whether the
rows match). The correctness judge doesn't see the performance policy or the
static analysis findings: speed is for the performance metrics, not for it.

The report also shows the plugin's steps, how long each took and any second
attempts, from its progress stream (no LLM; see plugin/internals.py). No
metric passes or fails on them.

Behaviour that doesn't depend on how good the answer is (refusing destructive
requests, retrying a bad draft, showing errors) is left to the record & replay
acceptance tests, which hold the LLM's answer fixed.

Notes on the tables involved (and the database's plan, for explain-plan) are
given to the judge as the test case context (judge/context.py).

Run:
    ./run_evals.sh                  # every golden, once
    ./run_evals.sh accelerate       # one part (see SUITE_PARTS in suite/dataset.py)
    ./run_evals.sh --runs 3         # every golden 3 times, for pass rates
    deepeval test run evals/llm_quality -m llm_quality -id "plugin v1.2"
"""

import os

import pytest
from deepeval import assert_test
from deepeval.test_case import LLMTestCase

from judge.context import build_performance_context, plan_context, without_performance_notes
from metrics.clarification import cant_answer_text, clarification_text
from metrics.common import NO_OUTPUT
from metrics.query_results import describe_results, frames_equivalent, run_queries
from metrics.sql_performance import analyze_query_performance
from metrics.sql_safety import tables_in_sql
from plugin.endpoints import SQL, endpoint_for
from plugin.internals import plugin_steps, retries_in_stream
from suite.dataset import golden_id, load_dataset, select_part
from suite.endpoint_metrics import Answer, metrics_for, skip_judges_after_failure

pytestmark = pytest.mark.llm_quality

# EVAL_PART limits the run to one part of the suite (set by run_evals.sh).
PART = os.getenv("EVAL_PART")
GOLDENS = select_part(list(load_dataset(PART).goldens), PART)


def _params(goldens):
    params = []
    for i, g in enumerate(goldens):
        meta = g.additional_metadata or {}
        name = meta.get("endpoint") or endpoint_for(meta).name
        params.append(pytest.param(g, id=f"{name}:{golden_id(g, i)}"))
    return params


def _relevant_tables(golden, result, dialect: str):
    """Tables the judge needs described: those in any SQL involved, plus
    must_use / must_not_use. None means all tables."""
    meta = golden.additional_metadata or {}
    tables = {
        t.lower()
        for t in meta.get("must_use", []) + meta.get("must_not_use", [])
    }
    candidates = (
        golden.input,
        golden.expected_output,
        meta.get("sql"),
        meta.get("previous_sql"),
        result.output if endpoint_for(meta).output == SQL else None,
    )
    for sql in candidates:
        if sql:
            try:
                tables |= tables_in_sql(sql, dialect)
            except Exception:  # prose, or SQL sqlglot can't parse
                pass
    return tables or None


def _diagnosis(result):
    """explain-sql-error's explanation of the error, as one block of text."""
    answer = result.raw.get("result") or {}
    parts = [
        ("Problem", answer.get("problemSummary")),
        ("Cause", answer.get("cause")),
        ("Fix", answer.get("fixExplanation")),
        ("Risks", "; ".join(answer.get("remainingRisks") or [])),
    ]
    return "\n".join(f"{label}: {text}" for label, text in parts if text) or None


def _test_case(
    golden, endpoint, result, context=None, expected_output=None, query_results=None
):
    meta = golden.additional_metadata or {}
    extra = {}
    if query_results:
        extra["query_results"] = query_results
    if endpoint.name == "accelerate":
        extra["original_sql"] = golden.input
        # Why the plugin says it's faster (or that it can't be), for the report.
        extra["explanation"] = (result.raw.get("result") or {}).get("explanation")
    if endpoint.name == "explain-sql-error":
        extra["diagnosis"] = _diagnosis(result)
        extra["expected_diagnosis"] = meta.get("expected_diagnosis")
    output = result.output or NO_OUTPUT
    if meta.get("expect_clarification"):
        # Graded on the questions it asked; the SQL and assumptions, when it
        # answered instead, explain the failure.
        extra.update(
            expect_clarification=True,
            clarifications=result.clarifications,
            plugin_status=result.status,
            plugin_sql=result.output,
            assumptions=result.assumptions,
        )
        output = clarification_text(result.clarifications) or output
    if meta.get("expect_cant_answer"):
        # Graded on what it said instead of SQL; SQL, if it wrote some, is a
        # failure, explained by what it assumed.
        extra.update(
            expect_cant_answer=True,
            clarifications=result.clarifications,
            plugin_status=result.status,
            plugin_sql=result.output,
            assumptions=result.assumptions,
        )
        output = cant_answer_text(result.failure, result.clarifications) or output
    return LLMTestCase(
        name=golden.name,
        input=endpoint.describe(golden.input, meta),
        actual_output=output,
        expected_output=expected_output,
        context=context,
        completion_time=result.latency_s,
        metadata={
            "endpoint": endpoint.name,
            "must_use": meta.get("must_use", []),
            "must_not_use": meta.get("must_not_use", []),
            "refused": result.refused,
            "plugin_error": result.error,
            "plugin_crashed": result.crashed,
            "plugin_response": result.raw,
            "request_id": result.request_id,
            "plugin_steps": plugin_steps(result.events),
            "plugin_retries": retries_in_stream(result.events),
            **extra,
        },
    )


def _closest_reading(client, sql, meta, schema, dialect):
    """For an ambiguous request the plugin answered with SQL instead of a
    question: (the reference for the reading its rows match, the rows, what
    the judge is told the reference is). With no match, the first reading's
    reference, and every reading for the judge, since any one is a fair
    answer if it says which it chose."""
    references = meta["reading_references"]
    every = (
        "The request is ambiguous; any of these readings is correct, if the answer "
        "says which it chose:\n\n" + "\n\n".join(references)
    )
    if not meta.get("compare_rows", True):
        return references[0], None, every
    first = None
    for reference in references:
        results = run_queries(client, sql, reference, schema, dialect)
        first = first or results
        if results.actual is not None and results.expected is not None:
            if frames_equivalent(results.actual, results.expected)[0]:
                return reference, results, reference
    return references[0], first, every


def _assert_graded(test_case, metrics, asked_once=False):
    """Grades `test_case` with `metrics`, without asking the judge once a
    check that the answer is right has failed (skip_judges_after_failure).
    `asked_once` marks a steady golden asked once in a run of several
    (suite/steady.py), for the report."""
    if asked_once:
        test_case.metadata["asked_once"] = True
    assert_test(test_case, skip_judges_after_failure(metrics, test_case))


def _call(client, golden):
    meta = golden.additional_metadata or {}
    endpoint = endpoint_for(meta)
    return endpoint, client.call_plugin(
        endpoint, golden.input, meta, case_id=golden.name
    )


@pytest.mark.parametrize("golden", _params(GOLDENS))
def test_golden(
    golden, live_superset, suite_config, schema_metadata, judge_model, asked_once
):
    endpoint, result = _call(live_superset, golden)
    meta = golden.additional_metadata or {}
    dialect = suite_config.sql_dialect
    tables = _relevant_tables(golden, result, dialect)

    def answer(reference=None, results=None):
        return Answer(
            endpoint=endpoint,
            meta=meta,
            reference=reference,
            config=suite_config,
            schema=schema_metadata,
            client=live_superset,
            results=results,
            judge=judge_model,
        )

    if meta.get("expect_clarification") and result.output and meta.get("reading_references"):
        # It answered instead of asking: graded like generate-sql's SQL too,
        # against the reading it chose, so the two can be compared.
        reference, results, expected = _closest_reading(
            live_superset, result.output, meta, schema_metadata, dialect
        )
        _assert_graded(
            _test_case(
                golden,
                endpoint,
                result,
                build_performance_context(schema_metadata, tables=tables),
                expected_output=expected,
                query_results=describe_results(results) if results else None,
            ),
            metrics_for(answer(reference, results)),
            asked_once,
        )
        return

    if meta.get("expect_cant_answer"):
        # A request the data can't answer: graded on what the plugin said
        # instead, against the golden's account of what's missing.
        context = without_performance_notes(
            build_performance_context(schema_metadata, tables=tables)
        )
        _assert_graded(
            _test_case(golden, endpoint, result, context, expected_output=meta.get("missing")),
            metrics_for(answer()),
            asked_once,
        )
        return

    if meta.get("expect_clarification"):
        # A request the plugin should answer with a question: the judge gets
        # the tables (to tell which options the data can answer) and the
        # golden's account of the ambiguity, not the performance policy.
        context = without_performance_notes(
            build_performance_context(schema_metadata, tables=tables)
        )
        _assert_graded(
            _test_case(golden, endpoint, result, context, expected_output=meta.get("ambiguity")),
            metrics_for(answer()),
            asked_once,
        )
        return

    if endpoint.output != SQL:
        context = build_performance_context(schema_metadata, tables=tables)
        plan = (result.raw.get("result") or {}).get("plan")
        if plan:
            context.append(plan_context(plan))
        _assert_graded(
            _test_case(golden, endpoint, result, context), metrics_for(answer()), asked_once
        )
        return

    accelerate = endpoint.name == "accelerate"
    # A faster rewrite must return exactly what the original query returns.
    reference = golden.expected_output or (golden.input if accelerate else None)
    findings = (
        analyze_query_performance(
            result.output,
            schema_metadata,
            question=endpoint.describe(golden.input, meta),
            dialect=dialect,
        )
        if result.output
        else []
    )
    context = build_performance_context(
        schema_metadata, tables=tables, findings=findings
    )
    # Run the SQL and its reference once: SQL Result Match, SQL Runs and the
    # correctness judge all use these rows. Not for a golden with the
    # original's result recorded, which Same Rows as Original checks without
    # running the original.
    results = (
        run_queries(live_superset, result.output, reference, schema_metadata, dialect)
        if meta.get("execute", True) and result.output and not meta.get("expected_result")
        else None
    )
    _assert_graded(
        _test_case(
            golden,
            endpoint,
            result,
            context,
            expected_output=reference,
            query_results=describe_results(results) if results else None,
        ),
        metrics_for(answer(reference, results)),
        asked_once,
    )
