"""Every LLM call is recorded - with the Superset user, the token counts the provider reported, and
(when configured) as spans sent to Phoenix."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from flask import Flask

from waxwing.nltosqlandback import llm_client, otlp, request_context, telemetry
from waxwing.nltosqlandback.llm_provider import LlmProvider
from waxwing.nltosqlandback.request_context import CallContext


def _serve(handler_cls):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _UsageStreamHandler(BaseHTTPRequestHandler):
    """A stream whose last chunk carries usage, as OpenAI-compatible servers send it when asked."""

    seen_bodies: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).seen_bodies.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for text in ("SELECT ", "1"):
            chunk = {"id": "gen-1", "model": "served-model", "choices": [{"delta": {"content": text}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        final = {
            "id": "gen-1",
            "model": "served-model",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 7,
                "total_tokens": 127,
                "completion_tokens_details": {"reasoning_tokens": 3},
                "prompt_tokens_details": {"cached_tokens": 100},
                "cost": 0.00042,
            },
        }
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *_args):
        pass


def _context(**overrides):
    values = dict(endpoint="refine_sql", user_id=42, username="alice", request_id="req-1", database_id=3)
    values.update(overrides)
    return CallContext(**values)


def test_a_call_is_logged_with_the_user_and_the_token_counts_the_provider_reported(caplog):
    server = _serve(_UsageStreamHandler)
    provider = LlmProvider(model="asked-model", base_url=f"http://127.0.0.1:{server.server_port}")
    try:
        with caplog.at_level(logging.INFO, logger="waxwing.nltosqlandback.llm_calls"), \
                request_context.bound(_context()):
            text = llm_client.complete(provider, "prompt text", purpose="refine_sql")
    finally:
        server.shutdown()
    assert text == "SELECT 1"
    assert _UsageStreamHandler.seen_bodies[-1]["stream_options"] == {"include_usage": True}
    lines = [record.getMessage() for record in caplog.records if record.getMessage().startswith("llm_call ")]
    assert len(lines) == 1
    logged = json.loads(lines[0][len("llm_call "):])
    assert logged["user.id"] == 42 and logged["user.name"] == "alice"
    assert logged["requestId"] == "req-1" and logged["databaseId"] == 3 and logged["endpoint"] == "refine_sql"
    assert logged["gen_ai.usage.input_tokens"] == 120
    assert logged["gen_ai.usage.output_tokens"] == 7
    assert logged["totalTokens"] == 127
    assert logged["reasoningTokens"] == 3
    assert logged["cachedInputTokens"] == 100
    assert logged["costUsd"] == 0.00042
    assert logged["gen_ai.request.model"] == "asked-model"
    assert logged["gen_ai.response.model"] == "served-model"
    assert logged["gen_ai.response.finish_reasons"] == ["stop"]
    assert logged["outcome"] == "ok"
    assert "timeToFirstTokenMs" in logged
    # Prompt text stays out of the log line unless asked for.
    assert "prompt" not in logged


def test_a_failed_call_is_logged_too(caplog):
    provider = LlmProvider(model="m", base_url="http://127.0.0.1:9")  # nothing listens on the discard port
    with caplog.at_level(logging.INFO, logger="waxwing.nltosqlandback.llm_calls"), request_context.bound(_context()):
        try:
            llm_client.complete(provider, "p")
        except Exception:  # noqa: BLE001
            pass
    logged = [json.loads(r.getMessage()[len("llm_call "):]) for r in caplog.records if r.getMessage().startswith("llm_call ")]
    assert logged and logged[0]["outcome"] == "error" and logged[0]["user.id"] == 42


def test_usage_can_be_left_out_of_the_request_for_servers_that_reject_it():
    server = _serve(_UsageStreamHandler)
    provider = LlmProvider(model="m", base_url=f"http://127.0.0.1:{server.server_port}", request_usage=False)
    try:
        llm_client.complete(provider, "p")
    finally:
        server.shutdown()
    assert "stream_options" not in _UsageStreamHandler.seen_bodies[-1]


def test_log_lines_go_to_a_jsonl_file_when_configured(tmp_path):
    app = Flask(__name__)
    path = tmp_path / "llm.jsonl"
    app.config["NLSQL_LLM_LOG_FILE"] = str(path)
    app.config["NLSQL_LLM_LOG_PROMPTS"] = True
    record = telemetry.LlmCallRecord(
        operation="chat", request_model="m", base_url="https://openrouter.ai/api/v1",
        start_ns=1, end_ns=2_000_001, outcome="ok", input_tokens=5, prompt="hello", response="world",
    )
    with app.app_context(), request_context.bound(_context()):
        telemetry.record_llm_call(record)
    line = json.loads(path.read_text().strip())
    assert line["kind"] == "llm_call"
    assert line["user.id"] == 42
    assert line["gen_ai.system"] == "openrouter"
    assert line["prompt"] == "hello" and line["response"] == "world"
    assert line["durationMs"] == 2


class _CollectorHandler(BaseHTTPRequestHandler):
    received: list = []
    event = threading.Event()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        type(self).received.append((self.path, self.headers.get("Content-Type"), self.headers.get("Authorization"), self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()
        type(self).event.set()

    def log_message(self, *_args):
        pass


def _fields(message: bytes) -> dict:
    out: dict = {}
    for number, _wire, value in otlp.decode(message):
        out.setdefault(number, []).append(value)
    return out


def _attributes(span_bytes: bytes) -> dict:
    attributes = {}
    for key_value in _fields(span_bytes).get(9, []):
        kv = _fields(key_value)
        key = kv[1][0].decode()
        any_value = otlp.decode(kv[2][0])[0]
        number, _wire, raw = any_value
        attributes[key] = raw.decode() if number == 1 else raw
    return attributes


def test_spans_are_sent_to_phoenix_as_otlp_protobuf_under_the_request_trace():
    collector = _serve(_CollectorHandler)
    app = Flask(__name__)
    app.config["NLSQL_PHOENIX_ENDPOINT"] = f"http://127.0.0.1:{collector.server_port}"
    app.config["NLSQL_PHOENIX_API_KEY"] = "secret"
    app.config["NLSQL_LLM_LOG_ENABLED"] = False
    context = _context()
    context.attributes["tabId"] = "tab-7"
    record = telemetry.LlmCallRecord(
        operation="chat", request_model="m", base_url="http://localhost:11434/v1",
        start_ns=1_000, end_ns=5_000, outcome="ok", purpose="draft", input_tokens=9, output_tokens=4,
        prompt="the prompt", response="the answer",
    )
    try:
        with app.app_context(), request_context.bound(context):
            telemetry.record_llm_call(record)
        assert _CollectorHandler.event.wait(10)
    finally:
        collector.shutdown()
    path, content_type, authorization, body = _CollectorHandler.received[0]
    assert path == "/v1/traces"
    assert content_type == "application/x-protobuf"
    assert authorization == "Bearer secret"
    resource_spans = _fields(_fields(body)[1][0])
    scope_spans = _fields(resource_spans[2][0])
    span = _fields(scope_spans[2][0])
    assert span[1][0].hex() == context.trace_id
    assert span[4][0].hex() == context.span_id  # a child of the request's own span
    attributes = _attributes(scope_spans[2][0])
    assert attributes["openinference.span.kind"] == "LLM"
    assert attributes["user.id"] == "42"
    assert attributes["session.id"] == "tab-7"
    assert attributes["llm.token_count.prompt"] == 9
    assert attributes["llm.provider"] == "ollama"
    assert attributes["llm.input_messages.0.message.content"] == "the prompt"
    assert attributes["output.value"] == "the answer"


def test_nothing_is_sent_or_raised_without_a_request_context():
    with patch.object(telemetry, "_exporter_for") as exporter:
        telemetry.record_llm_call(telemetry.LlmCallRecord(
            operation="chat", request_model="m", base_url="x", start_ns=0, end_ns=1, outcome="ok",
        ))
    exporter.assert_not_called()


def test_feedback_is_logged_at_its_own_level_and_sent_into_the_rated_request_s_trace(caplog):
    _CollectorHandler.received.clear()
    _CollectorHandler.event.clear()
    collector = _serve(_CollectorHandler)
    app = Flask(__name__)
    app.config["NLSQL_PHOENIX_ENDPOINT"] = f"http://127.0.0.1:{collector.server_port}"
    feedback = telemetry.Feedback(
        trace_id="ab" * 16, span_id="cd" * 8, feedback_id="f1", rating="down", target="sql",
        reason="wrong-tables", label="Wrong tables or columns", variant={"layout": "flow"}, user_id=42, username="alice",
    )
    try:
        with app.app_context(), caplog.at_level(logging.INFO, logger="waxwing.nltosqlandback.llm_calls"):
            telemetry.record_feedback(feedback)
        assert _CollectorHandler.event.wait(10)
    finally:
        collector.shutdown()
    [log_record] = [r for r in caplog.records if r.getMessage().startswith("feedback ")]
    assert log_record.levelname == "FEEDBACK" and logging.INFO < log_record.levelno < logging.WARNING
    line = json.loads(log_record.getMessage()[len("feedback "):])
    assert line["rating"] == "down" and line["traceId"] == "ab" * 16 and line["user.id"] == 42
    body = _CollectorHandler.received[0][3]
    scope_spans = _fields(_fields(_fields(body)[1][0])[2][0])
    span = _fields(scope_spans[2][0])
    assert span[1][0].hex() == "ab" * 16  # in the rated request's trace...
    assert span[4][0].hex() == "cd" * 8  # ...under its span
    assert span[5][0].decode() == "feedback · down · Wrong tables or columns"
    attributes = _attributes(scope_spans[2][0])
    assert attributes["feedback.rating"] == "down" and attributes["feedback.reason"] == "wrong-tables"
    assert attributes["user.name"] == "alice"


def test_a_feedback_span_is_named_by_its_comment_when_there_is_no_offered_reason():
    feedback = telemetry.Feedback(trace_id="a" * 32, span_id="b" * 16, feedback_id="f", rating="down", target="fix",
                                  comment="It   quietly dropped the NULL rows from the join, which matters a lot here")
    assert telemetry.feedback_span_name(feedback) == "feedback · down · It quietly dropped the NULL rows from the join, which matte…"
    assert telemetry.feedback_span_name(telemetry.Feedback("a" * 32, "b" * 16, "f", "up", "fix")) == "feedback · up"
