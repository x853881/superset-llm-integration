"""Thin client for Superset's REST API and the plugin's endpoints."""

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

from metrics.sql_safety import strip_sql_fences
from plugin.endpoints import SQL, Endpoint
from suite import locks
from suite.config import SuiteConfig
from suite.examples_db import psql


# Sent with every plugin call so the plugin can tag its DeepEval trace with
# the golden it is answering (see README "Capturing the plugin's full prompt").
EVAL_CASE_HEADER = "X-LLM-Eval-Case"
EVAL_RUN_HEADER = "X-LLM-Eval-Run"


@dataclass
class PluginResult:
    endpoint: str
    # The answer: SQL for SQL endpoints, prose for the others. None when the
    # plugin refused, failed or errored.
    output: Optional[str]
    raw: Dict[str, Any]
    latency_s: float
    # The plugin's own failureMessage, or the message of an error event.
    error: Optional[str] = None
    # The requestId sent with the call.
    request_id: str = ""
    # Every event the plugin streamed: its steps (with their durations and
    # any second attempt) as well as the result.
    events: List[Dict[str, Any]] = field(default_factory=list)
    # The plugin failed to produce an answer (an error or cancelled event),
    # as opposed to answering with a refusal.
    crashed: bool = False

    @property
    def refused(self) -> bool:
        return not self.output

    @property
    def _result(self) -> Dict[str, Any]:
        result = self.raw.get("result")
        return result if isinstance(result, dict) else {}

    @property
    def status(self) -> Optional[str]:
        """The plugin's status for its answer: Success, Failure or
        ClarificationsRequired (SQL endpoints only)."""
        return self._result.get("status")

    @property
    def clarifications(self) -> List[Dict[str, Any]]:
        """The questions the plugin asked back instead of answering, each
        {"question", "options"}; an open question has no options."""
        specific = [
            {
                "question": str(c.get("question") or ""),
                "options": [str(o) for o in c.get("options") or []],
            }
            for c in self._result.get("specificClarificationsNeeded") or []
            if isinstance(c, dict)
        ]
        general = [
            {"question": str(q), "options": []}
            for q in self._result.get("generalClarificationsNeeded") or []
            if q
        ]
        return specific + general

    @property
    def failure(self) -> Dict[str, Any]:
        """The plugin's answer when it reports it can't do what was asked:
        {"message", "tried", "suggestions"} (empty without a failure)."""
        if self.status != "Failure":
            return {}
        return {
            "message": str(self._result.get("failureMessage") or ""),
            "tried": [str(t) for t in self._result.get("whatWasTriedOnFailure") or [] if t],
            "suggestions": [str(s) for s in self._result.get("suggestionsForFixingOnFailure") or [] if s],
        }

    @property
    def assumptions(self) -> List[str]:
        """What the plugin assumed to answer (assumptionsMade)."""
        return [str(a) for a in self._result.get("assumptionsMade") or [] if a]


def parse_sse_events(text: str) -> List[Any]:
    """JSON payloads of a server-sent events stream (`data: {...}` blocks)."""
    events, data_lines = [], []
    for line in text.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif not line.strip() and data_lines:
            payload, data_lines = "\n".join(data_lines), []
            try:
                events.append(json.loads(payload))
            except ValueError:
                pass  # non-JSON keepalives such as [DONE]
    return events


def _is_event_stream(resp: requests.Response) -> bool:
    return "text/event-stream" in resp.headers.get(
        "Content-Type", ""
    ) or resp.text.lstrip().startswith("data:")


def _plugin_events(resp: requests.Response) -> List[Dict[str, Any]]:
    if not _is_event_stream(resp):
        return []
    return [e for e in parse_sse_events(resp.text) if isinstance(e, dict)]


def _plugin_response_body(resp: requests.Response) -> Dict[str, Any]:
    """The plugin streams progress as server-sent events and finishes with a
    `{"type": "result", ...}` event; plain JSON responses are also accepted."""
    if not _is_event_stream(resp):
        return resp.json()
    text = resp.text
    events = _plugin_events(resp)
    results = [e for e in events if e.get("type") == "result"]
    if results:
        return results[-1]
    if events:
        return events[-1]
    raise ValueError(f"no JSON events in the plugin's response: {text[:200]!r}")


def _superset_error(resp: requests.Response) -> str:
    """The database's own message from a Superset error response."""
    try:
        errors = resp.json().get("errors") or []
        messages = [e.get("message", "").strip() for e in errors]
        return "; ".join(m for m in messages if m) or resp.text[:500]
    except ValueError:
        return resp.text[:500]


