"""Helpers for testing the event-stream pipelines in `nlsql_backend` and `sql_error_fix`.

Both are generators that yield progress events and *return* their result (see `progress`), so a
test has to drain one to get at either - `run` does that, handing back both halves.
"""

from __future__ import annotations

from typing import Any, Optional

from waxwing.nltosqlandback import progress
from waxwing.nltosqlandback.llm_client import Chunk


def run(stream: Any) -> tuple[Any, list[dict[str, Any]]]:
    """Drains `stream`, returning what it returned and every event it yielded on the way."""
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(stream))
        except StopIteration as stop:
            return stop.value, events


def events_of(events: list[dict[str, Any]], event_type: str) -> list[dict[str, Any]]:
    return [event for event in events if event["type"] == event_type]


def streamed_text(events: list[dict[str, Any]], field: str) -> str:
    """What a client watching `field` would have ended up showing - the deltas for it joined in
    order, with everything before a `reset` of that field discarded, exactly as the panel does."""
    text = ""
    for event in events:
        if event.get("field") != field:
            continue
        if event["type"] == "reset":
            text = ""
        elif event["type"] == "delta":
            text += event["text"]
    return text


def stage_keys(events: list[dict[str, Any]]) -> list[str]:
    """The stages announced, in order, once each - a re-emitted key updates a stage in place
    rather than adding one, so it isn't listed twice."""
    keys: list[str] = []
    for event in events_of(events, "stage"):
        if event["key"] not in keys:
            keys.append(event["key"])
    return keys


def planned_keys(events: list[dict[str, Any]]) -> list[str]:
    """The steps the call said up front that it intended to take."""
    plans = events_of(events, "plan")
    return [stage["key"] for stage in plans[0]["stages"]] if plans else []


def final_stage(events: list[dict[str, Any]], key: str) -> Optional[dict[str, Any]]:
    """The last thing stage `key` said about itself - i.e. its outcome."""
    matching = [event for event in events_of(events, "stage") if event["key"] == key]
    return matching[-1] if matching else None


class FakeLlm:
    """Stands in for `llm_client.stream_complete`, handing back `responses` in order, one per call,
    in small chunks - so a test exercises the same partial-text path a real model stream does.

    `reasoning` is yielded first, on the reasoning channel, as a model that thinks before answering
    does; it is not part of what the call returns.
    """

    def __init__(self, *responses: str, chunk_size: int = 8, reasoning: str = "") -> None:
        self._responses = list(responses)
        self._chunk_size = chunk_size
        self._reasoning = reasoning
        self.prompts: list[str] = []

    def __call__(self, _provider: Any, prompt: str, cancel_token: Any = None, **_kwargs: Any) -> Any:
        self.prompts.append(prompt)
        text = self._responses.pop(0) if self._responses else ""
        reasoning = self._reasoning
        chunk_size = self._chunk_size

        def stream() -> Any:
            for start in range(0, len(reasoning), chunk_size):
                yield Chunk(reasoning[start: start + chunk_size], reasoning=True)
            for start in range(0, len(text), chunk_size):
                yield Chunk(text[start: start + chunk_size])
            return text

        return stream()

    @property
    def call_count(self) -> int:
        return len(self.prompts)


def fake_schema_preamble(text: str = "Database schema:\nTABLE public.orders\n\n") -> Any:
    """A stand-in for `nlsql_backend.schema_preamble` - the real one embeds the request and needs
    Flask config, neither of which has anything to do with what these tests are checking. Reports
    the same step it does, since a flow's step sequence is one of the things under test."""

    def schema_preamble(_query_text: str, steps: Any) -> Any:
        yield steps.start("tables")
        yield steps.done("tables", "stubbed")
        return text

    return schema_preamble
