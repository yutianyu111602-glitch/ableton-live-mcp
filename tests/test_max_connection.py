"""Exercise the actual Max wire framing against isolated loopback fixtures."""

import json
import socket
import threading

import pytest

from ableton_live_mcp import max_connection as module
from ableton_live_mcp.max_connection import MaxConnection, MaxTransportError


def server(reply):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    calls = []

    def serve():
        with listener:
            client, _ = listener.accept()
            with client:
                raw = bytearray()
                while b"\n" not in raw:
                    raw.extend(client.recv(1024))
                request = json.loads(raw.split(b"\n", 1)[0])
                calls.append(request)
                for chunk in reply(request):
                    client.sendall(chunk)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return port, thread, calls


def test_fragmented_correlated_response_and_closed_connection():
    def reply(request):
        raw = (
            json.dumps({"id": request["id"], "result": {"status": "ok", "value": 12}}) + "\n"
        ).encode()
        return [raw[:8], raw[8:]]

    port, thread, calls = server(reply)
    connection = MaxConnection(port=port)
    assert connection.request({"op": "get"}) == {"status": "ok", "value": 12}
    connection.close()
    thread.join(2)
    assert len(calls) == 1


def test_uncertain_write_is_not_retried_on_correlation_failure():
    port, thread, calls = server(lambda _: [b'{"id":"wrong","result":{"status":"ok"}}\n'])
    connection = MaxConnection(port=port)
    with pytest.raises(MaxTransportError) as error:
        connection.request({"op": "set", "dry_run": False})
    assert error.value.write_may_have_occurred is True
    assert connection._socket is None
    thread.join(2)
    assert len(calls) == 1


def test_response_bounds_close_stream(monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 64)
    port, thread, _ = server(lambda _: [b"x" * 80 + b"\n"])
    connection = MaxConnection(port=port)
    with pytest.raises(MaxTransportError, match="4 MiB"):
        connection.request({"op": "get"})
    thread.join(2)
    assert connection._socket is None


def test_invalid_port_and_nonfinite_request_are_rejected_without_connection():
    with pytest.raises(ValueError):
        MaxConnection(port=65536)
    connection = MaxConnection(port=1)
    with pytest.raises(ValueError):
        connection.request({"op": "set", "value": float("nan")})
    assert connection._socket is None
