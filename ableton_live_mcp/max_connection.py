"""Bounded, correlated loopback protocol for the optional official LiveAPI backend."""

import json
import os
import socket
import threading
import uuid

from .lom_effects import may_write

MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class MaxTransportError(RuntimeError):
    def __init__(self, message, write_may_have_occurred=False):
        super().__init__(message)
        self.write_may_have_occurred = write_may_have_occurred


class MaxConnection:
    """One ordered stream. No operation is retried after bytes may have been sent."""

    def __init__(self, port=None, timeout=10.0):
        self.port = int(port if port is not None else os.environ.get("ABLETON_MAX_PORT", "9878"))
        if not 1 <= self.port <= 65535:
            raise ValueError("ABLETON_MAX_PORT must be between 1 and 65535")
        self.timeout = timeout
        self._socket = None
        self._buffer = bytearray()
        self._lock = threading.Lock()

    def _close(self):
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        self._socket = None
        self._buffer.clear()

    def close(self):
        with self._lock:
            self._close()

    def request(self, payload):
        request_id = uuid.uuid4().hex
        frame = (
            json.dumps(
                {"id": request_id, "request": payload}, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            + b"\n"
        )
        if len(frame) > MAX_REQUEST_BYTES:
            raise ValueError("Max request exceeds 1 MiB")
        if not self._lock.acquire(timeout=self.timeout):
            raise MaxTransportError("Max backend is busy; no request was sent")
        attempted = False
        possible_write = may_write(payload)
        try:
            if self._socket is None:
                self._socket = socket.create_connection(("127.0.0.1", self.port), self.timeout)
                self._socket.settimeout(self.timeout)
            attempted = True
            self._socket.sendall(frame)
            while b"\n" not in self._buffer:
                chunk = self._socket.recv(65536)
                if not chunk:
                    raise OSError("Max backend closed before a complete response")
                self._buffer.extend(chunk)
                if len(self._buffer) > MAX_RESPONSE_BYTES:
                    raise ValueError("Max response exceeds 4 MiB")
            line, _, remaining = self._buffer.partition(b"\n")
            self._buffer = bytearray(remaining)
            response = json.loads(line)
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise ValueError("Max response correlation mismatch")
            result = response.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                raise ValueError("Max response has no typed result/status")
            return result
        except (OSError, ValueError, UnicodeError) as error:
            self._close()
            raise MaxTransportError(str(error), attempted and possible_write) from error
        finally:
            self._lock.release()


_connection = None


def get_max_connection():
    global _connection
    if _connection is None:
        _connection = MaxConnection()
    return _connection


def disconnect_max():
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
