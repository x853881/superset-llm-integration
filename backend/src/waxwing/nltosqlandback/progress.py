"""The events a call reports on its way to a result.

Every pipeline entry point in ``nlsql_backend`` is a generator yielding these dicts and returning
its final result, which ``api`` forwards to the browser as server-sent events. They are what the
panel's progress display is drawn from: the user sees which stage the call has reached, and the
text of the answer as it's written, rather than a spinner of unknown duration.

Generators rather than a callback or a worker thread so the whole pipeline stays on the request's
own thread inside Flask's application context - the work and the reporting of it are the same call
stack, and a client that hangs up simply stops pulling.
"""

from __future__ import annotations

import time
from typing import Any, Generator, Optional

# Yields progress events, returns whatever the step it wraps produces - the finished result
# payload at the top level, an intermediate value (a prompt fragment, a decoded result) further in.
EventStream = Generator[dict[str, Any], None, Any]

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def plan(stages: list[tuple[str, str]]) -> dict[str, Any]:
    """The steps this call intends to take, as (key, label) pairs, before any of them has run.

    Sent up front so the panel can show the whole shape of the work with the steps still to come
    greyed out, rather than a list that grows from nothing. A plan is what the call intends, not a
    promise: a step can be skipped (no summary is written for a request that turned out to need
    clarifying) and the detail on each one is only known once it has run.
    """
    return {"type": "plan", "stages": [{"key": key, "label": label} for key, label in stages]}


def stage(
    key: str,
    label: str,
    detail: Optional[str] = None,
    status: str = STATUS_ACTIVE,
    seconds: Optional[float] = None,
) -> dict[str, Any]:
    """One step of the pipeline, identified by `key`.

    Re-emitting the same `key` updates that step rather than adding another, which is how a step
    reports its own outcome: announced with what it's about to do, then amended with what it found,
    how long it took, and `status` "done" (or "failed"). Announcing a *new* step implies every
    earlier one is done.
    """
    return {
        "type": "stage",
        "key": key,
        "label": label,
        "detail": detail,
        "status": status,
        "seconds": seconds,
    }


def delta(field: str, text: str) -> dict[str, Any]:
    """Text just written by the model, for the named field of the result being built - or for
    `REASONING_FIELD`, its thinking on the way there, which is not part of the result at all."""
    return {"type": "delta", "field": field, "text": text}


#: The field a reasoning model's thinking is streamed into. Not a field of any result: the panel
#: shows it while the call runs and keeps it only as a record of how the answer was arrived at.
REASONING_FIELD = "reasoning"


def partial(field: str, value: Any) -> dict[str, Any]:
    """Part of the result that's already settled, sent before the result itself - replacing
    whatever was last sent for `field`. For results whose parts are worth showing as they're
    decided rather than as text: the SQL a breakdown is about, its sections so far."""
    return {"type": "partial", "field": field, "value": value}


def reset(field: str) -> dict[str, Any]:
    """Discard whatever has been streamed for `field` so far and start it again - a retry after an
    attempt that didn't work out is writing a fresh answer, not continuing the last one."""
    return {"type": "reset", "field": field}


def result(payload: dict[str, Any], trace: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """The call's answer. `trace` - the request's trace and span ids - is what feedback on the
    answer is filed under (see the `/feedback` endpoint), so the two meet in one trace."""
    event: dict[str, Any] = {"type": "result", "result": payload}
    if trace:
        event["trace"] = trace
    return event


def error(message: str) -> dict[str, Any]:
    return {"type": "error", "message": message}


def cancelled() -> dict[str, Any]:
    return {"type": "cancelled"}


class Steps:
    """The plan for one call, and the events that report it running.

    Holds each step's label and start time so the panel gets both halves of what it shows: the whole
    shape of the work up front (`plan_event`), and then each step's own outcome and duration as it
    finishes. A step's label changes with its state - "Drafting SQL" while it runs, "Drafted the
    SQL" once it has - which is why both are given up front.
    """

    def __init__(self, steps: list[tuple[str, str, str]]) -> None:
        """`steps` are (key, label while pending or running, label once done), in the order they
        are expected to run."""
        self._running_labels = {key: running for key, running, _ in steps}
        self._done_labels = {key: done for key, _, done in steps}
        self._started_at: dict[str, float] = {}
        self.plan_event = plan([(key, running) for key, running, _ in steps])

    def has(self, key: str) -> bool:
        return key in self._running_labels

    def start(self, key: str, detail: Optional[str] = None) -> dict[str, Any]:
        self._started_at[key] = time.monotonic()
        return stage(key, self._running_labels[key], detail, STATUS_ACTIVE)

    def update(self, key: str, detail: Optional[str]) -> dict[str, Any]:
        """Says more about a step already running - what the model is doing now, which attempt this
        is - without ending it."""
        return stage(key, self._running_labels[key], detail, STATUS_ACTIVE)

    def done(self, key: str, detail: Optional[str] = None) -> dict[str, Any]:
        return stage(key, self._done_labels[key], detail, STATUS_DONE, self._elapsed(key))

    def failed(self, key: str, detail: Optional[str]) -> dict[str, Any]:
        return stage(key, self._running_labels[key], detail, STATUS_FAILED, self._elapsed(key))

    def _elapsed(self, key: str) -> Optional[float]:
        started = self._started_at.get(key)
        return None if started is None else round(time.monotonic() - started, 1)
