"""The plugin's LLM endpoints: how to call each one for a golden, and how to
read its answer.

A golden picks its endpoint with additional_metadata.endpoint. Without one, a
golden with previous_sql goes to update-sql-from-nl and anything else to
generate-sql. Each endpoint's other inputs also live in additional_metadata:

    endpoint                 golden.input       additional_metadata
    generate-sql             question           -
    update-sql-from-nl       new question       previous_input, previous_sql
    refine-sql               instruction        sql
    explain-sql-error        broken SQL         errors [{message, line?, column?}], dialect?
    accelerate               SQL to speed up    -
    generate-nl              SQL                -
    update-nl-from-sql-diff  new SQL            previous_input (old description), previous_sql
    explain-plan             SQL                -
    describe-breakdown       SQL                -

Endpoints that change what the plugin has stored (interrogate-db,
db-knowledge/forget, feedback) are deliberately left out, as are the ones
that don't involve the LLM (validate-sql, format-sql, query-plan, cancel).
"""

from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

SQL = "sql"
TEXT = "text"


@dataclass(frozen=True)
class Endpoint:
    name: str
    # SQL or TEXT: what the plugin answers with.
    output: str
    # Endpoint-specific request fields, from the golden's input and metadata.
    payload: Callable[[str, Mapping[str, Any]], Dict[str, Any]]
    # The answer, from the `result` event's payload.
    extract: Callable[[Mapping[str, Any]], Optional[str]]
    # What the judge is shown as the request.
    describe: Callable[[str, Mapping[str, Any]], str]
    required_metadata: Tuple[str, ...] = ()
    # TEXT answers: whether the plugin may mark terms as [[term|meaning]] for
    # the panel's hover. Descriptions go into the question box, so never.
    marks_terms: bool = True

    @property
    def path(self) -> str:
        return f"/{self.name}"


def _field(name: str) -> Callable[[Mapping[str, Any]], Optional[str]]:
    def extract(result: Mapping[str, Any]) -> Optional[str]:
        value = result.get(name)
        if not isinstance(value, str):
            return None
        return value.strip() or None

    return extract


def _breakdown(result: Mapping[str, Any]) -> Optional[str]:
    # Each section's detail is included: it is where the plugin is told to put
    # the conditions and anything surprising, so the judge needs it.
    lines = [str(result.get("summary") or "").strip()]
    for s in result.get("sections") or []:
        lines.append(
            f"- Lines {s.get('startLine')}-{s.get('endLine')} {s.get('title', '')}: "
            f"{s.get('description', '')}"
        )
        detail = str(s.get("detail") or "").strip()
        if detail:
            lines.append(f"  Detail: {detail}")
    text = "\n".join(line for line in lines if line)
    return text or None


def _errors(meta: Mapping[str, Any]) -> str:
    out = []
    for e in meta.get("errors", []):
        where = f" (line {e['line']})" if e.get("line") else ""
        out.append(f"- {e.get('message', '')}{where}")
    return "\n".join(out)


