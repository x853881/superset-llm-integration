"""Unit tests for the LLM judge: choosing the judge model, other judges
re-grading a run, and what each judged metric is given.

Offline: the DeepEval model classes are patched out, and no test calls a
judge (FakeJudge fails if one does).

Run: pytest -m unit
"""

import asyncio
import json
import os
import sys
from collections import defaultdict
from unittest import mock

import pytest
from deepeval.metrics.g_eval.schema import ReasonScore
from deepeval.models import DeepEvalBaseLLM
from deepeval.models.utils import EvaluationCost
from deepeval.test_case import LLMTestCase, SingleTurnParams

from metrics.sql_performance import analyze_query_performance
from judge import model as judge_model
from judge.metrics import (
    error_diagnosis_metric,
    prose_metric,
    query_performance_metric,
    sql_correctness_metric,
)
from judge.model import JudgeConfigError, JudgeSettings, judge_enabled
from judge.prompts import AUDIENCE
from judge_compare import _free_path, comparison_text, rejudge
from plugin.endpoints import ENDPOINTS, TEXT
from judge.context import build_performance_context
from unit.support import ROLLUP_SQL, FakeJudge, sql_case

pytestmark = pytest.mark.unit


###############################################
# Choosing the judge model (judge/model.py)
###############################################


@pytest.mark.parametrize(
    "provider, model_class",
    [
        ("openrouter", "OpenRouterModel"),
        ("anthropic", "AnthropicModel"),
    ],
)
def test_judge_provider_selects_deepeval_model(provider, model_class):
    env = {
        "JUDGE_PROVIDER": provider,
        "JUDGE_MODEL_NAME": "some-model",
        "JUDGE_BASE_URL": "http://judge:8000/v1",
    }
    with mock.patch.object(judge_model, model_class) as model_cls:
        judge = judge_model.get_judge_model(env)

    assert judge is model_cls.return_value
    kwargs = model_cls.call_args.kwargs
    assert kwargs["model"] == "some-model"
    assert kwargs["temperature"] == 0.0


def test_a_judge_without_a_model_is_rejected():
    with pytest.raises(JudgeConfigError, match="JUDGE_MODEL_NAME"):
        JudgeSettings.from_env({"JUDGE_PROVIDER": "openrouter"})


def test_judge_can_be_disabled():
    assert not judge_enabled({"JUDGE_PROVIDER": "none"})
    assert judge_enabled({"JUDGE_PROVIDER": "anthropic"})


###############################################
# Other judges re-grading a run (judge_compare.py)
###############################################


class ScriptedJudge(DeepEvalBaseLLM):
    """Gives every judgement the same score, so judges can be told apart."""

    def __init__(self, name, score):
        self.score = score
        super().__init__(name)

    def load_model(self):
        return None

    def get_model_name(self):
        return f"{self.name} (On This Machine)"

    def generate(self, prompt, schema=None):
        return schema(reason=f"{self.name} gives {self.score}", score=self.score), EvaluationCost(
            0.0, input_tokens=100, output_tokens=10
        )

    async def a_generate(self, prompt, schema=None):
        return self.generate(prompt, schema)


def _judged(name, score, success):
    return {"name": name, "score": score, "success": success, "reason": "saved",
            "evaluationModel": "large-model (On This Machine)",
            "inputTokenCount": 50, "outputTokenCount": 5}


def _run_to_compare():
    return {"testCases": [
        {"name": "region_totals", "input": "q", "actualOutput": ROLLUP_SQL,
         "expectedOutput": "SELECT 1", "context": ["Table notes"],
         "metadata": {"endpoint": "generate-sql", "query_results": "Running ... 3 rows"},
         "metricsData": [
             {"name": "SQL Result Match", "score": 0.0, "success": False},
             _judged("SQL Correctness [GEval]", 0.2, False),
             _judged("Query Performance & Table Selection [GEval]", 0.9, True),
         ]},
        {"name": "plan_top_n", "input": "Explain", "actualOutput": "It scans.",
         "context": ["Database plan: {}"], "metadata": {"endpoint": "explain-plan"},
         "metricsData": [_judged("Plan Explanation [GEval]", 0.9, True)]},
    ]}


