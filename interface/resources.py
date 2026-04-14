"""Runtime paths for bundled assets (PyInstaller-aware)."""
from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """Directory containing ``styles/``, ``assets/``, and ``interface/``."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent.parent


def resource_path(*relative: str) -> Path:
    """Resolve a path under the project / bundle root."""
    return project_root().joinpath(*relative)
