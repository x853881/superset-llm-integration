"""`NLSQL_LLM_USER_HEADER`: the Superset user sent to the LLM and embedding endpoints as a header."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch
from urllib.parse import unquote

from flask import Flask

from waxwing.nltosqlandback import embedding_client, llm_client, request_context
from waxwing.nltosqlandback.embedding_provider import EmbeddingProvider, resolve_embedding_provider
from waxwing.nltosqlandback.llm_provider import LlmProvider, resolve_llm_provider
from waxwing.nltosqlandback.request_context import CallContext

HEADER = "X-Superset-User-Name"


class _CaptureHandler(BaseHTTPRequestHandler):
    """Answers one non-streamed completion, keeping the request's headers in `seen`."""

    seen: dict = {}

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        type(self).seen = dict(self.headers.items())
        body = json.dumps({"choices": [{"message": {"content": "SELECT 1"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _headers_of_a_call(username, user_header=HEADER, api_key="x") -> dict:
    """The headers one LLM call arrives with, made for `username` (None: for no one)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = LlmProvider(
        model="m", base_url=f"http://127.0.0.1:{server.server_port}", api_key=api_key, user_header=user_header
    )
    try:
        with request_context.bound(CallContext(endpoint="generate_sql", username=username)):
            assert llm_client.complete(provider, "prompt") == "SELECT 1"
        return _CaptureHandler.seen
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_plain_ascii_names_are_sent_as_they_are():
    assert request_context.header_value("admin") == "admin"
    assert request_context.header_value("jane.doe@example.com") == "jane.doe@example.com"
    assert request_context.header_value("DOMAIN\\jdoe") == "DOMAIN\\jdoe"


def test_anything_else_is_percent_encoded_so_it_always_decodes_back():
    for name, sent in [
        ("José", "Jos%C3%A9"),
        ("Jane Doe", "Jane%20Doe"),
        ("100%", "100%25"),
        ("李雷", "%E6%9D%8E%E9%9B%B7"),
        ("evil\r\nX-Other: 1", "evil%0D%0AX-Other:%201"),
    ]:
        assert request_context.header_value(name) == sent
        assert unquote(sent) == name


def test_the_header_goes_only_with_a_call_made_for_someone():
    with request_context.bound(CallContext(endpoint="generate_sql", username="José")):
        assert request_context.user_headers(HEADER) == {HEADER: "Jos%C3%A9"}
        assert request_context.user_headers("") == {}
    with request_context.bound(CallContext(endpoint="generate_sql", username=None)):
        assert request_context.user_headers(HEADER) == {}
    assert request_context.user_headers(HEADER) == {}  # outside a request altogether


def test_an_llm_call_carries_the_user_when_configured():
    headers = _headers_of_a_call("José")
    assert headers[HEADER] == "Jos%C3%A9"
    assert headers["Authorization"] == "Bearer x"


def test_an_llm_call_carries_no_user_header_by_default():
    headers = _headers_of_a_call("admin", user_header="")
    assert HEADER not in headers


def test_a_user_header_named_like_the_credential_cannot_replace_it():
    headers = _headers_of_a_call("admin", user_header="Authorization")
    assert headers["Authorization"] == "Bearer x"


def test_an_embedding_call_carries_the_user_too():
    provider = EmbeddingProvider(model="e", base_url="http://embed/v1", api_key="k", user_header=HEADER)
    response = MagicMock()
    response.json.return_value = {"data": [{"embedding": [0.1, 0.2]}], "usage": {}}
    with patch.object(embedding_client.requests, "post", return_value=response) as post, request_context.bound(
        CallContext(endpoint="generate_sql", username="José")
    ):
        assert embedding_client.embed_query(provider, "orders") == [0.1, 0.2]
    headers = post.call_args.kwargs["headers"]
    assert headers == {HEADER: "Jos%C3%A9", "Authorization": "Bearer k"}


def test_one_setting_configures_both_endpoints():
    app = Flask(__name__)
    app.config["NLSQL_LLM_USER_HEADER"] = f"  {HEADER} "
    with app.app_context():
        assert resolve_llm_provider().user_header == HEADER
        assert resolve_embedding_provider().user_header == HEADER


def test_unset_means_off():
    with Flask(__name__).app_context():
        assert resolve_llm_provider().user_header == ""
        assert resolve_embedding_provider().user_header == ""


def test_a_name_http_would_refuse_is_left_off_and_logged_once(caplog):
    app = Flask(__name__)
    app.config["NLSQL_LLM_USER_HEADER"] = "X Superset User"
    with app.app_context(), caplog.at_level(logging.WARNING):
        assert resolve_llm_provider().user_header == ""
        assert resolve_embedding_provider().user_header == ""
    assert [record.getMessage() for record in caplog.records].count(
        "NLSQL_LLM_USER_HEADER 'X Superset User' is not a valid HTTP header name; the user is not being sent"
    ) == 1


def test_a_header_the_calls_set_themselves_is_refused(caplog):
    app = Flask(__name__)
    app.config["NLSQL_LLM_USER_HEADER"] = "authorization"
    with app.app_context(), caplog.at_level(logging.WARNING):
        assert resolve_llm_provider().user_header == ""
    assert "NLSQL_LLM_USER_HEADER 'authorization' is a header the calls already send; the user is not being sent" in [
        record.getMessage() for record in caplog.records
    ]