def test_other_judges_grade_the_runs_own_answers():
    run = _run_to_compare()
    judges = {"small-model": 3, "other-model": 9}

    rows = asyncio.run(rejudge(run, list(judges), make_judge=lambda m: ScriptedJudge(m, judges[m])))

    by_model = defaultdict(list)
    for r in rows:
        by_model[r["model"]].append(r)
    assert set(by_model) == {"large-model", "small-model", "other-model"}
    assert all(len(v) == 3 for v in by_model.values())  # every judged metric, once each
    assert {r["score"] for r in by_model["small-model"]} == {0.3}
    assert not any(r["success"] for r in by_model["small-model"])
    assert by_model["other-model"][0]["input_tokens"] == 100

    text = comparison_text(rows, "test_run_x.json")
    assert "The run was judged by large-model" in text
    assert "Where they disagree (3 of 3 judged metrics)" in text
    assert "region_totals (generate-sql): SQL Correctness, rows match: False" in text
    assert "small-model          0.3 FAIL  small-model gives 3" in text


def test_the_runs_judge_can_judge_again_on_the_metrics_you_choose():
    judges = {"large-model": 8, "small-model": 3}

    rows = asyncio.run(rejudge(
        _run_to_compare(), list(judges),
        make_judge=lambda m: ScriptedJudge(m, judges[m]), wanted=["sql correctness"],
    ))

    # Today's criteria first; the run's own verdicts last, to see what changed.
    assert list(dict.fromkeys(r["model"] for r in rows)) == [
        "large-model", "small-model", "large-model (run)",
    ]
    assert {r["metric"] for r in rows} == {"SQL Correctness"}
    assert [r["score"] for r in rows] == [0.8, 0.3, 0.2]

    text = comparison_text(rows)
    assert "its verdicts in the run are 'large-model (run)'" in text
    lines = {line.split("  ")[0]: line for line in text.splitlines()}
    assert "1/1 (100%)" in lines["Passes although rows differ"]  # 0.8, rows differing
    assert lines["Mean score gap to large-model"].split()[-2:] == ["0.50", "0.60"]


def test_a_comparison_never_overwrites_an_earlier_one(tmp_path):
    path = tmp_path / "judge_comparison_test_run_x.json"
    assert _free_path(path) == path
    path.write_text("[]")
    (tmp_path / "judge_comparison_test_run_x_2.json").write_text("[]")
    assert _free_path(path) == tmp_path / "judge_comparison_test_run_x_3.json"


###############################################
# What each judged metric is given (judge/metrics.py, judge/prompts.py)
###############################################


def test_metrics_are_graded_by_injected_judge(monkeypatch):
    monkeypatch.setenv("JUDGE_THRESHOLD", "0.8")
    judge = FakeJudge()

    correctness = sql_correctness_metric(judge)
    performance = query_performance_metric(judge, threshold=0.6)

    assert correctness.model is judge and performance.model is judge
    assert correctness.threshold == 0.8
    assert performance.threshold == 0.6
    assert SingleTurnParams.EXPECTED_OUTPUT in correctness._geval().evaluation_params
    assert SingleTurnParams.CONTEXT in performance.evaluation_params
    assert SingleTurnParams.EXPECTED_OUTPUT not in performance.evaluation_params
    # Each says what it leaves to the other, so one flaw isn't failed twice.
    correctness_steps = " ".join(correctness._geval().evaluation_steps)
    assert "Don't penalize how fast it runs" in correctness_steps
    assert "the same result includes NULLs" in correctness_steps
    assert "another check grades those" in " ".join(performance.evaluation_steps)

    explanation = prose_metric(judge, "explain-plan")
    assert explanation.model is judge

    # Quality metrics grade against a rubric; correctness stays open-ended.
    assert performance.rubric and explanation.rubric
    assert not correctness._geval().rubric
    diagnosis = error_diagnosis_metric(judge)
    assert diagnosis.model is judge and diagnosis.threshold == 0.8
    for rubric in (
        performance.rubric,
        explanation.rubric,
        diagnosis._geval(True).rubric,
    ):
        covered = [
            s
            for r in rubric
            for s in range(r.score_range[0], r.score_range[1] + 1)
        ]
        assert covered == list(range(11))  # 0-10, no gaps or overlaps
    assert SingleTurnParams.EXPECTED_OUTPUT not in explanation.evaluation_params


