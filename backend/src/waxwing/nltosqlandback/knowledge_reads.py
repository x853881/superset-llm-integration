"""Reading a database's structure without anyone waiting for it, and one read at a time.

The first user to open the panel on a database nothing is known about starts a read of every schema
its connection can see, in the background (``NLSQL_INTERROGATE_ON_FIRST_USE``, on by default). Now
and then after that (``NLSQL_INTERROGATE_CHECK_SECONDS``, an hour by default) a user opening the
panel starts the cheap check, which lists each schema's tables and reads only the added ones,
dropping the gone ones. "Read again" in the panel reads everything, merged with what's kept. All of
them are ``db_interrogation.interrogate``.

Superset reads every database through its connection's own login, so a table missing from a
schema's list has been dropped, not hidden from this user. What's read is narrowed to each user
when it's used.

Only one read of a database runs at a time, across every process: each holds Superset's distributed
lock for the database. The lock isn't renewed, so it lasts ``NLSQL_INTERROGATE_LOCK_SECONDS`` (half
an hour by default) - longer than a read takes - and a process that dies holding it blocks reads of
that database until then. While a read runs it keeps a status record any panel can read: who
started it, how far it has got, and how it ended.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import ExitStack, contextmanager, nullcontext
from typing import Any, Iterator, Optional

from flask import current_app

from . import db_interrogation, request_context, schema_knowledge, telemetry
from .cancellation import CancelledError
from .progress import EventStream
from .request_context import CallContext

logger = logging.getLogger(__name__)

READING, DONE, FAILED, FORGOTTEN = "reading", "done", "failed", "forgotten"
DEFAULT_CHECK_SECONDS = 3600
DEFAULT_LOCK_SECONDS = 1800
#: How often a running read writes down how far it has got.
STATUS_EVERY_SECONDS = 2.0
#: How long after a read failed before opening the panel starts another.
RETRY_SECONDS = 600
_LOCK_NAMESPACE = "nltosqlandback_read"


class Busy(RuntimeError):
    """Another read of the database is running."""


def _config(name: str, default: Any) -> Any:
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _lock_seconds() -> int:
    return int(_config("NLSQL_INTERROGATE_LOCK_SECONDS", DEFAULT_LOCK_SECONDS))


# --- The status record ---------------------------------------------------------------------------


def _status_name(database_id: int, catalog: Optional[str]) -> str:
    return f"nltosqlandback:reading:{database_id}:{catalog or ''}"


def status(database_id: int, catalog: Optional[str]) -> Optional[dict[str, Any]]:
    """The last read of the database: ``state`` (reading, done, failed or forgotten), ``kind``
    (fill, check or read), ``by``, ``startedAt``, ``updatedAt``, ``detail`` and ``error``. A read
    that has said nothing for as long as the lock lasts is reported as failed - its process died."""
    record = schema_knowledge.store.get_record(_status_name(database_id, catalog))
    if record and record.get("state") == READING and time.time() - float(record.get("updatedAt") or 0) > _lock_seconds():
        return {**record, "state": FAILED, "error": "The read stopped without finishing."}
    return record


def _put_status(database_id: int, catalog: Optional[str], record: dict[str, Any]) -> None:
    schema_knowledge.store.put_record(_status_name(database_id, catalog), record)


def mark_forgotten(database_id: int, catalog: Optional[str], by: Optional[str]) -> None:
    """After "Forget", opening the panel doesn't read the database again until someone asks."""
    _put_status(database_id, catalog, {"state": FORGOTTEN, "by": by, "updatedAt": time.time()})


# --- One read at a time --------------------------------------------------------------------------

_local = threading.Lock()
#: Outside a Superset there's no distributed lock: the locks held in this process.
_held: set[tuple[Any, ...]] = set()
#: The databases this process is reading in the background.
_running: set[tuple[int, str]] = set()


