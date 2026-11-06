"""Explicit, recoverable Remote Script setup. Never edits MCP client configuration."""

import argparse
import hashlib
import json
import os
import platform
import socket
import tempfile
from importlib import resources
from pathlib import Path

from . import __version__
from .connection import ABLETON_HOST, ABLETON_PORT

SCRIPT_FOLDER = "AbletonMCP"


def _remote_scripts_dir(user_library=None):
    override = user_library or os.environ.get("ABLETON_USER_LIBRARY")
    if override:
        return Path(override).expanduser().absolute() / "Remote Scripts"
    base = Path.home() / ("Documents" if platform.system() == "Windows" else "Music")
    return base / "Ableton" / "User Library" / "Remote Scripts"


def _remote_script_source():
    return resources.files(__package__).joinpath("remote_script", "__init__.py").read_bytes()


def _remote_script_payload():
    package = resources.files(__package__)
    return {
        "lom_catalog.py": package.joinpath("lom_catalog.py").read_bytes(),
        "lom_runtime.py": package.joinpath("lom_runtime.py").read_bytes(),
        "lom_schema.json": package.joinpath("lom_schema.json").read_bytes(),
        # Entry point last: a reload never sees a new entry point with old helpers.
        "__init__.py": _remote_script_source(),
    }


def _target(user_library=None):
    folder = _remote_scripts_dir(user_library) / SCRIPT_FOLDER
    for candidate in (folder.parent, folder, folder / "__init__.py"):
        if candidate.is_symlink():
            raise ValueError(f"Refusing a symlink at {candidate}; use a real User Library path.")
    return folder / "__init__.py"


def _backup(dest):
    with tempfile.NamedTemporaryFile(
        prefix=dest.name + ".", suffix=".bak", dir=dest.parent, delete=False
    ) as backup:
        backup.write(dest.read_bytes())
        return Path(backup.name)


def _max_bridge_payload():
    package = resources.files(__package__).joinpath("max_bridge")
    names = (
        "LOM_API_Bridge.amxd",
        "LOM_API_Bridge.maxpat",
        "lom_schema.json",
        "max_lom_runtime.js",
        "node_bridge.js",
        "README.md",
    )
    return {name: package.joinpath(name).read_bytes() for name in names}


def install_max(user_library=None, destination=None, replace=False):
    """Copy the optional companion; never load a device or open/change a Set."""
    library = _remote_scripts_dir(user_library).parent
    if destination is None and not library.is_dir():
        raise ValueError("User Library not found; supply --user-library or --destination")
    folder = (
        Path(destination).expanduser().absolute()
        if destination
        else (library / "Presets/MIDI Effects/LOM API Bridge")
    )
    if folder.is_symlink():
        raise ValueError("Refusing a symlink destination")
    payload = _max_bridge_payload()
    changes = []
    for name, raw in payload.items():
        target = folder / name
        if target.is_symlink():
            raise ValueError("Refusing a symlink companion resource")
        if not target.exists() or target.read_bytes() != raw:
            changes.append((target, raw))
    if any(target.exists() for target, _ in changes) and not replace:
        raise ValueError(
            "Different Max companion exists; review and use --replace to retain backups"
        )
    folder.mkdir(parents=True, exist_ok=True)
    for target, raw in changes:
        if target.exists():
            print(f"Recovery copy: {_backup(target)}")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            temporary.replace(target)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    assert all((folder / name).read_bytes() == raw for name, raw in payload.items())
    print(
        f"Max companion ready: {folder / 'LOM_API_Bridge.amxd'}\n"
        "Load one instance in an independent test Set. No Set was opened or modified."
    )
    return 0


