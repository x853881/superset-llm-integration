"""How the tables are picked for the SQL prompt when a database has more than fit - one of five
options, chosen per request so they can be compared on site without a release.

1. ``embedding``: the tables closest to the request by embedding, until the prompt is full (and no
   more than ``NLSQL_TABLE_SELECTION_TOP_N``, if set). The SQL call without reasoning.
2. ``embedding-reasoning``: the same, the SQL call with reasoning.
3. ``embedding-then-model``: the closest ``NLSQL_TABLE_SELECTION_SHORTLIST`` (40) go to a model call
   of their own, which picks the tables the request needs - the bridge and lookup tables too - and
   says how they join and why each is there. Those tables, and that advice, go to the SQL call,
   without reasoning.
4. ``embedding-then-model-reasoning``: the same, the SQL call with reasoning.
5. ``embedding-then-model-tools``: as 3, but before it answers the picking call may look further,
   ``NLSQL_TABLE_SELECTION_LOOKS`` (3) times at most: find tables by words, describe a table, list a
   table's joins - over the tables the user may see, nothing else. The SQL call stays one shot.

Without embeddings (none configured, or none made yet) "closest" is the tables sharing the most
words with the request. A picking call that fails, or picks nothing it was offered, falls back to
option 1's tables, so a request is never answered against no tables because the picking went wrong.

``NLSQL_TABLE_SELECTION`` is the site's default and ``NLSQL_TABLE_SELECTION_ALLOWED`` the options it
allows. A request may ask for one (``tableSelection``); it gets it only if it's allowed and the user
has one of the ``NLSQL_TABLE_SELECTION_ROLES``, and the default otherwise.

"With reasoning" merges ``NLSQL_LLM_REASONING_ON`` into the SQL call's request body, and "without"
``NLSQL_LLM_REASONING_OFF``. What turns a model's thinking on or off depends on the model service -
OpenRouter's is ``{"reasoning": {"enabled": True}}`` - so nothing is sent unless they're set.

The picking call and the parsing of its answer started on the two-steps branch.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from flask import current_app

from . import derived_columns, llm_client, request_context, schema_knowledge, table_embeddings, table_ranking
from .cancellation import CancelledError, CancelToken
from .llm_provider import LlmProvider, resolve_llm_provider
from .semantic_schema import TableDescription

logger = logging.getLogger(__name__)

DEFAULT_SHORTLIST = 40
DEFAULT_MAX = 12
DEFAULT_MAX_CHARS = 60_000
DEFAULT_LOOKS = 3
DEFAULT_ROLES = ("Admin",)
#: Tables a "find" look returns.
FIND_LIMIT = 10


@dataclass(frozen=True)
class Option:
    name: str
    #: How the shortlist is cut down: None for not at all, "model" for a picking call, "tools" for a
    #: picking call that may look further first.
    picker: Optional[str]
    #: Whether the SQL call reasons.
    reasoning: bool
    label: str


OPTIONS: dict[str, Option] = {
    option.name: option
    for option in (
        Option("embedding", None, False, "Closest by embedding"),
        Option("embedding-reasoning", None, True, "Closest by embedding, SQL with reasoning"),
        Option("embedding-then-model", "model", False, "Closest, then a model picks"),
        Option("embedding-then-model-reasoning", "model", True, "Closest, then a model picks; SQL with reasoning"),
        Option("embedding-then-model-tools", "tools", False, "Closest, then a model looks and picks"),
    )
}
DEFAULT_OPTION = "embedding"


def _config(name: str, default: Any) -> Any:
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _default() -> str:
    name = str(_config("NLSQL_TABLE_SELECTION", DEFAULT_OPTION))
    return name if name in OPTIONS else DEFAULT_OPTION


def allowed() -> list[str]:
    names = _config("NLSQL_TABLE_SELECTION_ALLOWED", None) or [_default()]
    out = [str(name) for name in names if str(name) in OPTIONS]
    return out if _default() in out else [_default(), *out]


def may_choose() -> bool:
    """Whether the current user has one of the roles that may choose an option."""
    roles = {str(role).lower() for role in _config("NLSQL_TABLE_SELECTION_ROLES", DEFAULT_ROLES) or []}
    try:
        from flask import g

        user = getattr(g, "user", None)
    except Exception:  # noqa: BLE001 - no request
        return False
    return any(str(getattr(role, "name", role)).lower() in roles for role in getattr(user, "roles", None) or [])


def choice() -> Option:
    """The option for the current request: the one it asked for if that's allowed and its user may
    choose, else the site's default."""
    context = request_context.current()
    requested = context.table_selection if context is not None else None
    if requested and requested in allowed() and may_choose():
        return OPTIONS[requested]
    return OPTIONS[_default()]


