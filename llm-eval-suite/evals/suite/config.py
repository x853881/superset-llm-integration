"""The suite's settings, from environment variables (loaded from .env.eval
by suite/__init__.py). See .env.eval.example for every variable and where to
find its value.
"""

import os
import re
import uuid
from dataclasses import dataclass, fields
from typing import Dict, List, Mapping, Optional

from plugin.endpoints import parse_latency_budgets


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


# A value an example file leaves to be filled in: YOUR_SCHEMA_NAME, <id>.
_PLACEHOLDER = re.compile(r"YOUR_|<[^<>]+>")

# The settings whose name isn't their field's in capitals.
_SETTING_NAMES = {
    "username": "SUPERSET_USERNAME",
    "password": "SUPERSET_PASSWORD",
    "auth": "SUPERSET_AUTH",
    "auth_provider": "SUPERSET_AUTH_PROVIDER",
    "database_id": "SUPERSET_DATABASE_ID",
    "catalog": "SUPERSET_CATALOG",
    "schema": "SUPERSET_SCHEMA",
}

_HEADER_NAME = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
# Headers the suite's own requests to Superset set.
_OWN_HEADERS = {"x-csrftoken", "referer", "cookie", "host", "content-type", "content-length"}


def parse_superset_headers(text: str, auth: str = "headers") -> Dict[str, str]:
    """SUPERSET_HEADERS as {name: value}: Name=value pairs separated by
    commas. A value may hold commas itself (a list of groups): a comma starts
    a new pair only when a header name and `=` follow it. Raises `ValueError`
    for a pair that isn't Name=value or a header the suite sets itself."""
    headers: Dict[str, str] = {}
    for pair in re.split(rf",(?=\s*{_HEADER_NAME}=)", text or ""):
        pair = pair.strip().rstrip(",").strip()
        if not pair:
            continue
        name, equals, value = pair.partition("=")
        name, value = name.strip(), value.strip()
        if not equals or not re.fullmatch(_HEADER_NAME, name):
            raise ValueError(
                f"SUPERSET_HEADERS: {pair!r} isn't Name=value "
                "(separate several with commas)"
            )
        own = _OWN_HEADERS | ({"authorization"} if auth == "password" else set())
        if name.lower() in own:
            raise ValueError(
                f"SUPERSET_HEADERS: {name!r} is a header the suite's requests already send"
            )
        if not value or not all(0x20 <= ord(c) < 0x7F for c in value):
            raise ValueError(
                f"SUPERSET_HEADERS: {name} needs a value, in plain ASCII characters"
            )
        headers[name] = value
    return headers


