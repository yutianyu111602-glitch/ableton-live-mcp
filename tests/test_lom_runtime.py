"""Pure fake-Live behavior; passing here does not establish Live acceptance."""

import struct
import weakref
from types import SimpleNamespace

import pytest

from ableton_live_mcp.lom_runtime import LomRuntime


class Clip:
    def __init__(self, name="Phrase"):
        self.name = name
        self.start_marker = 16.0
        self.modification_calls = 0

    def apply_note_modifications(self, *_arguments):
        self.modification_calls += 1
        raise AssertionError("A JSON dictionary must never reach the Python native MIDI API")


class Device:
    class_name = "PluginDevice"
    name = "Synth"
    parameters = []


class Track:
    def __init__(self):
        self.name = "Track"
        self.arrangement_clips = [Clip()]
        self.devices = [Device()]
        self.duplicate_calls = []

    def duplicate_clip_to_arrangement(self, clip, time):
        self.duplicate_calls.append((clip, time))
        return Clip("Returned object")


class Song:
    def __init__(self):
        self.tracks = [Track(), Track()]
        self._tempo = 140.0
        self.tempo_writes = 0
        self.listeners = []
        self.removals = 0
        self.is_playing = False
        self.play_calls = 0
        self.invalid = False
        self.setter_raises = False
        self.readback_raises = False

    @property
    def tempo(self):
        if self.invalid or (self.tempo_writes and self.readback_raises):
            raise RuntimeError("native property unavailable")
        return self._tempo

    @tempo.setter
    def tempo(self, value):
        self._tempo = value
        self.tempo_writes += 1
        if self.setter_raises:
            raise RuntimeError("write acknowledgement lost")

    def add_tempo_listener(self, callback):
        self.listeners.append(callback)

    def remove_tempo_listener(self, callback):
        self.listeners.remove(callback)
        self.removals += 1

    def emit_tempo(self, value):
        self._tempo = value
        for callback in list(self.listeners):
            callback()

    def get_current_beats_song_time(self):
        return "1.1.1"

    def start_playing(self):
        self.play_calls += 1
        self.is_playing = True


class Application:
    pass


class Surface:
    def __init__(self):
        self.scheduled = []

    def schedule_message(self, delay, callback):
        self.scheduled.append((delay, callback))

    def tick(self):
        self.scheduled.pop(0)[1]()


@pytest.fixture
def state():
    song, surface = Song(), Surface()
    clock = SimpleNamespace(value=0.0)
    runtime = LomRuntime(song, Application(), surface, clock=lambda: clock.value)
    yield SimpleNamespace(runtime=runtime, song=song, surface=surface, clock=clock)
    runtime.close()


def request(state, op="get", **arguments):
    return state.runtime.request({"op": op, "path": "live_set", "object_type": "Song", **arguments})


def identity(state, path="live_set", object_type="Song"):
    result = request(state, "resolve", path=path, object_type=object_type)
    assert result["status"] == "ok", result
    return result["target"]["identity"]


def test_scalar_read_and_canonical_children_preserve_source_time(state):
    result = request(state, member="tempo")
    assert result["status"] == "ok" and result["value"] == 140.0
    assert result["target"]["identity"].startswith("python:")
    result = request(
        state,
        path="live_set tracks 0 arrangement_clips 0",
        object_type="Clip",
        member="start_marker",
    )
    assert result["value"] == 16.0


