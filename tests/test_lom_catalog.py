"""Catalogue contract checks; these never import or operate Ableton Live."""

import json
import math
import re
from pathlib import Path

import pytest

from ableton_live_mcp.lom_catalog import LomCatalog, LomValidationError

ROOT = Path(__file__).parents[1]
CATALOG = LomCatalog()
INVENTORY = json.loads((ROOT / "api/lom_inventory.json").read_text(encoding="utf-8"))
DECLARED = [
    (owner, name) for owner, definition in CATALOG.objects.items() for name in definition["members"]
]
BOOL_SETTERS = [
    (owner, name)
    for owner, definition in CATALOG.objects.items()
    for name, member in definition["members"].items()
    if member["value_type"] == "bool" and "set" in member["access"]
]
REFERENCE_SETTERS = [
    (owner, name)
    for owner, definition in CATALOG.objects.items()
    for name, member in definition["members"].items()
    if member["value_type"] == "reference" and "set" in member["access"]
]


def test_exact_45_objects_and_588_declared_members_match_original_inventory():
    expected = {
        (obj["name"], member["name"]) for obj in INVENTORY["objects"] for member in obj["members"]
    }
    assert len(CATALOG.objects) == 45
    assert len(DECLARED) == 588
    assert set(DECLARED) == expected
    assert set(CATALOG.objects) == {obj["name"] for obj in INVENTORY["objects"]}
    assert all(
        CATALOG.objects[obj["name"]]["source_sha256"] == obj["source_sha256"]
        for obj in INVENTORY["objects"]
    )


@pytest.mark.parametrize("owner,name", DECLARED, ids=[f"{o}.{n}" for o, n in DECLARED])
def test_each_declared_member_accepts_exact_documented_operations(owner, name):
    member = CATALOG.resolve_member(owner, name)
    assert member["declaring_type"] == owner
    assert member["name"] == name
    assert member["access"]
    for operation in ("get", "set", "call", "observe"):
        if operation in member["access"]:
            assert CATALOG.validate_operation(owner, name, operation) == member
        else:
            with pytest.raises(LomValidationError):
                CATALOG.validate_operation(owner, name, operation)
    assert set(member["operation_coverage"]) == set(member["access"])
    assert all(
        row["runtime_acceptance"] == "not_run" for row in member["operation_coverage"].values()
    )
    if member["kind"] == "functions":
        assert member["access"] == ["call"]
    if member["min_version"] is not None:
        assert re.fullmatch(r"\d+\.\d+(?:\.\d+)?", member["min_version"])
        version = tuple(int(p) for p in member["min_version"].split("."))
        assert (1, 0) <= version <= (12, 4, 5)


def test_inheritance_resolves_shared_members_but_preserves_declaring_type():
    assert CATALOG.is_a("DrumChain", "Chain")
    assert CATALOG.is_a("RackDevice", "Device")
    assert CATALOG.is_a("RoarDevice", "Device")
    assert CATALOG.is_a("WavetableDevice", "Device")
    assert CATALOG.is_a("RackDevice.View", "Device.View")
    assert not CATALOG.is_a("Song", "Device")
    assert CATALOG.resolve_member("PluginDevice", "parameters")["declaring_type"] == "Device"
    assert CATALOG.resolve_member("DrumChain", "devices")["declaring_type"] == "Chain"
    assert CATALOG.resolve_member("DeviceParameter", "__str__")["mutating"] is False
    with pytest.raises(LomValidationError):
        CATALOG.resolve_member("Song", "__str__")
    for name in ("__dict__", "__class__", "parameters.value", "getattr", "eval"):
        with pytest.raises(LomValidationError):
            CATALOG.resolve_member("Device", name)


