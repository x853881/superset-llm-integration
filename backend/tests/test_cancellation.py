import socket

from waxwing.nltosqlandback import cancellation
from waxwing.nltosqlandback.cancellation import CancelToken


def _connected_pair() -> tuple[socket.socket, socket.socket]:
    return socket.socketpair()


def test_cancel_shuts_down_a_registered_socket():
    token = CancelToken()
    sock, peer = _connected_pair()
    try:
        token.register(sock)
        token.cancel()
        assert peer.recv(1) == b""  # shut-down socket's peer sees EOF
    finally:
        sock.close()
        peer.close()


def test_cancel_before_register_shuts_the_socket_down_immediately():
    token = CancelToken()
    sock, peer = _connected_pair()
    try:
        token.cancel()
        token.register(sock)
        assert peer.recv(1) == b""
    finally:
        sock.close()
        peer.close()


def test_clear_after_settling_makes_a_later_cancel_a_no_op():
    token = CancelToken()
    sock, peer = _connected_pair()
    try:
        token.register(sock)
        token.clear()
        token.cancel()
        sock.sendall(b"x")
        assert peer.recv(1) == b"x"  # still usable - never shut down
    finally:
        sock.close()
        peer.close()


def test_cancelled_property_reflects_whether_cancel_was_called():
    token = CancelToken()
    assert token.cancelled is False
    token.cancel()
    assert token.cancelled is True


def test_registry_create_cancel_and_release():
    token = cancellation.create("req-1")
    assert cancellation.cancel("req-1") is True
    assert token.cancelled is True
    cancellation.release("req-1")
    assert cancellation.cancel("req-1") is False


def test_cancelling_an_unknown_request_id_is_a_no_op():
    assert cancellation.cancel("does-not-exist") is False


def test_create_replaces_a_stale_token_under_the_same_id():
    stale = cancellation.create("req-2")
    fresh = cancellation.create("req-2")
    cancellation.cancel("req-2")
    assert fresh.cancelled is True
    assert stale.cancelled is False
    cancellation.release("req-2")
