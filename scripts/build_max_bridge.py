"""Build an original MIDI-pass-through M4L LOM bridge container.

The AMXD chunk structure is the uncompressed format used by Ableton's installed
blank MIDI Effect template. No template patcher, artwork or product code is copied.
Building and validating this container is not a Live/Max load acceptance test.
"""

import argparse
import json
import shutil
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "max_bridge"


def box(identity, text, x, y, width=240, maxclass="newobj"):
    return {
        "box": {
            "id": identity,
            "maxclass": maxclass,
            "text": text,
            "patching_rect": [x, y, width, 22],
        }
    }


def patcher():
    boxes = [
        box("midi-in", "midiin", 30, 270, 60),
        box("midi-out", "midiout", 30, 320, 60),
        box("device-ready", "live.thisdevice", 30, 80, 100),
        box("ready-defer", "deferlow", 30, 120, 80),
        box("ready-message", "ready", 30, 160, 60, "message"),
        box("node", "node.script node_bridge.js @autostart 1 @defer 1 @restart 0", 180, 80, 430),
        box("request-defer", "deferlow", 180, 125, 80),
        box("runtime", "js max_lom_runtime.js", 180, 205, 180),
        box("label", "LOM API Bridge — local loopback only", 30, 20, 500, "comment"),
        box(
            "note",
            "Load one instance in a dedicated test Set. Default writes are previews.",
            30,
            45,
            560,
            "comment",
        ),
        box("close", "closebang", 420, 270, 80),
        box("shutdown", "shutdown", 420, 310, 80, "message"),
    ]
    links = [
        ("midi-in", 0, "midi-out", 0),
        ("device-ready", 0, "ready-defer", 0),
        ("ready-defer", 0, "ready-message", 0),
        ("ready-message", 0, "runtime", 0),
        ("node", 0, "request-defer", 0),
        ("request-defer", 0, "runtime", 0),
        ("runtime", 0, "node", 0),
        ("close", 0, "shutdown", 0),
        ("shutdown", 0, "node", 0),
    ]
    return {
        "patcher": {
            "fileversion": 1,
            "appversion": {
                "major": 8,
                "minor": 6,
                "revision": 5,
                "architecture": "x64",
                "modernui": 1,
            },
            "classnamespace": "box",
            "rect": [80, 80, 700, 390],
            "openinpresentation": 0,
            "default_fontsize": 12.0,
            "default_fontface": 0,
            "default_fontname": "Arial",
            "gridonopen": 1,
            "gridsize": [15.0, 15.0],
            "boxes": boxes,
            "lines": [
                {"patchline": {"source": [a, ao], "destination": [b, bi]}} for a, ao, b, bi in links
            ],
            "dependency_cache": [
                {"name": "max_lom_runtime.js", "type": "TEXT", "implicit": 1},
                {"name": "node_bridge.js", "type": "TEXT", "implicit": 1},
                {"name": "lom_schema.json", "type": "JSON", "implicit": 1},
            ],
            "autosave": 0,
        }
    }


def container(payload):
    raw = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8") + b"\0"
    return (
        b"ampf"
        + struct.pack("<I", 4)
        + b"mmmmmeta"
        + struct.pack("<II", 4, 0)
        + b"ptch"
        + struct.pack("<I", len(raw))
        + raw
    )


def validate_container(raw):
    if raw[:24] != b"ampf\x04\0\0\0mmmmmeta\x04\0\0\0\0\0\0\0" or raw[24:28] != b"ptch":
        raise ValueError("Invalid uncompressed MIDI AMXD chunk header")
    size = struct.unpack("<I", raw[28:32])[0]
    if size != len(raw) - 32 or raw[-1:] != b"\0":
        raise ValueError("Invalid AMXD patch chunk size")
    return json.loads(raw[32:-1])


def build(destination=DEST, schema=None):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    schema = Path(schema) if schema else ROOT / "ableton_live_mcp/lom_schema.json"
    if not schema.is_file():
        raise ValueError("Generate the shared official LOM schema before building the device")
    model = json.loads(schema.read_text(encoding="utf-8"))
    if len(model["objects"]) < 30:
        raise ValueError("Incomplete shared official API schema")
    shutil.copyfile(schema, destination / "lom_schema.json")
    if destination.resolve() != DEST.resolve():
        for filename in ("max_lom_runtime.js", "node_bridge.js", "README.md"):
            shutil.copyfile(DEST / filename, destination / filename)
    payload = patcher()
    (destination / "LOM_API_Bridge.maxpat").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    raw = container(payload)
    assert validate_container(raw) == payload
    path = destination / "LOM_API_Bridge.amxd"
    path.write_bytes(raw)
    return path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEST)
    parser.add_argument("--schema", type=Path)
    args = parser.parse_args()
    print(build(args.destination, args.schema))