@pytest.mark.parametrize(
    "path",
    [
        "live_set",
        "live_app view",
        "live_set tracks 0 devices 0 chains 1 devices 0 parameters 3",
        "live_set tracks 0 devices 0 drum_pads 60 chains 0 devices 1",
        "live_set tracks 0 arrangement_clips 0 view",
        "live_set tracks 0 clip_slots 2 clip",
        "this_device canonical_parent",
        "control_surfaces 0",
        "live_app control_surfaces 0",
        "id 1234",
        "id -1",
        "id -2",
        "id -2147483648",
        "id 2147483647",
        "id python:scope_01:0123456789abcdef0123456789abcdef",
    ],
)
def test_documented_child_paths_and_scoped_ids_are_normalized(path):
    assert CATALOG.validate_path("  " + path.replace(" ", "  ") + "  ") == path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "song tracks 0",
        "live_set tracks -1",
        "live_set tracks 0 name",
        "live_set tracks 0 __class__",
        "live_set tracks 0 .devices",
        "live_set tracks 0 devices()",
        "live_set tracks 0;print(1)",
        "live_set tracks 0 unknown_child",
        "control_surfaces __dict__",
        "id 0",
        "id -0",
        "id -2147483649",
        "id 2147483648",
        "id arbitrary_string",
        "id python:scope:import_os",
        "live_set " + "canonical_parent " * 64,
    ],
)
def test_invalid_or_arbitrary_attribute_paths_are_rejected(path):
    with pytest.raises(LomValidationError):
        CATALOG.validate_path(path)


def test_strict_types_documented_ranges_and_finite_values():
    pitch = CATALOG.resolve_member("Clip", "pitch_coarse")
    assert CATALOG.validate_value(pitch, 12) == 12
    for value in (True, 1.5, "12", math.nan, math.inf):
        with pytest.raises(LomValidationError):
            CATALOG.validate_value(pitch, value)
    for value in (-49, 49):
        with pytest.raises(LomValidationError):
            CATALOG.validate_value(pitch, value)
    muted = CATALOG.resolve_member("Clip", "muted")
    assert CATALOG.validate_value(muted, True) is True
    assert CATALOG.validate_value(muted, 1) is True
    assert CATALOG.validate_value(muted, 0) is False
    for invalid in (2, -1, 1.0, "1"):
        with pytest.raises(LomValidationError):
            CATALOG.validate_value(muted, invalid)
    gain = CATALOG.resolve_member("Clip", "gain")
    assert CATALOG.validate_value(gain, 0.5) == 0.5
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(gain, 2.0)
    strings = CATALOG.resolve_member("Clip", "name")
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(strings, "x" * 65537)
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(strings, "a\x00b")


def test_reference_types_paths_and_native_reference_scope_are_preserved():
    function = CATALOG.resolve_member("Song.View", "select_device")
    value = {"$ref": "live_set tracks 0 devices 0", "object_type": "PluginDevice"}
    assert CATALOG.validate_arguments(function, [value]) == [value]
    wrong = {"$ref": "live_set", "object_type": "Song"}
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(function, [wrong])
    native = {
        "$live_id": "python:scope:0123456789abcdef0123456789abcdef",
        "object_type": "PluginDevice",
    }
    assert CATALOG.validate_arguments(function, [native]) == [native]
    max_ref = {"$live_id": 7, "identity": "max:scope:7", "object_type": "PluginDevice"}
    assert CATALOG.validate_arguments(function, [max_ref]) == [max_ref]
    for bad in (
        {"$live_id": True},
        {"$live_id": 7, "extra": 1},
        {"$ref": "live_set", "object_type": "Song", "extra": 1},
    ):
        with pytest.raises(LomValidationError):
            CATALOG.validate_arguments(function, [bad])


