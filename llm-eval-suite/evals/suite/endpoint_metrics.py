"""Which metrics grade each endpoint's answers.

METRICS lists them by name for each endpoint, in the order the report shows
them: to give an endpoint a metric or take one away, edit its list. A metric
listed for an endpoint still runs only on the goldens it applies to, as
BUILDERS says, for example SQL Result Match only for goldens with reference
SQL, and judged metrics only when there is a judge (JUDGE_PROVIDER not none).

To add a new metric: write it in metrics/ (or judge/ if the judge grades it),
add a builder for it to BUILDERS, list it under its endpoints in METRICS, and
say what it means in METRICS in reports/report.py.
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from deepeval.metrics import BaseMetric
from deepeval.models import DeepEvalBaseLLM

from judge.metrics import (
    NotJudgedMetric,
    is_judged,
    clarification_quality_metric,
    missing_data_explanation_metric,
    error_diagnosis_metric,
    prose_metric,
    query_performance_metric,
    sql_correctness_metric,
)
from judge.prompts import PROSE_METRICS
from metrics.clarification import (
    AskedForClarificationMetric,
    ClarificationCoversReadingsMetric,
    SaidItCantAnswerMetric,
)
from metrics.common import ForbiddenTextMetric, LatencyMetric, PluginAnsweredMetric
from metrics.prose import (
    MAX_TERMS,
    BreakdownCoverageMetric,
    DescriptionUnchangedMetric,
    TooltipTermsMetric,
)
from metrics.query_results import QueryResults
from metrics.sql import (
    ExecutionMatchMetric,
    SqlRunsMetric,
    SqlSafetyMetric,
    StaticPerformanceMetric,
)
from metrics.expected_result import SameRowsAsOriginalMetric
from metrics.timing import FasterThanOriginalMetric, SpeedVsReferenceMetric
from plugin.endpoints import Endpoint
from suite.config import SuiteConfig
from suite.schema_metadata import TableMetadata

# Instead of its endpoint's metrics, for a golden with
# "expect_clarification": true - a request the plugin should answer with a
# question, so there is no SQL to grade.
CLARIFICATION_METRICS = (
    "Plugin Latency",
    "Asked for Clarification",
    "Clarification Covers Readings",
    "Clarification Quality",
)

# For a clarification golden whose question the plugin answered with SQL
# instead: a pass if it stated which reading it assumed (Asked for
# Clarification) and its SQL, graded like generate-sql's, gets that reading
# right (see "reading_references").
CLARIFICATION_ANSWERED_METRICS = (
    "Plugin Latency",
    "Asked for Clarification",
    "SQL Safety",
    "SQL Static Performance",
    "SQL Result Match",
    "SQL Correctness",
    "Query Performance & Table Selection",
)

# Instead of its endpoint's metrics, for a golden with "expect_cant_answer":
# true - a request the data can't answer, which the plugin should say so to.
CANT_ANSWER_METRICS = (
    "Plugin Latency",
    "Said It Can't Answer",
    "Explains What's Missing",
)

# Instead of accelerate's, for a golden with "already_fast": true - a query
# with nothing to gain, which the plugin should leave as it is (or at least
# not slow down): no Faster Than Original, but no slower than it.
ALREADY_FAST_METRICS = (
    "SQL Safety",
    "SQL Static Performance",
    "Plugin Latency",
    "SQL Result Match",
    "Speed vs Reference",
    "SQL Correctness",
)

# Every endpoint that answers with SQL, except accelerate.
SQL_ENDPOINT_METRICS = (
    "SQL Safety",
    "SQL Static Performance",
    "Plugin Latency",
    "SQL Result Match",
    "SQL Runs",
    "Speed vs Reference",
    "SQL Correctness",
    "Query Performance & Table Selection",
    "Forbidden Text",
)

# Every endpoint that answers in plain English, before its own.
PROSE_ENDPOINT_METRICS = ("Plugin Answered", "Plugin Latency", "Tooltip Terms", "Forbidden Text")

METRICS: Dict[str, Tuple[str, ...]] = {
    "generate-sql": SQL_ENDPOINT_METRICS,
    "update-sql-from-nl": SQL_ENDPOINT_METRICS,
    "refine-sql": SQL_ENDPOINT_METRICS,
    "explain-sql-error": (*SQL_ENDPOINT_METRICS, "Error Diagnosis"),
    # Its reference is the original query, whose rows the rewrite must
    # return: checked against the original's recorded result where the golden
    # has one, otherwise by running both. Its speed is timed against that
    # query, so it isn't judged.
    "accelerate": (
        "SQL Safety",
        "SQL Static Performance",
        "Plugin Latency",
        "SQL Result Match",
        "Same Rows as Original",
        "Faster Than Original",
        "SQL Correctness",
        "Forbidden Text",
    ),
    "generate-nl": (*PROSE_ENDPOINT_METRICS, "Description Accuracy"),
    "update-nl-from-sql-diff": (
        *PROSE_ENDPOINT_METRICS,
        "Description Unchanged",
        "Description Update",
    ),
    "explain-plan": (*PROSE_ENDPOINT_METRICS, "Plan Explanation"),
    "describe-breakdown": (*PROSE_ENDPOINT_METRICS, "Breakdown Coverage", "Breakdown Accuracy"),
}


@dataclass(frozen=True)
class Answer:
    """One answer to grade, and what the metrics' builders need for it."""

    endpoint: Endpoint
    # The golden's additional_metadata.
    meta: Mapping[str, Any]
    # SQL whose rows the answer must match: the golden's reference SQL, or
    # for accelerate the original query. None for prose and without one.
    reference: Optional[str]
    config: SuiteConfig
    schema: Mapping[str, TableMetadata]
    client: Any
    # What running the SQL and its reference returned, run once for every
    # metric that needs the rows.
    results: Optional[QueryResults]
    # None when JUDGE_PROVIDER=none.
    judge: Optional[DeepEvalBaseLLM]

    @property
    def dialect(self) -> str:
        return self.config.sql_dialect

    @property
    def executes(self) -> bool:
        """False for goldens with "execute": false: SQL too slow to run in
        full on every eval, or "compare_rows": false, where no one reference
        gives the right rows."""
        return self.meta.get("execute", True) and self.meta.get("compare_rows", True)

    @property
    def expects_clarification(self) -> bool:
        """True for goldens the plugin should answer with a question."""
        return bool(self.meta.get("expect_clarification"))

    @property
    def expected_result(self) -> Optional[Mapping[str, Any]]:
        """The original's recorded result, for accelerate goldens that have
        one (record_expected_results.py)."""
        return self.meta.get("expected_result")