def test_child_collection_pagination_produces_stable_native_references(state):
    first = request(state, member="tracks", limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1
    token = first["value"][0]["$live_id"]
    original = state.song.tracks[0]
    state.song.tracks.reverse()
    result = request(state, path="id " + token, object_type="Track", member="name")
    assert result["status"] == "ok" and result["value"] == original.name


@pytest.mark.parametrize(
    "path,object_type,member",
    [
        ("live_set __dict__", "Song", "tempo"),
        ("live_set tempo", "Song", "tempo"),
        ("live_set tracks -1", "Track", "name"),
        ("live_set tracks 0", "Clip", "name"),
        ("live_set", "Song", "undocumented"),
    ],
)
def test_undocumented_paths_or_forged_runtime_types_are_rejected(state, path, object_type, member):
    result = request(state, path=path, object_type=object_type, member=member)
    assert result["status"] in {"error", "unsupported"}
    assert state.song.tempo_writes == 0


def test_set_preview_cas_and_same_value_have_no_side_effects(state):
    token = identity(state)
    preview = request(state, "set", member="tempo", value=125.0, expected_identity=token)
    assert preview["status"] == "preview" and state.song.tempo_writes == 0
    stale = request(
        state,
        "set",
        member="tempo",
        value=125.0,
        expected_identity=token,
        expected_value=139.0,
        dry_run=False,
    )
    assert stale["status"] == "error" and "stale_value" in stale["error"]
    same = request(
        state, "set", member="tempo", value=140.0, expected_identity=token, dry_run=False
    )
    assert same["status"] == "no_change" and state.song.tempo_writes == 0


def test_stale_identity_after_index_replacement_prevents_writing(state):
    path = "live_set tracks 0 arrangement_clips 0"
    token = identity(state, path, "Clip")
    state.song.tracks[0].arrangement_clips[0] = Clip()
    result = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="start_marker",
        value=17.0,
        expected_identity=token,
        dry_run=False,
    )
    assert result["status"] == "error" and "stale_target" in result["error"]
    assert state.song.tracks[0].arrangement_clips[0].start_marker == 16.0


def test_real_property_write_reads_back_once_and_reports_success(state):
    result = request(
        state,
        "set",
        member="tempo",
        value=125.0,
        expected_identity=identity(state),
        expected_value=140.0,
        dry_run=False,
    )
    assert result["status"] == "ok" and result["readback"] == 125.0
    assert result["verified"] is True and state.song.tempo_writes == 1


@pytest.mark.parametrize("setter_raises,readback_raises", [(True, False), (False, True)])
def test_uncertain_property_write_is_not_retried_or_labelled_unsupported(
    state, setter_raises, readback_raises
):
    state.song.setter_raises = setter_raises
    state.song.readback_raises = readback_raises
    result = request(
        state,
        "set",
        member="tempo",
        value=125.0,
        expected_identity=identity(state),
        dry_run=False,
    )
    assert result["status"] == "partial" and result["write_may_have_occurred"] is True
    assert state.song.tempo_writes == 1
    assert result["readback"] == (125.0 if setter_raises else "unknown")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "125"])
def test_invalid_property_types_and_nonfinite_values_are_zero_write(state, value):
    result = request(
        state,
        "set",
        member="tempo",
        value=value,
        expected_identity=identity(state),
        dry_run=False,
    )
    assert result["status"] == "error" and state.song.tempo_writes == 0


def test_readonly_and_mutating_functions_have_distinct_preview_semantics(state):
    read = request(state, "call", member="get_current_beats_song_time", arguments=[])
    assert read["status"] == "ok" and read["result"] == "1.1.1"
    preview = request(
        state, "call", member="start_playing", arguments=[], expected_identity=identity(state)
    )
    assert preview["status"] == "preview" and state.song.play_calls == 0
    actual = request(
        state,
        "call",
        member="start_playing",
        arguments=[],
        expected_identity=identity(state),
        dry_run=False,
    )
    assert actual["status"] == "ok" and state.song.play_calls == 1
    assert actual["effect_verification"] == "not_available_for_function"


def test_native_reference_arguments_and_returned_object_ids_round_trip(state):
    path = "live_set tracks 0"
    clip = state.song.tracks[0].arrangement_clips[0]
    result = request(
        state,
        "call",
        path=path,
        object_type="Track",
        member="duplicate_clip_to_arrangement",
        arguments=[{"$ref": path + " arrangement_clips 0", "object_type": "Clip"}, 32.0],
        expected_identity=identity(state, path, "Track"),
        dry_run=False,
    )
    assert result["status"] == "ok", result
    assert state.song.tracks[0].duplicate_calls == [(clip, 32.0)]
    returned = result["result"]
    read = request(state, path="id " + returned["$live_id"], object_type="Clip", member="name")
    assert read["status"] == "ok" and read["value"] == "Returned object"
    again = request(
        state,
        "call",
        path=path,
        object_type="Track",
        member="duplicate_clip_to_arrangement",
        arguments=[returned, 64.0],
        expected_identity=identity(state, path, "Track"),
        dry_run=False,
    )
    assert again["status"] == "ok", again