def test_formal_function_signatures_include_inline_and_optional_arguments():
    scroll = CATALOG.resolve_member("Application.View", "scroll_view")
    assert CATALOG.validate_arguments(scroll, [2, "Arranger", False]) == [2, "Arranger", False]
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(scroll, [2, False])
    move = CATALOG.resolve_member("Sample", "move_slice")
    assert CATALOG.validate_arguments(move, [10, 20]) == [10, 20]
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(move, [10])
    insert = CATALOG.resolve_member("Track", "insert_device")
    assert CATALOG.validate_arguments(insert, ["Compressor"]) == ["Compressor"]
    assert CATALOG.validate_arguments(insert, ["Compressor", 2]) == ["Compressor", 2]
    for owner in ("Track", "TakeLane"):
        function = CATALOG.resolve_member(owner, "create_midi_clip")
        assert [arg["name"] for arg in function["arguments"]] == ["start_time", "length"]
        assert CATALOG.validate_arguments(function, [0.0, 4.0]) == [0.0, 4.0]


def test_strict_midi_note_dictionary_and_read_filter_modes():
    add = CATALOG.resolve_member("Clip", "add_new_notes")
    notes = {"notes": [{"pitch": 60, "start_time": 0, "duration": 0.5, "release_velocity": 64}]}
    assert CATALOG.validate_arguments(add, [notes]) == [notes]
    for bad in (
        {"notes": [{"pitch": 128, "start_time": 0, "duration": 1}]},
        {"notes": [{"pitch": 60, "start_time": 0, "duration": 0}]},
        {"notes": [{"pitch": 60, "duration": 1}]},
        {"notes": [{"pitch": 60, "start_time": 0, "duration": 1, "unknown": 1}]},
        {"notes": [], "unknown": True},
    ):
        with pytest.raises(LomValidationError):
            CATALOG.validate_arguments(add, [bad])
    get = CATALOG.resolve_member("Clip", "get_notes_by_id")
    dictionary = {"note_ids": [1, 2], "return": ["note_id", "release_velocity"]}
    assert CATALOG.validate_arguments(get, [dictionary]) == [dictionary]
    assert CATALOG.validate_arguments(get, [[1, 2]]) == [[1, 2]]
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(get, [{"note_ids": [1], "return": ["unknown"]}])
    for name in ("remove_notes_by_id", "select_notes_by_id"):
        function = CATALOG.resolve_member("Clip", name)
        assert function["dictionary"] is None
        with pytest.raises(LomValidationError):
            CATALOG.validate_arguments(function, [{"note_ids": [1]}])


def test_routing_dictionaries_reject_extra_keys_and_missing_required_keys():
    routing = CATALOG.resolve_member("Track", "input_routing_type")
    value = {"display_name": "No Input", "identifier": "none"}
    assert CATALOG.validate_value(routing, value) == value
    for bad in ({"identifier": "none"}, dict(value, eval="injected")):
        with pytest.raises(LomValidationError):
            CATALOG.validate_value(routing, bad)


def test_native_signature_gaps_are_honest_bounded_data_not_fabricated_keys():
    tuning = CATALOG.resolve_member("TuningSystem", "reference_pitch")
    assert tuning["signature_validation"] == "native_signature_validation"
    assert tuning["dictionary"]["native_schema_from_readback"] is True
    actual_native_data = {"native_key_observed_at_runtime": [440.0]}
    assert CATALOG.validate_value(tuning, actual_native_data) == actual_native_data
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(tuning, {"x": math.nan})
    setter = CATALOG.resolve_member("WavetableDevice", "set_modulation_value")
    assert setter["signature_validation"] == "native_signature_validation"
    assert CATALOG.validate_arguments(setter, [1, 2, 0.5]) == [1, 2, 0.5]
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(
            setter, [{"arbitrary_extra": "not a documented dictionary mode"}]
        )


def test_json_depth_and_size_bounds_apply_before_native_validation():
    descriptor = {"value_type": "native"}
    deep = 0
    for _ in range(18):
        deep = [deep]
    for invalid in (deep, [0] * 8193, object(), math.inf, ["x" * 65536] * 17):
        with pytest.raises(LomValidationError):
            CATALOG.validate_value(descriptor, invalid)


