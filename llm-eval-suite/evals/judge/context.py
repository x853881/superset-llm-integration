"""What the judge is shown as a test case's context, besides the question and
the answer: the performance policy, notes on the tables involved, the static
analysis findings and, for explain-plan, the database's plan. The wording
and limits are in judge/prompts.py.
"""

import json
from typing import Any, Iterable, List, Mapping, Optional

from judge.prompts import PERFORMANCE_POLICY, PLAN_CHARS
from metrics.sql_performance import PerformanceFinding
from suite.schema_metadata import TableMetadata


POLICY_HEADING = "Performance policy:"
FINDINGS_HEADING = "Static analysis findings for the generated SQL:"


def build_performance_context(
    metadata: Mapping[str, TableMetadata],
    tables: Optional[Iterable[str]] = None,
    findings: Iterable[PerformanceFinding] = (),
) -> List[str]:
    """Context chunks for `LLMTestCase.context`: policy, then one chunk per
    table (all tables, or only `tables`), then any static analysis findings."""
    wanted = {t.lower() for t in tables} if tables is not None else None
    chunks = [
        f"{POLICY_HEADING}\n" + "\n".join(f"- {p}" for p in PERFORMANCE_POLICY)
    ]
    chunks += [
        t.to_context()
        for key, t in metadata.items()
        if wanted is None or key in wanted
    ]
    findings = list(findings)
    if findings:
        chunks.append(
            f"{FINDINGS_HEADING}\n"
            + "\n".join(
                f"- [{f.severity}] {f.rule}: {f.message}" for f in findings
            )
        )
    return chunks


def without_performance_notes(context: Iterable[str]) -> List[str]:
    """`context` without the performance policy and static analysis findings,
    for a judge that grades something else: seeing them, it marks a correct
    answer down for being slow, which the performance metrics already do."""
    return [
        chunk
        for chunk in context
        if not chunk.startswith((POLICY_HEADING, FINDINGS_HEADING))
    ]


def plan_context(plan: Any) -> str:
    """The database's plan for explain-plan, cut to PLAN_CHARS characters."""
    return "Database plan:\n" + json.dumps(plan, indent=1)[:PLAN_CHARS]
