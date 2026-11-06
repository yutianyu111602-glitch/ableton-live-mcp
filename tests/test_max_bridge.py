"""Max adapter tests use fake LiveAPI; loopback tests do not launch Live or Max."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_max_runtime_and_transport_with_fake_liveapi():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the companion JavaScript tests")
    result = subprocess.run(
        [node, "--test", str(ROOT / "max_bridge/tests/runtime.test.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_original_amxd_container_and_low_priority_ready_graph(tmp_path):
    path = ROOT / "scripts/build_max_bridge.py"
    spec = importlib.util.spec_from_file_location("build_max_bridge", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.build(tmp_path)
    patch = module.validate_container(output.read_bytes())["patcher"]
    boxes = {b["box"]["id"]: b["box"] for b in patch["boxes"]}
    links = {
        (tuple(p["patchline"]["source"]), tuple(p["patchline"]["destination"]))
        for p in patch["lines"]
    }
    assert boxes["node"]["text"].endswith("@autostart 1 @defer 1 @restart 0")
    assert (("device-ready", 0), ("ready-defer", 0)) in links
    assert (("ready-defer", 0), ("ready-message", 0)) in links
    assert (("node", 0), ("request-defer", 0)) in links
    assert (("request-defer", 0), ("runtime", 0)) in links
    assert (("midi-in", 0), ("midi-out", 0)) in links
    assert (tmp_path / "lom_schema.json").read_bytes() == (
        ROOT / "ableton_live_mcp/lom_schema.json"
    ).read_bytes()
    assert (tmp_path / "node_bridge.js").is_file()
    assert (tmp_path / "max_lom_runtime.js").is_file()
    assert json.loads(
        (tmp_path / "LOM_API_Bridge.maxpat").read_text()
    ) == module.validate_container(output.read_bytes())