def test_describe_is_bounded_and_does_not_expose_mutable_catalogue_objects():
    result = CATALOG.describe(limit=2)
    assert len(result["objects"]) == 2 and result["next_offset"] == 2
    inherited = CATALOG.describe("PluginDevice", limit=500)
    assert any(member["name"] == "parameters" for member in inherited["members"])
    member = CATALOG.describe("Clip", "name")
    member["access"].clear()
    assert "get" in CATALOG.resolve_member("Clip", "name")["access"]
    for values in ({"limit": 0}, {"offset": -1}, {"limit": True}, {"limit": 501}):
        with pytest.raises(LomValidationError):
            CATALOG.describe(**values)


@pytest.mark.parametrize("owner,name", BOOL_SETTERS, ids=[f"{o}.{n}" for o, n in BOOL_SETTERS])
@pytest.mark.parametrize("value,expected", [(False, False), (True, True), (0, False), (1, True)])
def test_every_documented_boolean_setter_accepts_both_states_and_integer_encoding(
    owner, name, value, expected
):
    member = CATALOG.validate_operation(owner, name, "set")
    assert "enum" not in member
    assert CATALOG.validate_value(member, value) is expected


def test_stale_one_sided_boolean_enum_is_explanatory_not_a_domain_constraint():
    for explained_side in (0, 1):
        old_member = {"value_type": "bool", "enum": [explained_side]}
        assert CATALOG.validate_value(old_member, False) is False
        assert CATALOG.validate_value(old_member, True) is True
        assert CATALOG.validate_value(old_member, 0) is False
        assert CATALOG.validate_value(old_member, 1) is True
    arm = CATALOG.resolve_member("Track", "arm")
    assert arm["documented_enum_values"] == [1]
    assert "enum" not in arm


def test_numeric_examples_and_continuous_endpoints_are_not_exhaustive_enums():
    transient = CATALOG.resolve_member("Sample", "beats_transient_envelope")
    assert transient["documented_enum_values"] == [0, 100]
    assert "enum" not in transient
    assert CATALOG.validate_value(transient, 50.5) == 50.5
    root_note = CATALOG.resolve_member("Song", "root_note")
    assert root_note["documented_enum_values"] == [0, 11]
    assert "enum" not in root_note
    assert CATALOG.validate_value(root_note, 5) == 5
    fired_index = CATALOG.resolve_member("Track", "fired_slot_index")
    assert fired_index["documented_enum_values"] == [-2, -1]
    assert "enum" not in fired_index
    assert CATALOG.validate_value(fired_index, 12) == 12


def test_complete_integer_option_tables_include_all_inline_options():
    crossfade = CATALOG.resolve_member("MixerDevice", "crossfade_assign")
    assert crossfade["enum"] == [0, 1, 2] and crossfade["enum_exhaustive"] is True
    assert CATALOG.validate_value(crossfade, 1) == 1
    assert CATALOG.validate_value(crossfade, 2) == 2
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(crossfade, 3)
    modulation = CATALOG.resolve_member("SpectralResonatorDevice", "mod_mode")
    assert modulation["enum"] == [0, 1, 2, 3]
    for owner, definition in CATALOG.objects.items():
        for member in definition["members"].values():
            if "enum" in member:
                assert member["value_type"] == "int", (owner, member["name"])
                assert member["enum_exhaustive"] is True
                assert len(member["enum"]) > 1


@pytest.mark.parametrize("owner,name", REFERENCE_SETTERS)
def test_reference_setters_accept_native_null_values_without_claiming_host_support(owner, name):
    member = CATALOG.validate_operation(owner, name, "set")
    assert member["nullable"] is True
    assert member["native_ref_nullability"] == "host_validated"
    assert CATALOG.validate_value(member, None) is None
    assert CATALOG.validate_value(member, {"$live_id": 0}) is None
    assert CATALOG.validate_value(member, {"$live_id": "0"}) is None
    declared = member["ref_types"][0]
    assert CATALOG.validate_value(member, {"$ref": "id 0", "object_type": declared}) is None
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(member, {"$live_id": 0, "arbitrary_attribute": "not allowed"})


