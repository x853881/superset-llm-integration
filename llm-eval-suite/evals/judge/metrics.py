"""The judged metrics: DeepEval GEval metrics graded by the judge model
(judge/model.py), with the wording in judge/prompts.py.
"""

from typing import Optional

from deepeval.metrics import BaseMetric, GEval
from deepeval.metrics.g_eval import GEvalTemplate
from deepeval.models import DeepEvalBaseLLM
from deepeval.test_case import LLMTestCase, SingleTurnParams

from judge.model import judge_threshold
from judge.prompts import (
    CANT_ANSWER_RUBRIC,
    CANT_ANSWER_STEPS,
    CLARIFICATION_RUBRIC,
    CLARIFICATION_STEPS,
    DIAGNOSIS_RUBRIC,
    DIAGNOSIS_STEPS,
    EXPLANATION_RUBRIC,
    PROSE_METRICS,
    PROSE_CORE_STEPS,
    QUERY_PERFORMANCE_RUBRIC,
    QUERY_PERFORMANCE_STEPS,
    REASON_RULE,
    SQL_CORRECTNESS_STEPS,
)
from judge.context import without_performance_notes


class OneLineReasonTemplate(GEvalTemplate):
    """DeepEval's grading prompt with REASON_RULE added, so the judge's
    reason is one sentence naming the problem. Only the reason changes: the
    steps, rubric and score are as before."""

    @staticmethod
    def generate_evaluation_results(**kwargs) -> str:
        prompt = GEvalTemplate.generate_evaluation_results(**kwargs)
        marker = "Only return valid JSON"
        if marker not in prompt:  # DeepEval reworded it: put the rule first
            return f"{REASON_RULE}\n\n{prompt}"
        return prompt.replace(marker, f"{REASON_RULE}\n\n{marker}", 1)


# The start of the reason of a judged metric that wasn't asked
# (NotJudgedMetric), which the report leaves out of the metric's counts.
NOT_JUDGED = "not judged: "


def is_judged(metric: BaseMetric) -> bool:
    """Whether the judge grades `metric`."""
    return isinstance(metric, (GEval, _OwnCaseMetric))


class NotJudgedMetric(BaseMetric):
    """A judged metric that isn't asked, because a check that the answer is
    right already failed (skip_judges_after_failure in
    suite/endpoint_metrics.py): it fails, with the check's name as its
    reason, and costs no judgement."""

    def __init__(self, judged: BaseMetric, failed: str):
        # Kept as attributes: DeepEval's copy_metrics rebuilds a metric from
        # the attributes that match its __init__'s parameters.
        self.judged, self.failed = judged, failed
        self.name = judged.__name__
        self.threshold = judged.threshold
        self.evaluation_model = getattr(judged, "evaluation_model", None)
        self.score, self.success = 0.0, False
        self.reason = f"{NOT_JUDGED}{failed} failed"

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.score

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.score

    def is_successful(self) -> bool:
        return False

    @property
    def __name__(self):
        return self.name


def _take_judgement(metric: BaseMetric, geval: GEval) -> float:
    """Copies a GEval's verdict, cost, token counts and steps onto the metric
    that ran it on a test case of its own, so they reach the report."""
    metric.score = geval.score
    metric.reason = geval.reason
    metric.success = geval.success
    metric.error = geval.error
    metric.evaluation_cost = geval.evaluation_cost
    metric.input_tokens = geval.input_tokens
    metric.output_tokens = geval.output_tokens
    metric.verbose_logs = geval.verbose_logs
    return metric.score


class _OwnCaseMetric(BaseMetric):
    """A GEval run on a test case of its own, which `_case` builds from the
    one the metric is given. `_case` returning None means there is nothing to
    judge, and `_nothing_to_judge` scores it without calling the judge."""

    def __init__(self, model: DeepEvalBaseLLM, threshold: Optional[float] = None):
        self.model = model
        self.threshold = judge_threshold() if threshold is None else threshold
        self.evaluation_model = model.get_model_name()

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        case = self._case(test_case)
        if case is None:
            return self._nothing_to_judge()
        geval = self._geval_for(case)
        geval.measure(case, _show_indicator=False)
        return _take_judgement(self, geval)

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        case = self._case(test_case)
        if case is None:
            return self._nothing_to_judge()
        geval = self._geval_for(case)
        await geval.a_measure(case, _show_indicator=False)
        return _take_judgement(self, geval)

    def is_successful(self) -> bool:
        return bool(self.success)