def test_dictionary_midi_operations_require_typed_max_backend_before_native_call(state):
    path = "live_set tracks 0 arrangement_clips 0"
    result = request(
        state,
        "call",
        path=path,
        object_type="Clip",
        member="apply_note_modifications",
        arguments=[{"notes": [{"note_id": 4, "start_time": 1.01}]}],
        expected_identity=identity(state, path, "Clip"),
        dry_run=False,
    )
    assert result["status"] == "unsupported" and "use_max_backend" in result["reason"]
    assert state.song.tracks[0].arrangement_clips[0].modification_calls == 0


def test_this_device_and_unknown_or_foreign_native_ids_are_explicitly_unsupported(state):
    result = request(state, "resolve", path="this_device", object_type="Device")
    assert result["status"] == "unsupported" and "use_max_backend" in result["reason"]
    result = request(state, "resolve", path="id python:foreign:" + "a" * 32, object_type="Track")
    assert result["status"] == "unsupported"


def observe(state, owner="one", **arguments):
    return request(
        state,
        "observe",
        member="tempo",
        owner=owner,
        expected_identity=identity(state),
        **arguments,
    )


def poll(state, subscription_id, owner="one", **arguments):
    return state.runtime.request(
        {"op": "poll", "owner": owner, "subscription_id": subscription_id, **arguments}
    )


def test_observation_uses_real_native_listener_and_bounded_queue(state):
    subscription = observe(state, queue_limit=2)
    assert subscription["status"] == "ok" and len(state.song.listeners) == 1
    assert subscription["subscription_id"].startswith("python:")
    for value in range(5):
        state.song.emit_tempo(value)
    first = poll(state, subscription["subscription_id"], limit=1)
    assert first["events"][0]["value"] == 3 and first["dropped_events"] == 3
    assert first["remaining"] == 1
    second = poll(state, subscription["subscription_id"])
    assert second["events"][0]["value"] == 4 and second["remaining"] == 0


def test_observation_owner_isolation_and_owner_cleanup(state):
    one, two = observe(state), observe(state, owner="two")
    foreign = poll(state, one["subscription_id"], owner="two")
    assert foreign["status"] == "error" and len(state.song.listeners) == 2
    cleanup = state.runtime.request({"op": "cleanup", "owner": "one"})
    assert cleanup["closed_count"] == 1 and len(state.song.listeners) == 1
    assert poll(state, two["subscription_id"], owner="two")["status"] == "ok"


def test_ttl_cleanup_runs_on_scheduled_live_main_thread_without_polling(state):
    subscription = observe(state, ttl_seconds=1)
    assert subscription["ttl_cleanup"] == "main_thread_scheduled"
    state.clock.value = 1.1
    state.surface.tick()
    assert state.song.listeners == [] and state.song.removals == 1
    expired = poll(state, subscription["subscription_id"])
    assert expired["status"] == "error" and expired["reason"] == "expired"


def test_invalid_observed_object_reports_error_and_detaches_listener(state):
    subscription = observe(state)
    state.song.invalid = True
    state.song.emit_tempo(120)
    result = poll(state, subscription["subscription_id"])
    assert result["status"] == "partial" and result["active"] is False
    assert result["events"][0]["reason"] == "object_or_member_invalid"
    assert state.song.listeners == []


@pytest.mark.parametrize(
    "options", [{"ttl_seconds": 0}, {"ttl_seconds": 301}, {"queue_limit": 257}]
)
def test_invalid_observation_bounds_never_register_a_listener(state, options):
    result = observe(state, **options)
    assert result["status"] == "error" and state.song.listeners == []


def test_unobserve_and_close_remove_real_listeners_and_end_runtime(state):
    one, two = observe(state), observe(state, owner="two")
    result = state.runtime.request(
        {"op": "unobserve", "owner": "one", "subscription_id": one["subscription_id"]}
    )
    assert result["status"] == "ok" and len(state.song.listeners) == 1
    assert state.runtime.close()["status"] == "ok" and state.song.listeners == []
    assert poll(state, two["subscription_id"], owner="two")["status"] == "error"


def test_capabilities_without_member_pages_inherited_members_without_calling_them(state):
    result = request(
        state,
        "capabilities",
        path="live_set tracks 0 devices 0",
        object_type="PluginDevice",
        limit=1024,
    )
    assert result["status"] == "ok", result
    members = {item["member"]["name"]: item for item in result["members"]}
    assert members["name"]["member"]["declaring_type"] == "Device"
    assert members["name"]["availability"] == "exposed"
    assert members["is_editor_open"]["availability"] == "unsupported"
    assert all(item["write_or_call_behavior"] == "not_probed" for item in members.values())
    page = request(state, "capabilities", limit=1)
    assert page["status"] == "ok" and len(page["members"]) == 1
    assert page["next_offset"] == 1 and state.song.play_calls == 0


