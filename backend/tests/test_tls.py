"""Client certificates and CA bundles: the model calls made to an endpoint that wants mutual TLS.

Against a real HTTPS server that requires a client certificate signed by its own CA - both made
here with `openssl`, so no key sits in the repo - the LLM and embedding calls get through with the
certificate and the CA, and are turned away without either.
"""

import json
import os
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import requests
from flask import Flask

from waxwing.nltosqlandback import embedding_client, llm_client, tls
from waxwing.nltosqlandback.embedding_provider import EmbeddingProvider, resolve_embedding_provider
from waxwing.nltosqlandback.llm_provider import LlmProvider, resolve_llm_provider
from waxwing.nltosqlandback.tls import TlsSettings, tls_settings

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command to make certificates")

PASSWORD = "not-a-secret"


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def certs(tmp_path_factory) -> Path:
    """A CA; a server certificate for 127.0.0.1 signed by it; a client certificate signed by it, with
    its key plain and encrypted; and a second CA that signed nothing here."""
    d = tmp_path_factory.mktemp("certs")
    for ca in ("ca", "other-ca"):
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{ca}.key", "-out", f"{ca}.pem", "-days", "2", "-subj", f"/CN={ca}", cwd=d)
    (d / "server.ext").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    for name, ext in (("server", ["-extfile", "server.ext"]), ("client", [])):
        _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN=test-{name}", cwd=d)
        _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{name}.pem", "-days", "2", *ext, cwd=d)
    _openssl("pkey", "-in", "client.key", "-aes256", "-passout", f"pass:{PASSWORD}", "-out", "client-encrypted.key", cwd=d)
    (d / "client-and-key.pem").write_text((d / "client.pem").read_text() + (d / "client.key").read_text())
    return d


class _Handler(BaseHTTPRequestHandler):
    """Answers a completion or an embedding, noting who the client certificate said it was."""

    seen: list = []

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        subject = dict(x[0] for x in self.connection.getpeercert()["subject"])
        type(self).seen.append(subject["commonName"])
        if self.path.endswith("/embeddings"):
            body = {"data": [{"embedding": [0.5, 0.25]}], "usage": {}}
        else:
            body = {"choices": [{"message": {"content": "SELECT 1"}}]}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture(scope="module")
def endpoint(certs):
    """An HTTPS server on 127.0.0.1 that only talks to a client with a certificate from `ca.pem`."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH, cafile=str(certs / "ca.pem"))
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certs / "server.pem", certs / "server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"https://127.0.0.1:{server.server_port}/v1"
    server.shutdown()
    thread.join(timeout=2)


def _settings(certs, key="client.key", password="", cert="client.pem", ca="ca.pem") -> TlsSettings:
    return TlsSettings(
        client_cert=str(certs / cert) if cert else "",
        client_key=str(certs / key) if key else "",
        key_password=password,
        ca_bundle=str(certs / ca) if ca else "",
    )


def test_an_llm_call_gets_through_with_the_client_certificate(certs, endpoint):
    _Handler.seen = []
    provider = LlmProvider(model="m", base_url=endpoint, tls=_settings(certs))
    assert llm_client.complete(provider, "prompt") == "SELECT 1"
    assert _Handler.seen == ["test-client"]


def test_an_embedding_call_gets_through_with_an_encrypted_key(certs, endpoint):
    _Handler.seen = []
    provider = EmbeddingProvider(model="e", base_url=endpoint, tls=_settings(certs, key="client-encrypted.key", password=PASSWORD))
    assert embedding_client.embed_query(provider, "orders") == [0.5, 0.25]
    assert _Handler.seen == ["test-client"]


def test_a_certificate_file_can_hold_its_key_too(certs, endpoint):
    provider = LlmProvider(model="m", base_url=endpoint, tls=_settings(certs, cert="client-and-key.pem", key=""))
    assert llm_client.complete(provider, "prompt") == "SELECT 1"


def test_without_a_client_certificate_the_endpoint_turns_the_call_away(certs, endpoint):
    with pytest.raises((ssl.SSLError, ConnectionError, OSError)):
        llm_client.complete(LlmProvider(model="m", base_url=endpoint, tls=_settings(certs, cert="", key="")), "prompt")
    with pytest.raises(requests.exceptions.RequestException):
        embedding_client.embed_query(EmbeddingProvider(model="e", base_url=endpoint, tls=_settings(certs, cert="", key="")), "orders")


def test_without_the_ca_the_endpoint_is_not_trusted(certs, endpoint):
    with pytest.raises(ssl.SSLCertVerificationError):
        llm_client.complete(LlmProvider(model="m", base_url=endpoint, tls=_settings(certs, ca="other-ca.pem")), "prompt")
    with pytest.raises(requests.exceptions.SSLError):
        embedding_client.embed_query(EmbeddingProvider(model="e", base_url=endpoint, tls=_settings(certs, ca="")), "orders")


def test_a_wrong_password_or_a_missing_file_names_the_setting(certs):
    with pytest.raises(ValueError, match="NLSQL_LLM_CLIENT_CERT: couldn't load"):
        tls.ssl_context(_settings(certs, key="client-encrypted.key", password="wrong"))
    with pytest.raises(ValueError, match="NLSQL_LLM_CA_BUNDLE: can't read"):
        tls.ssl_context(_settings(certs, ca="nowhere.pem"))
    with pytest.raises(ValueError, match="NLSQL_LLM_CLIENT_KEY is set but NLSQL_LLM_CLIENT_CERT isn't"):
        tls.ssl_context(_settings(certs, cert=""))


def test_nothing_set_means_the_default_context(certs):
    assert tls.ssl_context(TlsSettings()) is None


def test_a_rotated_certificate_is_read_again(certs, tmp_path):
    for name in ("client.pem", "client.key", "ca.pem"):
        shutil.copy(certs / name, tmp_path / name)
    settings = _settings(tmp_path)
    first = tls.ssl_context(settings)
    assert tls.ssl_context(settings) is first
    stat = os.stat(tmp_path / "client.pem")
    os.utime(tmp_path / "client.pem", ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert tls.ssl_context(settings) is not first


def test_the_embedding_settings_fall_back_to_the_llm_ones(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        NLSQL_LLM_BASE_URL="https://llm.example/v1",
        NLSQL_EMBEDDING_BASE_URL="https://llm.example/v1",
        NLSQL_LLM_CLIENT_CERT="/certs/client.pem",
        NLSQL_LLM_CLIENT_KEY="/certs/client.key",
        NLSQL_LLM_CA_BUNDLE="/certs/ca.pem",
    )
    monkeypatch.setenv("NLSQL_LLM_CLIENT_KEY_PASSWORD", "from-the-environment")
    with app.app_context():
        llm = resolve_llm_provider().tls
        assert (llm.client_cert, llm.client_key, llm.key_password, llm.ca_bundle) == ("/certs/client.pem", "/certs/client.key", "from-the-environment", "/certs/ca.pem")
        assert resolve_embedding_provider().tls == llm
        app.config["NLSQL_EMBEDDING_CA_BUNDLE"] = "/certs/embedding-ca.pem"
        embedding = resolve_embedding_provider().tls
        assert (embedding.client_cert, embedding.ca_bundle, embedding.prefix) == ("/certs/client.pem", "/certs/embedding-ca.pem", "NLSQL_EMBEDDING")


def test_by_default_nothing_is_set():
    with Flask(__name__).app_context():
        assert not resolve_llm_provider().tls.configured
        assert not resolve_embedding_provider().tls.configured
    assert tls_settings({}, "NLSQL_LLM") == TlsSettings()
