import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from waxwing.nltosqlandback import llm_client
from waxwing.nltosqlandback.llm_client import Chunk
from waxwing.nltosqlandback.cancellation import CancelledError, CancelToken
from waxwing.nltosqlandback.llm_provider import LlmProvider


def _run_server(handler_cls):
    # Threading, not plain HTTPServer: a handler mid-`time.sleep` (the slow-response tests below)
    # must not stop a *different* test's server.shutdown() below from returning promptly - a
    # single-threaded server's accept loop can't notice shutdown() until that handler call returns.
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.shutdown()
    thread.join(timeout=2)


def _provider_for(server) -> LlmProvider:
    return LlmProvider(model="m", base_url=f"http://127.0.0.1:{server.server_port}", api_key="x")


class _EchoHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming convention
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"content": "SELECT 1"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # keep test output quiet
        pass


class _InBandErrorHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({"error": {"message": "boom"}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _sse_chunk(text: str) -> bytes:
    """One OpenAI-compatible `chat.completion.chunk` event carrying `text`."""
    return f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n".encode()


class _StreamingHandler(BaseHTTPRequestHandler):
    """Answers with a real SSE stream, flushed chunk by chunk as a provider would."""

    chunks = ("SELECT ", "1 FROM ", "orders")

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.request_body = json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(b": keep-alive\n\n")  # providers send these; they carry no content
        for chunk in self.chunks:
            self.wfile.write(_sse_chunk(chunk))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *_args):
        pass


class _StreamingErrorHandler(_StreamingHandler):
    """A router reporting an upstream failure part-way through an otherwise successful stream."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(_sse_chunk("SELECT "))
        self.wfile.write(b'data: {"error": {"message": "upstream is down"}}\n\n')
        self.wfile.flush()

    def log_message(self, *_args):
        pass


class _StallingStreamHandler(_StreamingHandler):
    """Sends one chunk, then stops - as a provider whose model has stalled would look."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(_sse_chunk("SELECT "))
        self.wfile.flush()
        time.sleep(5)  # long enough that the test's cancel() interrupts the read first

    def log_message(self, *_args):
        pass


class _SlowHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        time.sleep(5)  # long enough that the test's cancel() interrupts it first
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *_args):
        pass


def test_successful_call_returns_the_completion_text():
    server, thread = _run_server(_EchoHandler)
    try:
        assert llm_client.complete(_provider_for(server), "prompt") == "SELECT 1"
    finally:
        _stop_server(server, thread)


def test_an_in_band_error_from_the_provider_raises():
    server, thread = _run_server(_InBandErrorHandler)
    try:
        with pytest.raises(RuntimeError, match="boom"):
            llm_client.complete(_provider_for(server), "prompt")
    finally:
        _stop_server(server, thread)


def test_cancel_interrupts_an_in_flight_call():
    server, thread = _run_server(_SlowHandler)
    try:
        token = CancelToken()
        outcome: dict[str, BaseException] = {}

        def run():
            try:
                llm_client.complete(_provider_for(server), "prompt", cancel_token=token)
            except BaseException as ex:  # noqa: BLE001
                outcome["error"] = ex

        call_thread = threading.Thread(target=run)
        call_thread.start()
        time.sleep(0.3)  # let the call connect and register its socket first
        token.cancel()
        call_thread.join(timeout=5)

        assert not call_thread.is_alive()
        assert isinstance(outcome.get("error"), CancelledError)
    finally:
        _stop_server(server, thread)


def test_a_call_that_finishes_before_any_cancel_is_unaffected():
    server, thread = _run_server(_EchoHandler)
    try:
        token = CancelToken()
        assert llm_client.complete(_provider_for(server), "prompt", cancel_token=token) == "SELECT 1"
        assert token.cancelled is False
    finally:
        _stop_server(server, thread)


def test_a_streamed_response_is_yielded_chunk_by_chunk():
    server, thread = _run_server(_StreamingHandler)
    try:
        stream = llm_client.stream_complete(_provider_for(server), "prompt")
        chunks = []
        while True:
            try:
                chunks.append(next(stream))
            except StopIteration as stop:
                assert stop.value == "SELECT 1 FROM orders"
                break
        assert chunks == [Chunk("SELECT "), Chunk("1 FROM "), Chunk("orders")]
    finally:
        _stop_server(server, thread)