class SupersetClient:
    """Thin client for Superset's REST API and the plugin's endpoints.
    Pass a `session` to inject a mock in unit tests.

    Every request holds a Superset slot, and every query the database lock
    (suite/locks.py), so parallel workers neither queue in Superset nor run a
    query while another is being timed."""

    def __init__(
        self, config: SuiteConfig, session: Optional[requests.Session] = None
    ):
        self.config = config
        self.session = session if session is not None else requests.Session()
        # On every request, the health check included: whatever stands in
        # front of Superset may want them before it lets anything through.
        self.session.headers.update(config.superset_request_headers())
        self._authenticated = False

    def _url(self, path: str) -> str:
        return f"{self.config.superset_url}/{path.lstrip('/')}"

    def _database_id(self) -> int:
        try:
            return int(self.config.database_id)
        except ValueError:
            raise ValueError(
                f"SUPERSET_DATABASE_ID must be an integer, got {self.config.database_id!r}"
            ) from None

    def health(self) -> bool:
        try:
            with locks.superset_request():
                resp = self.session.get(self._url("/health"), timeout=5)
        except requests.RequestException:
            return False
        return resp.status_code == 200

    def login(self) -> None:
        """Sign in the way SUPERSET_AUTH says, then fetch the CSRF token."""
        by_headers = self.config.auth == "headers"
        if by_headers:
            self._open_session()
        else:
            self._password_login()

        with locks.superset_request():
            csrf = self.session.get(
                self._url("/api/v1/security/csrf_token/"),
                timeout=self.config.request_timeout_s,
            )
        if by_headers and csrf.status_code in (401, 403):
            raise PermissionError(
                f"Superset answered {csrf.status_code} to a request carrying the "
                f"sign-in headers ({', '.join(self.config.superset_request_headers())}). "
                "Check SUPERSET_HEADERS with the Superset administrator: the "
                "header names, the user they name, and that Superset knows that user."
            )
        csrf.raise_for_status()
        self.session.headers["X-CSRFToken"] = csrf.json()["result"]
        self.session.headers["Referer"] = self.config.superset_url
        self._authenticated = True

    def _password_login(self) -> None:
        with locks.superset_request():
            resp = self.session.post(
                self._url("/api/v1/security/login"),
                json={
                    "username": self.config.username,
                    "password": self.config.password,
                    "provider": self.config.auth_provider,
                    "refresh": True,
                },
                timeout=self.config.request_timeout_s,
            )
        resp.raise_for_status()
        self.session.headers["Authorization"] = (
            f"Bearer {resp.json()['access_token']}"
        )

    def _open_session(self) -> None:
        """SUPERSET_AUTH=headers: there is no login to post. Superset's stock
        header sign-in (AUTH_REMOTE_USER) reads the headers when /login/ is
        opened and remembers the user in a session cookie, so open it. A
        Superset that reads the headers on every request needs no session and
        isn't harmed by one. A failure here shows at the next request."""
        try:
            with locks.superset_request():
                self.session.get(
                    self._url("/login/"), timeout=self.config.request_timeout_s
                )
        except requests.RequestException:
            pass

    def _ensure_login(self) -> None:
        if not self._authenticated:
            self.login()

    def _post(self, path: str, **kwargs: Any) -> Tuple[requests.Response, float]:
        """POST to Superset, logging in first if needed. Superset's login
        expires (after 15 minutes by default), so a 401 means log in again
        and retry once. Returns the response and how long the final attempt
        took, which leaves any re-login, and any wait for a Superset slot,
        out of the plugin's latency."""
        self._ensure_login()
        for attempt in (1, 2):
            with locks.superset_request():
                start = time.perf_counter()
                resp = self.session.post(self._url(path), **kwargs)
                took = time.perf_counter() - start
            if resp.status_code != 401 or attempt == 2:
                return resp, took
            self._authenticated = False
            self.login()
        raise AssertionError("unreachable")

    def call_plugin(
        self,
        endpoint: Endpoint,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
        case_id: Optional[str] = None,
    ) -> PluginResult:
        """Call one of the plugin's LLM endpoints the way the SQL Lab panel
        does: the tab's database context plus the endpoint's own fields."""
        request_id = f"llm-eval-{uuid.uuid4().hex[:12]}"
        payload = {
            "databaseId": self._database_id(),
            "catalog": self.config.catalog,
            "tabId": "1",
            "requestId": request_id,
            "verbosity": "full",
            **endpoint.payload(text, meta or {}),
        }
        headers = {EVAL_RUN_HEADER: self.config.eval_run_id}
        if case_id:
            headers[EVAL_CASE_HEADER] = case_id
        resp, latency = self._post(
            self.config.plugin_base_path + endpoint.path,
            json=payload,
            headers=headers,
            timeout=self.config.request_timeout_s,
        )
        resp.raise_for_status()
        body = _plugin_response_body(resp)
        trace = {"request_id": request_id, "events": _plugin_events(resp)}

        if body.get("type") in ("error", "cancelled"):
            message = body.get("message") or f"call {body['type']}"
            return PluginResult(
                endpoint.name, None, body, latency, error=message, crashed=True, **trace
            )
        result = (
            body.get("result") if isinstance(body.get("result"), dict) else {}
        )
        output = endpoint.extract(result)
        if output and endpoint.output == SQL:
            output = strip_sql_fences(output) or None
        return PluginResult(
            endpoint.name,
            output,
            body,
            latency,
            error=result.get("failureMessage") or None,
            **trace,
        )

    def execute_sql(self, sql: str) -> pd.DataFrame:
        """Run SQL synchronously through SQL Lab, so it is subject to the same
        database permissions and DML restrictions as the plugin's users.
        Never while another worker times a query (locks.querying)."""
        with locks.querying():
            resp, _ = self._post(
                "/api/v1/sqllab/execute/",
                json={
                    "database_id": self._database_id(),
                    "sql": sql,
                    "schema": self.config.schema,
                    "runAsync": False,
                    "select_as_cta": False,
                    "queryLimit": self.config.sql_row_limit,
                    "client_id": uuid.uuid4().hex[:10],
                    "tab": "llm-eval",
                },
                timeout=self.config.request_timeout_s,
            )
        if resp.status_code >= 400:
            raise requests.HTTPError(
                f"SQL Lab returned {resp.status_code}: {_superset_error(resp)}",
                response=resp,
            )
        body = resp.json()
        columns = [
            c.get("column_name") or c.get("name")
            for c in body.get("columns", [])
        ]
        frame = pd.DataFrame(body.get("data", []), columns=columns or None)
        # DROPDOWN: SQL Lab stopped at queryLimit with rows left over. A
        # LIMIT in the SQL itself shows as QUERY, and nothing is missing.
        frame.attrs["cut_off"] = (
            (body.get("query") or {}).get("limitingFactor") == "DROPDOWN"
            or bool(body.get("displayLimitReached"))
        )
        return frame

    def time_query(self, sql: str, timeout_s: float) -> Optional[float]:
        """How long `sql` takes the database to run, in milliseconds, or None
        if it was still running after `timeout_s` (the database then cancels
        it). Measured by EXPLAIN ANALYZE, which runs the query in full and
        reports the database's own execution time: no LLM, and none of
        Superset's overhead or the time to send rows back. TIMING OFF skips
        timing each plan step on every row, which can add more than the query
        itself takes; the total is still measured. No other query the suite
        sends runs meanwhile (locks.timing). PostgreSQL only.

        With TIMING_VIA=psql (the default), the statement goes through psql
        in the examples database's container, as the same user Superset
        connects as, with SQL Lab's schema and read-only: the same number,
        and Superset stays free for other workers' plugin calls meanwhile.
        TIMING_VIA=sqllab sends it through SQL Lab instead (needed where
        there's no docker command, as in the suite's own container)."""
        if self.config.sql_dialect not in ("postgres", "postgresql"):
            raise NotImplementedError(
                f"timing is only implemented for PostgreSQL, "
                f"not {self.config.sql_dialect}"
            )
        statement = (
            f"SET statement_timeout TO '{int(timeout_s * 1000)}ms'; "
            "EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + sql.strip().rstrip(";")
        )
        if self.config.timing_via == "psql":
            with locks.timing():
                try:
                    out = psql(
                        f'SET search_path TO "{self.config.schema}"; '
                        "SET TRANSACTION READ ONLY; " + statement
                    )
                except RuntimeError as e:
                    if "statement timeout" in str(e):
                        return None
                    raise
            plan = json.loads("\t".join(out))
            return float((plan[0] if isinstance(plan, list) else plan)["Execution Time"])
        if self.config.timing_via != "sqllab":
            raise ValueError(f"TIMING_VIA must be psql or sqllab, not {self.config.timing_via!r}")
        try:
            with locks.timing():
                try:
                    frame = self.execute_sql(statement)
                except requests.Timeout:
                    # Superset itself stalled, not the query (which the
                    # database cancels at timeout_s): try once more.
                    frame = self.execute_sql(statement)
        except requests.HTTPError as e:
            if "statement timeout" in str(e):
                return None
            raise
        plan = frame.iloc[0, 0]
        if isinstance(plan, str):
            plan = json.loads(plan)
        if isinstance(plan, list):
            plan = plan[0]
        return float(plan["Execution Time"])
