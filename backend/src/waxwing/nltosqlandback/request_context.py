"""Who a call is being made for, and where - carried alongside the pipeline rather than through it.

Every LLM call has to be logged with the Superset user who caused it (see ``telemetry``), and the
calls that touch a database need to know which one. Threading those through every function in
``nlsql_backend`` and ``sql_error_fix`` would add the same three parameters to a dozen signatures
that have nothing else to do with them, so they live in a context variable instead: ``api`` sets it
for the duration of one streamed request, and anything below reads it.

Set inside the response generator, not the view function: a streamed response's body runs after the
view has returned, so a value set in the view and reset on its way out would be gone by the time
the pipeline - and the LLM calls in it - actually ran.
"""

from __future__ import annotations

import contextvars
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator, Optional
from urllib.parse import quote


@dataclass
class CallContext:
    """One API request: who made it, which endpoint, and which database/schema it is about.

    `trace_id`/`span_id` identify the request as a whole in the trace exported to Phoenix (see
    `telemetry`) - every LLM call made while answering it becomes a child span of `span_id`.
    """

    endpoint: str
    user_id: Optional[int] = None
    username: Optional[str] = None
    request_id: Optional[str] = None
    database_id: Optional[int] = None
    catalog: Optional[str] = None
    schema: Optional[str] = None
    trace_id: str = field(default_factory=lambda: secrets.token_hex(16))
    span_id: str = field(default_factory=lambda: secrets.token_hex(8))
    #: Filled in lazily by `superset_db.resolve_dialect` the first time something asks.
    dialect: Optional[str] = None
    #: "brief" or "full": how long the prose the model writes should be (see `prose`).
    verbosity: str = "full"
    #: Free-form facts about the request worth having on its log line - e.g. `runPlan` for
    #: accelerate - added to by the pipeline as it goes.
    attributes: dict[str, object] = field(default_factory=dict)

    def log_fields(self) -> dict[str, object]:
        """The fields every log line about this request carries."""
        return {
            "endpoint": self.endpoint,
            "user.id": self.user_id,
            "user.name": self.username,
            "requestId": self.request_id,
            "databaseId": self.database_id,
            "catalog": self.catalog,
            "schema": self.schema,
            "dialect": self.dialect,
            "verbosity": self.verbosity,
            "traceId": self.trace_id,
        }


_current: contextvars.ContextVar[Optional[CallContext]] = contextvars.ContextVar(
    "nltosqlandback_call_context", default=None
)


def current() -> Optional[CallContext]:
    """The request being answered, or None outside one (a test, a startup task)."""
    return _current.get()


@contextmanager
def bound(context: CallContext) -> Iterator[CallContext]:
    """Makes `context` the current one for the duration of the `with` block."""
    token = _current.set(context)
    try:
        yield context
    finally:
        try:
            _current.reset(token)
        except ValueError:
            # A streamed response's generator closed from somewhere other than where it ran (garbage
            # collection on another thread): that context is gone anyway.
            _current.set(None)


def current_user() -> tuple[Optional[int], Optional[str]]:
    """The logged-in Superset user's id and username, read from Flask-AppBuilder's `g.user`.

    Best effort: outside a request (or for an anonymous one) there is no user, and that is recorded
    as such rather than failing the call.
    """
    try:
        from flask import g

        user = getattr(g, "user", None)
    except Exception:  # noqa: BLE001 - no app/request context
        return None, None
    if user is None or getattr(user, "is_anonymous", False):
        return None, None
    user_id = getattr(user, "id", None)
    try:
        user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    return user_id, getattr(user, "username", None)


# Every printable ASCII character but `%` - left as it is in a header value. `%` itself is encoded
# so that every value decodes the same way, whatever was in it.
_HEADER_SAFE = "".join(chr(code) for code in range(0x21, 0x7F) if chr(code) != "%")


def header_value(text: str) -> str:
    """`text` as an HTTP header value: percent-encoded UTF-8 for anything but printable ASCII
    ("José" -> "Jos%C3%A9", "Jane Doe" -> "Jane%20Doe"), and `%` encoded too, so a receiver can
    always decode it (`urllib.parse.unquote`). Header values can only be Latin-1 - Python's
    `http.client` refuses anything else, which would fail the call - and a space, tab or newline
    could be trimmed, folded or used to end the header early."""
    return quote(text, safe=_HEADER_SAFE)


def user_headers(header_name: str) -> dict[str, str]:
    """The header that tells an LLM or embedding endpoint which Superset user a call is for (see
    `NLSQL_LLM_USER_HEADER`): `{header_name: username}`, or nothing when no header is configured or
    the call isn't for anyone - made outside a request, or by no one logged in."""
    if not header_name:
        return {}
    context = current()
    if context is None or not context.username:
        return {}
    return {header_name: header_value(context.username)}