def test_single_function_capability_is_not_a_function_invocation(state):
    result = request(state, "capabilities", member="start_playing")
    assert result["status"] == "ok" and result["native_attribute_present"] is True
    assert result["write_or_call_behavior"] == "not_probed" and state.song.play_calls == 0


def test_specialized_device_type_is_confirmed_before_inherited_member_access(state):
    result = request(state, path="live_set tracks 0 devices 0", object_type="Device", member="name")
    assert result["status"] == "ok" and result["target"]["actual_type"] == "PluginDevice"
    forged = request(
        state, path="live_set tracks 0 devices 0", object_type="MeldDevice", member="name"
    )
    assert forged["status"] == "unsupported"


def test_listener_registration_exception_removes_possible_listener_without_retry(state):
    calls = []

    def uncertain_add(callback):
        calls.append(callback)
        state.song.listeners.append(callback)
        raise RuntimeError("listener acknowledgement lost")

    state.song.add_tempo_listener = uncertain_add
    result = observe(state)
    assert result["status"] == "partial" and result["cleanup_error"] is None
    assert len(calls) == 1 and state.song.listeners == []
    assert result["listener_registration_may_have_occurred"] is True


def test_invalid_listener_cleanup_failure_is_still_reported_by_close(state):
    def failed_remove(_callback):
        raise RuntimeError("native listener detach failed")

    state.song.remove_tempo_listener = failed_remove
    subscription = observe(state)
    state.song.invalid = True
    state.song.emit_tempo(120)
    report = poll(state, subscription["subscription_id"])
    assert report["cleanup_error"] == "native listener detach failed"
    closed = state.runtime.close()
    assert closed["status"] == "partial"
    assert closed["cleanup_errors"] == ["native listener detach failed"]


def test_total_json_item_budget_is_checked_before_any_native_mutation(state):
    result = request(
        state,
        "set",
        member="tempo",
        value=125,
        expected_identity=identity(state),
        dry_run=False,
        unrelated_payload=[[0] * 4096, [0] * 4096],
    )
    assert result["status"] == "error" and "item count" in result["error"]
    assert state.song.tempo_writes == 0


def test_observe_requires_identity_and_rejects_replaced_index_target_before_listener(state):
    missing = request(state, "observe", member="tempo", owner="one")
    assert missing["status"] == "error" and state.song.listeners == []
    path = "live_set tracks 0 arrangement_clips 0"
    old_identity = identity(state, path, "Clip")
    replacement = Clip()
    attached = []
    replacement.add_name_listener = attached.append
    replacement.remove_name_listener = attached.remove
    state.song.tracks[0].arrangement_clips[0] = replacement
    result = request(
        state,
        "observe",
        path=path,
        object_type="Clip",
        member="name",
        owner="one",
        expected_identity=old_identity,
    )
    assert result["status"] == "error" and "stale_target" in result["error"]
    assert attached == []


def test_failed_owner_cleanup_retains_detach_handle_consumes_quota_and_can_recover(state):
    original_remove = state.song.remove_tempo_listener
    fail = {"value": True}

    def uncertain_remove(callback):
        if fail["value"]:
            raise RuntimeError("temporary native detach failure")
        original_remove(callback)

    state.song.remove_tempo_listener = uncertain_remove
    state.runtime.MAX_SUBSCRIPTIONS = 1
    subscription = observe(state)
    result = state.runtime.request({"op": "cleanup", "owner": "one"})
    assert result["status"] == "partial" and result["closed_count"] == 0
    assert result["detach_pending_count"] == 1
    assert result["pending_subscriptions"] == [subscription["subscription_id"]]
    assert len(state.song.listeners) == len(state.runtime.subscriptions) == 1
    blocked = observe(state, owner="two")
    assert blocked["status"] == "error" and "limit" in blocked["error"]
    state.song.emit_tempo(150)
    report = poll(state, subscription["subscription_id"])
    assert report["active"] is False and report["detach_pending"] is True
    assert report["events"] == []
    fail["value"] = False
    cleanup = state.runtime.request({"op": "cleanup", "owner": "one"})
    assert cleanup["status"] == "ok" and cleanup["closed_count"] == 1
    assert state.song.listeners == [] and state.runtime.subscriptions == {}