def test_streaming_is_asked_for_by_the_request_itself():
    captured = {}

    class _CaptureHandler(_StreamingHandler):
        def do_POST(self):  # noqa: N802
            super().do_POST()
            captured.update(self.request_body)

    server, thread = _run_server(_CaptureHandler)
    try:
        assert llm_client.complete(_provider_for(server), "prompt") == "SELECT 1 FROM orders"
        assert captured["stream"] is True
    finally:
        _stop_server(server, thread)


def test_an_error_reported_part_way_through_a_stream_raises():
    server, thread = _run_server(_StreamingErrorHandler)
    try:
        with pytest.raises(RuntimeError, match="upstream is down"):
            llm_client.complete(_provider_for(server), "prompt")
    finally:
        _stop_server(server, thread)


def test_a_provider_that_ignores_streaming_still_works():
    # `_EchoHandler` answers with one whole JSON body, as a provider that doesn't support
    # `stream: true` would - there is nothing to stream, but the completion is still usable.
    server, thread = _run_server(_EchoHandler)
    try:
        stream = llm_client.stream_complete(_provider_for(server), "prompt")
        assert list(_yields(stream)) == [Chunk("SELECT 1")]
    finally:
        _stop_server(server, thread)


def _yields(stream):
    """Everything `stream` yields, discarding what it returns."""
    while True:
        try:
            yield next(stream)
        except StopIteration:
            return


def test_cancel_interrupts_a_stream_that_has_already_started_arriving():
    server, thread = _run_server(_StallingStreamHandler)
    try:
        token = CancelToken()
        outcome: dict[str, object] = {}

        def run():
            try:
                outcome["chunks"] = list(_yields(llm_client.stream_complete(_provider_for(server), "p", token)))
            except BaseException as ex:  # noqa: BLE001
                outcome["error"] = ex

        call_thread = threading.Thread(target=run)
        call_thread.start()
        time.sleep(0.5)  # let the first chunk arrive, so the call is mid-stream rather than waiting
        token.cancel()
        call_thread.join(timeout=5)

        assert not call_thread.is_alive()
        assert isinstance(outcome.get("error"), CancelledError)
    finally:
        _stop_server(server, thread)


class _ReasoningHandler(_StreamingHandler):
    """A reasoning model as LM Studio streams one: its thinking on `reasoning_content` first, then
    the answer on `content`."""

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for thought in ("The users table ", "has a has_2fa column."):
            payload = {"choices": [{"delta": {"reasoning_content": thought}}]}
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.write(_sse_chunk("SELECT 1"))
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *_args):
        pass


def test_a_models_thinking_is_streamed_separately_from_its_answer():
    server, thread = _run_server(_ReasoningHandler)
    try:
        stream = llm_client.stream_complete(_provider_for(server), "prompt")
        chunks = list(_yields(stream))
        assert [chunk for chunk in chunks if chunk.reasoning] == [
            Chunk("The users table ", reasoning=True),
            Chunk("has a has_2fa column.", reasoning=True),
        ]
        assert [chunk for chunk in chunks if not chunk.reasoning] == [Chunk("SELECT 1")]
    finally:
        _stop_server(server, thread)


def test_the_completion_is_the_answer_alone_with_the_thinking_left_out():
    # It is about to be parsed as JSON by the caller, so a stray paragraph of reasoning in front of
    # it would break the call outright.
    server, thread = _run_server(_ReasoningHandler)
    try:
        assert llm_client.complete(_provider_for(server), "prompt") == "SELECT 1"
    finally:
        _stop_server(server, thread)


def test_thinking_reported_on_a_non_streamed_response_is_still_separated():
    class _NonStreamedReasoningHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            body = json.dumps({
                "choices": [{"message": {"content": "SELECT 1", "reasoning_content": "thinking out loud"}}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server, thread = _run_server(_NonStreamedReasoningHandler)
    try:
        stream = llm_client.stream_complete(_provider_for(server), "prompt")
        assert list(_yields(stream)) == [Chunk("thinking out loud", reasoning=True), Chunk("SELECT 1")]
    finally:
        _stop_server(server, thread)