class SqlCorrectnessMetric(_OwnCaseMetric):
    """SQL Correctness, judged with what running the SQL returned. The
    summary in `metadata["query_results"]` (describe_results in
    metrics/query_results.py) is added to this judge's context only, and the
    performance policy and static analysis findings are left out of it, so a
    slow but right answer isn't failed twice; the test case keeps the context
    the other judges see."""

    def _geval(self) -> GEval:
        return GEval(
            name="SQL Correctness",
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
                SingleTurnParams.CONTEXT,
            ],
            evaluation_steps=SQL_CORRECTNESS_STEPS,
            model=self.model,
            threshold=self.threshold,
            evaluation_template=OneLineReasonTemplate,
        )

    def _geval_for(self, case: LLMTestCase) -> GEval:
        return self._geval()

    def _case(self, test_case: LLMTestCase) -> LLMTestCase:
        meta = test_case.metadata or {}
        added = [x for x in (meta.get("query_results"), _stated_assumptions(meta)) if x]
        context = without_performance_notes(test_case.context or [])
        if not added and context == (test_case.context or []):
            return test_case
        return LLMTestCase(
            input=test_case.input,
            actual_output=test_case.actual_output,
            expected_output=test_case.expected_output,
            context=[*context, *added],
        )

    @property
    def __name__(self):
        return "SQL Correctness [GEval]"


def _stated_assumptions(meta: dict) -> Optional[str]:
    """For an ambiguous request answered with SQL: the assumptions the plugin
    showed the user beside it, which say which reading it chose."""
    assumptions = meta.get("assumptions") if meta.get("expect_clarification") else None
    if not assumptions:
        return None
    return "The answer's stated assumptions, shown to the user with the SQL:\n" + "\n".join(
        f"- {a}" for a in assumptions
    )


def sql_correctness_metric(
    judge: DeepEvalBaseLLM, threshold: Optional[float] = None
) -> SqlCorrectnessMetric:
    return SqlCorrectnessMetric(judge, threshold)


def query_performance_metric(
    judge: DeepEvalBaseLLM, threshold: Optional[float] = None
) -> GEval:
    return GEval(
        name="Query Performance & Table Selection",
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.CONTEXT,
        ],
        evaluation_steps=QUERY_PERFORMANCE_STEPS,
        rubric=QUERY_PERFORMANCE_RUBRIC,
        model=judge,
        threshold=judge_threshold() if threshold is None else threshold,
        evaluation_template=OneLineReasonTemplate,
    )


def prose_metric(
    judge: DeepEvalBaseLLM, endpoint: str, threshold: Optional[float] = None
) -> GEval:
    """The judged metric for an endpoint that answers in plain English: the
    shared core (nothing invented, readable for AUDIENCE) plus what that
    endpoint's answer has to do."""
    name, setup, steps = PROSE_METRICS[endpoint]
    return GEval(
        name=name,
        evaluation_params=[
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.CONTEXT,
        ],
        evaluation_steps=[setup, *PROSE_CORE_STEPS, *steps],
        rubric=EXPLANATION_RUBRIC,
        model=judge,
        threshold=judge_threshold() if threshold is None else threshold,
        evaluation_template=OneLineReasonTemplate,
    )