def test_failed_detach_has_one_bounded_automatic_retry_then_stops_scheduling(state):
    calls = []

    def failed_remove(callback):
        calls.append(callback)
        raise RuntimeError("native detach still unavailable")

    state.song.remove_tempo_listener = failed_remove
    subscription = observe(state, ttl_seconds=1)
    state.clock.value = 1.1
    state.surface.tick()
    assert len(calls) == 1
    state.clock.value = 2.2
    state.surface.tick()
    assert len(calls) == 2 and state.surface.scheduled == []
    for _ in range(10):
        state.clock.value += 100
        state.runtime.expire()
    assert len(calls) == 2
    record = state.runtime.subscriptions[subscription["subscription_id"]]
    assert record["detach_pending"] is True and record["active"] is False
    closed = state.runtime.close()
    assert closed["status"] == "partial"
    assert closed["pending_subscriptions"][0]["subscription_id"] == subscription["subscription_id"]


class DeviceParameter:
    min = 0.0
    max = 1.0

    def __init__(self):
        self._value = 0.5
        self.writes = 0

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self.writes += 1
        self._value = value


@pytest.mark.parametrize("minimum", [0.0, None, float("inf")])
def test_parameter_dynamic_bounds_are_read_and_validated_before_native_setter(state, minimum):
    parameter = DeviceParameter()
    parameter.min = minimum
    state.song.tracks[0].devices[0].parameters = [parameter]
    path = "live_set tracks 0 devices 0 parameters 0"
    result = request(
        state,
        "set",
        path=path,
        object_type="DeviceParameter",
        member="value",
        value=1.5,
        expected_identity=identity(state, path, "DeviceParameter"),
        dry_run=False,
    )
    assert result["status"] == ("error" if minimum == 0.0 else "unsupported")
    assert parameter.writes == 0 and parameter.value == 0.5


class Float32Parameter(DeviceParameter):
    @DeviceParameter.value.setter
    def value(self, value):
        self.writes += 1
        self._value = struct.unpack("f", struct.pack("f", value))[0]


def write_parameter(state, parameter, value):
    state.song.tracks[0].devices[0].parameters = [parameter]
    path = "live_set tracks 0 devices 0 parameters 0"
    return request(
        state,
        "set",
        path=path,
        object_type="DeviceParameter",
        member="value",
        value=value,
        expected_identity=identity(state, path, "DeviceParameter"),
        dry_run=False,
    )


def test_float32_parameter_rounding_is_verified_and_actual_value_is_returned(state):
    parameter = Float32Parameter()
    result = write_parameter(state, parameter, 0.71)
    assert result["status"] == "ok" and result["verified"] is True
    assert result["verification_kind"] == "native_rounding" and result["exact_match"] is False
    assert result["readback"] == 0.7099999785423279
    assert result["no_observable_change"] is False and parameter.writes == 1


def test_fine_native_calibration_is_not_dropped_by_postwrite_float_tolerance(state):
    parameter = DeviceParameter()
    requested = 0.5 + 1e-10
    result = write_parameter(state, parameter, requested)
    assert parameter.writes == 1 and parameter.value == requested
    assert result["status"] == "ok" and result["exact_match"] is True
    assert result["verification_kind"] == "exact"


def test_unobservable_float32_micro_change_is_reported_partial_not_rounding_success(state):
    parameter = Float32Parameter()
    result = write_parameter(state, parameter, 0.5 + 1e-10)
    assert parameter.writes == 1 and parameter.value == 0.5
    assert result["status"] == "partial" and result["verified"] is False
    assert result["verification_kind"] == "no_observable_change"
    assert result["no_observable_change"] is True and result["exact_match"] is False


def test_significant_native_clamping_is_still_a_readback_mismatch(state):
    class ClampingParameter(DeviceParameter):
        @DeviceParameter.value.setter
        def value(self, value):
            self.writes += 1
            self._value = min(0.6, value)

    parameter = ClampingParameter()
    result = write_parameter(state, parameter, 0.71)
    assert parameter.writes == 1 and parameter.value == 0.6
    assert result["status"] == "partial" and result["verified"] is False
    assert result["verification_kind"] == "mismatch" and result["exact_match"] is False


class Groove:
    def __init__(self, name):
        self.name = name


