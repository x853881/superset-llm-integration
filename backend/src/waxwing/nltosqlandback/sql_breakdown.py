"""A query described section by section, each section tied to the lines of SQL it covers.

The holistic description (``nlsql_backend.generate_nl``) says what a query is *for* in a sentence
or two. This says how it gets there: an overview, then one entry per part of the query - each CTE,
the sources and joins, the filters, the grouping, the ordering - with the line range it occupies,
so the panel can draw the SQL with each section marked and highlight one when its description is
hovered (and select it in the real editor when clicked).

Line ranges come from the model, against a line-numbered copy of the query - the same trick the
error-fix prompt uses, because a model asked to count lines in an unnumbered query does it badly.
They're then clamped to the query and sorted, and sections whose range makes no sense are dropped;
a section the model mis-numbers by a line is a highlight that's slightly off, not a broken panel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from flask import current_app

from . import nlsql_backend, progress, prose, query_tables, sql_checks, sql_format
from .cancellation import CancelToken
from .progress import EventStream
from .semantic_schema import TableDescription
from .streaming_json import complete_array_items

SUMMARY_FIELD = "summary"
SECTION_KINDS = ("cte", "select", "source", "join", "filter", "aggregate", "window", "order", "limit", "subquery", "other")


@dataclass
class Section:
    title: str
    description: str
    start_line: int
    end_line: int
    kind: str = "other"
    #: The longer explanation, shown when the reader asks for it.
    detail: str = ""
    #: The stored tables this section reads ({schema, name, kind, rows}) - from Interrogate DB, not
    #: the model; empty when the database hasn't been interrogated.
    tables: list[dict[str, Any]] = field(default_factory=list)
    #: Things worth knowing about its joins and filters (see `query_tables.section_facts`).
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "detail": self.detail,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "kind": self.kind,
            "tables": self.tables,
            "hints": self.hints,
        }


def describe_breakdown(sql: str, cancel_token: Optional[CancelToken] = None, format_sql: bool = False) -> EventStream:
    """The breakdown of `sql` - or, with `format_sql`, of `sql` laid out one clause per line (see
    `sql_format`), which is then the `sql` the result carries and its line numbers refer to.

    Sent as it's decided, not only at the end: the SQL the sections refer to as soon as it's known
    (a `partial` "sql" event), and the sections themselves one at a time as the model finishes
    each (`partial` "sections", the whole list so far each time) - so the panel can draw them as
    they arrive. The prompt carries only the tables the query reads (see
    `nlsql_backend.query_schema_preamble`), and each section comes back with those tables' sizes
    and any hints about its joins and filters (see `query_tables.section_facts`)."""
    steps = progress.Steps(nlsql_backend.with_tables_step([
        ("read", "Read the SQL", "Read the SQL"),
        nlsql_backend.TABLES_STEP,
        ("draft", "Breaking the query down", "Broke the query down"),
    ]))
    yield steps.plan_event
    database = sql_checks.current_database()
    dialect = database.dialect if database else None
    formatted = (
        sql_format.format_sql(sql, dialect, database.backend if database else None)
        if format_sql
        else None
    )
    if formatted is not None and formatted.strip() == sql.strip():
        formatted = None  # already laid out: the editor's own SQL, so its sections can be selected there
    if formatted is not None:
        sql = formatted
    line_count = len(sql.splitlines())
    yield steps.done(
        "read",
        f"{line_count} lines from the editor" + (", laid out one clause per line" if formatted is not None else ""),
    )
    yield progress.partial("sql", {"sql": sql, "formatted": formatted is not None})
    tables = _tables_read(sql)
    preamble = yield from nlsql_backend.query_schema_preamble(sql, steps)
    prompt = _prompt(sql, preamble)
    streamed = _StreamedSections(sql, dialect, line_count, tables)
    raw = yield from nlsql_backend.stream_json_fields(
        prompt, steps, [SUMMARY_FIELD], cancel_token, on_text=streamed.events
    )
    payload = json.loads(nlsql_backend.extract_json(raw))
    sections = with_facts(normalise_sections(payload.get("sections"), line_count), sql, dialect, tables)
    return {
        "summary": str(payload.get(SUMMARY_FIELD) or "").strip(),
        "sections": [s.to_dict() for s in sections],
        "sql": sql,
        "formatted": formatted is not None,
        # Brief breakdowns have no detailed explanations (see `_detail_instructions`).
        "brief": prose.is_brief(),
    }


class _StreamedSections:
    """Turns the response so far into a `partial` "sections" event whenever another section has
    been written in full."""

    def __init__(self, sql: str, dialect: Optional[str], line_count: int, tables: list[TableDescription]) -> None:
        self._sql = sql
        self._dialect = dialect
        self._line_count = line_count
        self._tables = tables
        self._sent = 0

    def events(self, buffer: str) -> list[dict[str, Any]]:
        items = complete_array_items(buffer, "sections")
        if len(items) <= self._sent:
            return []
        self._sent = len(items)
        sections = with_facts(normalise_sections(items, self._line_count), self._sql, self._dialect, self._tables)
        return [progress.partial("sections", [section.to_dict() for section in sections])]


def _tables_read(sql: str) -> list[TableDescription]:
    """The interrogated tables `sql` reads, for the facts on each section; [] when the database
    hasn't been interrogated."""
    knowledge = nlsql_backend.interrogated_schema()
    if knowledge is None:
        return []
    try:
        return nlsql_backend.query_tables_in_knowledge(sql, knowledge)
    except Exception:  # noqa: BLE001 - facts are extras
        return []


