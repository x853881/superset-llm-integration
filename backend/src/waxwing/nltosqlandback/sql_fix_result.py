"""The structured result of an "explain this SQL error" call.

Mirrors the JSON shape the model is asked to respond in (see
``sql_error_fix.FIX_RESPONSE_FORMAT_INSTRUCTIONS``), so a decoded response can be handed straight
to the frontend as the API result with no extra translation.

Three shapes, as with ``SqlGenerationResult``, distinguished by what the frontend can offer to do
about them:

- ``Fixed``: the error is explained *and* there is corrected SQL that parses, so the panel can
  offer to put it in the editor in place of what's there.
- ``Explained``: the error is explained but no correction can be proposed with any confidence -
  the explanation is still worth reading, there's just nothing to apply.
- ``Failure``: the error couldn't be made sense of at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .change_anchors import Change, parse_changes

STATUS_FIXED = "Fixed"
STATUS_EXPLAINED = "Explained"
STATUS_FAILURE = "Failure"


@dataclass
class SqlFixResult:
    status: str
    # What the error message means, in plain English rather than the engine's own wording.
    problem_summary: str = ""
    # Why this particular query triggered it.
    cause: str = ""
    # What the proposed correction changes, and why that addresses the cause.
    fix_explanation: str = ""
    fixed_sql: str = ""
    # The model's own confidence in the correction - shown next to it, because "a model wrote this
    # and it parses" is not the same as "this is what you meant".
    confidence: str = ""
    # Anything the correction doesn't settle and the user should check for themselves.
    remaining_risks: list[str] = field(default_factory=list)
    failure_message: str = ""
    #: What the correction changed, each with the SQL either side of it (see `change_anchors`).
    changes: list[Change] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "problemSummary": self.problem_summary,
            "cause": self.cause,
            "fixExplanation": self.fix_explanation,
            "fixedSql": self.fixed_sql,
            "confidence": self.confidence,
            "remainingRisks": self.remaining_risks,
            "failureMessage": self.failure_message,
            "changes": [change.to_dict() for change in self.changes],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SqlFixResult":
        return cls(
            status=raw.get("status") or STATUS_FAILURE,
            problem_summary=raw.get("problemSummary") or "",
            cause=raw.get("cause") or "",
            fix_explanation=raw.get("fixExplanation") or "",
            fixed_sql=raw.get("fixedSql") or "",
            confidence=raw.get("confidence") or "",
            remaining_risks=list(raw.get("remainingRisks") or []),
            failure_message=raw.get("failureMessage") or "",
            changes=parse_changes(raw.get("changes")),
        )
