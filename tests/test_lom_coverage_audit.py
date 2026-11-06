"""Audit artifact/CLI checks; no Live, Max, server, or transport is started."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "coverage_audit", ROOT / "scripts/audit_lom_coverage.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def test_committed_coverage_ledger_has_exact_denominators_and_no_native_acceptance_claim():
    ledger = json.loads((ROOT / AUDIT.LEDGER_PATH).read_text(encoding="utf-8"))
    assert ledger["counts"]["objects"] == 45
    assert ledger["counts"]["declared_members"] == len(ledger["members"]) == 588
    assert ledger["counts"]["documented_operations"] == 1107
    assert ledger["counts"]["operations"] == AUDIT.EXPECTED_OPERATIONS
    assert sum(len(member["operations"]) for member in ledger["members"]) == 1107
    assert ledger["tool_registration"]["count"] == 175
    assert ledger["strict_fake_matrix"]["status"] == "passed"
    assert len(ledger["formal_documentation_gaps"]) == 11
    assert ledger["counts"]["classes_with_parent"] == 20
    assert ledger["native_acceptance"] == "not_assessed_by_this_offline_audit"
    for path in ledger["source_hashes"]:
        assert not Path(path).is_absolute() and "\\" not in path
    assert all(
        member["official_source_url"].startswith(ledger["official_source"])
        for member in ledger["members"]
    )


def test_source_hash_is_independent_of_checkout_line_endings(tmp_path):
    lf, crlf = tmp_path / "lf.py", tmp_path / "crlf.py"
    lf.write_bytes(b"one\ntwo\n")
    crlf.write_bytes(b"one\r\ntwo\r\n")
    assert AUDIT.source_hash(lf) == AUDIT.source_hash(crlf)


def test_check_mode_accepts_matching_ledger_and_rejects_drift_without_rewriting(
    tmp_path, monkeypatch
):
    ledger = {"fixed": "offline evidence"}
    path = tmp_path / AUDIT.LEDGER_PATH
    path.parent.mkdir(parents=True)
    path.write_text(AUDIT.serialize(ledger), encoding="utf-8")
    monkeypatch.setattr(AUDIT, "ROOT", tmp_path)
    monkeypatch.setattr(AUDIT, "run_adapter_matrix", lambda _root: {"status": "passed"})
    monkeypatch.setattr(AUDIT, "build_ledger", lambda _root, **_kwargs: ledger)
    assert AUDIT.main(["--check"]) == 0
    path.write_text('{"stale": true}\n', encoding="utf-8")
    before = path.read_bytes()
    assert AUDIT.main(["--check"]) == 1
    assert path.read_bytes() == before