def _timing(a: Answer) -> Dict[str, Any]:
    # The rows, so SQL whose rows are wrong fails untimed; not where the
    # golden says no one reference gives the right rows.
    return {
        "runs": a.config.timing_runs,
        "timeout_s": a.config.timing_timeout_s,
        "results": a.results if a.executes else None,
    }


# Each metric for one answer, or None where it doesn't apply to the golden.
BUILDERS: Dict[str, Callable[[Answer], Optional[BaseMetric]]] = {
    "SQL Safety": lambda a: SqlSafetyMetric(a.schema, a.dialect),
    # Fails on a function around an indexed column of a large table on every
    # endpoint, which test data may be too small to make slow: the judge's
    # performance policy forbids it too.
    "SQL Static Performance": lambda a: StaticPerformanceMetric(a.schema, a.dialect),
    "Plugin Latency": lambda a: LatencyMetric(
        a.config.latency_budget_for(a.endpoint.name)
    ),
    "SQL Result Match": lambda a: (
        ExecutionMatchMetric(a.client, a.schema, a.dialect, results=a.results)
        if a.executes and a.reference and not a.expected_result
        else None
    ),
    # Instead of SQL Result Match, for accelerate goldens with the original's
    # result recorded (record_expected_results.py). Run even with
    # "execute": false, up to the time limit.
    "Same Rows as Original": lambda a: (
        SameRowsAsOriginalMetric(
            a.client,
            a.schema,
            a.expected_result,
            a.dialect,
            timeout_s=a.config.timing_timeout_s,
        )
        if a.expected_result
        else None
    ),
    # Instead of SQL Result Match, for goldens without reference SQL.
    "SQL Runs": lambda a: (
        SqlRunsMetric(a.client, a.schema, a.dialect, results=a.results)
        if a.executes and not a.reference
        else None
    ),
    # Timed even with "execute": false, up to its time limit.
    "Faster Than Original": lambda a: FasterThanOriginalMetric(
        a.client,
        a.schema,
        a.dialect,
        threshold=a.config.accelerate_min_saving,
        **_timing(a),
    ),
    "Speed vs Reference": lambda a: (
        SpeedVsReferenceMetric(
            a.client,
            a.schema,
            a.dialect,
            tolerance=a.config.reference_time_tolerance,
            min_slowdown_ms=a.config.reference_min_slowdown_ms,
            **_timing(a),
        )
        if a.reference
        else None
    ),
    "SQL Correctness": lambda a: (
        sql_correctness_metric(a.judge) if a.judge is not None and a.reference else None
    ),
    "Query Performance & Table Selection": lambda a: (
        query_performance_metric(a.judge) if a.judge is not None else None
    ),
    "Error Diagnosis": lambda a: error_diagnosis_metric(a.judge) if a.judge is not None else None,
    "Asked for Clarification": lambda a: AskedForClarificationMetric(a.meta.get("readings") or ()),
    "Said It Can't Answer": lambda a: SaidItCantAnswerMetric(),
    "Explains What's Missing": lambda a: (
        missing_data_explanation_metric(a.judge) if a.judge is not None else None
    ),
    # Only for goldens that list the readings their question should offer.
    "Clarification Covers Readings": lambda a: (
        ClarificationCoversReadingsMetric(a.meta["readings"])
        if a.meta.get("readings")
        else None
    ),
    "Clarification Quality": lambda a: (
        clarification_quality_metric(a.judge) if a.judge is not None else None
    ),
    "Plugin Answered": lambda a: PluginAnsweredMetric(),
    "Tooltip Terms": lambda a: TooltipTermsMetric(
        a.schema, max_terms=MAX_TERMS if a.endpoint.marks_terms else 0
    ),
    "Breakdown Coverage": lambda a: BreakdownCoverageMetric(),
    # Only for goldens with "forbidden_text": text that only an answer obeying
    # an instruction injected into the input would have.
    "Forbidden Text": lambda a: (
        ForbiddenTextMetric(a.meta["forbidden_text"])
        if a.meta.get("forbidden_text")
        else None
    ),
    # Only for goldens with "expect_unchanged": true.
    "Description Unchanged": lambda a: (
        DescriptionUnchangedMetric(a.meta["previous_input"])
        if a.meta.get("expect_unchanged")
        else None
    ),
}

