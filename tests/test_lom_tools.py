"""Front-end and backend routing tests with fake dispatch; no Live connection."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from ableton_live_mcp import lom_backend as backend
from ableton_live_mcp.app import mcp
from ableton_live_mcp.lom_catalog import LomValidationError
from ableton_live_mcp.max_connection import MaxConnection, MaxTransportError
from ableton_live_mcp.tools import lom

TOOL_NAMES = {
    "lom_describe",
    "lom_resolve",
    "lom_get",
    "lom_set",
    "lom_call",
    "lom_observe",
    "lom_poll",
    "lom_unobserve",
    "lom_capabilities",
}


@pytest.fixture
def fake_dispatch(monkeypatch):
    calls = []

    def send(payload, selected="auto", **kwargs):
        calls.append((payload, selected, kwargs))
        return {"status": "fake", "request": payload, "backend": selected}

    monkeypatch.setattr(lom, "dispatch", send)
    return calls


def object_type(name):
    return lom.ObjectType(name)


def tool_definitions():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools()) if tool.name in TOOL_NAMES}


def test_nine_registered_tools_expose_exact_45_type_enum_and_safe_annotations():
    tools = tool_definitions()
    assert set(tools) == TOOL_NAMES
    assert {member.value for member in lom.ObjectType} == set(lom.CATALOG.objects)
    assert len(lom.ObjectType) == 45
    schema = tools["lom_resolve"].inputSchema
    enum = schema["$defs"]["LomObjectType"]["enum"]
    assert set(enum) == set(lom.CATALOG.objects)
    for name in ("lom_describe", "lom_resolve", "lom_get", "lom_capabilities"):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in ("lom_set", "lom_call"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True
        assert tools[name].inputSchema["properties"]["dry_run"]["default"] is True
    for name in ("lom_set", "lom_observe"):
        assert "expected_identity" in tools[name].inputSchema["required"]
    # Read-only calls may execute without a mutation identity; the backend checks
    # an actual mutating call dynamically against its formal catalogue entry.
    assert "expected_identity" not in tools["lom_call"].inputSchema["required"]
    assert tools["lom_poll"].annotations.idempotentHint is False
    assert tools["lom_unobserve"].annotations.idempotentHint is True
    assert "member" not in tools["lom_capabilities"].inputSchema["properties"]
    for name in ("lom_observe", "lom_poll", "lom_unobserve"):
        assert "owner" not in tools[name].inputSchema["properties"]


def test_describe_all_declared_members_and_1107_operations_without_dispatch(fake_dispatch):
    root = json.loads(lom.lom_describe(None))
    assert root["object_count"] == 45 and root["member_count"] == 588
    declared = {}
    for name in root["objects"]:
        result = json.loads(lom.lom_describe(None, object_type(name), limit=500))
        for member in result["members"]:
            if member["declaring_type"] == name:
                declared[(name, member["name"])] = member
    expected = {
        (owner, name)
        for owner, definition in lom.CATALOG.objects.items()
        for name in definition["members"]
    }
    assert set(declared) == expected and len(declared) == 588
    assert sum(len(member["access"]) for member in declared.values()) == 1107
    assert fake_dispatch == []


def test_describe_member_retains_inheritance_and_pagination(fake_dispatch):
    result = json.loads(lom.lom_describe(None, object_type("PluginDevice"), "parameters"))
    assert result["declaring_type"] == "Device"
    page = json.loads(lom.lom_describe(None, object_type("Clip"), limit=2))
    assert len(page["members"]) == 2 and page["next_offset"] == 2
    assert fake_dispatch == []


@pytest.mark.parametrize(
    "function,kwargs",
    [
        (lom.lom_get, {"object_type": object_type("Song"), "member": "tap_tempo"}),
        (
            lom.lom_set,
            {
                "object_type": object_type("Clip"),
                "member": "length",
                "value": 4,
                "expected_identity": "python:scope:one",
            },
        ),
        (lom.lom_call, {"object_type": object_type("Song"), "member": "tempo"}),
        (
            lom.lom_observe,
            {
                "object_type": object_type("Clip"),
                "member": "gain_display_string",
                "expected_identity": "python:scope:one",
            },
        ),
    ],
)
def test_access_denied_before_any_dispatch(fake_dispatch, function, kwargs):
    with pytest.raises(LomValidationError):
        function(None, path="live_set", **kwargs)
    assert fake_dispatch == []


@pytest.mark.parametrize(
    "function,kwargs",
    [
        (lom.lom_get, {"object_type": object_type("Song"), "member": "__dict__"}),
        (lom.lom_call, {"object_type": object_type("Device"), "member": "__str__"}),
        (
            lom.lom_set,
            {
                "object_type": object_type("Clip"),
                "member": "pitch_coarse",
                "value": 49,
                "expected_identity": "python:scope:one",
            },
        ),
        (
            lom.lom_set,
            {
                "object_type": object_type("Song"),
                "member": "tempo",
                "value": float("nan"),
                "expected_identity": "python:scope:one",
            },
        ),
        (
            lom.lom_call,
            {
                "object_type": object_type("Clip"),
                "member": "quantize",
                "arguments": [1, float("inf")],
            },
        ),
        (
            lom.lom_call,
            {
                "object_type": object_type("Song.View"),
                "member": "select_device",
                "arguments": [{"$ref": "live_set", "object_type": "Song"}],
            },
        ),
        (
            lom.lom_set,
            {
                "object_type": object_type("Track"),
                "member": "input_routing_type",
                "value": {"identifier": "none", "display_name": "None", "extra": True},
                "expected_identity": "python:scope:one",
            },
        ),
    ],
)
def test_invalid_values_references_and_members_fail_before_dispatch(
    fake_dispatch, function, kwargs
):
    with pytest.raises(LomValidationError):
        function(None, path="live_set", **kwargs)
    assert fake_dispatch == []


@pytest.mark.parametrize(
    "path",
    ["live_set tracks -1", "live_set __class__", "live_set tracks 0;import_os", "id invalid"],
)
def test_path_rejection_applies_to_resolve_and_capabilities(fake_dispatch, path):
    for function in (lom.lom_resolve, lom.lom_capabilities):
        with pytest.raises(LomValidationError):
            function(None, path, object_type("Song"))
    assert fake_dispatch == []


def test_mutating_entry_points_default_to_preview_and_preserve_identity(fake_dispatch):
    lom.lom_set(None, "live_set", object_type("Song"), "tempo", 140.0, "python:scope:song")
    lom.lom_call(None, "live_set", object_type("Song"), "tap_tempo")
    first, second = fake_dispatch
    assert first[0]["dry_run"] is second[0]["dry_run"] is True
    assert first[0]["expected_identity"] == "python:scope:song"
    assert second[2]["mutating"] is True
    assert second[0]["arguments"] == []


def test_formal_readonly_call_with_dry_run_false_is_not_dispatched_as_mutation(fake_dispatch):
    lom.lom_call(
        None,
        "live_app",
        object_type("Application"),
        "get_major_version",
        dry_run=False,
    )
    assert len(fake_dispatch) == 1
    payload, _selected, metadata = fake_dispatch[0]
    assert payload["dry_run"] is False
    assert metadata["mutating"] is False
    assert payload["expected_identity"] is None


def test_reference_and_dictionary_arguments_reach_typed_dispatch(fake_dispatch):
    reference = {"$ref": "live_set tracks 0 devices 0", "object_type": "PluginDevice"}
    lom.lom_call(
        None,
        "live_set view",
        object_type("Song.View"),
        "select_device",
        [reference],
        "python:scope:view",
    )
    lom.lom_call(
        None,
        "live_set tracks 0 clip_slots 0 clip",
        object_type("Clip"),
        "get_all_notes_extended",
        [{"return": ["note_id", "release_velocity"]}],
    )
    assert fake_dispatch[0][0]["arguments"] == [reference]
    assert fake_dispatch[1][2]["mutating"] is False


def test_capabilities_without_member_is_a_real_dispatchable_request(fake_dispatch):
    result = json.loads(lom.lom_capabilities(None, "live_set", object_type("Song"), limit=9))
    assert result["request"]["op"] == "capabilities"
    assert "member" not in result["request"]
    assert result["request"]["limit"] == 9


def test_observation_entry_points_never_accept_or_forward_a_caller_owner(fake_dispatch):
    for name in ("lom_observe", "lom_poll", "lom_unobserve"):
        assert "owner" not in inspect.signature(getattr(lom, name)).parameters
    lom.lom_observe(None, "live_set", object_type("Song"), "tempo", "python:scope:song")
    lom.lom_poll(None, "python:scope:subscription", limit=7)
    lom.lom_unobserve(None, "python:scope:subscription")
    assert [request[0]["op"] for request in fake_dispatch] == ["observe", "poll", "unobserve"]
    assert all("owner" not in request[0] for request in fake_dispatch)


@pytest.mark.parametrize("selected", ["python", "max", "auto"])
def test_dispatch_cannot_write_without_identity_and_sends_nothing(monkeypatch, selected):
    sends = []
    monkeypatch.setattr(backend, "_send", lambda *args, **kwargs: sends.append((args, kwargs)))
    for operation in ("set", "call"):
        with pytest.raises(ValueError, match="expected_identity"):
            backend.dispatch({"op": operation, "dry_run": False}, selected, mutating=True)
    assert sends == []


def test_backend_selection_binds_identity_and_does_not_switch_after_possible_write(monkeypatch):
    sends = []

    def send(selected, payload, **_kwargs):
        sends.append((selected, payload))
        return {"status": "error", "write_may_have_occurred": True}

    monkeypatch.setattr(backend, "_send", send)
    result = backend.dispatch({"op": "set", "expected_identity": "max:scope:7", "dry_run": False})
    assert result["write_may_have_occurred"] is True
    assert len(sends) == 1 and sends[0][0] == "max"
    with pytest.raises(ValueError, match="another backend"):
        backend.dispatch({"op": "get", "expected_identity": "max:scope:7"}, "python")
    assert len(sends) == 1


def test_backend_overwrites_caller_owner_for_poll_and_unobserve(monkeypatch):
    sends = []

    def send(selected, payload, **_kwargs):
        sends.append((selected, payload))
        return {"status": "ok"}

    monkeypatch.setattr(backend, "_send", send)
    for operation in ("poll", "unobserve"):
        backend.dispatch(
            {"op": operation, "subscription_id": "python:scope:one", "owner": "attacker"}
        )
    assert all(payload["owner"] == backend.OWNER for _selected, payload in sends)
    assert all(selected == "python" for selected, _payload in sends)


def test_actual_readonly_call_transport_failure_remains_readonly_and_can_select_max(monkeypatch):
    requests = []

    def failed_python(_command, payload):
        requests.append(("python", payload))
        raise OSError("fixture: bridge unavailable")

    def successful_max(payload):
        requests.append(("max", payload))
        return {"status": "read", "backend": "max", "value": 12, "write_may_have_occurred": False}

    monkeypatch.setattr(
        backend, "get_ableton_connection", lambda: SimpleNamespace(send_command=failed_python)
    )
    monkeypatch.setattr(
        backend, "get_max_connection", lambda: SimpleNamespace(request=successful_max)
    )
    result = backend.dispatch(
        {
            "op": "call",
            "path": "live_app",
            "object_type": "Application",
            "member": "get_major_version",
            "arguments": [],
            "dry_run": False,
        },
        mutating=False,
    )
    assert result["status"] == "read"
    assert result["write_may_have_occurred"] is False
    assert [selected for selected, _payload in requests] == ["python", "max"]
    assert all(payload["owner"] == backend.OWNER for _selected, payload in requests)


def test_readonly_bank_query_python_failure_is_not_reported_as_an_uncertain_write(monkeypatch):
    def failed(_command, _payload):
        raise OSError("fixture: unavailable")

    monkeypatch.setattr(
        backend, "get_ableton_connection", lambda: SimpleNamespace(send_command=failed)
    )
    result = backend._send(
        "python",
        {"op": "call", "object_type": "MaxDevice", "member": "get_bank_count", "dry_run": False},
    )
    assert result["status"] == "error"
    assert result["write_may_have_occurred"] is False


@pytest.mark.parametrize("member", ["undo", "not_a_documented_function"])
def test_caller_cannot_downgrade_known_or_unknown_calls_to_readonly(monkeypatch, member):
    sends = []
    monkeypatch.setattr(backend, "_send", lambda *args, **kwargs: sends.append((args, kwargs)))
    with pytest.raises(ValueError, match="expected_identity"):
        backend.dispatch(
            {
                "op": "call",
                "object_type": "Song",
                "member": member,
                "dry_run": False,
                "mutating": False,
            },
            backend="python",
            mutating=False,
        )
    assert sends == []


@pytest.mark.parametrize(
    "object_class,member,uncertain",
    [("MaxDevice", "get_bank_count", False), ("Song", "undo", True), ("Song", "unknown", True)],
)
def test_max_transport_after_send_uses_catalogue_effects_not_call_name_alone(
    object_class, member, uncertain
):
    class LostReplySocket:
        def __init__(self):
            self.sent = []
            self.closed = False

        def sendall(self, frame):
            self.sent.append(frame)

        def recv(self, _length):
            raise OSError("fixture: reply lost after sending")

        def close(self):
            self.closed = True

    connection = MaxConnection(port=9878, timeout=0.1)
    socket = LostReplySocket()
    connection._socket = socket
    with pytest.raises(MaxTransportError) as caught:
        connection.request(
            {
                "op": "call",
                "object_type": object_class,
                "member": member,
                "dry_run": False,
                "mutating": False,
            }
        )
    assert caught.value.write_may_have_occurred is uncertain
    assert len(socket.sent) == 1 and socket.closed
