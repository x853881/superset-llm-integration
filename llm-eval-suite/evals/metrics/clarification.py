"""Metrics for goldens where the right answer isn't SQL. No judge involved;
Clarification Quality and Explains What's Missing (judge/metrics.py) judge
what the plugin said.

"expect_clarification": true - requests too ambiguous to answer, where the
plugin should ask back instead of guessing:
    Asked for Clarification         the plugin asked instead of answering,
                                    or answered stating which reading it
                                    assumed
    Clarification Covers Readings   its questions offer every reading the
                                    golden lists in "readings"

"expect_cant_answer": true - requests the data can't answer (deaths per
capita, with no population anywhere), where the plugin should say so:
    Said It Can't Answer            it said so, or asked what to show
                                    instead, rather than writing SQL

The plugin's questions reach the test case as `metadata["clarifications"]`:
one {"question", "options"} per question, options empty for an open one
(PluginResult.clarifications).
"""

import re
from typing import Any, Dict, List, Sequence

from deepeval.test_case import LLMTestCase

from metrics.common import PassFailMetric, no_output_reason


def clarification_text(clarifications: Sequence[Dict[str, Any]]) -> str:
    """The plugin's questions as the user would read them: each question,
    then its options."""
    lines = []
    for i, c in enumerate(clarifications, 1):
        lines.append(f"{i}. {c.get('question', '')}")
        lines += [f"   - {option}" for option in c.get("options") or []]
    return "\n".join(lines)


def cant_answer_text(failure: Dict[str, Any], clarifications: Sequence[Dict[str, Any]]) -> str:
    """What the plugin told the user instead of writing SQL: its failure
    message, what it tried, its suggestions, and any questions it asked."""
    lines = []
    if failure.get("message"):
        lines.append(failure["message"])
    if failure.get("tried"):
        lines += ["What was tried:"] + [f"   - {t}" for t in failure["tried"]]
    if failure.get("suggestions"):
        lines += ["Suggestions:"] + [f"   - {s}" for s in failure["suggestions"]]
    if clarifications:
        lines.append(clarification_text(clarifications))
    return "\n".join(lines)


def _asked(test_case: LLMTestCase) -> List[Dict[str, Any]]:
    return list((test_case.metadata or {}).get("clarifications") or [])


class AskedForClarificationMetric(PassFailMetric):
    """The plugin asked at least one question instead of answering, or
    answered with SQL and stated which reading it assumed: the user sees its
    assumptions beside the SQL, so they know what they got. With `readings`,
    the assumptions must mention one of them; an assumption about something
    else (which column holds the year) doesn't say which reading it took.
    Whether the SQL matches that reading is SQL Result Match's to check."""

    name = "Asked for Clarification"

    def __init__(self, readings: Sequence[Sequence[str]] = (), threshold: float = 1.0):
        super().__init__(threshold)
        self.readings = readings

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        asked = _asked(test_case)
        if asked:
            return self._finish(
                True, f"asked {len(asked)} question{'' if len(asked) == 1 else 's'}"
            )
        meta = test_case.metadata or {}
        if meta.get("plugin_sql"):
            assumptions = meta.get("assumptions") or []
            assumed = "; ".join(assumptions) or "none listed"
            if assumptions and self._names_a_reading(assumed):
                return self._finish(
                    True, f"answered with SQL, stating its assumption: {assumed}"
                )
            return self._finish(
                False,
                f"answered with SQL instead of asking, without stating which reading "
                f"it chose; assumed: {assumed}",
            )
        return self._finish(False, no_output_reason(test_case, "question or answer"))

    def _names_a_reading(self, text: str) -> bool:
        return not self.readings or any(
            re.search(p, text, re.IGNORECASE) for patterns in self.readings for p in patterns
        )


class ClarificationCoversReadingsMetric(PassFailMetric):
    """The questions and options mention every reading in `readings`: one
    list of regular expressions per reading (case-insensitive), any of which
    counts as offering it. A question that names both countries of an
    ambiguous "Congo", for example, offers both readings."""

    name = "Clarification Covers Readings"

    def __init__(self, readings: Sequence[Sequence[str]], threshold: float = 1.0):
        super().__init__(threshold)
        self.readings = readings

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        asked = _asked(test_case)
        if not asked:
            return self._finish(False, "the plugin didn't ask, so it offered no readings")
        text = clarification_text(asked)
        missing = [
            patterns[0]
            for patterns in self.readings
            if not any(re.search(p, text, re.IGNORECASE) for p in patterns)
        ]
        if missing:
            return self._finish(
                False,
                f"offers {len(self.readings) - len(missing)} of {len(self.readings)} "
                f"readings; missing: {', '.join(missing)}",
            )
        return self._finish(True, f"offers all {len(self.readings)} readings")


class SaidItCantAnswerMetric(PassFailMetric):
    """For goldens the data can't answer: the plugin reported a failure, or
    asked what to show instead, rather than writing SQL. SQL here means it
    answered something else - a proxy, or an invented column - as if it were
    what was asked; the reason gives what it assumed."""

    name = "Said It Can't Answer"

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        meta = test_case.metadata or {}
        if meta.get("plugin_sql"):
            assumed = "; ".join(meta.get("assumptions") or []) or "none listed"
            return self._finish(False, f"answered with SQL anyway; assumed: {assumed}")
        if meta.get("plugin_status") == "Failure":
            return self._finish(True, "said it can't answer this from the data")
        if meta.get("clarifications"):
            return self._finish(True, "asked what to show instead")
        return self._finish(False, no_output_reason(test_case, "answer"))
