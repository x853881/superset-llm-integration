"""Deterministic metrics for the plugin's plain-English answers. No LLM involved.

They check the rules the plugin itself is given (see prose.py, sql_breakdown.py
and nlsql_backend.py in the plugin), which plain code can check exactly, so the
judge is left with what needs judgement:

    Tooltip Terms         terms marked [[term|meaning]] for the panel's hover:
                          none in descriptions (they go into the question box),
                          at most 3 elsewhere, never SQL keywords or schema names
    Breakdown Coverage    describe-breakdown's sections cover every line of the SQL
    Description Unchanged an updated description that should read as before did
"""

import re
from typing import Any, Dict, Iterable, Mapping, Tuple

from deepeval.test_case import LLMTestCase

from metrics.common import no_output_reason, PassFailMetric, generated_sql
from suite.schema_metadata import TableMetadata

# The plugin's own markup for a term the panel explains on hover.
TERM = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]*)\]\]")

# The plugin marks at most this many terms per answer.
MAX_TERMS = 3

# Ordinary SQL the plugin is told never to mark.
SQL_WORDS = {
    "select", "from", "where", "join", "inner join", "left join", "right join",
    "full join", "on", "group by", "order by", "having", "limit", "offset", "union",
    "union all", "distinct", "with", "as", "case", "when", "and", "or", "not", "in",
    "exists", "not exists", "not in", "between", "like", "null", "is null", "count",
    "sum", "avg", "min", "max", "coalesce", "cast", "extract", "over", "partition by",
}


def _result(test_case: LLMTestCase) -> Dict[str, Any]:
    response = (test_case.metadata or {}).get("plugin_response") or {}
    result = response.get("result")
    return result if isinstance(result, dict) else {}


class TooltipTermsMetric(PassFailMetric):
    """Terms marked as [[term|meaning]]: at most `max_terms` (0 for
    descriptions, which go into the question box), and none that are SQL
    keywords or table or column names."""

    name = "Tooltip Terms"

    def __init__(
        self,
        metadata: Mapping[str, TableMetadata],
        max_terms: int = MAX_TERMS,
        threshold: float = 1.0,
    ):
        super().__init__(threshold)
        self.metadata = metadata
        self.max_terms = max_terms

    def _schema_names(self) -> Iterable[str]:
        for table in self.metadata.values():
            yield table.name.lower()
            yield from (column.lower() for column in table.columns)

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        if generated_sql(test_case) is None:
            return self._finish(False, no_output_reason(test_case, "answer"))
        # The answer is every piece of text the plugin wrote for the panel,
        # breakdown section details included (plugin/endpoints.py).
        terms = [m.group(1).strip() for m in TERM.finditer(test_case.actual_output or "")]
        allowed = (
            "none allowed: descriptions go into the question box"
            if self.max_terms == 0
            else f"allowed up to {self.max_terms}"
        )
        listed = f": {', '.join(terms)}" if terms else ""
        problems = []
        if len(terms) > self.max_terms:
            problems.append(f"{len(terms)} terms marked ({allowed}){listed}")
        names = set(self._schema_names())
        plain = [t for t in terms if t.lower() in SQL_WORDS or t.lower() in names]
        if plain:
            problems.append(
                "marked ordinary SQL or table/column names: " + ", ".join(plain)
            )
        if problems:
            return self._finish(False, "; ".join(problems))
        return self._finish(True, f"{len(terms)} terms marked ({allowed}){listed}")


class BreakdownCoverageMetric(PassFailMetric):
    """describe-breakdown's sections between them cover every non-blank line
    of the SQL they describe (the plugin's own copy, in its result), and no
    two partly overlap. The plugin is told not to overlap at all, but a
    section wholly inside another (a subquery in its outer query) reads fine."""

    name = "Breakdown Coverage"

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        result = _result(test_case)
        sections = result.get("sections") or []
        if not sections:
            return self._finish(False, "the plugin returned no sections")
        lines = str(result.get("sql") or "").splitlines()
        # The plugin may lay the SQL out afresh before numbering its lines,
        # so the golden's own SQL is no stand-in for its copy.
        if not any(line.strip() for line in lines):
            return self._finish(
                False, "the plugin returned no SQL for the sections to cover"
            )
        ranges = []
        for section in sections:
            try:
                ranges.append((int(section["startLine"]), int(section["endLine"])))
            except (KeyError, TypeError, ValueError):
                continue
        covered = {n for start, end in ranges for n in range(start, end + 1)}
        problems = []
        missed = [
            n for n, line in enumerate(lines, start=1) if line.strip() and n not in covered
        ]
        if missed:
            shown = ", ".join(str(n) for n in missed[:12]) + ("…" if len(missed) > 12 else "")
            problems.append(f"lines not in any section: {shown} (of {len(lines)})")
        # A section wholly inside another is fine: a subquery's lines inside
        # the outer query's reads naturally. Only a partial overlap fails.
        overlaps = [
            f"lines {a[0]}-{a[1]} and {b[0]}-{b[1]}"
            for i, a in enumerate(sorted(ranges))
            for b in sorted(ranges)[i + 1:]
            if b[0] <= a[1] and not _inside(a, b) and not _inside(b, a)
        ]
        if overlaps:
            problems.append(f"sections overlap: {'; '.join(overlaps)}")
        if problems:
            return self._finish(False, "; ".join(problems))
        return self._finish(
            True,
            f"{len(sections)} sections cover all {len(lines)} lines, without overlapping "
            "(one inside another is fine)",
        )


def _inside(inner: Tuple[int, int], outer: Tuple[int, int]) -> bool:
    """Whether the lines `inner` covers are all in `outer`, and fewer of them."""
    return outer[0] <= inner[0] and inner[1] <= outer[1] and inner != outer


def _plain(text: str) -> str:
    return " ".join(text.split()).strip().rstrip(".").lower()


class DescriptionUnchangedMetric(PassFailMetric):
    """For an edit that doesn't change the result (e.g. a renamed column): the
    updated description reads the same as `previous` (ignoring spacing, case
    and a final full stop)."""

    name = "Description Unchanged"

    def __init__(self, previous: str, threshold: float = 1.0):
        super().__init__(threshold)
        self.previous = previous

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        answer = generated_sql(test_case)
        if answer is None:
            return self._finish(False, no_output_reason(test_case, "answer"))
        if _plain(answer) == _plain(self.previous):
            return self._finish(True, "the description reads as before")
        return self._finish(
            False,
            f"the description changed, but the edit doesn't change the result; "
            f"before: {self.previous!r}",
        )
