"""A record of every LLM (and embedding) call, with who it was made for and what it cost.

Each call becomes one :class:`LlmCallRecord`, written to up to three places:

- **Superset's own log**, always (unless ``NLSQL_LLM_LOG_ENABLED = False``): one ``llm_call {json}``
  line per call on the ``waxwing.nltosqlandback.llm_calls`` logger, so it lands wherever the
  deployment already ships its logs.
- **A JSONL file**, when ``NLSQL_LLM_LOG_FILE`` names one: the same object, one per line, for
  anything that would rather read a file than scrape a log.
- **Arize Phoenix**, when ``NLSQL_PHOENIX_ENDPOINT`` is set: as OpenTelemetry spans using the
  OpenInference conventions Phoenix reads, so each API request shows up as one trace - a ``CHAIN``
  span for the request, named after its endpoint (``describe_breakdown``), with an ``LLM`` span per
  model call (``llm · describe_breakdown``) and an ``EMBEDDING``/``TOOL`` span for each schema lookup
  and each question put to the database (``db · explain``, ``db · validate``) under it. Sent from a background thread, batched, and never
  allowed to fail or slow the call it describes.

Every record carries the Superset user's id (and username) from ``request_context`` - the reason
this module exists rather than the single log line ``llm_client`` used to write.

**Feedback** from the panel (a thumbs up or down on a result, with an optional reason) goes the same
three ways (see :func:`record_feedback`): a ``feedback`` line logged at its own ``FEEDBACK`` level,
between INFO and WARNING, and a ``feedback`` span sent into the trace of the request whose result
it rates - a child of that request's span, so the prompt, the model's reply and the feedback on it
are one trace.

Field names follow the OpenTelemetry GenAI semantic conventions where one exists
(``gen_ai.usage.input_tokens`` and so on), as waxwing-sql's own LLM telemetry does, so the two can
be read side by side.

Config (all optional)::

    NLSQL_LLM_LOG_ENABLED = True           # the log line
    NLSQL_LLM_LOG_PROMPTS = False          # include prompt/response text in the log line and file
    NLSQL_LLM_LOG_FILE = "/app/superset_home/logs/llm-calls.jsonl"
    NLSQL_PHOENIX_ENDPOINT = "http://phoenix:6006"     # spans POSTed to <this>/v1/traces
    NLSQL_PHOENIX_PROJECT = "superset-sql-assistant"
    NLSQL_PHOENIX_API_KEY = "..."          # sent as "Authorization: Bearer ..." if set
    NLSQL_PHOENIX_HEADERS = {}             # any other headers the collector needs
    NLSQL_PHOENIX_INCLUDE_PROMPTS = True   # prompt/response text on the spans
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from . import otlp, request_context
from .request_context import CallContext

logger = logging.getLogger(__name__)
call_logger = logging.getLogger("waxwing.nltosqlandback.llm_calls")

#: The log level feedback lines are written at: its own name, so it can be filtered on like ERROR or
#: DEBUG, and above INFO so a deployment logging at INFO keeps it.
FEEDBACK = 25
logging.addLevelName(FEEDBACK, "FEEDBACK")

SERVICE_NAME = "superset-nltosqlandback"
DEFAULT_PHOENIX_PROJECT = "superset-sql-assistant"
#: Text attributes on a span are cut at this many characters - Phoenix is happy with more, but a
#: span is not where a 200KB plan belongs.
MAX_SPAN_TEXT = 60_000


@dataclass
class LlmCallRecord:
    """One call to a model, successful or not. Token counts are what the provider reported (None
    when it reported nothing - not every OpenAI-compatible server sends usage on a stream)."""

    operation: str  # "chat" or "embeddings"
    request_model: str
    base_url: str
    start_ns: int
    end_ns: int
    outcome: str  # "ok", "error" or "cancelled"
    purpose: Optional[str] = None  # which step of the pipeline made it: "draft", "retry", ...
    attempt: int = 1
    stream: bool = True
    response_model: Optional[str] = None
    response_id: Optional[str] = None
    finish_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    cached_input_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    time_to_first_token_ms: Optional[int] = None
    prompt_chars: int = 0
    completion_chars: int = 0
    reasoning_chars: int = 0
    error: Optional[str] = None
    prompt: Optional[str] = None
    response: Optional[str] = None
    call_id: str = field(default_factory=lambda: secrets.token_hex(8))

    @property
    def duration_ms(self) -> int:
        return int((self.end_ns - self.start_ns) / 1_000_000)


def now_ns() -> int:
    return time.time_ns()


def span_name_for_call(record: LlmCallRecord) -> str:
    """What a model call's span is called: ``llm · describe_breakdown`` (or ``embedding · ...``),
    so it can't be mistaken for the request span it sits under, which carries the endpoint's bare
    name. A retry says which attempt it is."""
    prefix = "embedding" if record.operation == "embeddings" else "llm"
    name = f"{prefix} · {record.purpose or record.operation}"
    return name + (f" (attempt {record.attempt})" if record.attempt > 1 else "")


def provider_name(base_url: str) -> str:
    """A short name for who served the call, from its base URL - what Phoenix shows as the provider."""
    host = (urlsplit(base_url).hostname or "").lower()
    for known in ("openrouter", "openai", "anthropic", "azure", "groq", "together", "mistral"):
        if known in host:
            return known
    if host in ("localhost", "127.0.0.1", "host.docker.internal") or "ollama" in host:
        return "ollama"
    return host or "unknown"


def record_llm_call(record: LlmCallRecord) -> None:
    """Writes `record` everywhere configured. Never raises - telemetry failing is not the call
    failing."""
    try:
        context = request_context.current()
        line = _log_object(record, context)
        _emit_log_line("llm_call", line)
        _send_span(_llm_span(record, context))
    except Exception:  # noqa: BLE001
        logger.debug("could not record an LLM call", exc_info=True)


def record_tool_call(
    name: str,
    start_ns: int,
    end_ns: int,
    input_text: str,
    output_text: str = "",
    error: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
) -> None:
    """A non-LLM step worth seeing in the trace - asking the database for a query plan, say. Logged
    as a ``tool_call`` line and sent as a ``TOOL`` span."""
    try:
        context = request_context.current()
        line = {
            "ts": _iso(start_ns),
            "tool": name,
            "durationMs": int((end_ns - start_ns) / 1_000_000),
            "outcome": "error" if error else "ok",
            "error": error,
            **(attributes or {}),
            **(context.log_fields() if context else {}),
        }
        _emit_log_line("tool_call", line)
        if context is None:
            return
        include_text = _config("NLSQL_PHOENIX_INCLUDE_PROMPTS", True)
        span_attributes: dict[str, Any] = {
            "openinference.span.kind": "TOOL",
            "tool.name": name,
            **_common_span_attributes(context),
            "metadata": json.dumps(attributes or {}, default=str),
        }
        if include_text:
            span_attributes["input.value"] = _clip(input_text)
            span_attributes["output.value"] = _clip(output_text)
        _send_span(otlp.Span(
            trace_id=context.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=context.span_id,
            name=name,
            start_ns=start_ns,
            end_ns=end_ns,
            kind=otlp.SPAN_KIND_CLIENT,
            attributes=span_attributes,
            status_code=otlp.STATUS_ERROR if error else otlp.STATUS_OK,
            status_message=error or "",
        ))
    except Exception:  # noqa: BLE001
        logger.debug("could not record a tool call", exc_info=True)


def record_request(context: CallContext, start_ns: int, end_ns: int, outcome: str, error: Optional[str] = None) -> None:
    """The API request as a whole, once it has finished: a ``nlsql_request`` log line and the root
    ``CHAIN`` span every LLM span of the request hangs off."""
    try:
        line = {
            "ts": _iso(start_ns),
            "durationMs": int((end_ns - start_ns) / 1_000_000),
            "outcome": outcome,
            "error": error,
            **context.log_fields(),
            **_public_attributes(context),
        }
        _emit_log_line("nlsql_request", line)
        include_text = _config("NLSQL_PHOENIX_INCLUDE_PROMPTS", True)
        attributes: dict[str, Any] = {
            "openinference.span.kind": "CHAIN",
            **_common_span_attributes(context),
            "metadata": json.dumps(_public_attributes(context), default=str),
        }
        if include_text and context.attributes.get("input") is not None:
            attributes["input.value"] = _clip(str(context.attributes["input"]))
        if include_text and context.attributes.get("output") is not None:
            attributes["output.value"] = _clip(_as_text(context.attributes["output"]))
            attributes["output.mime_type"] = "application/json"
        _send_span(otlp.Span(
            trace_id=context.trace_id,
            span_id=context.span_id,
            name=context.endpoint,
            start_ns=start_ns,
            end_ns=end_ns,
            kind=otlp.SPAN_KIND_SERVER,
            attributes=attributes,
            status_code=otlp.STATUS_ERROR if outcome == "error" else otlp.STATUS_OK,
            status_message=error or "",
        ))
    except Exception:  # noqa: BLE001
        logger.debug("could not record a request", exc_info=True)


@dataclass
class Feedback:
    """What someone thought of one result: a rating, and - for a thumbs down - why, either one of
    the panel's offered reasons or their own words."""

    #: The trace and request span of the call whose result is being rated.
    trace_id: str
    span_id: str
    #: Stable across one person's changes of mind about one result (a thumbs down, then a reason):
    #: the latest record with a given id is the one that counts.
    feedback_id: str
    rating: str  # "up" or "down"
    #: What was rated: "sql", "sql-failure", "fix", "accelerate", "explain-plan", "describe", "breakdown".
    target: str
    reason: Optional[str] = None  # an offered reason's id - "wrong-tables"
    label: Optional[str] = None  # its wording as shown - "Wrong tables or columns"
    comment: Optional[str] = None  # their own words, for "Other…"
    #: The panel's options when it was given (layout, verbosity) - which variant it is feedback on.
    variant: dict[str, Any] = field(default_factory=dict)
    user_id: Optional[int] = None
    username: Optional[str] = None


