"""Unit tests for the judge over mutual TLS (judge/tls.py, judge/model.py).

A throwaway HTTPS server on this machine stands in for the model server. It
requires a client certificate, answers as an OpenAI-compatible server does,
and refuses any request carrying an Authorization header. Its certificates
are made with openssl when the tests run, and never saved in the repo; the
tests are skipped where openssl is missing.

Run: pytest -m unit
"""

import asyncio
import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import openai
import pytest

from judge import model as judge_model
from judge.model import JudgeConfigError, JudgeSettings, get_judge_model
from judge.second_opinion import SecondOpinionModel
from judge.tls import TlsSettings, parse_headers, ssl_context

pytestmark = pytest.mark.unit

needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="openssl isn't installed"
)


def _openssl(folder, *args):
    subprocess.run(
        ["openssl", *args], cwd=folder, check=True, capture_output=True
    )


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A CA, a server certificate for 127.0.0.1 and a client certificate, both
    signed by the CA, plus a second CA that signed neither."""
    folder = tmp_path_factory.mktemp("certs")
    for ca in ("ca", "other-ca"):
        _openssl(
            folder, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-keyout", f"{ca}.key", "-out", f"{ca}.pem", "-subj", f"/CN=test {ca}",
        )
    (folder / "server.ext").write_text("subjectAltName=IP:127.0.0.1,DNS:localhost\n")
    (folder / "client.ext").write_text("extendedKeyUsage=clientAuth\n")
    for name in ("server", "client"):
        _openssl(
            folder, "req", "-newkey", "rsa:2048", "-nodes",
            "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}",
        )
        _openssl(
            folder, "x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem",
            "-CAkey", "ca.key", "-CAcreateserial", "-days", "2",
            "-extfile", f"{name}.ext", "-out", f"{name}.pem",
        )
    return folder


class _ModelServer(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        # Header names in lower case: HTTP doesn't care, and clients differ.
        headers = {name.lower(): value for name, value in self.headers.items()}
        self.server.requests.append((self.path, headers, body))
        if "Authorization" in self.headers and not self.server.allow_key:
            return self._send(401, {"error": {"message": "no keys here"}})
        self._send(
            200,
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "judged"},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    def _send(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(certs):
    """The stand-in model server: `.url` is its /v1 address, `.requests` what
    it was sent. Set `.allow_key` to let an Authorization header through."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ModelServer)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    context.load_verify_locations(certs / "ca.pem")
    context.verify_mode = ssl.CERT_REQUIRED
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    httpd.requests = []
    httpd.allow_key = False
    httpd.url = f"https://127.0.0.1:{httpd.server_address[1]}/v1"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.fixture(autouse=True)
def no_key_in_the_environment(monkeypatch):
    """A key in the developer's own environment must not change these tests."""
    monkeypatch.setattr(
        judge_model, "get_settings", lambda: SimpleNamespace(OPENAI_API_KEY=None)
    )
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


def _env(server, certs, **overrides):
    env = {
        "JUDGE_PROVIDER": "openai",
        "JUDGE_MODEL_NAME": "judge-model",
        "JUDGE_BASE_URL": server.url,
        "JUDGE_CLIENT_CERT": str(certs / "client.pem"),
        "JUDGE_CLIENT_KEY": str(certs / "client.key"),
        "JUDGE_CA_BUNDLE": str(certs / "ca.pem"),
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value is not None}


def _ask(judge):
    """One request straight through the judge's own client, without DeepEval's
    retries, so a refused connection fails at once."""
    return judge.load_model().chat.completions.create(
        model=judge.name, messages=[{"role": "user", "content": "hello"}]
    )


@needs_openssl
def test_judge_reaches_a_server_that_requires_a_client_certificate_without_a_key(
    server, certs
):
    judge = get_judge_model(
        _env(server, certs, JUDGE_HEADERS="X-Superset-User-Name=llm eval suite")
    )

    answer, _ = judge.generate("Grade this.")
    async_answer, _ = asyncio.run(judge.a_generate("Grade this."))

    assert answer == async_answer == "judged"
    assert len(server.requests) == 2
    for path, headers, body in server.requests:
        assert path == "/v1/chat/completions"
        assert "authorization" not in headers
        assert headers["x-superset-user-name"] == "llm%20eval%20suite"
        assert body["model"] == "judge-model"
        assert not body.get("stream")