def for_panel() -> dict[str, Any]:
    """What the panel needs to offer the choice: the options, the default, and whether this user may."""
    return {
        "default": _default(),
        "options": [{"name": name, "label": OPTIONS[name].label} for name in allowed()],
        "canChoose": may_choose() and len(allowed()) > 1,
    }


def top_n() -> Optional[int]:
    value = int(_config("NLSQL_TABLE_SELECTION_TOP_N", 0) or 0)
    return value if value > 0 else None


def shortlist_size() -> int:
    return int(_config("NLSQL_TABLE_SELECTION_SHORTLIST", DEFAULT_SHORTLIST))


def with_reasoning(provider: LlmProvider, reasoning: Optional[bool]) -> LlmProvider:
    """`provider` with the request-body fields that turn reasoning on or off, as configured."""
    if reasoning is None:
        return provider
    extra = _config("NLSQL_LLM_REASONING_ON" if reasoning else "NLSQL_LLM_REASONING_OFF", None)
    if not extra:
        return provider
    return dataclasses.replace(provider, extra_body={**provider.extra_body, **dict(extra)})


def closest(knowledge: schema_knowledge.DatabaseKnowledge, query_text: str, visible: set) -> tuple[str, list[tuple[str, str]]]:
    """Every table of `knowledge` in `visible`, closest to `query_text` first, as the prompt ranks
    them when they don't all fit - by embedding when it can, else by words - and which it was."""
    policy = derived_columns.Policy.from_config()
    tables = [derived_columns.apply(table, policy) for table in knowledge.visible_tables(visible)]
    keys = [(table.schema, table.name) for table in tables]
    order = table_embeddings.ranking(knowledge, query_text)
    if order is not None:
        wanted = set(keys)
        ranked = [key for key in order if key in wanted]
        seen = set(ranked)
        return table_ranking.EMBEDDING, ranked + [key for key in keys if key not in seen]
    return table_ranking.WORDS, [keys[index] for index in schema_knowledge.word_order(tables, query_text)]


# --- Picking --------------------------------------------------------------------------------------


@dataclass
class Join:
    left: str
    right: str


@dataclass
class Picked:
    """What the picking call chose: the tables (in the order offered), why each, and how they join."""

    tables: list[TableDescription] = field(default_factory=list)
    why: dict[str, str] = field(default_factory=dict)
    joins: list[Join] = field(default_factory=list)
    reason: Optional[str] = None
    error: Optional[str] = None
    looks: int = 0

    def advice(self, offered: int) -> str:
        """The advice for the SQL prompt: which tables were picked and why, and the joins."""
        lines = [f"A model picked these tables for this request, from the {offered} closest to it:"]
        for table in self.tables:
            name = f"{table.schema}.{table.name}"
            lines.append(f"- {name}" + (f": {self.why[name.lower()]}" if self.why.get(name.lower()) else ""))
        if self.joins:
            lines.append("Join them on:")
            lines.extend(f"- {join.left} = {join.right}" for join in self.joins)
        lines.append("If the request needs a table that isn't listed, say so rather than guessing at its name.")
        return "\n".join(lines)