def install(user_library=None, replace=False):
    dest = _target(user_library)
    if not dest.parent.parent.parent.is_dir():
        raise ValueError(
            "User Library not found. Check Live Settings, then use "
            "install --user-library '/path/to/User Library'."
        )
    payload = _remote_script_payload()
    changes = []
    for name, source in payload.items():
        target = dest.with_name(name)
        if target.is_symlink():
            raise ValueError(f"Refusing a symlink at {target}")
        if not target.exists() or target.read_bytes() != source:
            changes.append((target, source))
    if not changes:
        print(f"Remote Script already matches {__version__}: {dest}")
        return 0
    if any(target.exists() for target, _ in changes) and not replace:
        raise ValueError(
            f"A different Remote Script exists at {dest}. It may belong to "
            "another Ableton MCP. Review it, then use install --replace to "
            "back up and replace it. Do not enable two bridges on the same port."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    prepared = []
    try:
        for target, source in changes:
            if target.exists():
                print(f"Recovery copy: {_backup(target)}")
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as out:
                temporary = Path(out.name)
                out.write(source)
            temporary.chmod(0o644)
            prepared.append((temporary, target))
        for temporary, target in prepared:
            temporary.replace(target)
        if any(dest.with_name(name).read_bytes() != raw for name, raw in payload.items()):
            raise ValueError("Installed payload readback mismatch; recovery copies are retained")
    finally:
        for temporary, _ in prepared:
            if temporary.exists():
                temporary.unlink()
    print(
        f"Installed {__version__}: {dest}\nRestart Live. Select AbletonMCP in Settings > "
        "Tempo & MIDI > MIDI > Control Surface, Input/Output None. "
        "Older Live versions: Link, Tempo & MIDI or Link/MIDI > MIDI. Then run doctor."
    )
    return 0


def uninstall(user_library=None):
    dest = _target(user_library)
    if not dest.exists():
        print("Nothing to remove.")
        return 0
    payload = _remote_script_payload()
    if any(
        not dest.with_name(name).is_file()
        or dest.with_name(name).is_symlink()
        or dest.with_name(name).read_bytes() != source
        for name, source in payload.items()
    ):
        raise ValueError(
            "Installed script differs from this package. Refusing to remove it; "
            "review and remove it manually if intended."
        )
    # Keep recoverable bytes for every managed sidecar, not just the entry point.
    for name in reversed(payload):
        target = dest.with_name(name)
        backup = _backup(target)
        target.unlink()
        print(f"Removed {target}. Recovery copy: {backup}.")
    print("Restart Live to unload it.")
    return 0


def _probe():
    with socket.create_connection((ABLETON_HOST, ABLETON_PORT), timeout=3) as connection:
        connection.settimeout(15)
        connection.sendall(b'{"type":"get_session_info","params":{}}')
        data = bytearray()
        while len(data) < 1024 * 1024:
            chunk = connection.recv(8192)
            if not chunk:
                break
            data.extend(chunk)
            try:
                reply = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(reply, dict) or reply.get("status") != "success":
                raise ValueError("Remote Script returned an error, not a successful reply.")
            result = reply.get("result")
            if not isinstance(result, dict) or "tempo" not in result:
                raise ValueError("Unexpected service on the configured Ableton port.")
            return result
    raise ValueError("Incomplete or oversized JSON reply from Remote Script.")


def doctor(user_library=None, json_output=False):
    dest = _target(user_library)
    source = _remote_script_source()
    payload = _remote_script_payload()
    installed = dest.is_file()
    report = {
        "package_version": __version__,
        "script_path": str(dest),
        "script_installed": installed,
        "script_matches_package": all(
            dest.with_name(name).is_file()
            and not dest.with_name(name).is_symlink()
            and dest.with_name(name).read_bytes() == raw
            for name, raw in payload.items()
        ),
        "payload": {
            name: {
                "expected_sha256": hashlib.sha256(raw).hexdigest(),
                "matches": dest.with_name(name).is_file()
                and not dest.with_name(name).is_symlink()
                and dest.with_name(name).read_bytes() == raw,
            }
            for name, raw in payload.items()
        },
        "expected_sha256": hashlib.sha256(source).hexdigest(),
        "host": ABLETON_HOST,
        "port": ABLETON_PORT,
        "reachable": False,
        "running_script_matches": False,
    }
    try:
        live = _probe()
        report.update(
            reachable=True,
            live_version=live.get("live_version"),
            running_script_version=live.get("bridge_version"),
            running_script_matches=live.get("bridge_version") == __version__,
        )
    except (OSError, ValueError) as exc:
        report["error"] = str(exc)
    report["ok"] = bool(
        report["script_matches_package"]
        and report["reachable"]
        and report["running_script_matches"]
    )
    if json_output:
        print(json.dumps(report, indent=2))
    else:
        print(f"ableton-live-mcp {__version__} setup check\nScript: {dest}")
        for key in (
            "script_installed",
            "script_matches_package",
            "reachable",
            "running_script_matches",
        ):
            print(f"  [{'ok' if report[key] else 'x '}] {key.replace('_', ' ')}")
        print(
            "All good."
            if report["ok"]
            else "Check the User Library path, install the matching script (review --replace), "
            "restart Live, select AbletonMCP and dismiss modal dialogs. "
            "An older/different bridge cannot pass the running-version check."
        )
    return 0 if report["ok"] else 1


def run(argv):
    parser = argparse.ArgumentParser(
        prog="ableton-live-mcp", description="Run without arguments as an MCP stdio server."
    )
    parser.add_argument("--version", "-V", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "install-max", "uninstall", "doctor"):
        sub = commands.add_parser(name)
        sub.add_argument("--user-library", help="User Library directory, not Remote Scripts")
        if name in ("install", "install-max"):
            sub.add_argument(
                "--replace",
                action="store_true",
                help="Back up and replace a different existing script",
            )
        if name == "doctor":
            sub.add_argument("--json", dest="json_output", action="store_true")
        if name == "install-max":
            sub.add_argument("--destination", help="Output directory for the six companion files")
    if argv == ["version"]:
        argv = ["--version"]
    if argv == ["help"]:
        argv = ["--help"]
    try:
        args = vars(parser.parse_args(argv))
        command = args.pop("command")
        return {
            "install": install,
            "install-max": install_max,
            "uninstall": uninstall,
            "doctor": doctor,
        }[command](**args)
    except (OSError, ValueError) as exc:
        print(f"Setup failed: {exc}")
        return 1
