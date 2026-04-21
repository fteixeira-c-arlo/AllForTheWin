"""
Command definitions for camera control.

Device-specific commands are loaded per product line using core/command_profiles.json.
Each profile points at a JSON file (e.g. e3_wired_commands.json from Confluence).

To add another product line:
  1. Add core/your_line_commands.json (same shape as e3_wired_commands.json: { "commands": [...] }).
  2. Add a profile entry in command_profiles.json with "commands_file": "your_line_commands.json".
  3. In core/camera_models.py, set "command_profile" on the relevant CAMERA_MODEL_GROUPS entries.
"""

import json
from pathlib import Path
from typing import Any

from core.camera_models import get_command_profile_for_model_name

_THIS_DIR = Path(__file__).resolve().parent
_PROFILES_JSON = _THIS_DIR / "command_profiles.json"


def _load_profiles_manifest() -> dict[str, Any]:
    if not _PROFILES_JSON.exists():
        return {}
    try:
        with open(_PROFILES_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_device_commands_for_profile(profile_id: str) -> list[dict[str, Any]]:
    """
    Load device (camera CLI) commands for a profile id from the manifest.
    Profile 'none' or missing file → empty list.
    """
    pid = (profile_id or "none").strip() or "none"
    if pid == "none":
        return []
    manifest = _load_profiles_manifest()
    entry = manifest.get(pid)
    if not isinstance(entry, dict):
        return []
    fname = entry.get("commands_file")
    if not fname or not isinstance(fname, str):
        return []
    path = _THIS_DIR / fname
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        commands = data.get("commands") or []
        return [dict(c) for c in commands]
    except (json.JSONDecodeError, OSError):
        return []


def load_device_commands_for_model(model_name: str | None) -> list[dict[str, Any]]:
    """Load device commands for a detected/selected model (via its command_profile)."""
    profile = get_command_profile_for_model_name(model_name)
    return load_device_commands_for_profile(profile)


def load_device_commands(model_name: str) -> list[dict[str, Any]]:
    """
    Backwards-compatible name: load device commands for this model's profile.
    (E3 Wired still uses e3_wired_commands.json when profile is e3_wired.)
    """
    return load_device_commands_for_model(model_name)


def get_command_profile_manifest_entry(profile_id: str) -> dict[str, Any]:
    """Return the manifest entry for a profile id, or {} if missing."""
    pid = (profile_id or "none").strip() or "none"
    manifest = _load_profiles_manifest()
    entry = manifest.get(pid)
    return dict(entry) if isinstance(entry, dict) else {}


def get_profile_abstract_command_allowlist(profile_id: str) -> list[str] | None:
    """
    Optional per-profile filter for user-visible / dispatchable abstract commands.

    Returns:
      None — no filter; all entries from abstract_command_definitions.json apply.
      []   — empty allowlist (no abstract commands for this profile).
      list of names — only those abstracts (matched case-insensitively to JSON ``name``).
    """
    entry = get_command_profile_manifest_entry(profile_id)
    raw = entry.get("abstract_commands")
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    return [str(x) for x in raw]
