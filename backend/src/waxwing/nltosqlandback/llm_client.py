"""Sends a single text prompt to an LLM and returns its text completion.

Speaks the OpenAI-compatible chat-completions shape, which Ollama, LM Studio, OpenAI
and OpenRouter all serve.

Every call is made with ``stream: true`` and read back token by token, so the panel can show
partial output as it is produced rather than only once the whole completion lands - see
``stream_complete``, which is a generator yielding each :class:`Chunk`. ``complete`` is the same
call with the chunks thrown away, for callers that only want the finished text.

A reasoning model's thinking arrives on its own channel (``reasoning_content``, or ``reasoning``
depending on the provider) and is yielded as chunks marked ``reasoning``, separately from the
answer. It matters twice over: it is often the longest phase of a call by far - a 12B reasoning
model can think for a minute and a half before writing a character of the answer, which is a very
long time to show someone nothing - and it must be kept out of the completion text, which the
caller is going to parse as JSON.

Every call - successful, failed or cancelled - is recorded by ``telemetry`` with the token counts
the provider reported. Those come from the stream's final ``usage`` chunk, which an
OpenAI-compatible server only sends when asked (``stream_options.include_usage``); OpenRouter sends
it regardless, along with the call's cost.
"""

from __future__ import annotations

import http.client
import json
import logging
import select
import socket
import time
from dataclasses import dataclass
from typing import Generator, Optional
from urllib.parse import urlsplit

from . import request_context, telemetry, tls
from .cancellation import CancelledError, CancelToken
from .llm_provider import LlmProvider

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10
# The read timeout is per-provider rather than a constant here - see LlmProvider.
# How often the wait for a response re-checks for cancellation - see `_wait_until_readable`.
CANCEL_POLL_SECONDS = 0.25

@dataclass(frozen=True)
class Chunk:
    """One piece of text as it arrives, and which channel it came in on."""

    text: str
    #: True for a reasoning model's thinking, which is not part of the answer.
    reasoning: bool = False


# A generator that yields chunks as they arrive and returns the whole completion - the answer
# only, with any reasoning left out.
TextStream = Generator[Chunk, None, str]


class _CallMeta:
    """What the response said about itself besides its text - filled in as the stream is read, for
    the telemetry record."""

    def __init__(self) -> None:
        self.usage: dict = {}
        self.model: Optional[str] = None
        self.response_id: Optional[str] = None
        self.finish_reason: Optional[str] = None
        self.first_token_ns: Optional[int] = None

    def take(self, payload: dict) -> None:
        """Picks out whatever this chunk (or whole response) carries: the model that actually
        answered, its id, why it stopped, and - usually only on the last chunk - token usage."""
        if isinstance(payload.get("usage"), dict):
            self.usage = payload["usage"]
        self.model = payload.get("model") or self.model
        self.response_id = payload.get("id") or self.response_id
        for choice in payload.get("choices") or []:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]


