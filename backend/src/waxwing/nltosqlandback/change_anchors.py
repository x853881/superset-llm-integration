"""Where in the SQL each change the model made is - Accelerate's, a refinement's, an update's, a
fix's: found from the fragments the model quoted, never from positions it counted.

Each change comes back with the part of the original SQL it replaced (``before``) and the part of
the rewrite that replaced it (``after``), copied out of the SQL. Models copy text reliably and count
characters and lines badly, and a position is only right for the exact text it was counted in: not
after the SQL is laid out again, edited by hand, or compared with a version two steps back. So the
fragment is what is kept, and it is found again in whichever SQL it is shown against.

Fragments are matched as tokens rather than characters, so spacing, line breaks, comments and the
case of keywords and names don't stop a match - only string literals and quoted names must be exact.
``frontend/src/changeNotes.ts`` finds them the same way when drawing, so the two must tokenize
alike.

Here the fragments are checked against the SQL they were quoted from. One that is found is replaced
by the exact text it matched, so what the panel searches for is text that really was in the SQL. One
that isn't found is dropped, so a note is never pinned to the wrong place: its summary and reason
are kept, unanchored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import request_context

# One token at a time, longest alternatives first. Whitespace and comments are skipped. Mirrors
# `changeNotes.ts`: change one, change the other.
_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<comment>--[^\n]*|/\*.*?(?:\*/|\Z))
  | (?P<string>'(?:[^'\\]|\\.|'')*(?:'|\Z))
  | (?P<quoted>"(?:[^"]|"")*(?:"|\Z)|`(?:[^`]|``)*(?:`|\Z)|\[[^\]\n]*\])
  | (?P<number>(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+))
  | (?P<word>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<operator><>|!=|<=|>=|\|\||::|->>|->|[=<>+\-*/%^~!|&])
  | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)


@dataclass(frozen=True)
class Token:
    #: What two tokens are compared by: the text, lower-cased unless it is a string or quoted name.
    key: str
    start: int
    end: int


def tokens(sql: str) -> list[Token]:
    """`sql`'s tokens, with where each is in it - whitespace and comments left out."""
    out = []
    for match in _TOKEN.finditer(sql):
        kind = match.lastgroup
        if kind in ("space", "comment"):
            continue
        text = match.group()
        out.append(Token(text if kind in ("string", "quoted") else text.lower(), match.start(), match.end()))
    return out


def locate(fragment: str, sql: str) -> Optional[tuple[int, int]]:
    """Where `fragment` is in `sql`, as a character range (start, end): its first occurrence as
    tokens. None when it is empty or isn't there."""
    wanted = [token.key for token in tokens(fragment)]
    if not wanted:
        return None
    have = tokens(sql)
    keys = [token.key for token in have]
    for index in range(len(keys) - len(wanted) + 1):
        if keys[index:index + len(wanted)] == wanted:
            return have[index].start, have[index + len(wanted) - 1].end
    return None


@dataclass(frozen=True)
class Change:
    """One change Accelerate made: what, why, and the SQL either side of it."""

    summary: str
    why: str = ""
    #: The text of the original SQL it replaced - "" for an addition, or when it couldn't be found.
    before: str = ""
    #: The text of the rewrite that replaced it - "" for a removal, or when it couldn't be found.
    after: str = ""

    def to_dict(self) -> dict:
        return {"summary": self.summary, "why": self.why, "before": self.before, "after": self.after}


def parse_changes(raw: object) -> list[Change]:
    """The model's `changes`, as `Change`s. Takes the plain phrases of the older shape too, and
    leaves out entries with nothing to say."""
    out = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            change = Change(
                summary=_text(entry.get("summary")),
                why=_text(entry.get("why")),
                before=_text(entry.get("before")),
                after=_text(entry.get("after")),
            )
        else:
            change = Change(summary=_text(entry))
        if change.summary or change.why:
            out.append(change)
    return out


def anchor(changes: list[Change], original: str, rewritten: str) -> list[Change]:
    """`changes` with each fragment checked against the SQL it was quoted from: replaced by the exact
    text it matched, or dropped ("") when it isn't there."""
    return [
        Change(
            summary=change.summary,
            why=change.why,
            before=_found(change.before, original),
            after=_found(change.after, rewritten),
        )
        for change in changes
    ]


def anchored_count(changes: list[Change]) -> int:
    """How many changes can be placed in the SQL - for the call log, to see how well a model quotes."""
    return sum(1 for change in changes if change.before or change.after)


def note_on_request(changes: list[Change]) -> None:
    """Puts how many changes there were, and how many could be placed, on the request's log line."""
    context = request_context.current()
    if context is not None:
        context.attributes["changes"] = len(changes)
        context.attributes["changesAnchored"] = anchored_count(changes)


#: The JSON a response's `changes` field is shown as, in a prompt's response format.
CHANGES_SHAPE = '"changes": [{"summary": "...", "why": "...", "before": "...", "after": "..."}]'


def changes_instructions(new_field: str, original: str, why: str) -> str:
    """How to fill in `changes`, for a response whose field `new_field` holds a new version of the
    query described as `original` in the prompt. `why` is what each change's reason should say."""
    return (
        "- changes: each change you made, in the order it appears in the query - empty when nothing\n"
        "  changed:\n"
        '  - summary: what you changed, as a short phrase ("replaced the correlated subquery with a join").\n'
        f"  - why: {why}\n"
        f"  - before: the part of {original} the change replaced, copied from it exactly - enough to\n"
        '    find it, from a few words to a line or two, never the whole query. "" when you only added\n'
        "    something.\n"
        f"  - after: the part of {new_field} that replaced it, copied exactly in the same way. \"\" when you\n"
        "    only removed something.\n"
    )


def _found(fragment: str, sql: str) -> str:
    where = locate(fragment, sql) if fragment else None
    return sql[where[0]:where[1]] if where else ""


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""
