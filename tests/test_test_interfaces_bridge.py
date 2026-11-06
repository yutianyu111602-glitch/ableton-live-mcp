"""Behavioral tests for the Live bridge using a strict fake native note vector.

The Remote Script is AST-loaded without importing Live, starting sockets, or
opening a Set. These tests establish bridge behavior, never host/MPE acceptance.
"""

import ast
import hashlib
import json
import math
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "ableton_live_mcp/remote_script/__init__.py"
TREE = ast.parse(SCRIPT.read_text(encoding="utf-8"))
SOURCE_CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "AbletonMCP")
EXTRA_METHODS = {
    "_get_clip_snapshot",
    "_get_test_context",
    "_modify_notes_by_id",
    "_get_device_test_state",
    "_set_plugin_editor_state",
    "_get_clip_automation_snapshot",
    "_get_max_device_info",
    "_write_automation",
    "_get_clip",
    "_get_track",
    "_get_device",
    "_resolve_track",
    "_resolve_parameter",
}
TEST_CLASS = ast.parse("class BridgeUnderTest:\n    pass\n").body[0]
TEST_CLASS.body = [
    n
    for n in SOURCE_CLASS.body
    if isinstance(n, ast.FunctionDef) and (n.name.startswith("_test_") or n.name in EXTRA_METHODS)
]
NAMESPACE = {"hashlib": hashlib, "json": json, "math": math, "uuid": uuid, "BRIDGE_VERSION": "test"}
exec(
    compile(
        ast.fix_missing_locations(ast.Module(body=[TEST_CLASS], type_ignores=[])),
        str(SCRIPT),
        "exec",
    ),
    NAMESPACE,
)
Bridge = NAMESPACE["BridgeUnderTest"]


class FakeNativeVector:
    """List conversion loses the native type and must fail at the API boundary."""

    def __init__(self, notes):
        self.notes = notes

    def __iter__(self):
        return iter(self.notes)

    def __len__(self):
        return len(self.notes)


def note(note_id, start_time, pitch=60):
    return SimpleNamespace(
        note_id=note_id,
        pitch=pitch,
        start_time=start_time,
        duration=0.5,
        velocity=93.25,
        mute=False,
        probability=0.8,
        velocity_deviation=-7.0,
        release_velocity=71.0,
        mpe_sentinel=object(),
    )


def clone_note(value):
    # Native note proxies carry expression data not serialized by this bridge.
    return SimpleNamespace(**vars(value))


class FakeClip:
    def __init__(self, notes, *, start_marker=16.0, timeline_start=96.0):
        self.name = "Phrase"
        self.length = 16.0
        self.is_midi_clip = True
        self.is_audio_clip = False
        self.looping = False
        self.loop_start = start_marker
        self.loop_end = start_marker + 16.0
        self.start_marker = start_marker
        self.end_marker = start_marker + 48.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.color_index = 29
        self.start_time = timeline_start
        self.end_time = timeline_start + 16.0
        self.is_playing = False
        self.is_recording = False
        self.is_overdubbing = False
        self.stored = [clone_note(n) for n in notes]
        self.get_count = 0
        self.apply_calls = []
        self.last_vector = None
        self.mutate_on_get = None
        self.apply_mode = "normal"
        self.envelope = None
        self.cleared = []
        self.envelope_creates = 0

    def get_all_notes_extended(self):
        self.get_count += 1
        if self.get_count == self.mutate_on_get:
            self.stored[0].velocity += 1
        self.last_vector = FakeNativeVector([clone_note(n) for n in self.stored])
        return self.last_vector

    def apply_note_modifications(self, vector):
        assert type(vector) is FakeNativeVector
        assert vector is self.last_vector
        self.apply_calls.append(vector)
        if self.apply_mode != "ignore":
            self.stored = [clone_note(n) for n in vector]
        if self.apply_mode == "persist_then_raise":
            raise RuntimeError("native reply lost after applying")

    def clear_envelope(self, parameter):
        self.cleared.append(parameter)
        self.envelope = None

    def automation_envelope(self, _parameter):
        return self.envelope

    def create_automation_envelope(self, _parameter):
        self.envelope_creates += 1
        self.envelope = FakeEnvelope()
        return self.envelope

    def remove_notes_extended(self, *_args):
        raise AssertionError("Editing existing notes must not remove/recreate them")

    def add_new_notes(self, *_args):
        raise AssertionError("Editing existing notes must preserve native IDs")