def pick(
    option: Option,
    request_text: str,
    shortlist: list[TableDescription],
    visible: list[TableDescription],
    cancel_token: Optional[CancelToken] = None,
) -> Picked:
    """The tables of `shortlist` the model says `request_text` needs - with `option.picker` "tools",
    after looking further among `visible` if it wants to. Records how it went on the request
    (``tableSelection``)."""
    started = time.monotonic()
    most = int(_config("NLSQL_TABLE_SELECTION_MAX", DEFAULT_MAX))
    max_chars = int(_config("NLSQL_TABLE_SELECTION_MAX_CHARS", DEFAULT_MAX_CHARS))
    looks_left = int(_config("NLSQL_TABLE_SELECTION_LOOKS", DEFAULT_LOOKS)) if option.picker == "tools" else 0
    candidates = list(shortlist)
    found: list[str] = []
    picked = Picked()
    try:
        provider = resolve_llm_provider()
        while True:
            prompt = selection_prompt(request_text, candidates, most, max_chars, found, looks_left)
            text = llm_client.complete(provider, prompt, cancel_token, purpose="select_tables", attempt=picked.looks + 1)
            payload = _payload(text)
            wanted = payload.get("look")
            if looks_left > 0 and isinstance(wanted, list) and wanted and not payload.get("tables"):
                picked.looks += 1
                looks_left -= 1
                found.append(_look(wanted, candidates, visible))
                continue
            picked = dataclasses.replace(parse_selection(payload, candidates, most), looks=picked.looks)
            break
    except CancelledError:
        raise
    except Exception as ex:  # noqa: BLE001 - picking is an aid: without it, the prompt is what it was
        logger.info("table selection failed: %s", ex)
        picked = Picked(error=str(ex)[:300], looks=picked.looks)
    context = request_context.current()
    if context is not None:
        context.attributes["tableSelection"] = {
            "option": option.name,
            "offered": len(shortlist),
            "picked": len(picked.tables),
            "looks": picked.looks,
            "joins": len(picked.joins),
            "fallback": not picked.tables,
            "ms": int((time.monotonic() - started) * 1000),
        }
    return picked


def selection_prompt(
    request_text: str,
    candidates: list[TableDescription],
    most: int,
    max_chars: int,
    found: Optional[list[str]] = None,
    looks_left: int = 0,
) -> str:
    lines = [_line(table, columns=True) for table in candidates]
    note = ""
    if sum(len(line) + 1 for line in lines) > max_chars:
        lines = [_line(table, columns=False) for table in candidates]
        note = "(Columns are left out - there are too many tables to list them.)\n"
    from .nlsql_backend import dialect_note

    looked = "".join(f"\nWhat you looked up:\n{text}\n" for text in found or [])
    looking = (
        f"\nBefore answering you may look further, {looks_left} more time{'s' if looks_left != 1 else ''}, by "
        "responding with ONLY\n"
        '{"look": [{"find": "words to search table names, descriptions and columns for"}, '
        '{"describe": "schema.table"}, {"joins": "schema.table"}]}\n'
        "(one to three of them). find lists other tables matching the words; describe gives a table's "
        "columns and types; joins lists the tables a table joins to. Only look when the tables above "
        "aren't enough.\n"
        if looks_left > 0 else ""
    )
    return (
        f"{dialect_note()}"
        "Someone wants to query a database. Before any SQL is written, decide which of its tables the\n"
        "query will need. These are the tables closest to the request, one per line: schema.table,\n"
        "what it holds, its columns, and its keys (col->schema.table.col).\n"
        f"{note}\n"
        + "\n".join(lines)
        + "\n"
        + looked
        + "\nThe request (or the query to work from):\n"
        f"{request_text}\n"
        + looking
        + "\nRespond with ONLY a single JSON object (no markdown fences, no other text):\n"
        '{"tables": [{"table": "schema.table", "why": "..."}], '
        '"joins": [{"left": "schema.table.column", "right": "schema.table.column"}], "reason": "..."}\n\n'
        f"- tables: every table the query needs, at most {most}, written exactly as listed, each with one\n"
        "  short line on why. Include the tables a join has to go through to connect them, and lookup\n"
        "  tables needed to decode or name codes - not just the ones the request mentions.\n"
        "- joins: the column pairs the query should join the tables on.\n"
        "- Leave out tables that only look similar: an archive or staging copy, a summary of a table\n"
        "  you're already using, unless the request is about it.\n"
        "- If none of them holds what's asked for, return an empty list and say why in reason.\n"
        "- reason: one sentence on the choice.\n"
    )


def _payload(text: str) -> dict[str, Any]:
    from .nlsql_backend import extract_json

    payload = json.loads(extract_json(text))
    if not isinstance(payload, dict):
        raise ValueError("the response wasn't a JSON object")
    return payload


