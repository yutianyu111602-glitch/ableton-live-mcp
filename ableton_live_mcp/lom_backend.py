"""Route an allowlisted LOM request, preserving backend-bound identity on writes."""

import re
import uuid

from .connection import get_ableton_connection
from .lom_effects import is_mutating_request, may_write
from .max_connection import MaxTransportError, disconnect_max, get_max_connection

OWNER = uuid.uuid4().hex
_used = set()


def _send(backend, payload):
    if backend == "max":
        try:
            result = get_max_connection().request(payload)
            if payload.get("op") == "observe" and result.get("subscription_id"):
                _used.add(backend)
            return result
        except MaxTransportError as error:
            return {
                "status": "error",
                "backend": "max",
                "reason": "transport_unavailable",
                "detail": str(error),
                "write_may_have_occurred": error.write_may_have_occurred,
                "action": (
                    "Load the LOM API Bridge companion in an independent test Set; "
                    "do not retry an uncertain write."
                ),
            }
    try:
        result = get_ableton_connection().send_command("lom_request", payload)
        if payload.get("op") == "observe" and result.get("subscription_id"):
            _used.add(backend)
        return result
    except Exception as error:
        # Legacy command errors do not carry transport certainty. Be conservative
        # for any actual mutation; a readback must precede a retry.
        return {
            "status": "error",
            "backend": "python",
            "reason": "transport_unavailable",
            "detail": str(error),
            "write_may_have_occurred": may_write(payload),
            "action": "Install the matching Remote Script; read back before retrying.",
        }


def dispatch(payload, backend="auto", mutating=None):
    if backend not in {"auto", "python", "max"}:
        raise ValueError("backend must be auto, python, or max")
    payload = {**payload, "owner": OWNER}
    identity = payload.get("expected_identity") or payload.get("subscription_id") or ""
    bound = identity.split(":", 1)[0] if ":" in identity else None
    if bound in {"python", "max"}:
        if backend != "auto" and backend != bound:
            raise ValueError("Identity belongs to another backend; resolve the target again")
        return _send(bound, payload)
    mutates = is_mutating_request(payload)
    mutation = mutates and not payload.get("dry_run", True)
    if mutation and not payload.get("expected_identity"):
        raise ValueError("An actual mutation requires expected_identity from lom_resolve")
    if mutation and backend == "auto":
        raise ValueError("An actual mutation requires a backend-bound identity")
    if backend != "auto":
        return _send(backend, payload)
    native_id = re.fullmatch(r"id (-?[1-9][0-9]*)", payload.get("path", ""))
    if native_id:
        # Numeric Live IDs (including negative Python-ControlSurface IDs) come
        # from Max. The Python transport uses only its own opaque scoped IDs.
        return _send("max", payload)
    first = _send("python", payload)
    if first.get("write_may_have_occurred"):
        return first
    if first.get("status") not in {"unsupported", "error"}:
        return first
    # Only a preview/read without uncertain effects may select another backend.
    if first.get("status") == "error" and first.get("reason") != "transport_unavailable":
        return first
    second = _send("max", payload)
    if second.get("status") in {"error", "unsupported"}:
        return {**second, "backend_attempts": [first, second]}
    return second


def cleanup():
    for backend in tuple(_used):
        try:
            _send(backend, {"op": "cleanup", "owner": OWNER})
        except Exception:
            pass
    disconnect_max()
    _used.clear()