def feedback_span_name(feedback: Feedback) -> str:
    """``feedback · up``, or ``feedback · down · Wrong tables or columns`` - the rating, and the reason
    when there is one, readable in a trace's span list without opening the span."""
    name = f"feedback · {feedback.rating}"
    detail = feedback.label or (feedback.comment and _shorten(feedback.comment, 60))
    return f"{name} · {detail}" if detail else name


def record_feedback(feedback: Feedback) -> None:
    """A ``feedback`` log line at the ``FEEDBACK`` level, and a ``feedback`` span under the rated
    request's span in its trace. Never raises."""
    try:
        now = now_ns()
        line = {
            "ts": _iso(now),
            "feedbackId": feedback.feedback_id,
            "rating": feedback.rating,
            "target": feedback.target,
            "reason": feedback.reason,
            "label": feedback.label,
            "comment": feedback.comment,
            "variant": feedback.variant or None,
            "user.id": feedback.user_id,
            "user.name": feedback.username,
            "traceId": feedback.trace_id,
            "spanId": feedback.span_id,
        }
        _emit_log_line("feedback", line, FEEDBACK)
        attributes: dict[str, Any] = {
            "openinference.span.kind": "EVALUATOR",
            "user.id": str(feedback.user_id) if feedback.user_id is not None else None,
            "user.name": feedback.username,
            "feedback.id": feedback.feedback_id,
            "feedback.rating": feedback.rating,
            "feedback.score": 1 if feedback.rating == "up" else 0,
            "feedback.target": feedback.target,
            "feedback.reason": feedback.reason,
            "feedback.label": feedback.label,
            "feedback.comment": feedback.comment,
            "metadata": json.dumps({"variant": feedback.variant}, default=str),
        }
        if feedback.comment or feedback.label:
            attributes["input.value"] = feedback.comment or feedback.label
        _send_span(otlp.Span(
            trace_id=feedback.trace_id,
            span_id=secrets.token_hex(8),
            parent_span_id=feedback.span_id,
            name=feedback_span_name(feedback),
            start_ns=now,
            end_ns=now,
            kind=otlp.SPAN_KIND_INTERNAL,
            attributes=attributes,
            status_code=otlp.STATUS_OK,
        ))
    except Exception:  # noqa: BLE001
        logger.debug("could not record feedback", exc_info=True)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _public_attributes(context: CallContext) -> dict[str, Any]:
    """The request's attributes worth logging: not its input/output text (those go on the span, if
    anywhere) and not the pipeline's own working values (keys starting with "_")."""
    return {
        key: value for key, value in context.attributes.items()
        if key not in ("input", "output") and not key.startswith("_")
    }


