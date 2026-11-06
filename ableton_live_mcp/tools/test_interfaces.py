"""Bounded, explicit interfaces for MIDI and device acceptance work."""

import gzip
import hashlib
import io
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..app import mcp
from ..connection import get_ableton_connection
from ._util import DeviceIndex, DeviceParameter, DeviceParameterValue, TrackIndex

ClipView = Literal["session", "arrangement"]
TrackType = Literal["track", "return", "master"]
NonnegativeIndex = Annotated[int, Field(ge=0)]
ClipIndex = Annotated[
    int, Field(ge=0, description="Session slot or Arrangement clip index, selected by view.")
]
Fingerprint = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Exact target fingerprint from the preceding snapshot.",
    ),
]
ChainPath = Annotated[
    list[NonnegativeIndex],
    Field(
        max_length=16,
        description="Alternating chain index, device index pairs below the base device; empty for the base device.",
    ),
]
FiniteBeat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)


class NoteChange(BaseModel):
    """Change only specified fields of an existing native note."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    note_id: Annotated[int, Field(ge=0, strict=True)]
    start_time: FiniteBeat | None = None
    start_time_offset_beats: float | None = None
    start_time_offset_ms: float | None = None
    duration: Annotated[float, Field(gt=0)] | None = None
    pitch: Annotated[int, Field(ge=0, le=127, strict=True)] | None = None
    velocity: Annotated[float, Field(ge=0, le=127)] | None = None
    mute: Annotated[bool, Field(strict=True)] | None = None
    probability: Annotated[float, Field(ge=0, le=1)] | None = None
    velocity_deviation: Annotated[float, Field(ge=-127, le=127)] | None = None
    release_velocity: Annotated[float, Field(ge=0, le=127)] | None = None

    @model_validator(mode="after")
    def validate_change(self):
        values = self.model_dump(exclude_none=True)
        if len(values) < 2:
            raise ValueError("Specify at least one note field to modify")
        if (
            sum(
                values.get(k) is not None
                for k in ("start_time", "start_time_offset_beats", "start_time_offset_ms")
            )
            > 1
        ):
            raise ValueError("Use only one absolute or relative start time")
        return self


def _target(track_index, device_index, track_type, chain_path):
    if len(chain_path) % 2:
        raise ValueError("chain_path must contain chain/device index pairs")
    return dict(
        track_index=track_index,
        device_index=device_index,
        track_type=track_type,
        chain_path=chain_path,
    )


def _send(command, wire):
    return json.dumps(get_ableton_connection().send_command(command, wire), indent=2)


@mcp.tool(annotations=READ)
def get_test_context(ctx: Context) -> str:
    """Read the current Set, transport, selection and bridge capabilities without editing.

    Unavailable path/dirty/save capabilities remain explicitly unknown. Runtime
    fingerprints expire when the bridge or Set changes; they are not file hashes.
    """
    return _send("get_test_context", {})


@mcp.tool(annotations=READ)
def get_clip_snapshot(
    ctx: Context,
    track_index: TrackIndex,
    clip_index: ClipIndex,
    view: ClipView = "session",
    include_notes: bool = True,
    note_offset: Annotated[int, Field(ge=0)] = 0,
    note_limit: Annotated[int, Field(ge=1, le=4096)] = 512,
) -> str:
    """Read clip identity, markers, timeline and paged native MIDI notes including note_id.

    Arrangement indices come from get_arrangement_clips. Note times are absolute
    clip beats, not Arrangement song time; markers need not start at zero.
    Hidden notes outside markers are included. Use the returned fingerprint for
    modify_notes_by_id; no quantization, duplication or selection changes occur.
    """
    return _send(
        "get_clip_snapshot",
        dict(
            track_index=track_index,
            clip_index=clip_index,
            view=view,
            include_notes=include_notes,
            note_offset=note_offset,
            note_limit=note_limit,
        ),
    )


@mcp.tool(annotations=WRITE)
def modify_notes_by_id(
    ctx: Context,
    track_index: TrackIndex,
    clip_index: ClipIndex,
    changes: Annotated[list[NoteChange], Field(min_length=1, max_length=1024)],
    expected_fingerprint: Fingerprint,
    view: ClipView = "session",
    dry_run: bool = True,
) -> str:
    """Preview or apply individual MIDI edits using native note IDs, without remove/add.

    Defaults to a read-only preview. Set dry_run=false only for an authorized edit
    after inspecting its snapshot. Each change can specify an absolute start_time,
    a start_time_offset_beats, or start_time_offset_ms (exactly one); offsets add
    to the existing microtiming and never snap to a grid. Unspecified fields and
    note identities are retained. Returns readback and inverse changes. A stale
    fingerprint or invalid change fails before applying. Never retry a timeout
    without reading the clip again. MPE/listening acceptance remains separate.
    """
    return _send(
        "modify_notes_by_id",
        dict(
            track_index=track_index,
            clip_index=clip_index,
            changes=[v.model_dump(exclude_none=True) for v in changes],
            expected_fingerprint=expected_fingerprint,
            view=view,
            dry_run=dry_run,
        ),
    )


@mcp.tool(annotations=READ)
def get_device_test_state(
    ctx: Context,
    track_index: TrackIndex,
    device_index: DeviceIndex,
    track_type: TrackType = "track",
    chain_path: ChainPath = [],
) -> str:
    """Read a nested device's identity, parameters, enabled/automation states and capabilities.

    chain_path alternates chain/device indices below the base device. For master,
    track_index must be zero. Unknown remote ownership is not evidence of release.
    Exposed parameter values do not prove loaded binary version, audio or GUI behavior.
    """
    return _send(
        "get_device_test_state", _target(track_index, device_index, track_type, chain_path)
    )


@mcp.tool(annotations=WRITE)
def set_test_device_parameter(
    ctx: Context,
    track_index: TrackIndex,
    device_index: DeviceIndex,
    parameter: DeviceParameter,
    value: DeviceParameterValue,
    expected_fingerprint: Fingerprint,
    expected_value: Annotated[float | None, Field(allow_inf_nan=False)] = None,
    track_type: TrackType = "track",
    chain_path: ChainPath = [],
    dry_run: bool = True,
) -> str:
    """Preview/set a parameter at an explicit nested Rack path with identity and value guards.

    Read get_device_test_state first. Optionally require the raw expected_value
    to reject a concurrent change. Disabled/cannot-change parameters are refused;
    this never removes automation or LFO mappings. Returns actual readback and a
    restore command; a same-value request performs no write. Display units use
    the same validated conversion as set_device_parameter. Defaults to preview.
    """
    wire = _target(track_index, device_index, track_type, chain_path)
    wire.update(
        parameter=parameter,
        value=value,
        expected_fingerprint=expected_fingerprint,
        expected_value=expected_value,
        dry_run=dry_run,
    )
    return _send("set_test_device_parameter", wire)


@mcp.tool(annotations=WRITE)
def set_plugin_editor_state(
    ctx: Context,
    track_index: TrackIndex,
    device_index: DeviceIndex,
    opened: bool,
    expected_fingerprint: Fingerprint,
    track_type: TrackType = "track",
    chain_path: ChainPath = [],
    dry_run: bool = True,
) -> str:
    """Preview/open/close a plug-in editor only when Live exposes is_editor_open.

    Uses device identity from get_device_test_state. Unsupported Live builds return
    unsupported; this does not use keyboard input or guess another GUI property.
    """
    wire = _target(track_index, device_index, track_type, chain_path)
    wire.update(opened=opened, expected_fingerprint=expected_fingerprint, dry_run=dry_run)
    return _send("set_plugin_editor_state", wire)


@mcp.tool(annotations=READ)
def get_clip_automation_snapshot(
    ctx: Context,
    track_index: TrackIndex,
    clip_index: ClipIndex,
    device_index: DeviceIndex,
    parameter: DeviceParameter,
    view: ClipView = "session",
    track_type: TrackType = "track",
    chain_path: ChainPath = [],
    sample_times: Annotated[list[FiniteBeat], Field(max_length=128)] = [],
) -> str:
    """Read an existing clip envelope at explicit clip-beat sample times; never creates one.

    This is sampled evaluation, not a complete breakpoint export. A missing API,
    envelope, or parameter is reported explicitly. No automation is cleared.
    """
    wire = _target(track_index, device_index, track_type, chain_path)
    wire.update(clip_index=clip_index, parameter=parameter, view=view, sample_times=sample_times)
    return _send("get_clip_automation_snapshot", wire)


@mcp.tool(annotations=READ)
def get_max_device_info(
    ctx: Context,
    track_index: TrackIndex,
    device_index: DeviceIndex,
    track_type: TrackType = "track",
    chain_path: ChainPath = [],
) -> str:
    """Read Max-device parameter banks and actually exposed API capabilities.

    Custom Read/Preview/Build/Clear buttons require a device-defined action/status
    contract. This tool does not infer hidden indices or send arbitrary Max messages.
    """
    return _send("get_max_device_info", _target(track_index, device_index, track_type, chain_path))


@mcp.tool(annotations=READ)
def get_parameter_samples(
    ctx: Context,
    track_index: TrackIndex,
    device_index: DeviceIndex,
    parameter_indices: Annotated[list[NonnegativeIndex], Field(min_length=1, max_length=128)],
    track_type: TrackType = "track",
    chain_path: ChainPath = [],
    count: Annotated[int, Field(ge=1, le=50)] = 10,
    interval_ms: Annotated[int, Field(ge=20, le=1000)] = 100,
) -> str:
    """Observe bounded parameter changes with actual UTC timestamps and song positions.

    At most 50 samples and 10 seconds of requested waits. Sleeps happen outside
    Live's main thread. Does not start playback, map LFOs or write parameters.
    Device/schema changes terminate sampling with a partial result; unchanged
    values alone do not prove that remote mapping has been released.
    """
    if (count - 1) * interval_ms > 10000:
        raise ValueError("Requested sampling waits exceed 10 seconds")
    if len(parameter_indices) != len(set(parameter_indices)):
        raise ValueError("parameter_indices must be unique")
    target = _target(track_index, device_index, track_type, chain_path)
    connection = get_ableton_connection()
    started = time.monotonic()
    samples = []
    fingerprint = None
    for index in range(count):
        if index:
            time.sleep(interval_ms / 1000)
        state = connection.send_command("get_device_test_state", target)
        current = state.get("fingerprint")
        if not current:
            return json.dumps(
                dict(status="unsupported", reason="device_identity_unavailable", samples=samples)
            )
        if fingerprint is not None and current != fingerprint:
            return json.dumps(
                dict(status="partial", reason="device_identity_changed", samples=samples)
            )
        fingerprint = current
        by_index = {p["index"]: p for p in state.get("parameters", [])}
        if any(p not in by_index for p in parameter_indices):
            raise ValueError("A requested parameter index does not exist on the target device")
        samples.append(
            dict(
                timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                elapsed_seconds=time.monotonic() - started,
                current_song_time=state.get("current_song_time"),
                parameters=[by_index[p] for p in parameter_indices],
            )
        )
    return json.dumps(
        dict(
            status="complete",
            fingerprint=fingerprint,
            samples=samples,
            transport_modified=False,
            evidence="parameter_observation",
        ),
        indent=2,
    )


@mcp.tool(annotations=READ)
def verify_saved_set(
    ctx: Context,
    file_path: Annotated[
        str, Field(description="Absolute path of the specific saved .als to verify.")
    ],
    expected_sha256: Annotated[str | None, Field(pattern=r"^[0-9a-fA-F]{64}$")] = None,
) -> str:
    """Verify a named existing .als file's gzip/XML, size and SHA-256 without saving or loading it.

    This proves only the disk artifact. It cannot prove that the current unsaved
    Live RAM was saved to this path. Save As remains a separate native/GUI action.
    """
    path = Path(file_path)
    if not path.is_absolute() or path.suffix.lower() != ".als" or not path.is_file():
        raise ValueError("file_path must identify an existing absolute .als file")
    before = path.stat()
    if before.st_size > 64 * 1024 * 1024:
        raise ValueError("Compressed Set exceeds the 64 MiB verification limit")
    raw = path.read_bytes()
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
        xml = stream.read(128 * 1024 * 1024 + 1)
    if len(xml) > 128 * 1024 * 1024:
        raise ValueError("Expanded Set exceeds the 128 MiB verification limit")
    root = ET.fromstring(xml)
    if root.tag != "Ableton" or root.find("LiveSet") is None:
        raise ValueError("File is not an Ableton Live Set")
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise ValueError(
            "Set changed while it was being verified; read again before using this evidence"
        )
    digest = hashlib.sha256(raw).hexdigest()
    return json.dumps(
        dict(
            file_path=str(path),
            valid_als=True,
            bytes=len(raw),
            sha256=digest,
            expected_hash_matches=None
            if expected_sha256 is None
            else digest == expected_sha256.lower(),
            current_live_ram_matches="unknown",
            saved_by_this_call=False,
        ),
        indent=2,
    )
