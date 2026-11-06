"""Generate the distributable LOM contract from cached official documentation.

Only public technical identifiers, types and signatures are distributed. Full
HTML remains in the ignored work/lom-docs cache, never in package data.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import re
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "work/lom-docs"
INVENTORY = ROOT / "api/lom_inventory.json"
OUTPUT = ROOT / "ableton_live_mcp/lom_schema.json"

# Reviewed complete option tables from the pinned official pages. A lone
# "1 = armed" sentence, continuous endpoints, or special index sentinels are
# explanations, not exhaustive enumerations. Keep those separately as examples.
COMPLETE_INTEGER_ENUM_MEMBERS = {
    "Clip.launch_mode",
    "Clip.launch_quantization",
    "Clip.warp_mode",
    "ClipSlot.playing_status",
    "Device.type",
    "DeviceParameter.automation_state",
    "DeviceParameter.state",
    "Eq8Device.global_mode",
    "Groove.base",
    "MeldDevice.selected_engine",
    "MeldDevice.unison_voices",
    "MeldDevice.mono_poly",
    "MeldDevice.poly_voices",
    "MixerDevice.crossfade_assign",
    "MixerDevice.panning_mode",
    "Sample.beats_granulation_resolution",
    "Sample.beats_transient_loop_mode",
    "Sample.warp_mode",
    "Sample.slicing_style",
    "Sample.slicing_beat_division",
    "ShifterDevice.pitch_mode_index",
    "SimplerDevice.playback_mode",
    "SimplerDevice.slicing_playback_mode",
    "Song.clip_trigger_quantization",
    "Song.count_in_duration",
    "Song.midi_recording_quantization",
    "SpectralResonatorDevice.frequency_dial_mode",
    "SpectralResonatorDevice.midi_gate",
    "SpectralResonatorDevice.mod_mode",
    "SpectralResonatorDevice.mono_poly",
    "SpectralResonatorDevice.pitch_mode",
    "SpectralResonatorDevice.polyphony",
    "Track.fold_state",
    "Track.View.device_insert_mode",
    "WavetableDevice.filter_routing",
    "WavetableDevice.mono_poly",
    "WavetableDevice.oscillator_1_effect_mode",
    "WavetableDevice.unison_mode",
}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "li", "div", "pre"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"p", "li", "div", "pre"}:
            self.parts.append("\n")
        elif tag in {"span", "code", "a"}:
            self.parts.append(" ")

    def handle_data(self, data):
        self.parts.append(data)


def text(fragment):
    parser = TextExtractor()
    parser.feed(fragment)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def fetch_object(obj):
    path = CACHE / (obj["name"] + ".html")
    if not path.exists():
        request = urllib.request.Request(
            obj["url"], headers={"User-Agent": "AbletonMCP schema builder"}
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
        path.write_bytes(data)
    return obj["name"], path.read_bytes().decode("utf-8")


def document_members(source):
    pattern = re.compile(
        r'<h3\b[^>]*id="([^"]+)"[^>]*>(.*?)</h3>(.*?)(?=<h[23]\b|</article>)', re.S
    )
    return {
        html.unescape(match.group(1)): (match.group(2), match.group(3))
        for match in pattern.finditer(source)
    }


def type_spec(raw, object_names):
    raw = " ".join(raw.split()).strip()
    if raw in object_names:
        return {"value_type": "reference", "ref_types": [raw], "collection": False}
    aliases = {
        "int": "int",
        "long": "int",
        "bool": "bool",
        "float": "float",
        "double": "float",
        "float/int": "float",
        "symbol": "symbol",
        "unicode": "symbol",
        "list": "list",
        "bang": "bang",
        "dict": "dictionary",
        "dictionary": "dictionary",
        "dict/bang": "dictionary",
        "id": "reference",
        "live object": "reference",
    }
    if raw == "StringVector":
        return {
            "value_type": "list",
            "ref_types": [],
            "collection": False,
            "items": {"value_type": "symbol"},
        }
    if raw.lower().startswith("list of "):
        child = raw[8:].strip()
        if child in object_names:
            return {
                "value_type": "list",
                "ref_types": [child],
                "collection": True,
                "items": {"value_type": "reference", "ref_types": [child]},
            }
        if child.rstrip("s") in {"int", "symbol"}:
            return {
                "value_type": "list",
                "ref_types": [],
                "collection": False,
                "items": {"value_type": child.rstrip("s")},
            }
        if child == "note IDs":
            return {
                "value_type": "list",
                "ref_types": [],
                "collection": False,
                "items": {"value_type": "int", "minimum": 0},
            }
    return {"value_type": aliases.get(raw.lower(), "native"), "ref_types": [], "collection": False}


def argument_spec(name, type_name, object_names, required=True):
    result = {"name": name, "required": required, **type_spec(type_name, object_names)}
    result.pop("collection", None)
    return result


def parse_signature(body, object_names):
    plain = text(body)
    # Formal parameter listings consist of code identifiers followed by [type].
    parameter = re.search(r"Parameters?:\s*", plain)
    if not parameter:
        # Pages without a parameter section document a zero-argument method.
        return [], "formal", None
    section = plain[parameter.end() :]
    if section.lstrip().startswith("dictionary"):
        return [], "formal", "dictionary"
    section = re.split(r"Returns?:", section, maxsplit=1)[0]
    pairs = re.findall(
        r"\b([A-Za-z][A-Za-z0-9_]*(?: \(optional\))?)\s*\[([^\]]+)\](\s*\(optional\))?", section
    )
    if pairs:
        arguments = []
        for name, type_name, trailing_optional in pairs:
            if type_spec(type_name.strip(), object_names)["value_type"] == "native":
                # Narrative ranges such as 'range [0., 1576800.]' are not
                # argument declarations and must never become fake parameters.
                continue
            optional = "(optional)" in name or bool(trailing_optional)
            clean_name = name.replace(" (optional)", "")
            arguments.append(
                argument_spec(clean_name, type_name.strip(), object_names, not optional)
            )
        if arguments:
            return arguments, "formal", None
    # Preserve the exact public signature, without guessing undocumented types.
    return [], "native_signature_validation", None


def documented_range(owner, member_name, value_type, plain):
    result = {}
    patterns = [
        r"(?:range(?: is|:)?:?|range of)\s*([+-]?\d+(?:\.\d+)?)\s*"
        r"(?:to|\.\.\.|\.\.|–|—)\s*([+-]?\d+(?:\.\d+)?)",
        r"^\s*([+-]?\d+(?:\.\d+)?)\s*\.\.\.\s*([+-]?\d+(?:\.\d+)?)\s*(?:\(|\.|$)",
        r"([+-]?\d+(?:\.\d+)?)\s*(?:\.\.\.|\.\.| to | – | — )\s*([+-]?\d+(?:\.\d+)?)",
        r"between\s+([+-]?\d+(?:\.\d*)?)\s+and\s+([+-]?\d+(?:\.\d*)?)",
        r"range:\s*([+-]?\d+(?:\.\d*)?)\s+-\s+([+-]?\d+(?:\.\d*)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, plain, re.I)
        if match:
            low, high = map(float, match.groups())
            if low <= high:
                result.update(minimum=low, maximum=high)
                break
    # Option tables can occupy one line, so collect every numeric assignment.
    # Classification requires the reviewed table identity, not regex presence.
    examples = sorted({int(value) for value in re.findall(r"(?<![\w.])(-?\d+)\s*=", plain)})
    if examples:
        result["documented_enum_values"] = examples
    if (
        value_type == "int"
        and len(examples) > 1
        and owner + "." + member_name in COMPLETE_INTEGER_ENUM_MEMBERS
    ):
        result.update(enum=examples, enum_exhaustive=True)
    return result


def json_object(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def json_array(items, maximum=4096):
    return {"type": "array", "items": items, "maxItems": maximum}


def enrich_contract(schema):
    """Resolve formatting defects and dictionary modes from the same cached pages.

    These are public type/signature facts, not copied narrative documentation.
    A missing official signature is explicitly marked for native validation.
    """
    objects = schema["objects"]

    def member(owner, name):
        return objects[owner]["members"][name]

    def arg(name, value_type, required=True, **fields):
        return {
            "name": name,
            "value_type": value_type,
            "required": required,
            "ref_types": [],
            **fields,
        }

    def signature(owner, name, arguments, **fields):
        member(owner, name).update(arguments=arguments, signature_validation="formal", **fields)

    def ref(name, types=(), required=True):
        return arg(name, "reference", required, ref_types=list(types))

    def ids(name="note_ids"):
        return arg(name, "list", items={"value_type": "int", "minimum": 0})

    for owner in ("SimplerDevice", "ShifterDevice", "WavetableDevice", "RoarDevice"):
        objects[owner]["parent"] = "Device"
    for owner, name, target in (("Chain", "devices", "Device"), ("DrumPad", "chains", "Chain")):
        member(owner, name).update(
            value_type="list",
            collection=True,
            ref_types=[target],
            items={"value_type": "reference", "ref_types": [target]},
            type_note=(
                "The heading names the element class; "
                "canonical indexed child navigation is a collection."
            ),
        )
    objects["RoarDevice"]["inheritance_note"] = (
        "The official prose repeats 'Roar Device'; Device is the shared device base."
    )
    member("DeviceParameter", "__str__")["mutating"] = False
    member("DeviceParameter", "value")["dynamic_bounds"] = {"minimum": "min", "maximum": "max"}
    for owner in ("Chain", "Clip", "Scene", "Track"):
        member(owner, "color").update(minimum=0, maximum=16777215)
    member("Sample", "beats_transient_envelope").update(minimum=0, maximum=100)
    member("Clip", "velocity_amount").update(minimum=0, maximum=1)
    for owner, name in (
        ("Application.View", "available_main_views"),
        ("Sample", "gain_display_string"),
        ("SimplerDevice", "guess_playback_length"),
        ("Song", "find_device_position"),
    ):
        member(owner, name)["mutating"] = False
    signature("Application", "press_current_dialog_button", [arg("index", "int", minimum=0)])
    for name in ("focus_view", "hide_view", "is_view_visible", "show_view"):
        signature("Application.View", name, [arg("view_name", "symbol")])
    for name in ("scroll_view", "zoom_view"):
        signature(
            "Application.View",
            name,
            [
                arg("direction", "int", enum=[0, 1, 2, 3]),
                arg("view_name", "symbol"),
                arg("modifier_pressed", "bool"),
            ],
        )
    signature("Clip", "clear_envelope", [ref("device_parameter", ["DeviceParameter"])])
    signature("Clip.View", "select_envelope_parameter", [ref("parameter", ["DeviceParameter"])])
    signature("ClipSlot", "create_audio_clip", [arg("path", "symbol")])
    signature("ClipSlot", "create_clip", [arg("length", "float", exclusiveMinimum=0)])
    signature(
        "ClipSlot",
        "fire",
        [arg("record_length", "float", False), arg("launch_quantization", "int", False)],
    )
    signature("ControlSurface", "get_control", [arg("name", "symbol")])
    for name in ("grab_control", "release_control"):
        signature("ControlSurface", name, [ref("control")])
    signature(
        "ControlSurface",
        "send_midi",
        [arg("midi_message", "list", items={"value_type": "int", "minimum": 0, "maximum": 255})],
    )
    sysex = arg("sysex_message", "list", items={"value_type": "int", "minimum": 0, "maximum": 255})
    signature(
        "ControlSurface",
        "send_receive_sysex",
        [sysex],
        argument_modes=[
            [sysex],
            [
                sysex,
                arg("timeout_keyword", "symbol", enum=["timeout"]),
                arg("timeout", "float", minimum=0),
            ],
        ],
    )
    for name in ("create_audio_track", "create_midi_track", "create_scene"):
        signature("Song", name, [arg("index", "int", minimum=-1)])
    for name in (
        "delete_scene",
        "delete_track",
        "delete_return_track",
        "duplicate_scene",
        "duplicate_track",
    ):
        signature("Song", name, [arg("index", "int", minimum=0)])
    signature("Song", "capture_midi", [arg("destination", "int", False, enum=[0, 1, 2])])
    signature("Song", "stop_all_clips", [arg("quantized", "int", False, enum=[0, 1])])
    signature("Song", "trigger_session_record", [arg("record_length", "float", False)])
    for name in ("move_device", "find_device_position"):
        signature(
            "Song", name, [ref("device", ["Device"]), ref("target"), arg("target_position", "int")]
        )
    signature("Song.View", "select_device", [ref("device", ["Device"])])
    signature("Track", "delete_clip", [ref("clip", ["Clip"])])
    for name in ("delete_device", "duplicate_clip_slot"):
        signature("Track", name, [arg("index", "int", minimum=0)])
    signature(
        "Track",
        "duplicate_clip_to_arrangement",
        [ref("clip", ["Clip"]), arg("destination_time", "float")],
    )
    for owner in ("Track", "Chain"):
        signature(
            owner,
            "insert_device",
            [arg("device_name", "symbol"), arg("target_index", "int", False)],
        )
    signature("RackDevice", "insert_chain", [arg("index", "int", False)])
    signature("Sample", "move_slice", [arg("source_time", "int"), arg("destination_time", "int")])
    for owner in ("Track", "TakeLane"):
        signature(
            owner,
            "create_midi_clip",
            [arg("start_time", "float", minimum=0, maximum=1576800), arg("length", "float")],
        )
    signature(
        "Track",
        "create_audio_clip",
        [arg("file_path", "symbol"), arg("position", "float", minimum=0, maximum=1576800)],
    )
    signature(
        "TakeLane",
        "create_audio_clip",
        [arg("file_path", "symbol"), arg("start_time", "float", minimum=0, maximum=1576800)],
    )
    signature(
        "WavetableDevice",
        "get_modulation_value",
        [arg("modulation_target_index", "int"), arg("modulation_source_index", "int")],
    )
    # The setter's official signature omits the amount. Do not invent a third
    # formal argument: primitive arguments receive native signature validation.
    member("WavetableDevice", "set_modulation_value").update(
        signature_validation="native_signature_validation",
        signature_note=(
            "Official parameter listing omits the value being set; "
            "native method validates the actual signature."
        ),
    )

    note_fields = {
        "note_id": {"type": "integer", "minimum": 0},
        "pitch": {"type": "integer", "minimum": 0, "maximum": 127},
        "start_time": {"type": "number"},
        "duration": {"type": "number", "exclusiveMinimum": 0},
        "velocity": {"type": "number", "minimum": 0, "maximum": 127},
        "mute": {"type": "boolean"},
        "probability": {"type": "number", "minimum": 0, "maximum": 1},
        "velocity_deviation": {"type": "number", "minimum": -127, "maximum": 127},
        "release_velocity": {"type": "number", "minimum": 0, "maximum": 127},
    }
    note_return = json_array({"type": "string", "enum": list(note_fields)})
    for name in ("add_new_notes", "apply_note_modifications"):
        fields = dict(note_fields)
        if name == "add_new_notes":
            fields.pop("note_id")
        notes = json_array(
            json_object(
                fields,
                ["pitch", "start_time", "duration"] if name == "add_new_notes" else ["note_id"],
            ),
            4096,
        )
        dictionary = json_object({"notes": notes}, ["notes"])
        signature(
            "Clip",
            name,
            [arg("dictionary", "dictionary", dictionary=dictionary)],
            dictionary=dictionary,
            native_argument_adapter="midi_note_vector",
        )
    note_id_array = json_array({"type": "integer", "minimum": 0})
    for name in (
        "get_notes_by_id",
        "remove_notes_by_id",
        "select_notes_by_id",
        "duplicate_notes_by_id",
    ):
        fields = {"note_ids": note_id_array}
        if name == "get_notes_by_id":
            fields["return"] = note_return
        if name == "duplicate_notes_by_id":
            fields.update(
                destination_time={"type": "number"}, transposition_amount={"type": "integer"}
            )
        signature(
            "Clip",
            name,
            [ids()],
            dictionary=(
                json_object(fields, ["note_ids"])
                if name in {"get_notes_by_id", "duplicate_notes_by_id"}
                else None
            ),
        )
    for name in ("get_all_notes_extended", "get_selected_notes_extended"):
        dictionary = json_object({"return": note_return})
        signature(
            "Clip",
            name,
            [arg("dictionary", "dictionary", False, dictionary=dictionary)],
            dictionary=dictionary,
        )
    get_notes_dictionary = json_object(
        {
            "from_pitch": {"type": "integer"},
            "pitch_span": {"type": "integer"},
            "from_time": {"type": "number"},
            "time_span": {"type": "number"},
            "return": note_return,
        },
        ["from_pitch", "pitch_span", "from_time", "time_span"],
    )
    member("Clip", "get_notes_extended")["dictionary"] = get_notes_dictionary
    marker = json_object(
        {"beat_time": {"type": "number"}, "sample_time": {"type": "number", "minimum": 0}},
        ["beat_time"],
    )
    signature(
        "Clip",
        "add_warp_marker",
        [arg("marker", "dictionary", dictionary=marker)],
        dictionary=marker,
    )
    route = json_object(
        {"display_name": {"type": "string"}, "identifier": {"type": "string"}},
        ["display_name", "identifier"],
    )
    for owner, obj in objects.items():
        for name, definition in obj["members"].items():
            if definition["value_type"] != "dictionary" or definition["kind"] == "functions":
                continue
            if name.startswith("available_") and "routing" in name:
                definition["dictionary"] = json_object({name: json_array(route)}, [name])
            elif "routing" in name:
                definition["dictionary"] = route
            elif name == "warp_markers":
                definition["dictionary"] = {
                    "type": "object",
                    "native_schema_from_readback": True,
                    "documented_item_fields": marker["properties"],
                }
                definition["signature_validation"] = "native_signature_validation"
                definition["dictionary_signature_note"] = (
                    "Read-only result documents marker fields but omits its outer layout; "
                    "retain the actual native result."
                )
            elif owner == "TuningSystem":
                definition["dictionary"] = {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                    "native_schema_from_readback": True,
                }
                definition["signature_validation"] = "native_signature_validation"
                definition["dictionary_signature_note"] = (
                    "Official page does not list keys; backend must derive exact keys/types "
                    "from current native readback before writing."
                )
    for name in (
        "oscillator_1_wavetable_category",
        "oscillator_2_wavetable_category",
        "oscillator_1_wavetable_index",
        "oscillator_2_wavetable_index",
    ):
        member("WavetableDevice", name).update(
            signature_validation="native_signature_validation",
            signature_note=(
                "Official heading does not declare a value type; "
                "native property validates the value."
            ),
        )
    for obj in objects.values():
        for definition in obj["members"].values():
            if definition["value_type"] == "reference" and "set" in definition["access"]:
                # A reference setter can request native null. Whether this
                # particular property may be cleared is a host constraint,
                # verified by the backend write/readback rather than invented
                # from documentation silence. Required function refs stay strict.
                definition.update(nullable=True, native_ref_nullability="host_validated")
            if definition["kind"] == "functions":
                definition["value_type"] = "native"
                definition["return_type_validation"] = "native_result"
            definition.pop("dictionary_mode", None)
            for coverage in definition["operation_coverage"].values():
                coverage["schema"] = (
                    "native_signature_validation"
                    if definition["signature_validation"] == "native_signature_validation"
                    else "formal_access_and_input_contract"
                )


def build_schema(inventory, documents):
    object_names = {obj["name"] for obj in inventory["objects"]}
    objects = {}
    diagnostics = []
    for obj in inventory["objects"]:
        source = documents[obj["name"]]
        extracted = document_members(source)
        h1 = re.search(r"<h1\b", source)
        next_h2 = source.find("<h2", h1.end()) if h1 else 0
        intro = text(source[h1.start() : next_h2]) if h1 else ""
        parent = None
        for line in intro.splitlines():
            if "all " in line or "is a type of" in line:
                candidates = re.findall(r"(?:type of|of a|of an)\s+([A-Za-z][A-Za-z0-9_.]*)", line)
                parent = next(
                    (
                        candidate.rstrip(".")
                        for candidate in candidates
                        if candidate.rstrip(".") in object_names
                        and candidate.rstrip(".") != obj["name"]
                    ),
                    parent,
                )
        if obj["name"] == "this_device":
            parent = "Device"
        members = {}
        for entry in obj["members"]:
            name, kind = entry["name"], entry["kind"]
            if name not in extracted:
                raise ValueError(
                    f"Official cached page is missing inventory member {obj['name']}.{name}"
                )
            heading, body = extracted[name]
            plain = text(body)
            type_match = re.search(
                r'<span[^>]*class="[^"]*c74-api-type[^"]*"[^>]*>(.*?)</span>', heading, re.S
            )
            raw_type = text(type_match.group(1)) if type_match else ""
            documentation_section = kind
            if kind == "children" and raw_type in {"int", "bool", "float", "double", "symbol"}:
                # Groove's official page labels its primitive properties under
                # Children. Preserve the declared identifier, use its real type.
                kind = "properties"
            access_match = re.search(r"Access:\s*([^\n]+)", plain)
            access = (
                re.findall(r"\b(get|set|observe)\b", access_match.group(1)) if access_match else []
            )
            if kind == "functions":
                access = ["call"]
            arguments, signature_validation, dictionary_mode = (
                parse_signature(body, object_names)
                if kind == "functions"
                else ([], "not_applicable", None)
            )
            definition = {
                "name": name,
                "kind": kind,
                "documentation_section": documentation_section,
                **type_spec(raw_type, object_names),
                "documented_type": raw_type or None,
                "access": access,
                "arguments": arguments,
                "dictionary": None,
                "signature_validation": signature_validation,
                "mutating": "set" in access
                or (kind == "functions" and not name.startswith(("get_", "str_for_", "is_"))),
                "url": entry["url"],
                "min_version": entry.get("since_live"),
                "declared_operations": access,
                "operation_coverage": {
                    operation: {"schema": "validated", "runtime_acceptance": "not_run"}
                    for operation in access
                },
            }
            if raw_type == "dict/bang":
                definition["observe_value_type"] = "bang"
            if kind != "functions":
                definition.update(
                    documented_range(obj["name"], name, definition["value_type"], plain)
                )
            if dictionary_mode:
                definition["dictionary_mode"] = dictionary_mode
            if kind == "children":
                # Child headings use 'list of <Object>' or the single class.
                if not definition["ref_types"]:
                    refs = re.findall(r'<a[^>]*title="([^"]+)"[^>]*>', heading)
                    refs = [ref for ref in refs if ref in object_names]
                    if len(refs) == 1:
                        definition.update(ref_types=refs)
                        if "list of" in text(heading).lower():
                            definition.update(
                                value_type="list",
                                collection=True,
                                items={"value_type": "reference", "ref_types": refs},
                            )
                        else:
                            definition.update(value_type="reference", collection=False)
                    else:
                        raise ValueError(f"No formal child object type: {obj['name']}.{name}")
            if signature_validation == "native_signature_validation" or dictionary_mode:
                diagnostics.append({"object": obj["name"], "member": name, "text": plain})
            members[name] = definition
        objects[obj["name"]] = {
            "name": obj["name"],
            "parent": parent,
            "url": obj["url"],
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "members": members,
        }
    schema = {
        "schema_version": 1,
        "source": inventory["source"],
        "documentation_live_version": inventory["documentation_live_version"],
        "native_object_ids": {
            "minimum": -2147483648,
            "maximum": 2147483647,
            "null_id": 0,
            "target_must_be_nonzero": True,
            "scope_validation": "backend_owned",
        },
        "objects": objects,
        "object_count": len(objects),
        "member_count": sum(len(obj["members"]) for obj in objects.values()),
    }
    enrich_contract(schema)
    return schema, diagnostics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Require the cached official pages")
    args = parser.parse_args()
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.offline and any(
        not (CACHE / (obj["name"] + ".html")).exists() for obj in inventory["objects"]
    ):
        raise SystemExit("Official HTML cache incomplete; omit --offline to fetch once")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        documents = dict(pool.map(fetch_object, inventory["objects"]))
    schema, diagnostics = build_schema(inventory, documents)
    OUTPUT.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (CACHE / "signature-review.json").write_text(
        json.dumps(diagnostics, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "objects": schema["object_count"],
                "members": schema["member_count"],
                "signature_review_count": len(diagnostics),
                "output": str(OUTPUT),
            }
        )
    )


if __name__ == "__main__":
    main()
