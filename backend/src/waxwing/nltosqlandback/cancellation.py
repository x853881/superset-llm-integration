"""Cooperative cancellation for in-flight outbound HTTP calls to the LLM provider.

`requests`/`http.client` give no native way to interrupt a call already blocked waiting on the
server. `llm_client`'s own wait for a response is what actually makes that interruptible - it
polls in short intervals rather than blocking on a single read, checking `CancelToken.cancelled`
between polls (shutting a socket down from a thread other than the one already blocked reading it
does not reliably wake that read on every platform, so this doesn't rely on that as its mechanism).
`CancelToken` here additionally holds the call's raw socket so `cancel()` can forcibly shut it down
too - a backstop for the (normally sub-second) connect/send phase, which the polling wait doesn't
cover.

Tokens are registered here by a client-supplied request id so the `/cancel` endpoint - handled on
a different request thread than the one running the call - can find the right one.
"""

from __future__ import annotations

import socket
import threading
from typing import Optional


class CancelledError(Exception):
    """Raised by `llm_client.complete` in place of whatever lower-level error a forced disconnect
    produced, once confirmed the call was actually cancelled rather than having failed on its own."""


class CancelToken:
    """One per in-flight request. `register`/`clear` bracket the call it protects; `cancel` may be
    called concurrently, from the `/cancel` endpoint's own request thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._cancelled = False

    def register(self, sock: socket.socket) -> None:
        """Called once the call's socket is connected. If `cancel()` already ran (a race between
        connecting and registering), shuts this socket down immediately rather than letting the
        request proceed as if nothing had happened."""
        with self._lock:
            if self._cancelled:
                shut_down = True
            else:
                self._sock = sock
                shut_down = False
        if shut_down:
            self._shutdown(sock)

    def clear(self) -> None:
        """Called once the call has settled (success or failure) - a `cancel()` afterward should
        have nothing left to do."""
        with self._lock:
            self._sock = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            sock = self._sock
        if sock is not None:
            self._shutdown(sock)

    @staticmethod
    def _shutdown(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # already closed (or never fully connected) - nothing left to interrupt


_lock = threading.Lock()
_tokens: dict[str, CancelToken] = {}


def create(request_id: str) -> CancelToken:
    """Registers a new `CancelToken` for `request_id`, replacing any previous one under the same
    id - a stale id a caller forgot to release shouldn't block a new request from reusing it."""
    token = CancelToken()
    with _lock:
        _tokens[request_id] = token
    return token


def release(request_id: str) -> None:
    with _lock:
        _tokens.pop(request_id, None)


def cancel(request_id: str) -> bool:
    """Cancels the call registered under `request_id`, if one is still in flight. Returns whether
    one was found - the frontend calls this best-effort, without knowing whether the call it's
    cancelling has already finished, so a miss is not an error."""
    with _lock:
        token = _tokens.get(request_id)
    if token is None:
        return False
    token.cancel()
    return True