# Each prose endpoint's own judged metric, named in PROSE_METRICS in
# judge/prompts.py.
for _name, _setup, _steps in PROSE_METRICS.values():
    BUILDERS[_name] = lambda a: (
        prose_metric(a.judge, a.endpoint.name) if a.judge is not None else None
    )


# The checks that say an answer is wrong or missing, rather than slow or
# badly formatted. Each is quick (SQL Result Match and SQL Runs use rows
# already fetched) and scores 0 when it fails, which grades the run 0%
# (_grade in report.py), so once one fails a judge's verdict can't change the
# run's result or grade. Not Asked for Clarification: SQL written instead of
# a question is judged, to compare with the SQL for clear requests.
JUDGE_GATES = (
    PluginAnsweredMetric,
    SqlSafetyMetric,
    ExecutionMatchMetric,
    SqlRunsMetric,
    ForbiddenTextMetric,
    SaidItCantAnswerMetric,
    DescriptionUnchangedMetric,
    BreakdownCoverageMetric,
)


def skip_judges_after_failure(metrics: List[BaseMetric], test_case: Any) -> List[BaseMetric]:
    """`metrics`, with each judged one replaced by a NotJudgedMetric when one
    of JUDGE_GATES fails on `test_case`, which saves the judge's tokens. The
    gates are measured here and again with the rest."""
    judged = [m for m in metrics if is_judged(m)]
    if not judged:
        return metrics
    for gate in (m for m in metrics if isinstance(m, JUDGE_GATES)):
        gate.measure(test_case)
        if not gate.success:
            return [
                NotJudgedMetric(m, gate.__name__) if is_judged(m) else m
                for m in metrics
            ]
    return metrics


def metrics_for(answer: Answer) -> List[BaseMetric]:
    """The metrics that grade `answer`: its endpoint's, in METRICS order
    (CLARIFICATION_METRICS for a golden expecting a question), leaving out
    those that don't apply to its golden."""
    if answer.expects_clarification:
        # A reference means it answered with SQL instead of asking.
        names = CLARIFICATION_ANSWERED_METRICS if answer.reference else CLARIFICATION_METRICS
    elif answer.meta.get("expect_cant_answer"):
        names = CANT_ANSWER_METRICS
    elif answer.meta.get("already_fast"):
        names = ALREADY_FAST_METRICS
    else:
        names = METRICS[answer.endpoint.name]
    built = (BUILDERS[name](answer) for name in names)
    return [m for m in built if m is not None]