class FakeEnvelope:
    def __init__(self):
        self.steps = []

    def insert_step(self, start, duration, value):
        self.steps.append((start, duration, value))

    def value_at_time(self, time):
        for start, duration, value in reversed(self.steps):
            if start <= time < start + duration:
                return value
        return -1.0


@pytest.fixture
def setup_bridge():
    session = FakeClip([note(41, 16.5), note(7, 19.25, 67), note(99, 3.0, 48)])
    arrangement = FakeClip([note(501, 32.5, 48)], start_marker=32.0, timeline_start=248.0)
    parameter = SimpleNamespace(
        name="Macro 1",
        value=0.25,
        min=0.0,
        max=1.0,
        is_enabled=False,
        is_quantized=False,
        automation_state=1,
        str_for_value=lambda value: f"{value * 100:.1f} %",
    )
    device = SimpleNamespace(
        name="Synth",
        class_name="PluginDevice",
        class_display_name="Plug-in",
        parameters=[parameter],
        is_editor_open=False,
    )
    track = SimpleNamespace(
        name="Instrument",
        clip_slots=[SimpleNamespace(has_clip=True, clip=session), SimpleNamespace(has_clip=False)],
        arrangement_clips=[arrangement],
        devices=[device],
        view=SimpleNamespace(selected_device=device),
    )
    song = SimpleNamespace(
        tracks=[track],
        return_tracks=[],
        master_track=SimpleNamespace(name="Master"),
        view=SimpleNamespace(selected_track=track, detail_clip=arrangement),
        tempo=140.0,
        current_song_time=84.0,
        record_mode=False,
        session_record=False,
    )
    undo = []
    song.begin_undo_step = lambda: undo.append("begin")
    song.end_undo_step = lambda: undo.append("end")
    bridge = Bridge()
    bridge._song = song
    bridge._live_version = lambda: "12.4.1"
    return SimpleNamespace(
        bridge=bridge,
        song=song,
        track=track,
        session=session,
        arrangement=arrangement,
        device=device,
        parameter=parameter,
        undo=undo,
    )


def fingerprint(fixture, *, view="session"):
    return fixture.bridge._get_clip_snapshot(0, 0, view=view)["fingerprint"]


def stored_state(clip):
    return [dict(vars(n)) for n in clip.stored]


def test_arrangement_snapshot_preserves_source_markers_and_complete_notes(setup_bridge):
    f = setup_bridge
    result = f.bridge._get_clip_snapshot(0, 0, view="arrangement")
    assert result["metadata"]["start_marker"] == 32
    assert result["metadata"]["start_time"] == 248
    assert result["notes"][0]["start_time"] == 32.5
    assert result["notes"][0]["note_id"] == 501
    assert result["notes"][0]["release_velocity"] == 71
    assert f.session.apply_calls == f.arrangement.apply_calls == []


def test_snapshot_paging_does_not_change_full_content_fingerprint(setup_bridge):
    b = setup_bridge.bridge
    full = b._get_clip_snapshot(0, 0)
    page = b._get_clip_snapshot(0, 0, note_offset=1, note_limit=1)
    metadata_only = b._get_clip_snapshot(0, 0, include_notes=False)
    assert full["fingerprint"] == page["fingerprint"] == metadata_only["fingerprint"]
    assert full["note_count"] == metadata_only["note_count"] == 3
    assert page["notes"] == full["notes"][1:2]
    assert page["next_note_offset"] == 2
    assert metadata_only["notes"] == []
    assert any(n["start_time"] < full["metadata"]["start_marker"] for n in full["notes"])


def test_dry_run_reports_ms_delta_and_inverse_without_mutating_native_notes(setup_bridge):
    f = setup_bridge
    original = stored_state(f.session)
    result = f.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time_offset_ms": 24}], fingerprint(f)
    )
    assert result["status"] == "preview"
    assert result["preview"][0]["timing_delta_ms"] == pytest.approx(24)
    assert result["preview"][0]["after"]["start_time"] == pytest.approx(16.556)
    assert result["inverse_changes"] == [{"note_id": 41, "start_time": 16.5}]
    assert result["write_may_have_occurred"] is False
    assert stored_state(f.session) == original
    assert f.session.apply_calls == f.undo == []


