"""Explains why a SQL query was rejected, and proposes a corrected version of it.

Takes the errors Superset already shows in the editor's gutter - whichever of them is available:
the SQL validator's annotations (``POST /api/v1/database/<id>/validate_sql/``, the same call SQL
Lab's own editor makes) or the message a failed run came back with - and turns them into something
a user can act on: what the engine's wording actually means, why *this* query triggered it, and
the query as it should have been written. The frontend offers that last part as a one-click
replacement of the editor's contents (see ``FixErrorsCard``).

Deliberately separate from the ask/refine flow in ``nlsql_backend``, because it's a different job:
there is no natural-language request here and nothing is being authored - the input is a query the
user (or an earlier generation) already has, plus a machine-generated complaint about it. It does
reuse that module's schema selection and streaming machinery, since "which tables are in play" and
"show the answer as it's written" are the same problems either way.

A proposed correction is parse-checked like any other generated SQL, and one repair attempt is
made if it doesn't parse. If it still doesn't, the explanation is kept and the correction dropped
(``Explained`` rather than ``Fixed``) - an explanation that reads well is useful on its own, and
offering to paste in SQL known not to parse is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from . import change_anchors, nlsql_backend, progress, prose, sql_checks
from .cancellation import CancelToken
from .progress import EventStream
from .sql_fix_result import STATUS_EXPLAINED, STATUS_FAILURE, STATUS_FIXED, SqlFixResult

# The fields of the response streamed into the panel as the model writes them - the explanation
# first, so there is something to read while the corrected SQL is still being written.
STREAMED_FIELDS = ["problemSummary", "cause", "fixExplanation", "fixedSql"]


def _fix_steps(draft_running: str, draft_done: str) -> progress.Steps:
    """The plan for one explain-and-fix call: the same shape as a generation (see
    `nlsql_backend._sql_steps`), with the summary step left out - this flow's explanation *is* its
    answer, so there is no separate description of it to write afterwards."""
    return progress.Steps(nlsql_backend.with_tables_step([
        ("read", "Read the errors", "Read the errors"),
        nlsql_backend.TABLES_STEP,
        ("draft", draft_running, draft_done),
        ("parse", "Checking the correction is valid", "Checked the correction is valid"),
    ]))


@dataclass(frozen=True)
class SqlError:
    """One complaint about the query, as the editor's gutter shows it. `line`/`column` are
    1-based, as Superset's validator API reports them; both are optional because a failed run
    reports a message with no position at all."""

    message: str
    line: Optional[int] = None
    column: Optional[int] = None
    # Where the complaint came from - the SQL validator, or the engine rejecting an actual run.
    source: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SqlError":
        return cls(
            message=str(raw.get("message") or "").strip(),
            line=_optional_int(raw.get("line")),
            column=_optional_int(raw.get("column")),
            source=str(raw.get("source") or "").strip(),
        )

    def to_prompt_text(self, sql: str) -> str:
        """The error, its position, and the line of `sql` it points at - the line itself because a
        line number alone makes the model count lines, which it is not reliably good at."""
        where = ""
        if self.line is not None:
            where = f" at line {self.line}" + (f", column {self.column}" if self.column is not None else "")
        text = f"- {self.source or 'error'}{where}: {self.message}"
        offending = _line_of(sql, self.line)
        if offending is not None:
            text += f"\n    the line it points at: {offending.strip()}"
        return text


def explain_and_fix(
    sql: str,
    errors: list[SqlError],
    dialect: str = "",
    cancel_token: Optional[CancelToken] = None,
) -> EventStream:
    """Explains `errors` against `sql` and proposes a corrected query. `dialect` is the database's
    engine name where it's known, since half of what makes an error message confusing is that it's
    the engine's wording rather than standard SQL's."""
    # The errors, not just the SQL, are part of what decides which tables matter: "no such column:
    # customers.nmae" is about a table the query may only have misspelled.
    steps = _fix_steps("Working out what went wrong", "Worked out what went wrong")
    yield steps.plan_event
    yield steps.done("read", "; ".join(error.message for error in errors)[:160])
    preamble = yield from nlsql_backend.schema_preamble(
        f"{sql}\n{' '.join(error.message for error in errors)}", steps
    )
    prompt = _explain_prompt(sql, errors, dialect, preamble)
    result = yield from _request_fix(prompt, steps, cancel_token)
    if result.status != STATUS_FIXED:
        # No correction, so nothing changed - whatever the model listed.
        result.changes = []
        return result.to_dict()

    analysis = yield from _check_parses(result.fixed_sql, steps)
    if analysis.valid:
        return _with_placed_changes(result, sql).to_dict()

    retry = yield from _request_fix(
        nlsql_backend.retry_prompt_for(prompt, result.fixed_sql, analysis.error or ""),
        steps,
        cancel_token,
        attempt_note="Second attempt - the first correction wasn't valid itself",
    )
    if retry.status == STATUS_FIXED:
        retry_analysis = yield from _check_parses(retry.fixed_sql, steps)
        if retry_analysis.valid:
            return _with_placed_changes(retry, sql).to_dict()

    # Keep whichever explanation read best and drop the SQL - there is still something here worth
    # showing, just nothing safe to offer to paste into the editor.
    explained = retry if retry.status != STATUS_FAILURE and retry.problem_summary else result
    explained.status = STATUS_EXPLAINED
    explained.fixed_sql = ""
    explained.changes = []
    explained.fix_explanation = (
        f"{explained.fix_explanation}\n\n"
        "A corrected query was drafted but didn't parse or wasn't valid, so it isn't offered here."
    ).strip()
    return explained.to_dict()


def _with_placed_changes(result: SqlFixResult, sql: str) -> SqlFixResult:
    """`result` with its changes placed by the SQL they quote (see `change_anchors`)."""
    result.changes = change_anchors.anchor(result.changes, sql, result.fixed_sql)
    change_anchors.note_on_request(result.changes)
    return result


def _request_fix(
    prompt: str,
    steps: progress.Steps,
    cancel_token: Optional[CancelToken],
    attempt_note: Optional[str] = None,
) -> EventStream:
    raw_text = yield from nlsql_backend.stream_json_fields(
        prompt, steps, STREAMED_FIELDS, cancel_token, attempt_note
    )
    result = SqlFixResult.from_dict(json.loads(nlsql_backend.extract_json(raw_text)))
    if result.status == STATUS_FIXED:
        result.fixed_sql = nlsql_backend.strip_sql_fence(result.fixed_sql)
    return result


def _check_parses(sql: str, steps: progress.Steps) -> EventStream:
    return (yield from sql_checks.check(sql, steps, running_detail="Nothing replaces what's in the editor until you say so"))


FIX_RESPONSE_FORMAT_INSTRUCTIONS = """\
Respond with ONLY a single JSON object (no markdown fences, no other text) with this shape, with
the fields in this order - the explanation is shown to the user as you write it, so what comes
first matters:
{
  "status": "Fixed" | "Explained" | "Failure",
  "problemSummary": "...",
  "cause": "...",
  "fixExplanation": "...",
  "fixedSql": "...",
  "confidence": "High" | "Medium" | "Low",
  "remainingRisks": ["..."],
  "failureMessage": "...",
  "changes": [{"summary": "...", "why": "...", "before": "...", "after": "..."}]
}