@dataclass(frozen=True)
class SuiteConfig:
    superset_url: str
    username: str
    password: str
    auth_provider: str
    database_id: str
    catalog: Optional[str]
    schema: str
    plugin_base_path: str
    sql_dialect: str
    sql_row_limit: int
    request_timeout_s: float
    latency_budget_s: float
    # Endpoints with their own time limit, overriding latency_budget_s.
    latency_budgets: Dict[str, float]
    # Faster Than Original's pass mark: the share of the original's time
    # accelerate's rewrite must save (0.5 = twice as fast).
    accelerate_min_saving: float
    # Timing queries: runs per query (after one warm-up), and the longest a
    # query may run.
    timing_runs: int
    timing_timeout_s: float
    # How queries are timed: psql in the examples database's container
    # (frees Superset while a query is timed), or sqllab through Superset.
    timing_via: str
    # Other SQL fails Speed vs Reference only when it is both more than this
    # share slower than the golden's reference and more than this many ms slower.
    reference_time_tolerance: float
    reference_min_slowdown_ms: float
    eval_run_id: str
    strict_live: bool
    # How the suite signs in to Superset. "password": Superset's own login,
    # with username and password. "headers": no login; Superset is told who
    # is calling by the headers in superset_headers, as a sign-in proxy in
    # front of it would tell it.
    auth: str = "password"
    # SUPERSET_HEADERS: Name=value pairs sent with every request to Superset.
    superset_headers: str = ""
    # TABLE_SELECTION: the plugin's way of picking tables to ask for (its
    # table_selection.py), for comparing them. Taken only if the plugin allows
    # it and the suite's user has a role that may choose. Empty: its default.
    table_selection: str = ""

    @classmethod
    def from_env(cls) -> "SuiteConfig":
        return cls(
            superset_url=os.getenv(
                "SUPERSET_URL", "http://superset:8088"
            ).rstrip("/"),
            username=os.getenv("SUPERSET_USERNAME", "YOUR_SUPERSET_USERNAME"),
            password=os.getenv("SUPERSET_PASSWORD", "YOUR_SUPERSET_PASSWORD"),
            auth_provider=os.getenv("SUPERSET_AUTH_PROVIDER", "db"),
            database_id=os.getenv("SUPERSET_DATABASE_ID", "YOUR_DATABASE_ID"),
            catalog=os.getenv("SUPERSET_CATALOG") or None,
            schema=os.getenv("SUPERSET_SCHEMA", "YOUR_SCHEMA_NAME"),
            plugin_base_path=os.getenv(
                "PLUGIN_BASE_PATH", "/extensions/waxwing/nltosqlandback"
            ).rstrip("/"),
            sql_dialect=os.getenv("SQL_DIALECT", "postgres"),
            sql_row_limit=int(os.getenv("SQL_ROW_LIMIT", "10000")),
            request_timeout_s=float(os.getenv("REQUEST_TIMEOUT_S", "120")),
            latency_budget_s=float(os.getenv("PLUGIN_LATENCY_BUDGET_S", "45")),
            latency_budgets=parse_latency_budgets(
                os.getenv("PLUGIN_LATENCY_BUDGETS", "")
            ),
            accelerate_min_saving=float(
                os.getenv("ACCELERATE_MIN_SAVING", "0.5")
            ),
            reference_time_tolerance=float(
                os.getenv("REFERENCE_TIME_TOLERANCE", "0.2")
            ),
            reference_min_slowdown_ms=float(
                os.getenv("REFERENCE_MIN_SLOWDOWN_MS", "500")
            ),
            timing_runs=int(os.getenv("TIMING_RUNS", "3")),
            # Superset abandons synchronous SQL Lab queries after 30s.
            timing_timeout_s=float(os.getenv("TIMING_TIMEOUT_S", "25")),
            timing_via=os.getenv("TIMING_VIA", "psql").strip().lower(),
            # The workers of a parallel run share pytest-xdist's run id.
            eval_run_id=os.getenv("EVAL_RUN_ID")
            or os.getenv("PYTEST_XDIST_TESTRUNUID", "")[:8]
            or uuid.uuid4().hex[:8],
            strict_live=_flag("STRICT_LIVE"),
            auth=os.getenv("SUPERSET_AUTH", "password").strip().lower() or "password",
            superset_headers=os.getenv("SUPERSET_HEADERS", "").strip(),
            table_selection=os.getenv("TABLE_SELECTION", "").strip(),
        )

    def latency_budget_for(self, endpoint: str) -> float:
        return self.latency_budgets.get(endpoint, self.latency_budget_s)

    def superset_request_headers(self) -> Dict[str, str]:
        """The headers SUPERSET_HEADERS adds to every request to Superset.
        Raises `ValueError`, naming the setting, when SUPERSET_AUTH or
        SUPERSET_HEADERS is wrong."""
        if self.auth not in ("password", "headers"):
            raise ValueError(
                f"SUPERSET_AUTH must be password or headers, not {self.auth!r}"
            )
        headers = parse_superset_headers(self.superset_headers, self.auth)
        if self.auth == "headers" and not headers:
            raise ValueError(
                "SUPERSET_AUTH=headers needs SUPERSET_HEADERS: the headers that "
                "tell Superset who is calling, as Name=value pairs"
            )
        return headers

    def unresolved_placeholders(self) -> List[str]:
        """The settings still holding an example file's placeholder
        (YOUR_... or <...>), leaving out those this way of signing in
        doesn't use."""
        unused = {"username", "password"} if self.auth == "headers" else set()
        return [
            _SETTING_NAMES.get(f.name, f.name.upper())
            for f in fields(self)
            if f.name not in unused
            and isinstance(getattr(self, f.name), str)
            and _PLACEHOLDER.search(getattr(self, f.name))
        ]


def unresolved_judge_settings(env: Mapping[str, str] = os.environ) -> List[str]:
    """The judge's settings (JUDGE_...) still holding an example file's
    placeholder, such as production's JUDGE_MODEL_NAME=<model>. They aren't
    SuiteConfig's, so unresolved_placeholders doesn't see them, and left
    unfilled they would fail only later, as an error from the judge."""
    return sorted(
        name
        for name, value in env.items()
        if name.startswith("JUDGE_") and _PLACEHOLDER.search(value)
    )
