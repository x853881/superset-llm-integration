"""How the model writes the prose it hands back - descriptions, explanations, summaries - as opposed
to the SQL: how long it is, and which of its terms are marked up for the panel to explain.

**Brief.** The panel's "Brief" checkbox asks for the short form of everything it shows. The model
is told which form to write rather than asked for both and one thrown away: what a call costs in
time is mostly the text it writes, so writing both would make every call slower - brief ones
included, which is the opposite of what asking for brief is for. The cost is that switching the
checkbox only changes what is written next; what is already on screen stays as it was written.
Sent with every call (``verbosity``) and carried on the `request_context.CallContext`, so no
pipeline has to pass it down.

**Terms.** The model marks the few terms in its prose a SQL user may not know - complexity
notation, engine internals, statistics - as ``[[term|meaning]]``, and the panel draws each one
highlighted with its meaning on hover. Inline, rather than a separate glossary the panel would have
to find the terms of, because the model knows which occurrence it meant and the markup streams with
the text around it. Only in the fields named per prompt: never in SQL, nor in text that goes back
into the editor or the question box (a description, a refinement suggestion). ``strip_terms``
removes the markup from anything that comes back as input. ``NLSQL_MARK_TERMS`` (on by default)
turns it off.
"""

from __future__ import annotations

import re
from typing import Optional

from flask import current_app

from . import request_context

BRIEF = "brief"
FULL = "full"

#: The most terms one response is asked to mark - an explanation that is mostly highlighting is
#: harder to read, not easier.
MAX_TERMS = 3

_TERM = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]*)\]\]")


def verbosity(value: object) -> str:
    """`value` from a request body as one of `BRIEF` / `FULL` - full for anything else."""
    return BRIEF if str(value or "").strip().lower() == BRIEF else FULL


def is_brief() -> bool:
    """Whether the current request asked for brief prose."""
    context = request_context.current()
    return context is not None and context.verbosity == BRIEF


def marks_terms() -> bool:
    try:
        return bool(current_app.config.get("NLSQL_MARK_TERMS", True))
    except RuntimeError:
        return True


def instructions(fields: str, brief_lengths: Optional[str] = None) -> str:
    """The lines telling the model how to write the prose in `fields` (as the prompt names them,
    "the summary and the explanation"): short, when the request asked for brief - `brief_lengths`
    replacing the generic advice where a prompt has something more specific to say - and with its
    hard terms marked. Empty when neither applies."""
    parts = []
    if is_brief():
        parts.append(
            "The user has asked for brief explanations. Keep "
            f"{fields} as short as they can be while still saying what matters: "
            f"{brief_lengths or 'one short sentence each, or a phrase where a phrase will do'}. "
            "This overrides any longer lengths suggested elsewhere in these instructions. Leave out "
            "background, restatements of the SQL and hedging; keep warnings that matter."
        )
    if marks_terms():
        parts.append(
            f"In {fields} only, mark up to {MAX_TERMS} terms a typical SQL user may not know - "
            "complexity notation (\"O(n log n)\"), engine internals (\"hash join\", \"sequential "
            "scan\", \"predicate pushdown\"), statistical or domain jargon - as [[term|meaning]], "
            "where meaning is a plain one-sentence explanation of the term in this context, for "
            "example: \"Sorting makes this [[O(n log n)|The work grows a little faster than the "
            "number of rows: doubling the rows slightly more than doubles the time.]] overall.\" "
            "Mark a term once, where it first appears. Never mark ordinary SQL keywords or "
            "functions, table or column names, or anything inside SQL. Mark nothing when nothing "
            "needs it."
        )
    return "\n".join(parts) + "\n" if parts else ""


def strip_terms(text: str) -> str:
    """`text` with its ``[[term|meaning]]`` markup reduced to the term alone."""
    return _TERM.sub(lambda match: match.group(1), text)