def _log_object(record: LlmCallRecord, context: Optional[CallContext]) -> dict[str, Any]:
    include_text = _config("NLSQL_LLM_LOG_PROMPTS", False)
    line: dict[str, Any] = {
        "ts": _iso(record.start_ns),
        "callId": record.call_id,
        **(context.log_fields() if context else {}),
        "purpose": record.purpose,
        "attempt": record.attempt,
        "gen_ai.operation.name": record.operation,
        "gen_ai.system": provider_name(record.base_url),
        "gen_ai.request.model": record.request_model,
        "gen_ai.response.model": record.response_model,
        "gen_ai.response.id": record.response_id,
        "gen_ai.response.finish_reasons": [record.finish_reason] if record.finish_reason else None,
        "gen_ai.usage.input_tokens": record.input_tokens,
        "gen_ai.usage.output_tokens": record.output_tokens,
        "totalTokens": record.total_tokens,
        "reasoningTokens": record.reasoning_tokens,
        "cachedInputTokens": record.cached_input_tokens,
        "costUsd": record.cost_usd,
        "stream": record.stream,
        "timeToFirstTokenMs": record.time_to_first_token_ms,
        "durationMs": record.duration_ms,
        "promptChars": record.prompt_chars,
        "completionChars": record.completion_chars,
        "reasoningChars": record.reasoning_chars,
        "outcome": record.outcome,
        "error": record.error,
    }
    if include_text:
        line["prompt"] = record.prompt
        line["response"] = record.response
    return line