def _usage_fields(usage: dict) -> dict:
    """The provider's `usage` object as the telemetry record's fields. Reasoning and cached-token
    counts sit in `*_details` sub-objects in OpenAI's shape; OpenRouter adds `cost` in dollars."""
    def as_int(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    completion_details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    cost = usage.get("cost")
    try:
        cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost = None
    return {
        "input_tokens": as_int(usage.get("prompt_tokens")),
        "output_tokens": as_int(usage.get("completion_tokens")),
        "total_tokens": as_int(usage.get("total_tokens")),
        "reasoning_tokens": as_int(completion_details.get("reasoning_tokens")),
        "cached_input_tokens": as_int(prompt_details.get("cached_tokens")),
        "cost_usd": cost,
    }


def stream_complete(
    provider: LlmProvider,
    prompt: str,
    cancel_token: Optional[CancelToken] = None,
    purpose: Optional[str] = None,
    attempt: int = 1,
) -> TextStream:
    """Sends `prompt` to `provider`, yielding each chunk of text as it arrives and returning the
    full text once the stream ends. Every call is recorded by `telemetry` (who it was for, model,
    tokens, latency) regardless of outcome, so telemetry survives a failed call too. `purpose` and
    `attempt` say which step of the pipeline made it - "draft", attempt 2 - for that record.

    When `cancel_token` is given and gets cancelled while this call is in flight, its underlying
    connection is forcibly closed and this raises `CancelledError` in place of whatever lower-level
    error that produced, rather than looking like an ordinary request failure."""
    start_ns = telemetry.now_ns()
    meta = _CallMeta()
    answer: list[str] = []
    reasoning: list[str] = []

    def record(outcome: str, error: Optional[str] = None) -> None:
        text = "".join(answer)
        thinking = "".join(reasoning)
        telemetry.record_llm_call(telemetry.LlmCallRecord(
            operation="chat",
            request_model=provider.model,
            base_url=provider.base_url,
            start_ns=start_ns,
            end_ns=telemetry.now_ns(),
            outcome=outcome,
            purpose=purpose,
            attempt=attempt,
            response_model=meta.model,
            response_id=meta.response_id,
            finish_reason=meta.finish_reason,
            time_to_first_token_ms=(
                int((meta.first_token_ns - start_ns) / 1_000_000) if meta.first_token_ns is not None else None
            ),
            prompt_chars=len(prompt),
            completion_chars=len(text),
            reasoning_chars=len(thinking),
            error=error,
            prompt=prompt,
            response=text,
            **_usage_fields(meta.usage),
        ))
        logger.info(
            "llm_call model=%s latency_ms=%d prompt_chars=%d completion_chars=%d reasoning_chars=%d "
            "input_tokens=%s output_tokens=%s outcome=%s",
            provider.model, int((telemetry.now_ns() - start_ns) / 1_000_000), len(prompt), len(text),
            len(thinking), meta.usage.get("prompt_tokens"), meta.usage.get("completion_tokens"), outcome,
        )

    try:
        for chunk in _stream_request(provider, prompt, cancel_token, meta):
            if meta.first_token_ns is None:
                meta.first_token_ns = telemetry.now_ns()
            if chunk.reasoning:
                reasoning.append(chunk.text)
            else:
                answer.append(chunk.text)
            yield chunk
        # A cancel that lands while this is blocked reading gets the socket shut down under it
        # (see `CancelToken.cancel`), which reads as a clean end of stream rather than as an
        # error - so without this, a cancelled call would look like a successful short answer.
        if cancel_token is not None and cancel_token.cancelled:
            raise CancelledError("The request was cancelled.")
    except GeneratorExit:
        # The consumer stopped reading (the client hung up, or a caller closed the stream early):
        # still a call that was made and has to be accounted for.
        record("cancelled", "The stream was closed before it finished.")
        raise
    except Exception as ex:
        cancelled = cancel_token is not None and cancel_token.cancelled
        record("cancelled" if cancelled else "error", str(ex))
        if cancelled:
            raise CancelledError("The request was cancelled.") from ex
        raise
    record("ok")
    return "".join(answer)


def complete(
    provider: LlmProvider,
    prompt: str,
    cancel_token: Optional[CancelToken] = None,
    purpose: Optional[str] = None,
    attempt: int = 1,
) -> str:
    """`stream_complete` with the deltas discarded - for callers that only want the finished text."""
    return drain(stream_complete(provider, prompt, cancel_token, purpose=purpose, attempt=attempt))


def drain(stream: TextStream) -> str:
    """Runs `stream` to completion, discarding what it yields, and returns what it returned."""
    while True:
        try:
            next(stream)
        except StopIteration as stop:
            return stop.value or ""


def _wait_until_readable(sock: socket.socket, cancel_token: CancelToken, overall_timeout: float) -> None:
    """Blocks until `sock` has a response waiting to be read, polling in short intervals rather
    than a single blocking call so a cancellation lands within one poll instead of only being
    noticed once the wait ends on its own - unlike a plain blocking read, shutting the socket down
    from another thread while this thread is already blocked inside it isn't reliably interrupting
    on every platform, so cancellation is instead checked *before* any blocking read ever starts.

    Only used for the wait before the response's headers are read. Once the body is streaming,
    bytes can already be sitting in the response's own buffer with nothing left readable on the
    socket itself, which would make this wait out a timeout while the answer is in hand - so from
    there on cancellation is checked between reads instead (see `_iter_sse_deltas`), which is
    responsive enough because a token stream hands back a chunk every few hundred milliseconds."""
    deadline = time.monotonic() + overall_timeout
    while True:
        if cancel_token.cancelled:
            raise CancelledError("The request was cancelled.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for a response.")
        readable, _, _ = select.select([sock], [], [], min(CANCEL_POLL_SECONDS, remaining))
        if readable:
            return


def _stream_request(
    provider: LlmProvider, prompt: str, cancel_token: Optional[CancelToken], meta: Optional[_CallMeta] = None
) -> Generator[Chunk, None, None]:
    meta = meta if meta is not None else _CallMeta()
    url = urlsplit(f"{provider.base_url.rstrip('/')}/chat/completions")
    if url.scheme == "https":
        # None - the default context - unless a client certificate or CA bundle is set (see `tls`).
        conn = http.client.HTTPSConnection(url.hostname, url.port, timeout=CONNECT_TIMEOUT_SECONDS, context=tls.ssl_context(provider.tls))
    else:
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        conn.connect()
        if cancel_token is not None:
            # Backstops a cancel landing before or during the (normally sub-second) connect/send
            # phase below, which - unlike the wait for a response - isn't covered by the polling
            # wait: forcibly shuts the socket down, including immediately if already cancelled.
            cancel_token.register(conn.sock)
        conn.sock.settimeout(provider.timeout_seconds)

        path = url.path or "/"
        if url.query:
            path = f"{path}?{url.query}"
        request_body: dict = {
            "model": provider.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if provider.request_usage:
            # Without this an OpenAI-compatible server sends no token counts on a stream at all.
            request_body["stream_options"] = {"include_usage": True}
        body = json.dumps(request_body)
        headers = {
            # First, so a user header named like one of the others can't replace it.
            **request_context.user_headers(provider.user_header),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            # Deliberately no Accept-Encoding: reading the raw connection below (rather than
            # going through a library that transparently decompresses) means a compressed
            # response would come back undecoded - omitting it keeps a well-behaved server
            # from compressing the response in the first place.
        }
        # Only when there is a key to send. A local Ollama or LM Studio ignores the header
        # either way, but some gateways reject `Bearer ` with nothing after it, which reads
        # as a bad credential rather than as no credential at all.
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        conn.request("POST", path, body=body, headers=headers)
        if cancel_token is not None:
            _wait_until_readable(conn.sock, cancel_token, provider.timeout_seconds)
        response = conn.getresponse()
        if response.status >= 400:
            raw_body = response.read()
            raise RuntimeError(
                f"LLM request failed ({response.status}): {raw_body.decode('utf-8', errors='replace')[:500]}"
            )
        content_type = response.getheader("Content-Type") or ""
        if "event-stream" in content_type:
            yield from _iter_sse_deltas(response, cancel_token, meta)
        else:
            # A provider that ignored `stream: true` and answered with one whole JSON body
            # instead. Nothing to stream, but the completion itself is perfectly usable.
            yield from _single_completion(response.read(), meta)
    finally:
        if cancel_token is not None:
            cancel_token.clear()
        conn.close()


def _iter_sse_deltas(
    response: http.client.HTTPResponse, cancel_token: Optional[CancelToken], meta: Optional[_CallMeta] = None
) -> Generator[Chunk, None, None]:
    """Yields the text of each `chat.completion.chunk` in an SSE response body, from both channels:
    the answer, and a reasoning model's thinking. Whatever else a chunk says about the call - its
    model, id, finish reason, the usage on the final chunk - goes into `meta`."""
    meta = meta if meta is not None else _CallMeta()
    while True:
        if cancel_token is not None and cancel_token.cancelled:
            raise CancelledError("The request was cancelled.")
        raw_line = response.readline()
        if not raw_line:
            return
        line = raw_line.decode("utf-8", errors="replace").strip()
        # Blank lines separate events; a line starting with ":" is a keep-alive comment.
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except ValueError:
            continue  # a partial or non-JSON keep-alive frame - nothing to take from it
        # A router (OpenRouter among them) can report an upstream failure mid-stream this way
        # rather than with an HTTP error - surface its message rather than ending silently.
        error = payload.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(message or "The LLM backend returned an error.")
        meta.take(payload)
        choices = payload.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        # `reasoning_content` is what LM Studio and DeepSeek call it; OpenRouter says `reasoning`.
        thinking = delta.get("reasoning_content") or delta.get("reasoning")
        if thinking:
            yield Chunk(thinking, reasoning=True)
        content = delta.get("content")
        if content:
            yield Chunk(content)


def _single_completion(raw_body: bytes, meta: Optional[_CallMeta] = None) -> Generator[Chunk, None, None]:
    """The non-streamed chat-completions shape, for a provider that ignored `stream: true` - the
    whole answer as one chunk, preceded by the whole of its reasoning if it reported any."""
    parsed_body = json.loads(raw_body)
    error = parsed_body.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else str(error)
        raise RuntimeError(message or "The LLM backend returned an error.")
    if meta is not None:
        meta.take(parsed_body)
    choices = parsed_body.get("choices") or []
    if not choices:
        return
    message = choices[0].get("message") or {}
    thinking = message.get("reasoning_content") or message.get("reasoning")
    if thinking:
        yield Chunk(thinking, reasoning=True)
    content = message.get("content")
    if content:
        yield Chunk(content)
