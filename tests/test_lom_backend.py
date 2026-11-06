"""Backend selection must never turn an uncertain write into a second write."""

import pytest

from ableton_live_mcp import lom_backend as backend


def test_identity_binds_the_backend_even_when_auto(monkeypatch):
    calls = []
    monkeypatch.setattr(backend, "_send", lambda b, p: calls.append((b, p)) or {"status": "ok"})
    backend.dispatch({"op": "set", "expected_identity": "max:scope:2", "dry_run": False})
    assert [b for b, _ in calls] == ["max"]
    assert calls[0][1]["owner"] == backend.OWNER
    with pytest.raises(ValueError, match="another backend"):
        backend.dispatch({"op": "set", "expected_identity": "max:scope:2"}, "python")


def test_unbound_actual_write_never_attempts_a_backend(monkeypatch):
    monkeypatch.setattr(backend, "_send", lambda *_: pytest.fail("must not send"))
    with pytest.raises(ValueError, match="expected_identity"):
        backend.dispatch({"op": "set", "dry_run": False})
    with pytest.raises(ValueError, match="backend-bound"):
        backend.dispatch({"op": "set", "dry_run": False, "expected_identity": "unbound"})


@pytest.mark.parametrize(
    "first",
    [
        {"status": "partial", "write_may_have_occurred": True},
        {"status": "error", "write_may_have_occurred": True},
        {"status": "error", "reason": "invalid_value", "write_may_have_occurred": False},
    ],
)
def test_uncertain_or_validation_failure_is_never_rerouted(monkeypatch, first):
    calls = []
    monkeypatch.setattr(backend, "_send", lambda b, p: calls.append(b) or first)
    assert backend.dispatch({"op": "call", "dry_run": True}) == first
    assert calls == ["python"]


def test_explicit_unsupported_read_can_select_official_backend(monkeypatch):
    calls = []

    def send(name, _):
        calls.append(name)
        return {"status": "unsupported"} if name == "python" else {"status": "ok", "value": 1}

    monkeypatch.setattr(backend, "_send", send)
    assert backend.dispatch({"op": "get"})["value"] == 1
    assert calls == ["python", "max"]


@pytest.mark.parametrize("native_id", ["1", "-1", "-2"])
def test_numeric_native_id_routes_directly_to_max_without_wrong_pool_probe(monkeypatch, native_id):
    calls = []
    monkeypatch.setattr(
        backend, "_send", lambda name, request: calls.append(name) or {"status": "ok"}
    )
    backend.dispatch({"op": "resolve", "path": "id " + native_id})
    assert calls == ["max"]
