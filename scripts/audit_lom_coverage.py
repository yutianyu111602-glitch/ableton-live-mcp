"""Reproduce the offline official-LOM implementation ledger; never contact Live.

Default: generate api/full_lom_coverage.json. --check recomputes it and fails on
drift. Only the existing strict fake-LiveAPI matrix is executed. FastMCP tool
registration is inspected in-process without starting its server or lifespan.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ableton_live_mcp.lom_catalog import LomCatalog  # noqa: E402

LEDGER_PATH = "api/full_lom_coverage.json"
MATRIX_PATH = "max_bridge/tests/runtime.test.js"
MATRIX_NAME = (
    "all 588 declared members and 1107 operations reach real adapter methods; previews do not write"
)
EXPECTED_OPERATIONS = {"get": 422, "observe": 313, "call": 163, "set": 209}
GATEWAYS = {"get": "lom_get", "set": "lom_set", "call": "lom_call", "observe": "lom_observe"}
LOM_TOOLS = {
    "lom_describe",
    "lom_resolve",
    "lom_get",
    "lom_set",
    "lom_call",
    "lom_observe",
    "lom_poll",
    "lom_unobserve",
    "lom_capabilities",
}
SOURCE_PATHS = (
    "ableton_live_mcp/lom_schema.json",
    "ableton_live_mcp/lom_catalog.py",
    "ableton_live_mcp/lom_effects.py",
    "ableton_live_mcp/lom_backend.py",
    "ableton_live_mcp/lom_runtime.py",
    "ableton_live_mcp/remote_script/__init__.py",
    "ableton_live_mcp/tools/lom.py",
    "ableton_live_mcp/tools/_groups.py",
    "max_bridge/max_lom_runtime.js",
    "max_bridge/node_bridge.js",
    MATRIX_PATH,
    "api/lom_inventory.json",
    "scripts/audit_lom_coverage.py",
)


class AuditError(ValueError):
    """The pinned declaration or implementation evidence is incomplete."""


def source_hash(path: Path) -> str:
    """Normalize checkout line endings so Windows and CI produce identical ledgers."""
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def registered_tools() -> dict:
    """Read FastMCP's local metadata; do not start a server, lifespan or tool call."""
    old_toolsets = os.environ.pop("ABLETON_TOOLSETS", None)
    try:
        from ableton_live_mcp import tools  # noqa: F401
        from ableton_live_mcp.app import mcp

        definitions = asyncio.run(mcp.list_tools())
    finally:
        if old_toolsets is not None:
            os.environ["ABLETON_TOOLSETS"] = old_toolsets
    names = sorted(tool.name for tool in definitions)
    if len(names) != 175 or len(set(names)) != 175:
        raise AuditError("The default FastMCP registry must expose exactly 175 distinct tools")
    if {name for name in names if name.startswith("lom_")} != LOM_TOOLS:
        raise AuditError("The nine LOM gateways are not registered exactly once")
    annotations = {}
    enum_tools = LOM_TOOLS - {"lom_poll", "lom_unobserve"}
    for tool in definitions:
        if tool.name not in LOM_TOOLS:
            continue
        annotations[tool.name] = tool.annotations.model_dump(exclude_none=True)
        if tool.name in {"lom_get", "lom_describe", "lom_resolve", "lom_capabilities"}:
            if annotations[tool.name].get("readOnlyHint") is not True:
                raise AuditError("A read-only gateway has incorrect effect annotations")
        if tool.name in {"lom_set", "lom_call"}:
            if (
                annotations[tool.name].get("readOnlyHint") is not False
                or annotations[tool.name].get("destructiveHint") is not True
            ):
                raise AuditError("A mutating gateway has incorrect effect annotations")
        if tool.name in enum_tools:
            enumerations = []

            def visit(value):
                if isinstance(value, dict):
                    if "enum" in value:
                        enumerations.append(value["enum"])
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(tool.inputSchema)
            expected = set(LomCatalog().schema["objects"])
            if not any(
                isinstance(values, list) and set(values) == expected for values in enumerations
            ):
                raise AuditError("A gateway omits the exact 45-class object-type enum")
    return {
        "count": 175,
        "lom_gateway_count": 9,
        "toolsets": "default_all",
        "verification": "in_process_fastmcp_metadata_only",
        "server_or_lifespan_started": False,
        "tools_called": False,
        "names": names,
        "lom_annotations": dict(sorted(annotations.items())),
    }