def test_apply_uses_original_native_vector_keeps_expression_and_supports_inverse(setup_bridge):
    f = setup_bridge
    original = stored_state(f.session)
    result = f.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time_offset_beats": 0.04}], fingerprint(f), dry_run=False
    )
    assert result["status"] == "applied" and result["verified"]
    assert len(f.session.apply_calls) == 1
    vector = f.session.apply_calls[0]
    assert type(vector) is FakeNativeVector
    assert [n.note_id for n in vector] == [41, 7, 99]
    actual = stored_state(f.session)
    assert actual[0]["start_time"] == pytest.approx(16.54)
    for index, before in enumerate(original):
        assert actual[index]["mpe_sentinel"] is before["mpe_sentinel"]
        assert {k: v for k, v in actual[index].items() if k != "start_time"} == {
            k: v for k, v in before.items() if k != "start_time"
        }
    assert actual[1:] == original[1:]
    assert result["before_fingerprint"] != result["after_fingerprint"]
    restored = f.bridge._modify_notes_by_id(
        0, 0, result["inverse_changes"], result["inverse_expected_fingerprint"], dry_run=False
    )
    assert restored["verified"] and stored_state(f.session) == original
    assert f.undo == ["begin", "end", "begin", "end"]


def test_arrangement_edit_does_not_convert_source_time_or_touch_session(setup_bridge):
    f = setup_bridge
    original = stored_state(f.session)
    result = f.bridge._modify_notes_by_id(
        0,
        0,
        [{"note_id": 501, "start_time_offset_ms": -12}],
        fingerprint(f, view="arrangement"),
        view="arrangement",
        dry_run=False,
    )
    assert result["verified"]
    assert f.arrangement.stored[0].start_time == pytest.approx(32.472)
    assert f.arrangement.start_time == 248 and f.arrangement.start_marker == 32
    assert stored_state(f.session) == original and not f.session.apply_calls


@pytest.mark.parametrize(
    "changes",
    [
        [],
        [{"note_id": 41}],
        [{"note_id": 41, "start_time": 17}, {"note_id": 41, "velocity": 80}],
        [{"note_id": 123456, "start_time": 17}],
        [{"note_id": True, "start_time": 17}],
        [{"note_id": 41, "pitch": 128}],
        [{"note_id": 41, "velocity": 128}],
        [{"note_id": 41, "mute": 1}],
        [{"note_id": 41, "duration": 0}],
        [{"note_id": 41, "start_time": -0.01}],
        [{"note_id": 41, "start_time": 17, "start_time_offset_ms": 2}],
        [{"note_id": 41, "start_time_offset_ms": float("nan")}],
        [{"note_id": 41, "start_time_offset_beats": float("inf")}],
        [{"note_id": 41, "probability": -0.1}],
        [{"note_id": 41, "mpe_sentinel": "overwrite"}],
        [{"note_id": 41, "start_time": 17}, {"note_id": 7, "velocity": 500}],
    ],
)
def test_invalid_edit_request_is_fully_rejected_before_any_write(setup_bridge, changes):
    f = setup_bridge
    original = stored_state(f.session)
    with pytest.raises((ValueError, IndexError)):
        f.bridge._modify_notes_by_id(0, 0, changes, fingerprint(f), dry_run=False)
    assert stored_state(f.session) == original
    assert f.session.apply_calls == f.undo == []


def test_replaced_clip_with_identical_content_invalidates_old_fingerprint(setup_bridge):
    f = setup_bridge
    stale = fingerprint(f)
    f.track.clip_slots[0].clip = FakeClip(f.session.stored)
    with pytest.raises(ValueError, match="stale_target"):
        f.bridge._modify_notes_by_id(
            0, 0, [{"note_id": 41, "start_time": 17}], stale, dry_run=False
        )
    assert f.session.apply_calls == f.track.clip_slots[0].clip.apply_calls == []