@needs_openssl
def test_second_opinion_judge_gets_the_same_certificate_and_no_key(server, certs):
    judge = get_judge_model(
        _env(server, certs, JUDGE_SECOND_OPINION_MODEL="second-model")
    )

    assert isinstance(judge, SecondOpinionModel)
    assert _ask(judge.second).choices[0].message.content == "judged"
    path, headers, body = server.requests[-1]
    assert body["model"] == "second-model"
    assert "authorization" not in headers


@needs_openssl
def test_judge_sends_a_key_when_it_has_one(server, certs):
    server.allow_key = True
    judge = get_judge_model(_env(server, certs, JUDGE_API_KEY="a-real-key"))

    _ask(judge)

    assert server.requests[-1][1]["authorization"] == "Bearer a-real-key"


@needs_openssl
@pytest.mark.parametrize(
    "overrides",
    [
        # No client certificate: the server ends the handshake.
        {"JUDGE_CLIENT_CERT": None, "JUDGE_CLIENT_KEY": None},
        # A CA that didn't sign the server's certificate.
        {"JUDGE_CA_BUNDLE": "other-ca.pem"},
    ],
)
def test_judge_without_the_right_certificates_is_refused(server, certs, overrides):
    if "JUDGE_CA_BUNDLE" in overrides:
        overrides = {"JUDGE_CA_BUNDLE": str(certs / overrides["JUDGE_CA_BUNDLE"])}
    judge = get_judge_model(_env(server, certs, **overrides))

    with pytest.raises(openai.APIConnectionError):
        _ask(judge)
    assert server.requests == []


@needs_openssl
def test_certificate_mistakes_name_the_setting(server, certs, tmp_path):
    def error(**overrides):
        with pytest.raises(JudgeConfigError) as raised:
            get_judge_model(_env(server, certs, **overrides))
        return str(raised.value)

    missing = str(tmp_path / "missing.pem")
    assert f"JUDGE_CLIENT_CERT: can't read {missing}" in error(JUDGE_CLIENT_CERT=missing)
    assert f"JUDGE_CA_BUNDLE: can't read {missing}" in error(JUDGE_CA_BUNDLE=missing)
    assert "JUDGE_CLIENT_KEY is set but JUDGE_CLIENT_CERT isn't" in error(
        JUDGE_CLIENT_CERT=None
    )
    # The CA's key isn't the client certificate's.
    assert "JUDGE_CLIENT_CERT: couldn't load" in error(
        JUDGE_CLIENT_KEY=str(certs / "ca.key")
    )
    not_pem = tmp_path / "not.pem"
    not_pem.write_text("not a certificate")
    assert "JUDGE_CA_BUNDLE" in error(JUDGE_CA_BUNDLE=str(not_pem))


def test_no_certificate_settings_leave_the_default_context():
    assert ssl_context(TlsSettings.from_env({})) is None
    settings = JudgeSettings.from_env(
        {"JUDGE_PROVIDER": "openai", "JUDGE_MODEL_NAME": "gpt-4.1"}
    )
    assert not settings.tls.configured and settings.headers == ()


def test_hosted_openai_still_needs_a_key():
    """Only a server named by JUDGE_BASE_URL is called without a key."""
    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        get_judge_model(
            {"JUDGE_PROVIDER": "openai", "JUDGE_MODEL_NAME": "gpt-4.1"}
        ).load_model()


def test_judge_headers_are_parsed_and_checked():
    assert parse_headers("") == ()
    assert parse_headers("X-User=suite, X-Team = José ") == (
        ("X-User", "suite"),
        ("X-Team", "Jos%C3%A9"),
    )
    for bad, why in [
        ("X-User", "isn't Name=value"),
        ("Bad Name=1", "not a valid HTTP header name"),
        ("authorization=Bearer x", "already send"),
    ]:
        with pytest.raises(JudgeConfigError, match=why):
            JudgeSettings.from_env(
                {"JUDGE_MODEL_NAME": "m", "JUDGE_HEADERS": bad}
            )