@contextmanager
def exclusive(database_id: int, catalog: Optional[str]) -> Iterator[None]:
    """Holds the database's lock, or raises `Busy`."""
    with held(_LOCK_NAMESPACE, _lock_seconds(), "This database is being read already.", database_id=database_id, catalog=catalog or ""):
        yield


@contextmanager
def held(namespace: str, ttl_seconds: int, busy: str, **identity: Any) -> Iterator[None]:
    """Holds Superset's distributed lock on `namespace` and `identity`, or raises `Busy` saying
    `busy`. Without a Superset, a lock for this process alone."""
    try:
        from superset.distributed_lock import DistributedLock
        from superset.exceptions import AcquireDistributedLockFailedException
    except Exception:  # noqa: BLE001 - no host (tests, tools)
        DistributedLock = None  # noqa: N806
    if DistributedLock is None:
        key = (namespace, *sorted(identity.items()))
        with _local:
            if key in _held:
                raise Busy(busy)
            _held.add(key)
        try:
            yield
        finally:
            with _local:
                _held.discard(key)
        return
    with ExitStack() as stack:
        try:
            stack.enter_context(DistributedLock(namespace, ttl_seconds=ttl_seconds, **identity))
        except AcquireDistributedLockFailedException as ex:
            raise Busy(busy) from ex
        yield


def tracked(kind: str, stream: EventStream) -> EventStream:
    """Runs `stream`, a read of the current request's database, holding the database's lock and
    keeping its status record. Raises `Busy` when another read of it is running."""
    context = request_context.current()
    assert context is not None and context.database_id is not None
    database_id, catalog = context.database_id, context.catalog
    try:
        lock = exclusive(database_id, catalog)
        lock.__enter__()
    except Busy:
        running = status(database_id, catalog) or {}
        by = f" by {running['by']}" if running.get("state") == READING and running.get("by") else ""
        stream.close()
        raise Busy(f"This database is being read already{by}. What's found will be here when it's done.") from None
    now = time.time()
    record: dict[str, Any] = {"state": READING, "kind": kind, "by": context.username, "startedAt": now, "updatedAt": now}
    _put_status(database_id, catalog, record)
    written = time.monotonic()
    try:
        while True:
            try:
                event = next(stream)
            except StopIteration as stop:
                _put_status(database_id, catalog, {**record, "state": DONE, "updatedAt": time.time()})
                return stop.value
            if event.get("type") == "stage" and event.get("detail") and time.monotonic() - written >= STATUS_EVERY_SECONDS:
                record = {**record, "detail": f"{event.get('label')}: {event['detail']}", "updatedAt": time.time()}
                _put_status(database_id, catalog, record)
                written = time.monotonic()
            yield event
    except (CancelledError, GeneratorExit):
        _put_status(database_id, catalog, {**record, "state": FAILED, "error": "Stopped before it finished.", "updatedAt": time.time()})
        raise
    except Exception as ex:
        _put_status(database_id, catalog, {**record, "state": FAILED, "error": db_interrogation._brief(ex), "updatedAt": time.time()})
        raise
    finally:
        stream.close()
        lock.__exit__(None, None, None)


# --- In the background ---------------------------------------------------------------------------


def _first_use() -> bool:
    return bool(_config("NLSQL_INTERROGATE_ON_FIRST_USE", True))


def _check_seconds() -> int:
    return int(_config("NLSQL_INTERROGATE_CHECK_SECONDS", DEFAULT_CHECK_SECONDS))


def system_user() -> Optional[str]:
    """The Superset user scheduled reads run as (see `system_reads`); while it's set, opening the
    panel starts no reads."""
    return str(_config("NLSQL_SYSTEM_USER", "") or "").strip() or None


