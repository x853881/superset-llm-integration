"""The structured result of an LLM SQL-generation call.

Mirrors the JSON shape the model itself is asked to respond in (see
``nlsql_backend.SQL_RESPONSE_FORMAT_INSTRUCTIONS``), so a decoded response can be handed straight
to the frontend as the API result with no extra translation - whichever of the three shapes
(``Success``, ``Failure``, ``ClarificationsRequired``) the model reported, the frontend renders
directly from these same field names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .change_anchors import Change, parse_changes

STATUS_SUCCESS = "Success"
STATUS_FAILURE = "Failure"
STATUS_CLARIFICATIONS_REQUIRED = "ClarificationsRequired"


@dataclass
class SpecificClarification:
    question: str
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"question": self.question, "options": self.options}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SpecificClarification":
        return cls(question=raw.get("question") or "", options=list(raw.get("options") or []))


@dataclass
class SqlGenerationResult:
    status: str
    sql_text: str = ""
    sql_detailed_description: str = ""
    tables_used: list[str] = field(default_factory=list)
    suggestions_for_refinement: list[str] = field(default_factory=list)
    assumptions_made: list[str] = field(default_factory=list)
    specific_clarifications_needed: list[SpecificClarification] = field(default_factory=list)
    general_clarifications_needed: list[str] = field(default_factory=list)
    failure_message: str = ""
    what_was_tried_on_failure: list[str] = field(default_factory=list)
    suggestions_for_fixing_on_failure: list[str] = field(default_factory=list)
    #: What a refinement or an update changed, each with the SQL either side of it (see
    #: `change_anchors`). Empty for SQL written from scratch, which has nothing to change from.
    changes: list[Change] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "sqlText": self.sql_text,
            "sqlDetailedDescription": self.sql_detailed_description,
            "tablesUsed": self.tables_used,
            "suggestionsForRefinement": self.suggestions_for_refinement,
            "assumptionsMade": self.assumptions_made,
            "specificClarificationsNeeded": [c.to_dict() for c in self.specific_clarifications_needed],
            "generalClarificationsNeeded": self.general_clarifications_needed,
            "failureMessage": self.failure_message,
            "whatWasTriedOnFailure": self.what_was_tried_on_failure,
            "suggestionsForFixingOnFailure": self.suggestions_for_fixing_on_failure,
            "changes": [change.to_dict() for change in self.changes],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SqlGenerationResult":
        return cls(
            status=raw.get("status") or STATUS_FAILURE,
            sql_text=raw.get("sqlText") or "",
            sql_detailed_description=raw.get("sqlDetailedDescription") or "",
            tables_used=list(raw.get("tablesUsed") or []),
            suggestions_for_refinement=list(raw.get("suggestionsForRefinement") or []),
            assumptions_made=list(raw.get("assumptionsMade") or []),
            specific_clarifications_needed=[
                SpecificClarification.from_dict(c) for c in (raw.get("specificClarificationsNeeded") or [])
            ],
            general_clarifications_needed=list(raw.get("generalClarificationsNeeded") or []),
            failure_message=raw.get("failureMessage") or "",
            what_was_tried_on_failure=list(raw.get("whatWasTriedOnFailure") or []),
            suggestions_for_fixing_on_failure=list(raw.get("suggestionsForFixingOnFailure") or []),
            changes=parse_changes(raw.get("changes")),
        )
