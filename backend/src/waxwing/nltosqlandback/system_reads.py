"""Reading every database on a schedule, as a system account, so what's known is ready before
anyone opens the panel.

Off until ``NLSQL_SYSTEM_USER`` names a Superset user; till then the panel starts reads (see
`knowledge_reads`). With it set, the panel starts none: a minute after start-up and then every
``NLSQL_SYSTEM_READ_SCHEDULE`` seconds (an hour by default; 0 for start-up only), a round reads
every database that user can reach - the full read for one with nothing known, the check for
added and dropped tables for the rest. A database someone told it to forget is left until someone
asks again. It reads structure only, never rows, and each prompt still shows a user only their own
tables.

Every web process runs the timer, since an extension has no other way to run a job without the
site's own scheduler: Superset 6.1's task framework is behind a feature flag and has no repeat
schedule, and Celery beat would need the site's Celery config to import this module. So a round
takes Superset's distributed lock and notes when it started; a process whose turn comes within
the schedule of the last round skips it, and each database's own lock keeps two reads of it apart.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from flask import current_app

from . import knowledge_reads, schema_knowledge

logger = logging.getLogger(__name__)

DEFAULT_SCHEDULE_SECONDS = 3600
FIRST_DELAY_SECONDS = 60
#: A process whose turn comes this share of the schedule after the last round started skips it.
RECENT_SHARE = 0.9
_LOCK_NAMESPACE = "nltosqlandback_system_read"
_RECORD = "nltosqlandback:system-read"
_started = threading.Event()


def _config(name: str, default: Any) -> Any:
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def schedule_seconds() -> int:
    return max(0, int(_config("NLSQL_SYSTEM_READ_SCHEDULE", DEFAULT_SCHEDULE_SECONDS) or 0))


def last_round() -> Optional[dict[str, Any]]:
    """When the last round started and ended, as whom, and what it read."""
    return schema_knowledge.store.get_record(_RECORD)


def start_timer() -> bool:
    """Starts this process's timer, once, if a system user is configured."""
    try:
        app = current_app._get_current_object()  # type: ignore[attr-defined]
    except RuntimeError:
        return False
    with app.app_context():
        if not knowledge_reads.system_user() or _started.is_set():
            return False
    _started.set()
    threading.Thread(target=_loop, args=(app,), name="nltosqlandback-system-read", daemon=True).start()
    return True


def _loop(app: Any) -> None:
    time.sleep(FIRST_DELAY_SECONDS)
    while True:
        try:
            run_round(app)
        except Exception:  # noqa: BLE001 - the next round tries again
            logger.exception("the scheduled read of the databases failed")
        with app.app_context():
            seconds = schedule_seconds()
        if not seconds:
            return
        time.sleep(seconds)


def run_round(app: Any) -> dict[str, Any]:
    """One round: every database the system user can reach read or checked, unless it's off,
    another process holds the round's lock, or a round started within the schedule. Says which."""
    with app.app_context():
        username = knowledge_reads.system_user()
        if not username:
            return {"ran": False, "why": "off"}
        seconds = schedule_seconds()
        try:
            with knowledge_reads.held(_LOCK_NAMESPACE, max(seconds, knowledge_reads.DEFAULT_LOCK_SECONDS), "A round is running.", round="all"):
                last = last_round() or {}
                if seconds and time.time() - float(last.get("startedAt") or 0) < seconds * RECENT_SHARE:
                    return {"ran": False, "why": "recent"}
                user = _user(username)
                if user is None:
                    logger.error("NLSQL_SYSTEM_USER names %r, who isn't a Superset user: no databases were read", username)
                    return {"ran": False, "why": "no such user"}
                started = time.time()
                schema_knowledge.store.put_record(_RECORD, {"startedAt": started, "by": username})
                read: dict[str, str] = {}
                for database_id in _databases(user):
                    knowledge = schema_knowledge.store.get(database_id, None, fresh=True)
                    state = (knowledge_reads.status(database_id, None) or {}).get("state")
                    if knowledge is None and state == knowledge_reads.FORGOTTEN:
                        read[str(database_id)] = "forgotten"
                        continue
                    kind = "fill" if knowledge is None else "check"
                    ran = knowledge_reads.run_now(app, kind, database_id, None, getattr(user, "id", None), username)
                    read[str(database_id)] = kind if ran else "busy"
                schema_knowledge.store.put_record(_RECORD, {"startedAt": started, "endedAt": time.time(), "by": username, "databases": read})
                return {"ran": True, "databases": read}
        except knowledge_reads.Busy:
            return {"ran": False, "why": "busy"}


def _user(username: str) -> Any:
    try:
        from superset import security_manager
    except Exception:  # noqa: BLE001 - no host
        return None
    return security_manager.find_user(username=username)


def _databases(user: Any) -> list[int]:
    """The ids of the databases `user` can reach."""
    try:
        from superset.utils.core import override_user
        from superset_core.common.daos import DatabaseDAO
    except Exception:  # noqa: BLE001 - no host
        return []
    with override_user(user):
        return sorted(int(getattr(database, "id")) for database in DatabaseDAO.find_all())
