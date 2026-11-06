"""A complete backend payload is installed/reviewed together with the entry point."""

import pytest

from ableton_live_mcp import cli


def test_installer_copies_all_shared_helpers_and_schema(tmp_path):
    cli.install(tmp_path)
    dest = cli._target(tmp_path)
    payload = cli._remote_script_payload()
    assert set(payload) == {"__init__.py", "lom_catalog.py", "lom_runtime.py", "lom_schema.json"}
    for name, expected in payload.items():
        assert dest.with_name(name).read_bytes() == expected
    assert cli.install(tmp_path) == 0


def test_changed_sidecar_is_preserved_before_any_replacement(tmp_path):
    cli.install(tmp_path)
    dest = cli._target(tmp_path)
    entry = dest.read_bytes()
    sidecar = dest.with_name("lom_runtime.py")
    sidecar.write_bytes(b"user-edited sidecar")
    with pytest.raises(ValueError, match="different"):
        cli.install(tmp_path)
    assert sidecar.read_bytes() == b"user-edited sidecar"
    assert dest.read_bytes() == entry
    cli.install(tmp_path, replace=True)
    assert any(p.read_bytes() == b"user-edited sidecar" for p in dest.parent.glob("*.bak"))


def test_doctor_rejects_a_mismatched_sidecar_even_with_matching_bridge(
    tmp_path, monkeypatch, capsys
):
    cli.install(tmp_path)
    monkeypatch.setattr(cli, "_probe", lambda: {"bridge_version": cli.__version__, "tempo": 120})
    cli._target(tmp_path).with_name("lom_schema.json").write_bytes(b"{}")
    assert cli.doctor(tmp_path, json_output=True) == 1
    assert '"script_matches_package": false' in capsys.readouterr().out
