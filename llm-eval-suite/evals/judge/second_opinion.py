"""A second opinion on borderline judgements.

The judge scores in steps of 0.1 against a pass mark of 0.8, and a small
model gives the same answer 0.7 one time and 0.8 the next: canaries flipped both ways
(a right description failed at 0.7, a wrong one passed at 0.8). So when the
judge's score lands within JUDGE_SECOND_OPINION_MARGIN (default 0.1) below
the pass mark, or on it, the same prompt goes to a second, stronger model
(JUDGE_SECOND_OPINION_MODEL), whose verdict counts. Scores clear of the band
keep the first judge's verdict, so only the borderline ones cost the stronger
model a judgement, not the whole run.

A mid-sized second model flipped too (in a 5-run report, 33 of 81 re-asked
verdicts changed, 20 to a pass and 13 to a fail), and asking it again to get
two of three made each disagreement cost three judgements. One judgement by
the strongest model settles it instead.

It wraps the judge model rather than each metric, so every judged metric,
the canaries and the report get it. It sees the judge's raw score, which in
this suite is out of 10 for every metric (each rubric runs 0 to 10, as does
GEval's default).

The score and reason are the second model's, and the reason ends with both
scores, starting with SECOND_OPINION, which the report counts:
"[re-asked: small-model gave 0.8, large-model 0.7]".
"""

from typing import Any, Optional, Tuple, Type

from deepeval.models import DeepEvalBaseLLM
from deepeval.models.utils import EvaluationCost
from pydantic import BaseModel

# Starts what is added to the end of a re-asked judgement's reason.
SECOND_OPINION = "[re-asked: "

# Every judged metric's raw score is out of this (see the docstring).
SCORE_OUT_OF = 10


def _added(a: Any, b: Any) -> EvaluationCost:
    """Two judgements' cost and tokens together."""
    def total(x, y):
        return None if x is None and y is None else (x or 0) + (y or 0)

    cost = total(None if a is None else float(a), None if b is None else float(b))
    return EvaluationCost(
        cost,
        input_tokens=total(getattr(a, "input_tokens", None), getattr(b, "input_tokens", None)),
        output_tokens=total(getattr(a, "output_tokens", None), getattr(b, "output_tokens", None)),
    )


class SecondOpinionModel(DeepEvalBaseLLM):
    def __init__(
        self,
        first: DeepEvalBaseLLM,
        second: DeepEvalBaseLLM,
        threshold: float,
        margin: float = 0.1,
    ):
        self.first, self.second = first, second
        self.threshold, self.margin = threshold, margin
        super().__init__(first.name)

    def load_model(self):
        return self.name

    def get_model_name(self) -> str:
        return f"{self.first.get_model_name()}, borderline re-asked of {self.second.name}"

    def borderline(self, answer: Any) -> bool:
        """Whether `answer` is a score within the margin below the pass mark,
        or on it. 1e-9: 0.8 - 0.1 is 0.7000000000000001."""
        score = getattr(answer, "score", None)
        if not isinstance(score, (int, float)) or not hasattr(answer, "reason"):
            return False
        score /= SCORE_OUT_OF
        return self.threshold - self.margin - 1e-9 <= score <= self.threshold + 1e-9

    def _verdict(self, first: Tuple[Any, Any], second: Tuple[Any, Any]) -> Tuple[Any, EvaluationCost]:
        """The second answer, which counts, with both scores added to its
        reason, and the cost of both judgements."""
        answer = second[0]
        answer.reason = (
            f"{answer.reason} {SECOND_OPINION}{self.first.name} gave "
            f"{first[0].score / SCORE_OUT_OF:.1f}, {self.second.name} "
            f"{answer.score / SCORE_OUT_OF:.1f}]"
        )
        return answer, _added(first[1], second[1])

    def generate(
        self, prompt: str, schema: Optional[Type[BaseModel]] = None
    ) -> Tuple[Any, Any]:
        first = self.first.generate(prompt, schema)
        if not self.borderline(first[0]):
            return first
        return self._verdict(first, self.second.generate(prompt, schema))

    async def a_generate(
        self, prompt: str, schema: Optional[Type[BaseModel]] = None
    ) -> Tuple[Any, Any]:
        first = await self.first.a_generate(prompt, schema)
        if not self.borderline(first[0]):
            return first
        return self._verdict(first, await self.second.a_generate(prompt, schema))
