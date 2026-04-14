"""User-writable app directories.

Avoid ``os.getcwd()`` for logs: the GUI or shell may start with cwd set to
``C:\\Windows\\System32`` (WinError 5 when creating ``arlo_logs`` there).
"""
from __future__ import annotations

import os
import sys
import tempfile

from core.app_metadata import APP_NAME


def _app_state_root() -> str:
    """``%LOCALAPPDATA%/ArloHub`` (Windows) or ``~/.cache/ArloHub`` (Unix)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~") or tempfile.gettempdir()
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    d = os.path.join(base, APP_NAME)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = os.path.join(tempfile.gettempdir(), APP_NAME)
        os.makedirs(d, exist_ok=True)
    return d


def get_arlo_logs_dir() -> str:
    """Directory for ``system_log_*.log``, parse reports, etc. Always writable."""
    d = os.path.join(_app_state_root(), "arlo_logs")
    os.makedirs(d, exist_ok=True)
    return d