def with_facts(
    sections: list[Section], sql: str, dialect: Optional[str], tables: list[TableDescription]
) -> list[Section]:
    """`sections` with each one's tables and hints filled in (see `query_tables.section_facts`)."""
    if not sections or not tables:
        return sections
    try:
        large = int(current_app.config.get("NLSQL_BREAKDOWN_LARGE_TABLE_ROWS", query_tables.DEFAULT_LARGE_TABLE_ROWS))
    except RuntimeError:
        large = query_tables.DEFAULT_LARGE_TABLE_ROWS
    facts = query_tables.section_facts(
        sql, dialect, [(section.start_line, section.end_line) for section in sections], tables, large
    )
    for section, fact in zip(sections, facts):
        section.tables = fact["tables"]
        section.hints = fact["hints"]
    return sections


def normalise_sections(raw: Any, line_count: int) -> list[Section]:
    """The model's sections, made safe to draw: line numbers clamped to the query, start <= end,
    unknown kinds as "other", nonsense dropped, sorted by where they start."""
    sections: list[Section] = []
    if not isinstance(raw, list) or line_count <= 0:
        return sections
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(entry.get("startLine"))
            end = int(entry.get("endLine", start))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        if end < 1 or start > line_count:
            continue
        start, end = max(1, start), min(line_count, end)
        kind = str(entry.get("kind") or "other").lower()
        title = str(entry.get("title") or "").strip()
        description = str(entry.get("description") or "").strip()
        if not title and not description:
            continue
        sections.append(Section(
            title=title or kind.title(),
            description=description,
            start_line=start,
            end_line=end,
            kind=kind if kind in SECTION_KINDS else "other",
            detail=str(entry.get("detail") or "").strip(),
        ))
    sections.sort(key=lambda section: (section.start_line, -section.end_line))
    return sections


def _prompt(sql: str, schema_preamble_text: str) -> str:
    numbered = "\n".join(f"{number:>4} | {line}" for number, line in enumerate(sql.splitlines(), start=1))
    kinds = ", ".join(f'"{kind}"' for kind in SECTION_KINDS)
    return (
        f"{schema_preamble_text}"
        "Explain the following SQL query to someone who knows SQL but didn't write it, section by section.\n\n"
        "The query, with line numbers:\n"
        f"{numbered}\n\n"
        "Respond with ONLY a single JSON object (no markdown fences, no other text) with this shape:\n"
        "{\n"
        '  "summary": "...",\n'
        '  "sections": [{"title": "...", "kind": "...", "startLine": 1, "endLine": 3, "description": "...", "detail": "..."}]\n'
        "}\n\n"
        "- summary: the query's intent - what it produces and how, in two or three sentences (\"Finds the number\n"
        "  of active users per team. It builds a set of recent users, keeps the active ones, counts them per\n"
        "  team and returns the teams with the most first.\").\n"
        "- sections: the parts of the query in the order they appear, each with the line numbers (as shown\n"
        "  above) it starts and ends on. A query with CTEs: one section per CTE and one for the final query.\n"
        "  One without: its main clauses - the columns, the sources and joins, the filters, grouping, window\n"
        "  functions, ordering and limit - merging any that are only a line or two. Sections should not\n"
        "  overlap and together should cover the query. A short query can have just two or three sections.\n"
        f"- kind: one of {kinds}.\n"
        "- title: a few words. A CTE's own name followed by \"(CTE)\" (\"recent_users (CTE)\"); \"Final query\" for\n"
        "  the outer query of one with CTEs; otherwise what the part does (\"Only shipped orders\").\n"
        "- description: what that part does, in one sentence - the meaning, not a restatement of the SQL\n"
        "  (\"Keeps only orders that have shipped\", not \"WHERE status = 'Shipped'\").\n"
        f"{_detail_instructions()}"
        f"{prose.instructions('summary, description and detail', 'summary in one sentence, each description a short phrase')}"
    )


def _detail_instructions() -> str:
    """The longer explanation - left out when brief was asked for, since it's the bulk of what the
    model would write, and the panel only shows it on request anyway."""
    if prose.is_brief():
        return '- detail: leave it empty ("") - the user has asked for brief explanations.\n'
    return (
        "- detail: a fuller explanation in two to four sentences: how it does it - the tables and columns it\n"
        "  reads, the conditions, what one row of its output is - and anything surprising (a join that can\n"
        "  duplicate rows, a filter that drops NULLs, an implicit type conversion).\n"
    )