def _common_span_attributes(context: CallContext) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "user.id": str(context.user_id) if context.user_id is not None else None,
        "user.name": context.username,
        "superset.database_id": context.database_id,
        "superset.catalog": context.catalog,
        "superset.schema": context.schema,
        "superset.dialect": context.dialect,
        "nlsql.endpoint": context.endpoint,
        "nlsql.request_id": context.request_id,
    }
    tab_id = context.attributes.get("tabId")
    if tab_id:
        # Phoenix groups traces sharing a session id into one "session" - here, one SQL Lab tab's
        # back-and-forth with the panel.
        attributes["session.id"] = str(tab_id)
    return attributes


def _llm_span(record: LlmCallRecord, context: Optional[CallContext]) -> Optional[otlp.Span]:
    if context is None:
        return None
    include_text = _config("NLSQL_PHOENIX_INCLUDE_PROMPTS", True)
    embedding = record.operation == "embeddings"
    attributes: dict[str, Any] = {
        "openinference.span.kind": "EMBEDDING" if embedding else "LLM",
        **_common_span_attributes(context),
        "llm.system": "openai",  # the API shape every provider here is spoken to in
        "llm.provider": provider_name(record.base_url),
        "llm.model_name": record.response_model or record.request_model,
        "llm.token_count.prompt": record.input_tokens,
        "llm.token_count.completion": record.output_tokens,
        "llm.token_count.total": record.total_tokens,
        "llm.token_count.completion_details.reasoning": record.reasoning_tokens,
        "llm.token_count.prompt_details.cache_read": record.cached_input_tokens,
        "llm.cost.total": record.cost_usd,
        "llm.invocation_parameters": json.dumps({"stream": record.stream}),
        "metadata": json.dumps({
            "purpose": record.purpose,
            "attempt": record.attempt,
            "callId": record.call_id,
            "responseId": record.response_id,
            "finishReason": record.finish_reason,
            "timeToFirstTokenMs": record.time_to_first_token_ms,
            "promptChars": record.prompt_chars,
            "completionChars": record.completion_chars,
            "reasoningChars": record.reasoning_chars,
        }),
    }
    if embedding:
        attributes["embedding.model_name"] = record.request_model
    if include_text and record.prompt is not None:
        if embedding:
            attributes["embedding.embeddings.0.embedding.text"] = _clip(record.prompt)
        else:
            attributes["input.value"] = _clip(record.prompt)
            attributes["input.mime_type"] = "text/plain"
            attributes["llm.input_messages.0.message.role"] = "user"
            attributes["llm.input_messages.0.message.content"] = _clip(record.prompt)
    if include_text and record.response is not None and not embedding:
        attributes["output.value"] = _clip(record.response)
        attributes["output.mime_type"] = "text/plain"
        attributes["llm.output_messages.0.message.role"] = "assistant"
        attributes["llm.output_messages.0.message.content"] = _clip(record.response)
    name = span_name_for_call(record)
    return otlp.Span(
        trace_id=context.trace_id,
        span_id=secrets.token_hex(8),
        parent_span_id=context.span_id,
        name=name,
        start_ns=record.start_ns,
        end_ns=record.end_ns,
        kind=otlp.SPAN_KIND_CLIENT,
        attributes=attributes,
        status_code=otlp.STATUS_ERROR if record.outcome == "error" else otlp.STATUS_OK,
        status_message=record.error or "",
    )


def _emit_log_line(kind: str, line: dict[str, Any], level: int = logging.INFO) -> None:
    if not _config("NLSQL_LLM_LOG_ENABLED", True):
        return
    compact = {key: value for key, value in line.items() if value is not None}
    text = json.dumps(compact, default=str)
    call_logger.log(level, "%s %s", kind, text)
    path = _config("NLSQL_LLM_LOG_FILE", None)
    if path:
        _append_jsonl(str(path), {"kind": kind, **compact})


