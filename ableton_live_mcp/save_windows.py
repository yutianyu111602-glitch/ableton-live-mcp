"""Windows save fallback; no Live restart or Remote Script changes required.

Inspired by the design of bschoepke/ableton-live-mcp PR #18. Require an explicit
existing file because this bridge version cannot report song.file_path. Never
infer the current set from a recent-file list, title alone, or a filesystem scan.
"""

import ctypes
import gzip
import hashlib
import math
import os
import shutil
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
from ctypes import wintypes as W
from pathlib import Path

_SAVE_LOCK = threading.Lock()


def validate_timeout(timeout):
    if not math.isfinite(timeout) or not 0.5 <= timeout <= 30:
        raise ValueError("timeout must be finite and between 0.5 and 30 seconds")


def _fingerprint(path):
    data = path.read_bytes()
    root = ET.fromstring(gzip.decompress(data))
    if root.tag != "Ableton" or root.find("LiveSet") is None:
        raise ValueError("Not an Ableton Live Set")
    return {
        "mtime_ns": path.stat().st_mtime_ns,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _set_name(title):
    name, separator, edition = title.rpartition(" - Ableton Live ")
    if not separator or not edition:
        raise ValueError("Cannot identify the Live main window title")
    return name.removesuffix("*"), name.endswith("*")


def save_existing_set(file_path, timeout=10.0):
    validate_timeout(timeout)
    if sys.platform != "win32":
        return {"saved": False, "error": "windows_only"}
    if not file_path:
        return {
            "saved": False,
            "available": True,
            "error": "file_path_required",
            "note": "Supply the absolute path of the currently open, existing .als. "
            "For a never-saved set, use File > Save As first.",
        }
    path = Path(file_path)
    if not path.is_absolute() or path.suffix.lower() != ".als" or not path.is_file():
        return {"saved": False, "error": "existing_absolute_als_required"}
    if not _SAVE_LOCK.acquire(blocking=False):
        return {"saved": False, "error": "save_in_progress"}
    result = {
        "saved": False,
        "available": True,
        "method": "windows_ctrl_s",
        "file_path": str(path),
        "keystroke_sent": False,
    }
    try:
        desktop = WindowsLive()
        title_before = desktop.title()
        name, _ = _set_name(title_before)
        if name.casefold() != path.stem.casefold():
            raise ValueError("file_path does not match the open Live Set title")
        before = _fingerprint(path)
        # Keep old bytes even if Live's own automatic backup is disabled.
        backup = (
            path.parent / "Backup" / (path.stem + ".mcp-before-save-" + uuid.uuid4().hex + ".als")
        )
        backup.parent.mkdir(exist_ok=True)
        shutil.copy2(path, backup)
        if _fingerprint(backup)["sha256"] != before["sha256"]:
            raise ValueError("Backup verification failed; save was not sent")
        result.update(backup_path=str(backup), before=before, title_before=title_before)
        desktop.save(title_before)
        result["keystroke_sent"] = True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.2)
            title = desktop.title()
            current_name, dirty = _set_name(title)
            if current_name != name:
                result.update(error="set_changed_during_save", title_after=title)
                return result
            try:
                after = _fingerprint(path)
                if after != before and not dirty:
                    # Require a complete gzip/XML plus a stable second read.
                    time.sleep(0.2)
                    if _fingerprint(path) == after and desktop.title() == title:
                        result.update(
                            saved=True,
                            after=after,
                            title_after=title,
                            verification="als_changed_valid_xml_and_clean_title",
                        )
                        return result
            except (OSError, ValueError, EOFError, ET.ParseError, zlib.error):
                pass  # Live may replace the file while it is being read.
        result.update(
            error="save_not_observed",
            title_after=desktop.title(),
            note="Do not retry blindly. The set may already be clean, the path "
            "may identify another copy, or a dialog/focus issue blocked saving.",
        )
        return result
    except (OSError, ValueError, RuntimeError, EOFError, ET.ParseError, zlib.error) as exc:
        result.update(error="save_failed_or_unverified", detail=str(exc))
        return result
    finally:
        _SAVE_LOCK.release()