def test_live_change_between_validation_and_native_vector_read_is_rejected(setup_bridge):
    f = setup_bridge
    token = fingerprint(f)
    f.session.mutate_on_get = f.session.get_count + 2
    with pytest.raises(ValueError, match="stale_target"):
        f.bridge._modify_notes_by_id(
            0, 0, [{"note_id": 41, "start_time": 17}], token, dry_run=False
        )
    assert f.session.apply_calls == f.undo == []


def test_duplicate_native_ids_are_not_guessed(setup_bridge):
    f = setup_bridge
    f.session.stored[1].note_id = 41
    with pytest.raises(ValueError, match="unique native note IDs"):
        f.bridge._modify_notes_by_id(
            0, 0, [{"note_id": 41, "start_time": 17}], fingerprint(f), dry_run=False
        )
    assert f.session.apply_calls == f.undo == []


@pytest.mark.parametrize(
    "target", ["clip_recording", "clip_overdub", "song_record", "session_record"]
)
def test_recording_guards_prevent_edits(setup_bridge, target):
    f = setup_bridge
    if target == "clip_recording":
        f.session.is_recording = True
    elif target == "clip_overdub":
        f.session.is_overdubbing = True
    elif target == "song_record":
        f.song.record_mode = True
    else:
        f.song.session_record = True
    with pytest.raises(ValueError, match="recording|overdubbing"):
        f.bridge._modify_notes_by_id(
            0, 0, [{"note_id": 41, "start_time": 17}], fingerprint(f), dry_run=False
        )
    assert f.session.apply_calls == f.undo == []


def test_apply_failure_reports_possible_write_and_does_not_retry(setup_bridge):
    f = setup_bridge
    f.session.apply_mode = "persist_then_raise"
    result = f.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time": 17}], fingerprint(f), dry_run=False
    )
    assert result["status"] == "apply_failed"
    assert result["write_may_have_occurred"] is True
    assert len(f.session.apply_calls) == 1
    assert f.session.stored[0].start_time == 17
    assert result["after_fingerprint"] and result["verified"]
    assert f.undo == ["begin", "end"]


def test_noop_native_apply_is_reported_as_verification_failure(setup_bridge):
    f = setup_bridge
    f.session.apply_mode = "ignore"
    result = f.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time": 17}], fingerprint(f), dry_run=False
    )
    assert result["status"] == "verification_failed" and not result["verified"]
    assert result["verification_mismatches"]


def test_existing_batch_owns_undo_scope(setup_bridge):
    f = setup_bridge
    f.bridge._test_batch_depth = 1
    result = f.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time": 17}], fingerprint(f), dry_run=False
    )
    assert result["verified"] and not f.undo


def test_same_value_request_is_not_counted_as_a_write_or_accepted_change(setup_bridge):
    f = setup_bridge
    token = fingerprint(f)
    result = f.bridge._modify_notes_by_id(
        0, 0, [{"note_id": 41, "start_time": 16.5}], token, dry_run=False
    )
    assert result["status"] == "no_change" and result["verified"]
    assert result["requested_note_count"] == result["no_op_note_count"] == 1
    assert result["changed_note_count"] == 0
    assert result["write_may_have_occurred"] is False
    assert result["inverse_changes"] == []
    assert f.session.apply_calls == f.undo == []


def test_missing_native_modification_api_never_uses_note_replacement(setup_bridge):
    f = setup_bridge
    token = fingerprint(f)
    f.session.apply_note_modifications = None
    with pytest.raises(ValueError, match="native apply_note_modifications"):
        f.bridge._modify_notes_by_id(
            0, 0, [{"note_id": 41, "start_time": 17}], token, dry_run=False
        )
    assert not f.undo


def test_automation_positive_durations_reach_native_api_and_read_back_as_held_values(setup_bridge):
    f = setup_bridge
    points = [
        {"time": 16, "duration": 4, "value": 0.7},
        {"time": 20, "duration": 4, "value": 0.2},
    ]
    result = f.bridge._write_automation(0, 0, 0, 0, points)
    assert result["zero_duration_count"] == 0
    assert f.session.envelope.steps == [(16.0, 4.0, 0.7), (20.0, 4.0, 0.2)]
    samples = f.bridge._get_clip_automation_snapshot(0, 0, 0, 0, sample_times=[16, 18, 20, 23.9])
    assert [s["value"] for s in samples["samples"]] == [0.7, 0.7, 0.2, 0.2]
    assert samples["created_envelope"] is False