def for_panel(database_id: int, catalog: Optional[str], knowledge: Optional[schema_knowledge.DatabaseKnowledge]) -> Optional[dict[str, Any]]:
    """What the panel should say about reading the database - starting a read first if it's time:
    the first read when nothing is known, or the check when the last one is old enough. None when
    there's nothing to say."""
    current = status(database_id, catalog)
    state = (current or {}).get("state")
    if state == READING or system_user():
        return current
    recently_failed = state == FAILED and time.time() - float((current or {}).get("updatedAt") or 0) < RETRY_SECONDS
    if knowledge is None:
        if _first_use() and state != FORGOTTEN and not recently_failed and start("fill", database_id, catalog):
            return _starting("fill")
        return current if state in (FAILED, FORGOTTEN) else None
    seconds = _check_seconds()
    last = max(knowledge.checked_at, knowledge.version)
    if seconds > 0 and time.time() - last >= seconds and not recently_failed and start("check", database_id, catalog):
        return _starting("check")
    return None


def _starting(kind: str) -> dict[str, Any]:
    _, username = request_context.current_user()
    now = time.time()
    return {"state": READING, "kind": kind, "by": username, "startedAt": now, "updatedAt": now}


def start(kind: str, database_id: int, catalog: Optional[str]) -> bool:
    """Starts a background read ("fill" reads everything, "check" looks for added and dropped
    tables) of the database as the current user, unless this process is reading it already.
    Whether it then runs is the lock's to say."""
    try:
        app = current_app._get_current_object()  # type: ignore[attr-defined]
    except RuntimeError:
        return False
    if not _claim(database_id, catalog):
        return False
    user_id, username = request_context.current_user()
    thread = threading.Thread(
        target=_background,
        args=(app, kind, database_id, catalog, user_id, username),
        name=f"nltosqlandback-{kind}-{database_id}",
        daemon=True,
    )
    try:
        thread.start()
    except RuntimeError:
        with _local:
            _running.discard((database_id, catalog or ""))
        raise
    return True


def run_now(app: Any, kind: str, database_id: int, catalog: Optional[str], user_id: Optional[int], username: Optional[str]) -> bool:
    """`start`'s read, run in this thread as the given user - for a job with no request behind it.
    False when this process is reading the database already."""
    if not _claim(database_id, catalog):
        return False
    _background(app, kind, database_id, catalog, user_id, username)
    return True


def _claim(database_id: int, catalog: Optional[str]) -> bool:
    key = (database_id, catalog or "")
    with _local:
        if key in _running:
            return False
        _running.add(key)
    return True


def _background(app: Any, kind: str, database_id: int, catalog: Optional[str], user_id: Optional[int], username: Optional[str]) -> None:
    key = (database_id, catalog or "")
    context = CallContext(endpoint=f"{kind}_db_knowledge", user_id=user_id, username=username, database_id=database_id, catalog=catalog)
    try:
        with app.app_context(), _as_user(user_id), request_context.bound(context):
            start_ns = telemetry.now_ns()
            outcome, error = "ok", None
            try:
                stream = tracked(kind, db_interrogation.interrogate(changes_only=kind == "check"))
                while True:
                    try:
                        next(stream)
                    except StopIteration:
                        break
            except Busy:
                outcome = "busy"
            except Exception as ex:  # noqa: BLE001 - nobody is waiting on it; the status record says
                logger.exception("the background %s of database %s failed", kind, database_id)
                outcome, error = "error", str(ex)
            finally:
                telemetry.record_request(context, start_ns, telemetry.now_ns(), outcome, error)
                _end_session()
    finally:
        with _local:
            _running.discard(key)


def _as_user(user_id: Optional[int]) -> Any:
    """Superset's checks look at `g.user`, which a thread of its own doesn't have."""
    try:
        from superset import security_manager
        from superset.utils.core import override_user
    except Exception:  # noqa: BLE001 - no host
        return nullcontext()
    user = security_manager.get_user_by_id(user_id) if user_id is not None else None
    return override_user(user)


def _end_session() -> None:
    try:
        from superset.extensions import db
    except Exception:  # noqa: BLE001 - no host
        return
    try:
        db.session.remove()
    except Exception:  # noqa: BLE001
        pass
