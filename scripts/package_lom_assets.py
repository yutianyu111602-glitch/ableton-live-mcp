"""Copy the reviewed Max companion into package data; no Live operations."""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = (
    "LOM_API_Bridge.amxd",
    "LOM_API_Bridge.maxpat",
    "lom_schema.json",
    "max_lom_runtime.js",
    "node_bridge.js",
    "README.md",
)


def package():
    source = ROOT / "max_bridge"
    target = ROOT / "ableton_live_mcp/max_bridge"
    if (source / "lom_schema.json").read_bytes() != (
        ROOT / "ableton_live_mcp/lom_schema.json"
    ).read_bytes():
        raise ValueError("Rebuild the Max bridge with the final shared schema before packaging")
    target.mkdir(parents=True, exist_ok=True)
    for name in NAMES:
        shutil.copyfile(source / name, target / name)
        assert (target / name).read_bytes() == (source / name).read_bytes()
    print("Packaged the six matching Max companion resources")


if __name__ == "__main__":
    package()