def test_clip_groove_clear_null_does_not_make_id_zero_a_resolvable_target():
    groove = CATALOG.validate_operation("Clip", "groove", "set")
    assert CATALOG.validate_value(groove, None) is None
    assert (
        CATALOG.validate_value(
            groove, {"$live_id": 0, "identity": "max:scope:0", "object_type": "Groove"}
        )
        is None
    )
    with pytest.raises(LomValidationError):
        CATALOG.validate_path("id 0")
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(groove, {"$live_id": 0, "object_type": "Song"})
    with pytest.raises(LomValidationError):
        CATALOG.validate_value(groove, {"$live_id": False})


def test_required_function_references_stay_nonnullable_unless_explicitly_documented():
    select_device = CATALOG.resolve_member("Song.View", "select_device")
    for null in (None, {"$live_id": 0}, {"$ref": "id 0", "object_type": "Device"}):
        with pytest.raises(LomValidationError):
            CATALOG.validate_arguments(select_device, [null])
    nullable_function = {
        "kind": "functions",
        "signature_validation": "formal",
        "arguments": [
            {
                "name": "device",
                "value_type": "reference",
                "required": True,
                "ref_types": ["Device"],
                "nullable": True,
            }
        ],
    }
    assert CATALOG.validate_arguments(nullable_function, [None]) == [None]
    assert CATALOG.validate_arguments(nullable_function, [{"$live_id": 0}]) == [None]
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(nullable_function, [])
    dictionary = {
        "value_type": "dictionary",
        "dictionary": {
            "type": "object",
            "properties": {"device": {"$ref_types": ["Device"], "nullable": True}},
            "required": ["device"],
            "additionalProperties": False,
        },
    }
    assert CATALOG.validate_value(dictionary, {"device": None}) == {"device": None}


@pytest.mark.parametrize(
    "native_id", [-1, -2, -2147483648, 2147483647, "-1", "-2", "-2147483648", "2147483647"]
)
def test_signed_native_object_references_remain_distinct_from_nonnegative_indices(native_id):
    reference = {
        "$live_id": native_id,
        "identity": "max:scope:" + str(native_id),
        "object_type": "ControlSurface",
    }
    descriptor = {"value_type": "reference", "ref_types": ["ControlSurface"]}
    assert CATALOG.validate_value(descriptor, reference) == reference
    assert CATALOG.validate_path("id " + str(native_id)) == "id " + str(native_id)
    assert CATALOG.schema["native_object_ids"] == {
        "minimum": -2147483648,
        "maximum": 2147483647,
        "null_id": 0,
        "target_must_be_nonzero": True,
        "scope_validation": "backend_owned",
    }


@pytest.mark.parametrize(
    "native_id", [-2147483649, 2147483648, "-2147483649", "2147483648", "-0", -1.0, True]
)
def test_invalid_signed_object_reference_ids_are_rejected(native_id):
    with pytest.raises(LomValidationError):
        CATALOG.validate_value({"value_type": "reference"}, {"$live_id": native_id})


def test_negative_object_ids_do_not_enable_negative_collection_or_midi_note_ids():
    for path in ("live_set tracks -1", "control_surfaces -1", "live_set tracks 0 devices -2"):
        with pytest.raises(LomValidationError):
            CATALOG.validate_path(path)
    for function in (
        "get_notes_by_id",
        "remove_notes_by_id",
        "select_notes_by_id",
        "duplicate_notes_by_id",
    ):
        member = CATALOG.resolve_member("Clip", function)
        with pytest.raises(LomValidationError):
            CATALOG.validate_arguments(member, [[-1]])
        if member.get("dictionary"):
            with pytest.raises(LomValidationError):
                CATALOG.validate_arguments(member, [{"note_ids": [-2]}])
    mutation = CATALOG.resolve_member("Clip", "apply_note_modifications")
    with pytest.raises(LomValidationError):
        CATALOG.validate_arguments(mutation, [{"notes": [{"note_id": -1, "start_time": 1.0}]}])