ENDPOINTS: Dict[str, Endpoint] = {
    e.name: e
    for e in (
        Endpoint(
            name="generate-sql",
            output=SQL,
            payload=lambda text, meta: {"naturalLanguage": text},
            extract=_field("sqlText"),
            describe=lambda text, meta: text,
        ),
        Endpoint(
            name="update-sql-from-nl",
            output=SQL,
            payload=lambda text, meta: {
                "previousNl": meta.get("previous_input", ""),
                "previousSql": meta.get("previous_sql", ""),
                "newNl": text,
            },
            extract=_field("sqlText"),
            describe=lambda text, meta: (
                f"Previous question: {meta.get('previous_input', '')}\n"
                f"Previous SQL:\n{meta.get('previous_sql', '')}\n\n"
                f"New request: {text}"
            ),
        ),
        Endpoint(
            name="refine-sql",
            output=SQL,
            payload=lambda text, meta: {"command": text, "sql": meta["sql"]},
            extract=_field("sqlText"),
            describe=lambda text, meta: (
                f"Current SQL:\n{meta['sql']}\n\nInstruction: {text}"
            ),
            required_metadata=("sql",),
        ),
        Endpoint(
            name="explain-sql-error",
            output=SQL,
            payload=lambda text, meta: {
                "sql": text,
                "errors": meta["errors"],
                "dialect": meta.get("dialect", ""),
            },
            extract=_field("fixedSql"),
            describe=lambda text, meta: (
                f"Fix this SQL:\n{text}\n\nErrors reported:\n{_errors(meta)}"
            ),
            required_metadata=("errors",),
        ),
        Endpoint(
            name="accelerate",
            output=SQL,
            payload=lambda text, meta: {"sql": text, "runPlan": True},
            extract=_field("rewrittenSql"),
            describe=lambda text, meta: (
                f"Rewrite this SQL to run faster, returning exactly the same result:\n{text}"
            ),
        ),
        Endpoint(
            name="generate-nl",
            output=TEXT,
            payload=lambda text, meta: {"sql": text},
            extract=_field("naturalLanguage"),
            describe=lambda text, meta: f"Describe this SQL in plain English:\n{text}",
            marks_terms=False,
        ),
        Endpoint(
            name="update-nl-from-sql-diff",
            output=TEXT,
            payload=lambda text, meta: {
                "previousNl": meta.get("previous_input", ""),
                "previousSql": meta["previous_sql"],
                "newSql": text,
            },
            extract=_field("naturalLanguage"),
            describe=lambda text, meta: (
                f"Previous description: {meta.get('previous_input', '')}\n"
                f"Previous SQL:\n{meta['previous_sql']}\n\n"
                f"New SQL:\n{text}\n\n"
                "Update the description so it matches the new SQL."
            ),
            required_metadata=("previous_sql",),
            marks_terms=False,
        ),
        Endpoint(
            name="explain-plan",
            output=TEXT,
            payload=lambda text, meta: {"sql": text},
            extract=_field("explanation"),
            describe=lambda text, meta: (
                f"Explain the database's plan for this SQL in plain English:\n{text}"
            ),
        ),
        Endpoint(
            name="describe-breakdown",
            output=TEXT,
            payload=lambda text, meta: {"sql": text},
            extract=_breakdown,
            describe=lambda text, meta: (
                f"Describe this SQL section by section:\n{text}"
            ),
        ),
    )
}


def parse_latency_budgets(text: str) -> Dict[str, float]:
    """Per-endpoint time limits from "accelerate=60, describe-breakdown=45"."""
    budgets: Dict[str, float] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        name, _, seconds = item.partition("=")
        name = name.strip()
        if name not in ENDPOINTS:
            raise ValueError(
                f"PLUGIN_LATENCY_BUDGETS names unknown endpoint {name!r}; "
                f"expected one of {', '.join(ENDPOINTS)}"
            )
        try:
            budgets[name] = float(seconds)
        except ValueError:
            raise ValueError(
                f"PLUGIN_LATENCY_BUDGETS: {item!r} needs seconds, e.g. {name}=60"
            ) from None
    return budgets


def endpoint_for(meta: Optional[Mapping[str, Any]]) -> Endpoint:
    meta = meta or {}
    name = meta.get("endpoint") or (
        "update-sql-from-nl" if meta.get("previous_sql") else "generate-sql"
    )
    if name not in ENDPOINTS:
        raise ValueError(
            f"Unknown endpoint {name!r}; expected one of {', '.join(ENDPOINTS)}"
        )
    endpoint = ENDPOINTS[name]
    missing = [k for k in endpoint.required_metadata if not meta.get(k)]
    if missing:
        raise ValueError(
            f"Goldens for {name} need additional_metadata: {', '.join(missing)}"
        )
    return endpoint