class WindowsLive:
    """Identify Live by executable and refuse ambiguous windows or modal dialogs."""

    def __init__(self):
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.callback_type = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        declarations = [
            (self.u, "EnumWindows", [self.callback_type, W.LPARAM], W.BOOL),
            (self.u, "IsWindowVisible", [W.HWND], W.BOOL),
            (self.u, "IsWindowEnabled", [W.HWND], W.BOOL),
            (self.u, "IsIconic", [W.HWND], W.BOOL),
            (self.u, "GetWindow", [W.HWND, W.UINT], W.HWND),
            (self.u, "GetLastActivePopup", [W.HWND], W.HWND),
            (self.u, "GetForegroundWindow", [], W.HWND),
            (self.u, "GetWindowThreadProcessId", [W.HWND, ctypes.POINTER(W.DWORD)], W.DWORD),
            (self.u, "GetWindowTextW", [W.HWND, W.LPWSTR, ctypes.c_int], ctypes.c_int),
            (self.u, "SetForegroundWindow", [W.HWND], W.BOOL),
            (self.u, "ShowWindow", [W.HWND, ctypes.c_int], W.BOOL),
            (self.u, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short),
            (self.u, "SendInput", [W.UINT, ctypes.c_void_p, ctypes.c_int], W.UINT),
            (self.k, "OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            (self.k, "CloseHandle", [W.HANDLE], W.BOOL),
            (
                self.k,
                "QueryFullProcessImageNameW",
                [W.HANDLE, W.DWORD, W.LPWSTR, ctypes.POINTER(W.DWORD)],
                W.BOOL,
            ),
        ]
        for library, name, args, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = args, result
        found = []

        @self.callback_type
        def visit(hwnd, _):
            if not self.u.IsWindowVisible(hwnd) or self.u.GetWindow(hwnd, 4):
                return True
            pid = W.DWORD()
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            handle = self.k.OpenProcess(0x1000, False, pid.value)
            if handle:
                try:
                    buf = ctypes.create_unicode_buffer(32768)
                    size = W.DWORD(len(buf))
                    if self.k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        exe = os.path.basename(buf.value).casefold()
                        if exe.startswith("ableton live ") and exe.endswith(".exe"):
                            title = self._title(hwnd)
                            if " - Ableton Live " in title:
                                found.append((hwnd, pid.value))
                finally:
                    self.k.CloseHandle(handle)
            return True

        self.u.EnumWindows(visit, 0)
        if len(found) != 1:
            raise RuntimeError(f"Expected one Live main window, found {len(found)}")
        self.hwnd, self.pid = found[0]
        self._ready()

    def _title(self, hwnd):
        buf = ctypes.create_unicode_buffer(32768)
        self.u.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def title(self):
        return self._title(self.hwnd)

    def _ready(self):
        popup = self.u.GetLastActivePopup(self.hwnd)
        if not self.u.IsWindowEnabled(self.hwnd) or (
            popup != self.hwnd and self.u.IsWindowVisible(popup)
        ):
            raise RuntimeError("Live has an active dialog or owned window; close it first")

    def save(self, expected_title):
        self._ready()
        if self.u.IsIconic(self.hwnd):
            self.u.ShowWindow(self.hwnd, 9)
        self.u.SetForegroundWindow(self.hwnd)
        time.sleep(0.2)
        self._ready()
        if self.u.GetForegroundWindow() != self.hwnd or self.title() != expected_title:
            raise RuntimeError("Live focus/title verification failed; no keys sent")
        if any(
            self.u.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x53)
        ):
            raise RuntimeError("A modifier or S key is held; no keys sent")

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", W.WORD),
                ("wScan", W.WORD),
                ("dwFlags", W.DWORD),
                ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", W.LONG),
                ("dy", W.LONG),
                ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD),
                ("time", W.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        class PAYLOAD(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", W.DWORD), ("payload", PAYLOAD)]

        def key(vk, up=0):
            value = INPUT()
            value.type = 1
            value.payload.ki = KEYBDINPUT(vk, 0, up, 0, 0)
            return value

        keys = (INPUT * 4)(key(0x11), key(0x53), key(0x53, 2), key(0x11, 2))
        count = self.u.SendInput(4, keys, ctypes.sizeof(INPUT))
        if count != 4:
            releases = (INPUT * 2)(key(0x53, 2), key(0x11, 2))
            self.u.SendInput(2, releases, ctypes.sizeof(INPUT))
            raise RuntimeError(f"Partial SendInput ({count}/4); inspect Live before retrying")