def run_adapter_matrix(root: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        raise AuditError("Node.js is required for the existing strict fake-LiveAPI matrix")
    command = [
        "node",
        "--test",
        "--test-reporter=tap",
        "--test-name-pattern=^" + re.escape(MATRIX_NAME) + "$",
        MATRIX_PATH,
    ]
    completed = subprocess.run(
        [node, *command[1:]],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
        errors="replace",
    )
    passed = re.search(
        r"^ok \d+ - " + re.escape(MATRIX_NAME) + r"\s*$", completed.stdout, flags=re.MULTILINE
    )
    if completed.returncode != 0 or passed is None:
        raise AuditError("The named strict fake-LiveAPI matrix failed; run the recorded command")
    return {
        "status": "passed",
        "kind": "offline_strict_fake_liveapi",
        "test_file": MATRIX_PATH,
        "test_name": MATRIX_NAME,
        "command": command,
        "declared_members_exercised": 588,
        "operations_exercised": 1107,
        "checks": [
            "native_adapter_get_called",
            "preview_does_not_write",
            "setters_and_mutating_calls_executed_after_preview",
            "read_functions_executed",
            "observe_poll_unobserve_executed",
        ],
        "live_or_max_started": False,
        "native_acceptance": "not_assessed_by_this_audit",
    }


def _registered_function_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "tool"
                and isinstance(target.value, ast.Name)
                and target.value.id == "mcp"
            ):
                names.add(node.name)
    return names