_file_lock = threading.Lock()


def _append_jsonl(path: str, line: dict[str, Any]) -> None:
    try:
        with _file_lock, open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, default=str) + "\n")
    except OSError:
        logger.warning("could not append to NLSQL_LLM_LOG_FILE %s", path, exc_info=True)


def _send_span(span: Optional[otlp.Span]) -> None:
    if span is None:
        return
    endpoint = _config("NLSQL_PHOENIX_ENDPOINT", None)
    if not endpoint:
        return
    headers = {"Content-Type": "application/x-protobuf"}
    api_key = _config("NLSQL_PHOENIX_API_KEY", None)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    headers.update(_config("NLSQL_PHOENIX_HEADERS", None) or {})
    project = _config("NLSQL_PHOENIX_PROJECT", DEFAULT_PHOENIX_PROJECT)
    _exporter_for(_traces_url(str(endpoint)), headers, project).submit(span)


def _traces_url(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    return endpoint if endpoint.endswith("/v1/traces") else f"{endpoint}/v1/traces"


class PhoenixExporter:
    """Sends spans to an OTLP/HTTP endpoint from a daemon thread, in batches.

    A span is handed over with `submit` and the caller moves on; the thread sends whatever has
    queued up every `flush_seconds` or once `batch_size` spans are waiting. A collector that's down
    costs a log line per failed batch (at most one a minute), never an exception in a request. The
    queue is bounded, so a collector that stays down drops spans rather than growing without limit.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        project: str,
        batch_size: int = 64,
        flush_seconds: float = 2.0,
        max_queue: int = 10_000,
    ) -> None:
        self.url = url
        self.headers = headers
        self.project = project
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._queue: "queue.Queue[otlp.Span]" = queue.Queue(maxsize=max_queue)
        self._last_failure_logged = 0.0
        self._thread = threading.Thread(target=self._run, name="nlsql-phoenix-exporter", daemon=True)
        self._thread.start()

    def submit(self, span: otlp.Span) -> None:
        try:
            self._queue.put_nowait(span)
        except queue.Full:
            pass

    def _run(self) -> None:
        while True:
            batch = [self._queue.get()]
            deadline = time.monotonic() + self._flush_seconds
            while len(batch) < self._batch_size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(self._queue.get(timeout=remaining))
                except queue.Empty:
                    break
            self._send(batch)

    def _send(self, spans: list[otlp.Span]) -> None:
        import requests

        body = otlp.encode_export_request(
            spans,
            {"service.name": SERVICE_NAME, "openinference.project.name": self.project},
            scope_name="waxwing.nltosqlandback",
            scope_version="0.1.0",
        )
        try:
            response = requests.post(self.url, data=body, headers=self.headers, timeout=10)
            if response.status_code >= 400:
                self._log_failure(f"HTTP {response.status_code}: {response.text[:200]}")
        except Exception as ex:  # noqa: BLE001
            self._log_failure(str(ex))

    def _log_failure(self, message: str) -> None:
        now = time.monotonic()
        if now - self._last_failure_logged > 60:
            self._last_failure_logged = now
            logger.warning("could not send spans to Phoenix at %s: %s", self.url, message)


_exporters: dict[tuple[str, str, str], PhoenixExporter] = {}
_exporters_lock = threading.Lock()


def _exporter_for(url: str, headers: dict[str, str], project: str) -> PhoenixExporter:
    key = (url, json.dumps(headers, sort_keys=True), project)
    with _exporters_lock:
        exporter = _exporters.get(key)
        if exporter is None:
            exporter = PhoenixExporter(url, headers, project)
            _exporters[key] = exporter
        return exporter


def _config(name: str, default: Any) -> Any:
    try:
        from flask import current_app

        return current_app.config.get(name, default)
    except Exception:  # noqa: BLE001 - outside an app context
        return default


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat(timespec="milliseconds")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SPAN_TEXT else text[:MAX_SPAN_TEXT] + f"… [{len(text) - MAX_SPAN_TEXT:,} more characters]"


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


__all__ = [
    "FEEDBACK",
    "Feedback",
    "LlmCallRecord",
    "PhoenixExporter",
    "asdict",
    "now_ns",
    "feedback_span_name",
    "provider_name",
    "record_feedback",
    "record_llm_call",
    "record_request",
    "record_tool_call",
    "span_name_for_call",
]
