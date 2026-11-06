"""Failure acknowledgements must retain recovery evidence and stop dependent edits."""

import ast
import math
import re
from types import MethodType, SimpleNamespace

import pytest
import test_test_interfaces_bridge as bridge_tests


@pytest.fixture
def state():
    return bridge_tests.setup_bridge.__wrapped__()


def attach_batch(bridge, handlers):
    method = next(
        node
        for node in bridge_tests.SOURCE_CLASS.body
        if isinstance(node, ast.FunctionDef) and node.name == "_batch"
    )
    namespace = {}
    exec(compile(ast.Module(body=[method], type_ignores=[]), "batch_failures", "exec"), namespace)
    bridge._batch = MethodType(namespace["_batch"], bridge)
    bridge._BATCH_ALIASES = {}
    bridge._ALL_COMMANDS = handlers


@pytest.mark.parametrize(
    "status,verified",
    [
        ("apply_failed", True),
        ("verification_failed", False),
        ("readback_failed", False),
        ("other_failure", False),
    ],
)
def test_reported_note_failure_stops_batch_and_keeps_recovery_evidence(state, status, verified):
    calls = []
    failure = {
        "status": status,
        "verified": verified,
        "write_may_have_occurred": True,
        "after_fingerprint": "readback-fingerprint",
        "inverse_changes": [{"note_id": 41, "start_time": 16.5}],
    }

    def first(_bridge, _params):
        calls.append("first")
        return {"status": "applied"}

    def failing(_bridge, _params):
        calls.append("failed")
        return failure

    def later(_bridge, _params):
        calls.append("later")
        return {"status": "applied"}

    attach_batch(state.bridge, {"first": first, "modify_notes_by_id": failing, "later": later})
    result = state.bridge._batch(
        [{"type": "first"}, {"type": "modify_notes_by_id"}, {"type": "later"}]
    )
    assert calls == ["first", "failed"]
    assert result["status"] == "batch_failed"
    assert result["executed"] == 2 and result["completed"] == 1
    assert result["failed_index"] == 1
    assert result["failed_result"] is failure
    assert result["results"][-1]["result"] is failure
    assert result["partial"] is True and result["rollback_performed"] is False
    assert "Inspect" in result["undo_hint"]
    assert state.undo == ["begin", "end"]
    assert state.bridge._test_batch_depth == 0


def test_successful_batch_keeps_existing_result_shape(state):
    attach_batch(state.bridge, {"read": lambda _bridge, _params: {"value": 3}})
    result = state.bridge._batch([{"type": "read"}])
    assert result == {"executed": 1, "results": [{"type": "read", "result": {"value": 3}}]}
    assert state.undo == ["begin", "end"]


def test_batch_undo_error_does_not_hide_reported_failure(state):
    failure = {"status": "readback_failed", "write_may_have_occurred": True}

    def end_undo():
        raise RuntimeError("undo acknowledgement lost")

    state.song.end_undo_step = end_undo
    attach_batch(state.bridge, {"modify_notes_by_id": lambda _bridge, _params: failure})
    result = state.bridge._batch([{"type": "modify_notes_by_id"}])
    assert result["failed_result"] is failure
    assert result["undo_error"] == "undo acknowledgement lost"
    assert state.bridge._test_batch_depth == 0


def test_automation_endpoint_overflow_is_rejected_before_clearing(state):
    original = bridge_tests.FakeEnvelope()
    state.session.envelope = original
    selected = state.song.view.detail_clip
    with pytest.raises(ValueError, match="plus duration"):
        state.bridge._write_automation(
            0, 0, 0, 0, [{"time": 1e308, "duration": 1e308, "value": 0.5}]
        )
    assert state.session.envelope is original
    assert state.session.cleared == []
    assert state.song.view.detail_clip is selected


def test_note_undo_error_still_returns_actual_readback_and_inverse(state):
    expected = bridge_tests.fingerprint(state)

    def end_undo():
        raise RuntimeError("undo acknowledgement lost")

    state.song.end_undo_step = end_undo
    result = state.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time_offset_ms": 24}], expected, dry_run=False
    )
    assert result["status"] == "applied" and result["verified"] is True
    assert result["undo_error"] == "undo acknowledgement lost"
    assert result["after_fingerprint"] != expected
    assert result["inverse_changes"] == [{"note_id": 41, "start_time": 16.5}]


class UncertainEditor:
    def __init__(self, parameter, *, setter_raises=False, readback_raises=False):
        self.name = "Editor fixture"
        self.class_name = "PluginDevice"
        self.class_display_name = "Plug-in"
        self.parameters = [parameter]
        self._open = False
        self._written = False
        self.setter_raises = setter_raises
        self.readback_raises = readback_raises

    @property
    def is_editor_open(self):
        if self._written and self.readback_raises:
            raise RuntimeError("editor readback lost")
        return self._open

    @is_editor_open.setter
    def is_editor_open(self, value):
        self._open = value
        self._written = True
        if self.setter_raises:
            raise RuntimeError("editor setter acknowledgement lost")