- problemSummary: what the error message means, in plain English, for someone who did not write
  the engine that produced it. One or two sentences. Don't restate the error verbatim.
- cause: why this particular query triggered it - name the table, column or clause at fault.
- fixExplanation: what your correction changes and why that addresses the cause. Describe the
  change ("joins orders to customers on customer_id, which the query was missing"), not the
  diff.
- fixedSql: the whole corrected query, ready to replace what is in the editor. Not a fragment,
  and not annotated with comments about the fix.
- confidence: how sure you are that this is what the user meant, not merely that it will parse.
  Use "Low" freely - a guess labelled as a guess is more use than a confident wrong answer.
- remainingRisks: anything your correction does not settle and the user should check themselves,
  for example ["Assumes `amount` is already net of discounts", "The date filter now uses
  order_date rather than ship_date"]. Empty list if there are none.
- changes (only with "Fixed"): each change you made, in the order it appears in the query -
  empty when nothing changed:
  - summary: what you changed, as a short phrase ("joined orders to customers on customer_id").
  - why: which error it fixes, and how, in a sentence.
  - before: the part of the query in the editor the change replaced, copied from it exactly - enough
    to find it, from a few words to a line or two, never the whole query. "" when you only added
    something.
  - after: the part of fixedSql that replaced it, copied exactly in the same way. "" when you only
    removed something.

Use "Fixed" only when you can produce a corrected query you believe addresses every error above.
Use "Explained" when you understand the errors but cannot produce a correction you would stand
behind - fill in problemSummary and cause, leave fixedSql empty, and say in fixExplanation what
you would need to know to fix it.
Use "Failure" only when the errors don't make sense against this query at all, with the reason in
failureMessage.
"""


def _explain_prompt(sql: str, errors: list[SqlError], dialect: str, schema_preamble_text: str) -> str:
    engine = f" The database is {dialect}." if dialect else ""
    error_text = "\n".join(error.to_prompt_text(sql) for error in errors) or "- (no message given)"
    return (
        f"{schema_preamble_text}"
        f"A user's SQL query was rejected before it ran.{engine} The editor is showing these errors\n"
        "against it:\n"
        f"{error_text}\n\n"
        "The query, with line numbers as the errors above count them:\n"
        f"{_numbered(sql)}\n\n"
        "Explain what went wrong and correct it. Keep the query recognisably the user's own - fix\n"
        "what the errors complain about and leave the rest of its structure, naming and style\n"
        "alone. Do not add a LIMIT, reformat the whole query, or change what it returns beyond\n"
        "what fixing the error requires.\n\n"
        f"{FIX_RESPONSE_FORMAT_INSTRUCTIONS}"
        f"{_fix_prose()}"
    )


def _fix_prose() -> str:
    return prose.instructions(
        "problemSummary, cause, fixExplanation, remainingRisks and each change's summary and why",
        "one short sentence for each of problemSummary, cause and fixExplanation, each risk a phrase, each change a few words",
    )


def _numbered(sql: str) -> str:
    """`sql` with 1-based line numbers, matching how the validator reports error positions."""
    return "\n".join(f"{number:>4} | {line}" for number, line in enumerate(sql.splitlines(), start=1))


def _line_of(sql: str, line: Optional[int]) -> Optional[str]:
    if line is None:
        return None
    lines = sql.splitlines()
    return lines[line - 1] if 1 <= line <= len(lines) else None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