def parse_selection(payload: dict[str, Any] | str, candidates: list[TableDescription], most: int) -> Picked:
    """The picking call's answer, held to `candidates`: a table it names that wasn't offered, or a
    join on a column a picked table hasn't got, is dropped."""
    if isinstance(payload, str):
        payload = _payload(payload)
    if not isinstance(payload.get("tables"), list):
        raise ValueError("the response had no list of tables")
    by_name: dict[str, TableDescription] = {}
    bare: dict[str, list[TableDescription]] = {}
    for table in candidates:
        by_name[f"{table.schema}.{table.name}".lower()] = table
        bare.setdefault(table.name.lower(), []).append(table)

    def resolve(name: Any) -> Optional[TableDescription]:
        key = re.sub(r'["`\[\]]', "", str(name)).strip().lower()
        # A table named without its schema counts only when just one schema has it.
        return by_name.get(key) or (bare[key][0] if len(bare.get(key, [])) == 1 else None)

    picked: dict[int, str] = {}
    for entry in payload["tables"]:
        name, why = (entry.get("table"), entry.get("why")) if isinstance(entry, dict) else (entry, None)
        table = resolve(name)
        if table is not None:
            picked.setdefault(id(table), str(why or "").strip())
    tables = [table for table in candidates if id(table) in picked][:most]
    why = {f"{table.schema}.{table.name}".lower(): picked[id(table)] for table in tables if picked[id(table)]}
    chosen = {f"{table.schema}.{table.name}".lower(): table for table in tables}
    joins = []
    for entry in payload.get("joins") or []:
        if not isinstance(entry, dict):
            continue
        sides = [_column(entry.get(side), chosen) for side in ("left", "right")]
        if all(sides):
            joins.append(Join(*sides))  # type: ignore[arg-type]
    reason = str(payload.get("reason") or "").strip() or None
    if not tables:
        return Picked(reason=reason, error=reason or "no table named was one of those offered")
    return Picked(tables=tables, why=why, joins=joins, reason=reason)


def _column(text: Any, chosen: dict[str, TableDescription]) -> Optional[str]:
    """`schema.table.column`, written as the table and column are, if that table was picked and has
    that column."""
    parts = re.sub(r'["`\[\]]', "", str(text or "")).strip().split(".")
    if len(parts) < 3:
        return None
    table = chosen.get(".".join(parts[:-1]).lower())
    if table is None:
        return None
    column = next((column.name for column in table.columns if column.name.lower() == parts[-1].lower()), None)
    return f"{table.schema}.{table.name}.{column}" if column else None


def _look(wanted: list[Any], candidates: list[TableDescription], visible: list[TableDescription]) -> str:
    """Answers up to three looks, over `visible` only. Tables a look turns up become candidates."""
    by_name = {f"{table.schema}.{table.name}".lower(): table for table in visible}
    out: list[str] = []
    for entry in wanted[:3]:
        if not isinstance(entry, dict):
            continue
        if entry.get("find"):
            words = schema_knowledge._words(str(entry["find"]))
            offered = {id(table) for table in candidates}
            scored = sorted(
                (table for table in visible if id(table) not in offered),
                key=lambda table: -len(words & schema_knowledge._words(_line(table, columns=True))),
            )
            matches = [table for table in scored if words & schema_knowledge._words(_line(table, columns=True))][:FIND_LIMIT]
            candidates.extend(matches)
            out.append(f"find {entry['find']!r}:\n" + ("\n".join(_line(table, columns=True) for table in matches) or "(nothing)"))
        elif entry.get("describe"):
            table = by_name.get(str(entry["describe"]).strip().lower())
            if table is not None and table not in candidates:
                candidates.append(table)
            out.append(f"describe {entry['describe']}:\n" + (schema_knowledge._structure_text(table) if table else "(no such table)"))
        elif entry.get("joins"):
            name = str(entry["joins"]).strip().lower()
            table = by_name.get(name)
            if table is None:
                out.append(f"joins {entry['joins']}:\n(no such table)")
                continue
            lines = [f"{table.schema}.{table.name}.{r.from_column} -> {r.to_table}.{r.to_column}" for r in table.relationships]
            lines += [
                f"{other.schema}.{other.name}.{r.from_column} -> {r.to_table}.{r.to_column}"
                for other in visible if other is not table
                for r in other.relationships if r.to_table.lower() == name
            ]
            out.append(f"joins {entry['joins']}:\n" + ("\n".join(lines) or "(none known)"))
    return "\n".join(out) or "(nothing to look up)"


def _line(table: TableDescription, columns: bool) -> str:
    line = f"{table.schema}.{table.name}"
    if table.kind == "view":
        line += " (view)"
    if table.description:
        line += f" -- {table.description}"
    if columns:
        keys = {relationship.from_column: f"{relationship.to_table}.{relationship.to_column}" for relationship in table.relationships}
        line += ": " + ", ".join(
            column.name + (f"->{keys[column.name]}" if column.name in keys else "") for column in table.columns
        )
    return line
