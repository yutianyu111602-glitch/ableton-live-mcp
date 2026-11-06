"""Transport schemas and bounded observations, without connecting to Live."""

import asyncio
import gzip
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ableton_live_mcp.tools import test_interfaces as api


def connection(monkeypatch, replies):
    sent = []
    iterator = iter(replies)

    def send(command, wire):
        sent.append((command, wire))
        return next(iterator)

    monkeypatch.setattr(api, "get_ableton_connection", lambda: SimpleNamespace(send_command=send))
    return sent


@pytest.mark.parametrize(
    "change",
    [
        {"note_id": 1},
        {"note_id": True, "start_time": 1},
        {"note_id": 1, "start_time": 1, "start_time_offset_ms": 2},
        {"note_id": 1, "start_time": -1},
        {"note_id": 1, "duration": 0},
        {"note_id": 1, "velocity": 128},
        {"note_id": 1, "pitch": 1.5},
        {"note_id": 1, "start_time_offset_ms": float("nan")},
        {"note_id": 1, "start_time_offset_ms": float("inf")},
        {"note_id": 1, "anything": "unexpected"},
    ],
)
def test_note_change_schema_rejects_invalid_and_ambiguous_changes(change):
    with pytest.raises(ValidationError):
        api.NoteChange(**change)


def test_native_note_edit_wire_defaults_to_preview_without_erasing_omitted_fields(monkeypatch):
    sent = connection(monkeypatch, [{"status": "preview"}])
    result = json.loads(
        api.modify_notes_by_id(
            None,
            19,
            2,
            [api.NoteChange(note_id=17, start_time_offset_ms=24)],
            "readback-fingerprint",
            view="arrangement",
        )
    )
    assert result["status"] == "preview"
    assert sent == [
        (
            "modify_notes_by_id",
            {
                "track_index": 19,
                "clip_index": 2,
                "view": "arrangement",
                "dry_run": True,
                "expected_fingerprint": "readback-fingerprint",
                "changes": [{"note_id": 17, "start_time_offset_ms": 24.0}],
            },
        )
    ]


def test_sampling_is_read_only_bounded_and_detects_identity_change(monkeypatch):
    parameter = {"index": 1, "name": "Wet", "value": 0.1}
    sent = connection(
        monkeypatch,
        [
            {"fingerprint": "a", "parameters": [parameter], "current_song_time": 40},
            {"fingerprint": "b", "parameters": [parameter], "current_song_time": 41},
        ],
    )
    waits = []
    monkeypatch.setattr(api.time, "sleep", waits.append)
    result = json.loads(api.get_parameter_samples(None, 0, 0, [1], count=3))
    assert result["status"] == "partial"
    assert result["reason"] == "device_identity_changed"
    assert len(result["samples"]) == 1
    assert result["samples"][0]["timestamp_utc"].endswith("Z")
    assert result["samples"][0]["current_song_time"] == 40
    assert waits == [0.1]
    assert all(command == "get_device_test_state" for command, _ in sent)


def test_nested_parameter_write_defaults_to_preview_with_restore_value_guard(monkeypatch):
    sent = connection(monkeypatch, [{"status": "preview"}])
    api.set_test_device_parameter(
        None, 0, 1, "Cutoff", "1 kHz", "device-fp", expected_value=0.2, chain_path=[0, 2, 1, 3]
    )
    command, wire = sent[0]
    assert command == "set_test_device_parameter"
    assert wire["chain_path"] == [0, 2, 1, 3]
    assert wire["expected_value"] == 0.2 and wire["dry_run"] is True
    assert wire["value"] == "1 kHz"


def test_sampling_rejects_missing_indices_before_waiting(monkeypatch):
    sent = connection(monkeypatch, [{"fingerprint": "a", "parameters": []}])
    monkeypatch.setattr(api.time, "sleep", lambda _: pytest.fail("must not wait"))
    with pytest.raises(ValueError, match="does not exist"):
        api.get_parameter_samples(None, 0, 0, [2])
    assert len(sent) == 1


def test_sampling_budget_and_path_validation_precede_connection(monkeypatch):
    monkeypatch.setattr(api, "get_ableton_connection", lambda: pytest.fail("no connection"))
    with pytest.raises(ValueError, match="10 seconds"):
        api.get_parameter_samples(None, 0, 0, [0], count=50, interval_ms=1000)
    with pytest.raises(ValueError, match="unique"):
        api.get_parameter_samples(None, 0, 0, [1, 1])
    with pytest.raises(ValueError, match="pairs"):
        api.get_device_test_state(None, 0, 0, chain_path=[0])


def test_saved_file_verification_never_claims_live_ram_was_saved(tmp_path):
    file = tmp_path / "independent.als"
    file.write_bytes(gzip.compress(b"<Ableton><LiveSet /></Ableton>"))
    result = json.loads(api.verify_saved_set(None, str(file)))
    assert result["valid_als"] is True
    assert result["saved_by_this_call"] is False
    assert result["current_live_ram_matches"] == "unknown"
    mismatch = json.loads(api.verify_saved_set(None, str(file), "0" * 64))
    assert mismatch["expected_hash_matches"] is False
    match = json.loads(api.verify_saved_set(None, str(file), result["sha256"]))
    assert match["expected_hash_matches"] is True
    file.write_bytes(gzip.compress(b"<not_an_ableton_set />"))
    with pytest.raises(ValueError, match="not an Ableton"):
        api.verify_saved_set(None, str(file))


def test_new_tool_annotations_and_default_preview_are_explicit():
    from ableton_live_mcp.app import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    for name in (
        "get_test_context",
        "get_clip_snapshot",
        "get_device_test_state",
        "get_parameter_samples",
        "get_clip_automation_snapshot",
        "verify_saved_set",
    ):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.openWorldHint is False
    edit = tools["modify_notes_by_id"]
    assert edit.annotations.destructiveHint is True
    assert edit.inputSchema["properties"]["dry_run"]["default"] is True
    assert edit.inputSchema["properties"]["view"]["enum"] == ["session", "arrangement"]