@pytest.mark.parametrize(
    "bad_point",
    [
        {"time": 2, "duration": -1, "value": 0.5},
        {"time": float("nan"), "duration": 1, "value": 0.5},
        {"time": 2, "duration": 1, "value": 2},
        {"time": 2, "duration": True, "value": 0.5},
        {"time": 2, "duration": 1, "value": 0.5, "unknown": 1},
    ],
)
def test_invalid_late_automation_point_preserves_existing_envelope_and_selection(
    setup_bridge, bad_point
):
    f = setup_bridge
    prior = FakeEnvelope()
    prior.steps = [(0, 4, 0.9)]
    f.session.envelope = prior
    selected = f.song.view.detail_clip
    with pytest.raises(ValueError):
        f.bridge._write_automation(
            0, 0, 0, 0, [{"time": 0, "duration": 1, "value": 0.2}, bad_point]
        )
    assert f.session.envelope is prior and prior.steps == [(0, 4, 0.9)]
    assert not f.session.cleared and not f.session.envelope_creates
    assert f.song.view.detail_clip is selected


def test_automation_snapshot_never_creates_absent_envelope(setup_bridge):
    f = setup_bridge
    result = f.bridge._get_clip_automation_snapshot(0, 0, 0, 0, sample_times=[0, 1])
    assert result["status"] == "absent" and result["created_envelope"] is False
    assert not f.session.cleared and not f.session.envelope_creates


def test_context_exposes_unknown_project_state_and_selected_arrangement_target(setup_bridge):
    f = setup_bridge
    result = f.bridge._get_test_context()
    assert result["project"]["file_path"] is None
    assert result["project"]["is_modified"] is None
    assert "file_path" in result["project_unavailable"]
    assert result["selected"]["clip"]["view"] == "arrangement"
    assert result["selected"]["clip"]["track_index"] == 0
    assert result["save_capabilities"]["save_as"] == "requires_gui"


def test_nested_device_readback_keeps_disabled_and_unknown_remote_state(setup_bridge):
    f = setup_bridge
    nested = f.device
    f.track.devices = [SimpleNamespace(chains=[SimpleNamespace(devices=[nested])])]
    result = f.bridge._get_device_test_state(0, 0, chain_path=[0, 0])
    assert result["metadata"]["name"] == "Synth"
    assert result["parameters"][0]["is_enabled"] is False
    assert result["parameters"][0]["automation_state"] == 1
    assert result["parameters"][0]["remote_control_state"] == "unknown_not_exposed"
    assert result["m4l_action_status"]["supported"] is False


def test_editor_dry_run_stale_identity_and_unsupported_device(setup_bridge):
    f = setup_bridge
    state = f.bridge._get_device_test_state(0, 0)
    preview = f.bridge._set_plugin_editor_state(0, 0, True, state["fingerprint"])
    assert preview["status"] == "preview" and f.device.is_editor_open is False
    with pytest.raises(ValueError, match="stale_target"):
        f.bridge._set_plugin_editor_state(0, 0, True, "old", dry_run=False)
    applied = f.bridge._set_plugin_editor_state(0, 0, True, state["fingerprint"], dry_run=False)
    assert applied["verified"] and f.device.is_editor_open is True
    del f.device.is_editor_open
    result = f.bridge._set_plugin_editor_state(0, 0, True, state["fingerprint"], dry_run=False)
    assert result["status"] == "unsupported" and not result["written"]


def test_max_banks_report_explicit_actions_unavailable_instead_of_guessing(setup_bridge):
    f = setup_bridge
    unsupported = f.bridge._get_max_device_info(0, 0)
    assert unsupported["status"] == "unsupported"
    f.device.get_bank_count = lambda: 1
    f.device.get_bank_name = lambda _index: "Controls"
    f.device.get_bank_parameters = lambda _index: (0, -1)
    result = f.bridge._get_max_device_info(0, 0)
    assert result["banks"] == [{"index": 0, "name": "Controls", "parameter_indices": [0, -1]}]
    assert result["m4l_action_status"]["supported"] is False