def test_only_the_correctness_judge_gets_the_query_results():
    metric = sql_correctness_metric(FakeJudge())
    case = LLMTestCase(
        input="q",
        actual_output=ROLLUP_SQL,
        expected_output="SELECT 1",
        context=["Table notes"],
        metadata={"query_results": "Running the actual output returned 0 rows"},
    )
    judged = metric._case(case)
    assert judged.context == ["Table notes", "Running the actual output returned 0 rows"]
    assert case.context == ["Table notes"]  # what the other judges see

    no_results = LLMTestCase(input="q", actual_output=ROLLUP_SQL, context=["Table notes"])
    assert metric._case(no_results) is no_results


def test_the_correctness_judge_does_not_see_the_performance_notes(metadata):
    findings = analyze_query_performance("SELECT * FROM YOUR_DAILY_ROLLUP_TABLE", metadata)
    context = build_performance_context(
        metadata, tables=["YOUR_DAILY_ROLLUP_TABLE"], findings=findings
    )
    case = LLMTestCase(
        input="q", actual_output=ROLLUP_SQL, expected_output="SELECT 1", context=context
    )

    judged = sql_correctness_metric(FakeJudge())._case(case)

    # Seeing the policy and findings, a judge fails a right but slow answer,
    # which the performance metrics already grade.
    assert len(context) == 3
    assert judged.context == [context[1]]
    assert "YOUR_DAILY_ROLLUP_TABLE" in judged.context[0]
    assert case.context == context  # what the other judges see

    # Nor does the diagnosis judge: a fix's speed isn't what it grades.
    case.metadata = {"diagnosis": "The column is misspelt."}
    assert error_diagnosis_metric(FakeJudge())._case(case).context == [context[1]]


def test_error_diagnosis_metric_without_explanation_fails_without_judge():
    metric = error_diagnosis_metric(FakeJudge())
    assert metric.measure(sql_case(ROLLUP_SQL, diagnosis=None)) == 0.0
    assert metric.reason == "plugin gave no explanation of the error"

    case = metric._case(
        sql_case(ROLLUP_SQL, diagnosis="Cause: typo", expected_diagnosis="typo")
    )
    assert case.actual_output == "Cause: typo" and case.expected_output == "typo"
    params = metric._geval(False).evaluation_params
    assert SingleTurnParams.EXPECTED_OUTPUT not in params


PROSE_ENDPOINTS = [name for name, e in ENDPOINTS.items() if e.output == TEXT]


def test_each_prose_endpoint_has_its_own_judged_metric_sharing_one_audience():
    judged = {e: prose_metric(FakeJudge(), e) for e in PROSE_ENDPOINTS}
    assert [m.name for m in judged.values()] == [
        "Description Accuracy", "Description Update", "Plan Explanation", "Breakdown Accuracy",
    ]
    for metric in judged.values():
        steps = " ".join(metric.evaluation_steps)
        assert "Heavily penalize invented" in steps  # the shared core
        assert AUDIENCE in steps
        assert SingleTurnParams.EXPECTED_OUTPUT not in metric.evaluation_params
    assert "never a note saying what changed" in " ".join(
        judged["update-nl-from-sql-diff"].evaluation_steps
    )
    assert "where the cost goes" in " ".join(judged["explain-plan"].evaluation_steps)
    assert "typical SQL user" in judged["explain-plan"].rubric[-1].expected_outcome
    diagnosis = error_diagnosis_metric(FakeJudge())._geval(has_expected=False)
    assert AUDIENCE in " ".join(diagnosis.evaluation_steps)