class ErrorDiagnosisMetric(_OwnCaseMetric):
    """For explain-sql-error: does the plugin's explanation of the error
    (problem, cause, what the fix changes) identify what is really wrong?

    The fixed SQL is the test case's actual_output and is graded by the SQL
    metrics; the explanation comes from `metadata["diagnosis"]`, and the
    golden's `expected_diagnosis`, when it has one, from
    `metadata["expected_diagnosis"]`. They are graded as a test case of their
    own so the judge sees prose, not SQL."""

    def _geval(self, has_expected: bool) -> GEval:
        params = [
            SingleTurnParams.INPUT,
            SingleTurnParams.ACTUAL_OUTPUT,
            SingleTurnParams.CONTEXT,
        ]
        if has_expected:
            params.append(SingleTurnParams.EXPECTED_OUTPUT)
        return GEval(
            name="Error Diagnosis",
            evaluation_params=params,
            evaluation_steps=DIAGNOSIS_STEPS,
            rubric=DIAGNOSIS_RUBRIC,
            model=self.model,
            threshold=self.threshold,
            evaluation_template=OneLineReasonTemplate,
        )

    def _geval_for(self, case: LLMTestCase) -> GEval:
        return self._geval(case.expected_output is not None)

    def _case(self, test_case: LLMTestCase) -> Optional[LLMTestCase]:
        meta = test_case.metadata or {}
        if not meta.get("diagnosis"):
            return None
        # The fixed SQL's speed is Query Performance's to judge: with the
        # policy and findings in view, a right diagnosis was marked down for
        # the fix using EXTRACT.
        return LLMTestCase(
            input=test_case.input,
            actual_output=meta["diagnosis"],
            expected_output=meta.get("expected_diagnosis"),
            context=without_performance_notes(test_case.context or []),
        )

    def _nothing_to_judge(self) -> float:
        self.score, self.success = 0.0, False
        self.reason = "plugin gave no explanation of the error"
        return self.score

    @property
    def __name__(self):
        return "Error Diagnosis [GEval]"


class ClarificationQualityMetric(_OwnCaseMetric):
    """For goldens with "expect_clarification": are the plugin's questions
    about the real ambiguity, with options the data can answer? The test
    case's actual_output is the questions and options (clarification_text in
    metrics/clarification.py) and its expected_output the golden's
    "ambiguity". When the plugin didn't ask, there is nothing to judge."""

    def _geval(self) -> GEval:
        return GEval(
            name="Clarification Quality",
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
                SingleTurnParams.CONTEXT,
            ],
            evaluation_steps=CLARIFICATION_STEPS,
            rubric=CLARIFICATION_RUBRIC,
            model=self.model,
            threshold=self.threshold,
            evaluation_template=OneLineReasonTemplate,
        )

    def _geval_for(self, case: LLMTestCase) -> GEval:
        return self._geval()

    def _case(self, test_case: LLMTestCase) -> Optional[LLMTestCase]:
        meta = test_case.metadata or {}
        if "clarifications" in meta and not meta["clarifications"]:
            return None
        return test_case

    def _nothing_to_judge(self) -> float:
        self.score, self.success = 0.0, False
        self.reason = "the plugin didn't ask, so there was nothing to judge"
        return self.score

    @property
    def __name__(self):
        return "Clarification Quality [GEval]"


class MissingDataExplanationMetric(ClarificationQualityMetric):
    """For goldens with "expect_cant_answer": does what the plugin said
    instead of SQL name what the data lacks, and offer alternatives it can
    answer? The test case's actual_output is that message
    (cant_answer_text in metrics/clarification.py) and its expected_output
    the golden's "missing". When the plugin wrote SQL, there is nothing to
    judge."""

    def _geval(self) -> GEval:
        return GEval(
            name="Explains What's Missing",
            evaluation_params=[
                SingleTurnParams.INPUT,
                SingleTurnParams.ACTUAL_OUTPUT,
                SingleTurnParams.EXPECTED_OUTPUT,
                SingleTurnParams.CONTEXT,
            ],
            evaluation_steps=CANT_ANSWER_STEPS,
            rubric=CANT_ANSWER_RUBRIC,
            model=self.model,
            threshold=self.threshold,
            evaluation_template=OneLineReasonTemplate,
        )

    def _case(self, test_case: LLMTestCase) -> Optional[LLMTestCase]:
        meta = test_case.metadata or {}
        return None if meta.get("plugin_sql") or not test_case.actual_output else test_case

    def _nothing_to_judge(self) -> float:
        self.score, self.success = 0.0, False
        self.reason = "the plugin wrote SQL, so it explained nothing"
        return self.score

    @property
    def __name__(self):
        return "Explains What's Missing [GEval]"


def missing_data_explanation_metric(
    judge: DeepEvalBaseLLM, threshold: Optional[float] = None
) -> MissingDataExplanationMetric:
    return MissingDataExplanationMetric(judge, threshold)


def clarification_quality_metric(
    judge: DeepEvalBaseLLM, threshold: Optional[float] = None
) -> ClarificationQualityMetric:
    return ClarificationQualityMetric(judge, threshold)


def error_diagnosis_metric(
    judge: DeepEvalBaseLLM, threshold: Optional[float] = None
) -> ErrorDiagnosisMetric:
    return ErrorDiagnosisMetric(judge, threshold)
