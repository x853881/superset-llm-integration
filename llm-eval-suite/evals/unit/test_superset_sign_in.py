"""Unit tests for signing in to Superset by request headers
(SUPERSET_AUTH=headers, SUPERSET_HEADERS), as production does: the suite
posts no password and sends the headers that tell Superset who is calling.

A local stand-in for Superset checks them, in both ways a Superset may read
them: once, at /login/, remembering the user in a session cookie (Superset's
stock AUTH_REMOTE_USER), or on every request.

Run: pytest -m unit
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest
import requests

from plugin.endpoints import ENDPOINTS
from suite.config import parse_superset_headers, unresolved_judge_settings

pytestmark = pytest.mark.unit

USER_HEADER = "X-Test-User"
GENERATE = ENDPOINTS["generate-sql"]


class _Superset(ThreadingHTTPServer):
    """Knows a caller by USER_HEADER: at /login/ only when `by_session`, and
    then by the cookie it hands out; on every request otherwise."""

    def __init__(self, by_session):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.by_session = by_session
        self.seen = []  # (method, path, the request's headers)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, status, body, cookie=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _user(self):
        if self.server.by_session:
            cookie = self.headers.get("Cookie", "")
            return cookie.partition("session=")[2] or None
        return self.headers.get(USER_HEADER)

    def _handle(self):
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/health":
            return self._reply(200, "OK")
        if self.path == "/login/":
            user = self.headers.get(USER_HEADER)
            cookie = f"session={user}; Path=/" if user and self.server.by_session else None
            return self._reply(200, {}, cookie=cookie)
        if not self._user():
            return self._reply(401, {"msg": "Missing Authorization Header"})
        if self.path == "/api/v1/security/csrf_token/":
            return self._reply(200, {"result": "csrf-token"})
        if self.headers.get("X-CSRFToken") != "csrf-token":
            return self._reply(400, {"msg": "The CSRF token is missing"})
        return self._reply(200, {"result": {"sqlText": f"-- asked by {self._user()}"}})

    do_GET = do_POST = _handle


@pytest.fixture(params=[True, False], ids=["session cookie", "every request"])
def superset(request):
    server = _Superset(by_session=request.param)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _client(make_superset_client, superset, **settings):
    settings.setdefault("auth", "headers")
    settings.setdefault("superset_headers", f"{USER_HEADER}=llm-eval-suite")
    return make_superset_client(superset_url=superset.url, **settings)


def test_headers_sign_the_suite_in_without_a_password(make_superset_client, superset):
    client = _client(make_superset_client, superset)
    assert client.health()
    assert client.call_plugin(GENERATE, "a question").output == "-- asked by llm-eval-suite"

    paths = [path for _, path, _ in superset.seen]
    assert "/api/v1/security/login" not in paths
    # Every request carries the header, the health check included, and none
    # carries a password login's token.
    for _, path, headers in superset.seen:
        assert headers.get(USER_HEADER) == "llm-eval-suite", path
        assert "Authorization" not in headers, path


def test_headers_superset_refuses_stop_the_run_with_the_setting_named(
    make_superset_client, superset
):
    client = _client(make_superset_client, superset, superset_headers="X-Wrong-Header=someone")
    with pytest.raises(PermissionError) as error:
        client.login()
    assert "SUPERSET_HEADERS" in str(error.value)
    assert "X-Wrong-Header" in str(error.value)


def test_password_sign_in_is_unchanged_and_can_carry_headers_too(make_superset_client):
    from unit.test_without_llm import _response

    session = mock.Mock(spec=requests.Session)
    session.headers = {}
    session.get.return_value = _response({"result": "csrf-token"})
    session.post.side_effect = [
        _response({"access_token": "jwt"}),
        _response({"result": {"sqlText": "SELECT 1"}}),
    ]
    client = make_superset_client(session=session, superset_headers="X-Team=evals")
    assert client.call_plugin(GENERATE, "q").output == "SELECT 1"
    assert "security/login" in session.post.call_args_list[0].args[0]
    assert session.headers["Authorization"] == "Bearer jwt"
    assert session.headers["X-Team"] == "evals"
    assert not any("/login/" in call.args[0] for call in session.get.call_args_list)


def test_superset_headers_are_parsed_and_checked():
    assert parse_superset_headers("") == {}
    assert parse_superset_headers("X-User=suite") == {"X-User": "suite"}
    # A value may hold commas: only a comma before another Name= splits.
    assert parse_superset_headers("X-User = suite , X-Groups=a,b, c,X-Mail=s@example.org") == {
        "X-User": "suite",
        "X-Groups": "a,b, c",
        "X-Mail": "s@example.org",
    }
    assert parse_superset_headers("Authorization=Bearer abc") == {"Authorization": "Bearer abc"}
    for bad in ("X-User", "=suite", "bad name=x", "X-User=", "X-User=caf\u00e9", "Cookie=a=b"):
        with pytest.raises(ValueError, match="SUPERSET_HEADERS"):
            parse_superset_headers(bad)
    # A password login sets Authorization itself.
    with pytest.raises(ValueError, match="already send"):
        parse_superset_headers("Authorization=Bearer abc", auth="password")


def test_a_wrong_sign_in_setting_is_named(make_superset_client):
    with pytest.raises(ValueError, match="SUPERSET_AUTH=headers needs SUPERSET_HEADERS"):
        make_superset_client(auth="headers")
    with pytest.raises(ValueError, match="SUPERSET_AUTH must be password or headers"):
        make_superset_client(auth="sso")


def test_placeholders_left_in_the_settings_are_named(unit_config):
    from dataclasses import replace

    assert unit_config.unresolved_placeholders() == []
    left = replace(
        unit_config,
        superset_url="<superset-address>",
        database_id="YOUR_DATABASE_ID",
        username="<username>",
        auth="headers",
        superset_headers="<header-name>=<suite-user>",
    )
    # The username isn't used when headers sign the suite in.
    assert left.unresolved_placeholders() == [
        "SUPERSET_URL", "SUPERSET_DATABASE_ID", "SUPERSET_HEADERS",
    ]
    assert replace(left, auth="password").unresolved_placeholders() == [
        "SUPERSET_URL", "SUPERSET_USERNAME", "SUPERSET_DATABASE_ID", "SUPERSET_HEADERS",
    ]


def test_judge_settings_left_unfilled_are_named_too():
    env = {
        "JUDGE_MODEL_NAME": "<model>",
        "JUDGE_BASE_URL": "https://<model-server>/v1",
        "JUDGE_HEADERS": "X-Superset-User-Name=llm-eval-suite",
        "JUDGE_CLIENT_CERT": "/certs/client.pem",
        "SUPERSET_URL": "<superset-address>",  # SuiteConfig names this one
    }
    assert unresolved_judge_settings(env) == ["JUDGE_BASE_URL", "JUDGE_MODEL_NAME"]
    assert unresolved_judge_settings({"JUDGE_MODEL_NAME": "qwen-32b"}) == []