class GroovePool:
    def __init__(self, grooves):
        self.grooves = grooves


def test_nullable_groove_assignment_clear_and_restore_keep_parent_identity(state):
    original, replacement = Groove("Original"), Groove("Replacement")
    state.song.groove_pool = GroovePool([original, replacement])
    clip = state.song.tracks[0].arrangement_clips[0]
    clip.groove = original
    path = "live_set tracks 0 arrangement_clips 0"
    parent_identity = identity(state, path, "Clip")
    old = request(state, path=path, object_type="Clip", member="groove")["value"]
    pool = request(state, path="live_set groove_pool", object_type="GroovePool", member="grooves")
    assert pool["status"] == "ok", pool
    new = pool["value"][1]

    def set_groove(value, expected_value):
        return request(
            state,
            "set",
            path=path,
            object_type="Clip",
            member="groove",
            value=value,
            expected_identity=parent_identity,
            expected_value=expected_value,
            dry_run=False,
        )

    assigned = set_groove(new, old)
    assert assigned["status"] == "ok" and assigned["verified"] is True
    assert clip.groove is replacement and assigned["readback"] == new
    cleared = set_groove(None, new)
    assert cleared["status"] == "ok" and cleared["verified"] is True
    assert clip.groove is None and cleared["readback"] is None
    restored = set_groove(old, None)
    assert restored["status"] == "ok" and restored["verified"] is True
    assert clip.groove is original and restored["readback"] == old
    assert identity(state, path, "Clip") == parent_identity


@pytest.mark.parametrize("value", [{"$live_id": 0}, {"$live_id": "0"}])
def test_nullable_reference_zero_values_normalize_to_none_but_zero_is_not_a_target(state, value):
    clip = state.song.tracks[0].arrangement_clips[0]
    clip.groove = Groove("Original")
    path = "live_set tracks 0 arrangement_clips 0"
    result = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="groove",
        value=value,
        expected_identity=identity(state, path, "Clip"),
        dry_run=False,
    )
    assert result["status"] == "ok" and result["readback"] is None
    assert clip.groove is None
    invalid_target = request(state, "resolve", path="id 0", object_type="Groove")
    assert invalid_target["status"] in {"error", "unsupported"}


def test_nullable_groove_clear_still_requires_current_parent_identity(state):
    original = Groove("Original")
    clip = state.song.tracks[0].arrangement_clips[0]
    clip.groove = original
    result = request(
        state,
        "set",
        path="live_set tracks 0 arrangement_clips 0",
        object_type="Clip",
        member="groove",
        value=None,
        dry_run=False,
    )
    assert result["status"] == "error" and "stale_target" in result["error"]
    assert clip.groove is original


def test_canonical_reference_assignment_is_verified_by_native_identity_not_wire_shape(state):
    original, replacement = Groove("Original"), Groove("Replacement")
    state.song.groove_pool = GroovePool([original, replacement])
    clip = state.song.tracks[0].arrangement_clips[0]
    clip.groove = original
    path = "live_set tracks 0 arrangement_clips 0"
    result = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="groove",
        value={"$ref": "live_set groove_pool grooves 1", "object_type": "Groove"},
        expected_identity=identity(state, path, "Clip"),
        dry_run=False,
    )
    assert result["status"] == "ok" and result["verified"] is True
    assert result["exact_match"] is False
    assert result["verification_kind"] == "native_reference_identity"
    assert clip.groove is replacement
    assert result["readback"]["$live_id"].startswith("python:")


def test_existing_canonical_reference_and_already_null_values_are_zero_write(state):
    original = Groove("Original")
    state.song.groove_pool = GroovePool([original])
    clip = state.song.tracks[0].arrangement_clips[0]
    clip.groove = original
    path = "live_set tracks 0 arrangement_clips 0"
    token = identity(state, path, "Clip")
    result = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="groove",
        value={"$ref": "live_set groove_pool grooves 0", "object_type": "Groove"},
        expected_identity=token,
        dry_run=False,
    )
    assert result["status"] == "no_change" and result["write_may_have_occurred"] is False
    assert clip.groove is original
    clip.groove = None
    null = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="groove",
        value=None,
        expected_identity=token,
        dry_run=False,
    )
    assert null["status"] == "no_change" and null["write_may_have_occurred"] is False