def build_ledger(root: Path, *, matrix: dict | None = None, registry: dict | None = None) -> dict:
    root = Path(root)
    catalog = LomCatalog(root / "ableton_live_mcp/lom_schema.json")
    objects = catalog.schema["objects"]
    inventory = json.loads((root / "api/lom_inventory.json").read_text(encoding="utf-8-sig"))
    pinned = {
        obj["name"] + "." + item["name"] for obj in inventory["objects"] for item in obj["members"]
    }
    actual = {name + "." + member for name, obj in objects.items() for member in obj["members"]}
    if len(objects) != 45 or len(actual) != 588 or pinned != actual:
        raise AuditError("Official object/member identity differs from the pinned 45/588 inventory")
    gateways = _registered_function_names(root / "ableton_live_mcp/tools/lom.py")
    if gateways != LOM_TOOLS:
        raise AuditError("The typed LOM tool entrypoints are incomplete")
    runtime = ast.parse((root / "ableton_live_mcp/lom_runtime.py").read_text(encoding="utf-8-sig"))
    runtime_class = next(
        node
        for node in runtime.body
        if isinstance(node, ast.ClassDef) and node.name == "LomRuntime"
    )
    methods = {node.name for node in runtime_class.body if isinstance(node, ast.FunctionDef)}
    if not {"request", "_resolve", "_read", "_set", "_call", "_observe", "close"} <= methods:
        raise AuditError("The Python backend omits an operation entrypoint")
    bridge = (root / "ableton_live_mcp/remote_script/__init__.py").read_text(encoding="utf-8-sig")
    if '"lom_request":' not in bridge or "def _lom_request(" not in bridge:
        raise AuditError("The Remote Script does not register the LOM dispatcher")
    registration = registry if registry is not None else registered_tools()
    evidence = matrix if matrix is not None else {"status": "not_run_by_this_audit"}
    matrix_passed = evidence.get("status") == "passed"
    members, inherited, gaps = [], [], []
    counts = Counter()
    pinned_objects = {obj["name"]: obj for obj in inventory["objects"]}
    for object_type, obj in sorted(objects.items()):
        if obj["source_sha256"] != pinned_objects[object_type]["source_sha256"]:
            raise AuditError("A class source-page hash differs from the pinned official inventory")
        current, lineage, resolved = object_type, [], {}
        while current is not None:
            if current in lineage:
                raise AuditError("An inheritance cycle exists")
            lineage.append(current)
            definition = catalog.object_definition(current)
            for name in definition["members"]:
                resolved.setdefault(name, current)
            current = definition.get("parent")
        inherited_members = []
        for name, declaring in sorted(resolved.items()):
            found = catalog.resolve_member(object_type, name)
            if found["declaring_type"] != declaring:
                raise AuditError("Inherited member resolves to the wrong declaring class")
            if declaring != object_type:
                inherited_members.append({"name": name, "declaring_type": declaring})
        inherited.append(
            {
                "object_type": object_type,
                "parent": obj.get("parent"),
                "source_url": obj["url"],
                "source_page_sha256": obj["source_sha256"],
                "declared_member_count": len(obj["members"]),
                "inherited_member_count": len(inherited_members),
                "inherited_members": inherited_members,
            }
        )
        for name, definition in sorted(obj["members"].items()):
            identifier = object_type + "." + name
            operations = []
            for operation in ("get", "set", "call", "observe"):
                if operation not in definition["access"]:
                    try:
                        catalog.validate_operation(object_type, name, operation)
                    except ValueError:
                        continue
                    raise AuditError("An undocumented operation was accepted")
                checked = catalog.validate_operation(object_type, name, operation)
                if checked["name"] != name or checked["declaring_type"] != object_type:
                    raise AuditError("A declared operation resolves to a different member")
                counts[operation] += 1
                operations.append(
                    {
                        "operation": operation,
                        "mcp_gateway": GATEWAYS[operation],
                        "catalog_access": "validated",
                        "gateway_registration": "verified",
                        "python_route": "implemented_with_runtime_capability_checks",
                        "official_max_route": "strict_fake_exercised"
                        if matrix_passed
                        else "not_exercised_by_this_audit",
                        "native_acceptance": "not_assessed_by_this_audit",
                    }
                )
            members.append(
                {
                    "identifier": identifier,
                    "object_type": object_type,
                    "name": name,
                    "kind": definition["kind"],
                    "official_source_url": definition["url"],
                    "documented_operations": definition["access"],
                    "undeclared_operations": "rejected",
                    "operations": operations,
                }
            )
            if definition.get("signature_validation") == "native_signature_validation":
                gaps.append(
                    {
                        "identifier": identifier,
                        "official_source_url": definition["url"],
                        "documented_type": definition.get("documented_type"),
                        "validation": "bounded_json_then_actual_native_validation",
                        "native_schema_from_readback": bool(
                            (definition.get("dictionary") or {}).get("native_schema_from_readback")
                        ),
                        "note": definition.get("dictionary_signature_note")
                        or (
                            "The pinned official page does not specify the formal type/signature; "
                            "no invented type or argument schema is claimed."
                        ),
                    }
                )
    if dict(counts) != EXPECTED_OPERATIONS or sum(counts.values()) != 1107 or len(gaps) != 11:
        raise AuditError("Documented operation totals or the 11 formal-signature gaps have drifted")
    return {
        "schema_version": 1,
        "status": "offline_full_gateway_coverage"
        if matrix_passed
        else "offline_static_coverage_only",
        "scope": "official_declared_lom_members_and_operations",
        "official_source": catalog.schema["source"],
        "documentation_live_version": catalog.schema["documentation_live_version"],
        "counts": {
            "objects": 45,
            "declared_members": 588,
            "documented_operations": 1107,
            "operations": EXPECTED_OPERATIONS,
            "formal_documentation_gaps": 11,
            "classes_with_parent": sum(obj.get("parent") is not None for obj in objects.values()),
        },
        "limitations": [
            (
                "This is reproducible offline implementation/route evidence, "
                "not native Ableton acceptance."
            ),
            (
                "The existing strict fake executes every declared operation against adapter test "
                "doubles; it does not prove behavior on each Live version."
            ),
            (
                "Python and Max expose different binding conventions. Unsupported, version/edition "
                "gates and backend-bound identities remain explicit."
            ),
            (
                "No Live connection, hardware command, audio processing/listening "
                "or production Set acceptance is performed by this audit."
            ),
        ],
        "native_acceptance": "not_assessed_by_this_offline_audit",
        "legacy_coverage": {
            "superseded_paths": ["api/lom_inventory.json", "api/coverage_overrides.json"],
            "preserved": True,
            "supersedes": "implementation coverage labels only",
            "note": (
                "The old inventory remains the pinned identifier/source-hash input. Its "
                "historical coverage labels and overrides are superseded by this ledger, "
                "not rewritten. Native acceptance remains independent."
            ),
            "python_extensions": (
                "api/python_extensions.json is outside the official 45/588 "
                "denominator and remains separate."
            ),
        },
        "hash_format": (
            "SHA-256 of UTF-8 source text with CRLF normalized to LF; "
            "no machine paths or timestamps."
        ),
        "source_hashes": {path: source_hash(root / path) for path in SOURCE_PATHS},
        "tool_registration": registration,
        "strict_fake_matrix": evidence,
        "inheritance": inherited,
        "formal_documentation_gaps": gaps,
        "members": members,
    }


def serialize(ledger: dict) -> str:
    return json.dumps(ledger, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if the checked-in ledger is stale"
    )
    args = parser.parse_args(argv)
    try:
        matrix = run_adapter_matrix(ROOT)
        ledger = build_ledger(ROOT, matrix=matrix)
        expected = serialize(ledger)
        path = ROOT / LEDGER_PATH
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8-sig") != expected:
                raise AuditError(
                    "Coverage ledger is stale; run python scripts/audit_lom_coverage.py"
                )
            print(
                "LOM coverage ledger verified: "
                "45 objects / 588 members / 1107 operations / 175 tools."
            )
        else:
            path.write_text(expected, encoding="utf-8", newline="\n")
            print(
                "Generated "
                + LEDGER_PATH
                + ": offline coverage; native acceptance is not inferred."
            )
        return 0
    except (AuditError, OSError, subprocess.TimeoutExpired) as error:
        print("LOM coverage audit failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