def test_the_judge_is_not_asked_once_a_check_that_the_answer_is_right_fails():
    from metrics.common import NO_OUTPUT, LatencyMetric, PluginAnsweredMetric
    from suite.endpoint_metrics import skip_judges_after_failure
    from judge.metrics import NOT_JUDGED, NotJudgedMetric

    judge = FakeJudge()  # fails the test if it is called
    case = LLMTestCase(input="Explain the plan", actual_output=NO_OUTPUT, metadata={})
    explanation = prose_metric(judge, "explain-plan")
    metrics = [PluginAnsweredMetric(), LatencyMetric(30), explanation]
    graded = skip_judges_after_failure(metrics, case)
    assert graded[:2] == metrics[:2]
    skipped = graded[2]
    assert isinstance(skipped, NotJudgedMetric)
    assert skipped.__name__ == explanation.__name__ and skipped.threshold == explanation.threshold
    assert asyncio.run(skipped.a_measure(case)) == 0.0 and not skipped.is_successful()
    assert skipped.reason == f"{NOT_JUDGED}Plugin Answered failed"

    # assert_test copies every metric before measuring it.
    from deepeval.metrics.utils import copy_metrics
    [copied] = copy_metrics([skipped])
    assert isinstance(copied, NotJudgedMetric) and copied.reason == skipped.reason

    # Only slow: the judge is still asked.
    slow = LLMTestCase(input="Explain the plan", actual_output="It scans.", completion_time=99, metadata={})
    assert skip_judges_after_failure(metrics, slow) == metrics


###############################################
# A second opinion on borderline scores (judge/second_opinion.py)
###############################################


@pytest.mark.parametrize("first, second, reasked", [
    (5, 6, False),
    (7, 6, True),   # the second model agrees
    (7, 9, True),   # it disagrees, and counts
    (8, 9, True),
    (8, 7, True),
    (9, 6, False),
])
def test_only_a_borderline_score_is_judged_again_and_the_second_verdict_counts(first, second, reasked):
    from judge.second_opinion import SECOND_OPINION, SecondOpinionModel

    judge = SecondOpinionModel(
        ScriptedJudge("small-model", first), ScriptedJudge("large-model", second), 0.8
    )
    metric = prose_metric(judge, "explain-plan", threshold=0.8)
    case = LLMTestCase(input="Explain the plan", actual_output="It scans.", context=["Plan"])
    asyncio.run(metric.a_measure(case, _show_indicator=False))

    assert metric.score == (second / 10 if reasked else first / 10)
    assert (SECOND_OPINION in metric.reason) == reasked
    if reasked:
        assert metric.reason == (
            f"large-model gives {second} [re-asked: small-model gave "
            f"{first / 10}, large-model {second / 10}]"
        )
        assert metric.success == (second >= 8) and metric.input_tokens == 200  # both
    assert "borderline re-asked of large-model" in metric.evaluation_model


def test_a_second_opinion_model_is_used_only_when_one_is_named():
    from judge.second_opinion import SecondOpinionModel

    env = {"JUDGE_PROVIDER": "openrouter", "JUDGE_MODEL_NAME": "some-model"}
    with mock.patch.object(judge_model, "OpenRouterModel") as model_cls:
        assert judge_model.get_judge_model(env) is model_cls.return_value
        judge = judge_model.get_judge_model({**env, "JUDGE_SECOND_OPINION_MODEL": "better"})
        # Naming the judge itself, or nothing, gives no second opinion.
        assert judge_model.get_judge_model(
            {**env, "JUDGE_SECOND_OPINION_MODEL": "some-model"}
        ) is model_cls.return_value
    assert isinstance(judge, SecondOpinionModel)
    assert [c.kwargs["model"] for c in model_cls.call_args_list][1:3] == ["some-model", "better"]
    assert judge.threshold == 0.8 and judge.margin == 0.1