@pytest.mark.parametrize(
    "reference",
    [
        {"$ref": "live_set tracks 0", "object_type": "Groove"},
        {"$ref": "live_set groove_pool grooves 0", "object_type": "Track"},
        {"$live_id": "python:stale:" + "a" * 32, "object_type": "Groove"},
    ],
)
def test_wrong_native_reference_type_or_stale_reference_is_rejected_before_assignment(
    state, reference
):
    original = Groove("Original")
    state.song.groove_pool = GroovePool([original])
    clip = state.song.tracks[0].arrangement_clips[0]
    clip.groove = original
    path = "live_set tracks 0 arrangement_clips 0"
    result = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="groove",
        value=reference,
        expected_identity=identity(state, path, "Clip"),
        dry_run=False,
    )
    assert result["status"] in {"error", "unsupported"}
    assert result["write_may_have_occurred"] is False and clip.groove is original


@pytest.mark.parametrize("unchanged", [True, False])
def test_native_reference_readback_must_be_the_requested_object_not_just_the_same_class(
    state, unchanged
):
    original, replacement, wrong = Groove("Original"), Groove("Replacement"), Groove("Wrong")

    class RedirectingClip(Clip):
        def __init__(self):
            super().__init__()
            self._groove = original

        @property
        def groove(self):
            return self._groove

        @groove.setter
        def groove(self, _value):
            self._groove = original if unchanged else wrong

    state.song.groove_pool = GroovePool([original, replacement, wrong])
    state.song.tracks[0].arrangement_clips[0] = RedirectingClip()
    path = "live_set tracks 0 arrangement_clips 0"
    result = request(
        state,
        "set",
        path=path,
        object_type="Clip",
        member="groove",
        value={"$ref": "live_set groove_pool grooves 1", "object_type": "Groove"},
        expected_identity=identity(state, path, "Clip"),
        dry_run=False,
    )
    assert result["status"] == "partial" and result["verified"] is False
    assert result["no_observable_change"] is unchanged
    assert result["verification_kind"] == ("no_observable_change" if unchanged else "mismatch")


@pytest.mark.parametrize(
    "module_name",
    ["_Framework.ControlSurface", "ableton.v2.control_surface.control_surface"],
)
@pytest.mark.parametrize("reference_kind", ["direct", "weak_reference", "proxy"])
def test_registered_hardware_surface_subclasses_are_confirmed_by_trusted_mro(
    state, module_name, reference_kind
):
    base = type("ControlSurface", (), {"__module__": module_name})
    hardware_type = type(
        "HardwareSpecificSurface",
        (base,),
        {"get_control_names": lambda self: ("Pad", "Knob")},
    )
    hardware = hardware_type()
    registered = {
        "direct": hardware,
        "weak_reference": weakref.ref(hardware),
        "proxy": weakref.proxy(hardware),
    }[reference_kind]
    state.runtime.application.control_surfaces = [registered]
    result = request(
        state,
        "call",
        path="control_surfaces 0",
        object_type="ControlSurface",
        member="get_control_names",
        arguments=[],
    )
    assert result["status"] == "ok", result
    assert result["result"] == ["Pad", "Knob"]
    assert result["write_may_have_occurred"] is False


def test_unrelated_control_surface_class_name_does_not_grant_native_type(state):
    fake_type = type("ControlSurface", (), {"__module__": "unrelated.project"})
    state.runtime.application.control_surfaces = [fake_type()]
    result = request(state, "resolve", path="control_surfaces 0", object_type="ControlSurface")
    assert result["status"] == "unsupported"


def test_provided_surface_identity_is_trusted_without_inventing_hardware_inheritance(state):
    state.surface.get_control_names = lambda: ("Local only",)
    state.runtime.application.control_surfaces = [state.surface]
    result = request(
        state,
        "call",
        path="control_surfaces 0",
        object_type="ControlSurface",
        member="get_control_names",
        arguments=[],
    )
    assert result["status"] == "ok" and result["result"] == ["Local only"]


def test_expired_control_surface_weak_reference_is_not_resolved_as_a_live_target(state):
    base = type("ControlSurface", (), {"__module__": "_Framework.ControlSurface"})
    surface = base()
    reference = weakref.ref(surface)
    state.runtime.application.control_surfaces = [reference]
    del surface
    result = request(state, "resolve", path="control_surfaces 0", object_type="ControlSurface")
    assert result["status"] == "unsupported" and "expired" in result["reason"]