def test_editor_setter_failure_reports_possible_write_and_actual_readback(state):
    device = UncertainEditor(state.parameter, setter_raises=True)
    state.track.devices[0] = device
    expected = state.bridge._get_device_test_state(0, 0)["fingerprint"]
    result = state.bridge._set_plugin_editor_state(0, 0, True, expected, dry_run=False)
    assert device._open is True
    assert result["status"] == "apply_failed"
    assert result["write_may_have_occurred"] is True
    assert result["verified"] is True and result["readback"] is True
    assert "acknowledgement lost" in result["error"]


def test_editor_readback_failure_reports_unknown_after_write(state):
    device = UncertainEditor(state.parameter, readback_raises=True)
    state.track.devices[0] = device
    expected = state.bridge._get_device_test_state(0, 0)["fingerprint"]
    result = state.bridge._set_plugin_editor_state(0, 0, True, expected, dry_run=False)
    assert device._open is True
    assert result["status"] == "readback_failed"
    assert result["written"] is True and result["write_may_have_occurred"] is True
    assert result["current_state"] == "unknown" and result["readback"] is None
    assert result["verified"] is False and "readback lost" in result["readback_error"]


def attach_parameter_writer(bridge):
    helpers = [
        node
        for node in bridge_tests.TREE.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_display_number", "_parameter_value"}
    ]
    method = next(
        node
        for node in bridge_tests.SOURCE_CLASS.body
        if isinstance(node, ast.FunctionDef) and node.name == "_set_test_device_parameter"
    )
    namespace = {"math": math, "re": re}
    exec(
        compile(ast.Module(body=[*helpers, method], type_ignores=[]), "parameter_writer", "exec"),
        namespace,
    )
    bridge._set_test_device_parameter = MethodType(namespace[method.name], bridge)


class ControlledParameter:
    def __init__(
        self, *, enabled=True, parameter_state=0, setter_raises=False, readback_raises=False
    ):
        self.name = "Time"
        self.min = 0.0
        self.max = 1000.0
        self.is_quantized = False
        self.is_enabled = enabled
        self.state = parameter_state
        self.automation_state = 0
        self._value = 100.0
        self.write_count = 0
        self.setter_raises = setter_raises
        self.readback_raises = readback_raises

    @property
    def value(self):
        if self.write_count and self.readback_raises:
            raise RuntimeError("parameter readback lost")
        return self._value

    @value.setter
    def value(self, requested):
        self.write_count += 1
        self._value = requested
        if self.setter_raises:
            raise RuntimeError("parameter acknowledgement lost")

    def str_for_value(self, value):
        return f"{value} ms"


def parameter_target(state, parameter, *, nested=False):
    attach_parameter_writer(state.bridge)
    leaf = SimpleNamespace(name="Leaf", class_name="PluginDevice", parameters=[parameter])
    path = []
    if nested:
        middle = SimpleNamespace(
            name="Inner rack", parameters=[], chains=[SimpleNamespace(devices=[leaf])]
        )
        state.track.devices[0] = SimpleNamespace(
            name="Outer rack", parameters=[], chains=[SimpleNamespace(devices=[middle])]
        )
        path = [0, 0, 0, 0]
    else:
        state.track.devices[0] = leaf
    expected = state.bridge._get_device_test_state(0, 0, chain_path=path)["fingerprint"]
    return path, expected


def test_nested_parameter_dry_run_resolves_display_without_writing(state):
    parameter = ControlledParameter()
    path, expected = parameter_target(state, parameter, nested=True)
    result = state.bridge._set_test_device_parameter(
        0, 0, "Time", "250 ms", expected, expected_value=100.0, chain_path=path
    )
    assert result["status"] == "preview"
    assert result["proposed"] == pytest.approx(250.0, abs=0.01)
    assert parameter.value == 100.0 and parameter.write_count == 0
    assert result["required_target"]["chain_path"] == path
    assert result["inverse_command"]["params"]["value"] == 100.0


def test_nested_parameter_write_reads_actual_value_and_inverse_restores_it(state):
    parameter = ControlledParameter()
    path, expected = parameter_target(state, parameter, nested=True)
    result = state.bridge._set_test_device_parameter(
        0, 0, 0, 250.0, expected, expected_value=100.0, chain_path=path, dry_run=False
    )
    assert result["status"] == "applied" and result["verified"] is True
    assert result["readback"] == 250.0 and parameter.write_count == 1
    inverse = state.bridge._set_test_device_parameter(**result["inverse_command"]["params"])
    assert inverse["verified"] is True and parameter.value == 100.0
    assert parameter.write_count == 2


