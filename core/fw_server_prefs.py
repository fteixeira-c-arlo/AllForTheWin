"""Persisted firmware HTTP server root (optional; env FW_SERVER_ROOT still wins)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import Any

_PREFS_NAME = "fw_server_prefs.json"
_LEGACY_APP_DIR = "ArloShell"


def _app_base_dir() -> str:
    if sys.platform == "win32":
        return os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(os.path.expanduser("~"), ".local", "share")


def _arlo_app_data_dir() -> str:
    d = os.path.join(_app_base_dir(), "ArloHub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    return d


def _prefs_path() -> str:
    return os.path.join(_arlo_app_data_dir(), _PREFS_NAME)


def _legacy_prefs_path() -> str:
    return os.path.join(_app_base_dir(), _LEGACY_APP_DIR, _PREFS_NAME)


def uses_env_fw_server_root() -> bool:
    return bool((os.environ.get("FW_SERVER_ROOT") or "").strip())


def load_saved_fw_server_root() -> str | None:
    for path in (_prefs_path(), _legacy_prefs_path()):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data: Any = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        root = data.get("root")
        if not isinstance(root, str):
            continue
        t = root.strip()
        if t:
            return t
    return None


def save_fw_server_root(root: str) -> None:
    """Remember root for future sessions (ignored when FW_SERVER_ROOT is set)."""
    if uses_env_fw_server_root():
        return
    p = os.path.abspath(os.path.expandvars(os.path.expanduser((root or "").strip())))
    if not p:
        return
    try:
        with open(_prefs_path(), "w", encoding="utf-8") as f:
            json.dump({"root": p}, f, indent=2)
    except OSError:
        pass


def recommended_user_fw_server_root() -> str:
    """Writable first-time location (does not create on disk until user confirms)."""
    return os.path.join(_arlo_app_data_dir(), "firmware_server")


def create_fw_server_root_directory(path: str) -> tuple[bool, str]:
    """Create server root and parents. Returns (ok, error_message)."""
    p = os.path.abspath(os.path.expandvars(os.path.expanduser((path or "").strip())))
    if not p:
        return False, "Path is empty."
    try:
        os.makedirs(p, exist_ok=True)
    except OSError as e:
        return False, str(e)
    if not os.path.isdir(p):
        return False, f"Not a directory: {p}"
    return True, ""