@pytest.mark.parametrize("enabled,parameter_state", [(False, 0), (True, 2)])
def test_disabled_or_unchangeable_parameter_is_rejected_before_writing(
    state, enabled, parameter_state
):
    parameter = ControlledParameter(enabled=enabled, parameter_state=parameter_state)
    _path, expected = parameter_target(state, parameter)
    with pytest.raises(ValueError, match="disabled|cannot be changed"):
        state.bridge._set_test_device_parameter(0, 0, 0, 250.0, expected, dry_run=False)
    assert parameter.write_count == 0


@pytest.mark.parametrize("expected_value", [99.0, float("nan"), float("inf")])
def test_stale_or_nonfinite_expected_parameter_value_is_rejected(state, expected_value):
    parameter = ControlledParameter()
    _path, expected = parameter_target(state, parameter)
    with pytest.raises(ValueError, match="stale_value|finite"):
        state.bridge._set_test_device_parameter(
            0, 0, 0, 250.0, expected, expected_value=expected_value, dry_run=False
        )
    assert parameter.write_count == 0


def test_same_parameter_value_is_zero_write_and_stale_target_is_rejected(state):
    parameter = ControlledParameter()
    _path, expected = parameter_target(state, parameter)
    result = state.bridge._set_test_device_parameter(0, 0, 0, 100.0, expected, dry_run=False)
    assert result["status"] == "no_change" and result["verified"] is True
    assert result["write_may_have_occurred"] is False and parameter.write_count == 0
    with pytest.raises(ValueError, match="stale_target"):
        state.bridge._set_test_device_parameter(0, 0, 0, 250.0, "stale", dry_run=False)
    assert parameter.write_count == 0


def test_current_display_parameter_value_is_zero_write_despite_approximate_inversion(state):
    parameter = ControlledParameter()
    _path, expected = parameter_target(state, parameter)
    current_display = parameter.str_for_value(parameter.value)
    requested = "  " + current_display.upper().replace(" ", "   ") + "  "
    result = state.bridge._set_test_device_parameter(0, 0, 0, requested, expected, dry_run=False)
    assert result["status"] == "no_change" and result["proposed"] == 100.0
    assert result["write_may_have_occurred"] is False
    assert parameter.write_count == 0 and parameter.value == 100.0


def test_fine_native_parameter_calibration_is_not_suppressed_by_display_matching(state):
    parameter = ControlledParameter()
    _path, expected = parameter_target(state, parameter)
    requested = 100.0 + 1e-8
    result = state.bridge._set_test_device_parameter(0, 0, 0, requested, expected, dry_run=False)
    assert result["status"] == "applied" and result["verified"] is True
    assert parameter.write_count == 1 and parameter.value == requested


@pytest.mark.parametrize("requested", [True, float("nan"), float("inf")])
def test_bad_parameter_values_are_rejected_before_writing(state, requested):
    parameter = ControlledParameter()
    _path, expected = parameter_target(state, parameter)
    with pytest.raises(ValueError, match="finite"):
        state.bridge._set_test_device_parameter(0, 0, 0, requested, expected, dry_run=False)
    assert parameter.write_count == 0


@pytest.mark.parametrize("setter_raises,readback_raises", [(True, False), (False, True)])
def test_parameter_uncertain_write_is_read_back_and_never_retried(
    state, setter_raises, readback_raises
):
    parameter = ControlledParameter(setter_raises=setter_raises, readback_raises=readback_raises)
    _path, expected = parameter_target(state, parameter)
    result = state.bridge._set_test_device_parameter(0, 0, 0, 250.0, expected, dry_run=False)
    assert parameter.write_count == 1 and parameter._value == 250.0
    assert result["write_may_have_occurred"] is True
    if setter_raises:
        assert result["status"] == "apply_failed" and result["verified"] is True
        assert result["readback"] == 250.0
    else:
        assert result["status"] == "readback_failed" and result["verified"] is False
        assert result["current_value"] == "unknown" and result["inverse_requires_readback"] is True


def test_ambiguous_parameter_name_requires_explicit_readback_index(state):
    first, second = ControlledParameter(), ControlledParameter()
    parameter_target(state, first)
    state.track.devices[0].parameters.append(second)
    expected = state.bridge._get_device_test_state(0, 0)["fingerprint"]
    with pytest.raises(ValueError, match="ambiguous"):
        state.bridge._set_test_device_parameter(0, 0, "Time", 250.0, expected, dry_run=False)
    assert first.write_count == second.write_count == 0
    result = state.bridge._set_test_device_parameter(0, 0, 1, 250.0, expected, dry_run=False)
    assert result["verified"] is True and second.value == 250.0 and first.value == 100.0
