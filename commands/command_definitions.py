"""
Command definitions for camera control.

E3 Wired commands are loaded from e3_wired_commands.json, populated from
Arlo Confluence via arlochat MCP (confluence_search). Source page:
"Arlo E3 Wired - How to use CLI on Console" (AFS space).

To refresh E3 commands from Confluence: use arlochat MCP in Cursor with
  confluence_search(cql='title ~ "E3 Wired" AND title ~ "CLI"')
then update e3_wired_commands.json with any new commands.
"""

import json
from pathlib import Path
from typing import Any

# E3 Wired model codes that use the Confluence-sourced command list (must match camera_models CAMERA_MODEL_GROUPS)
E3_WIRED_MODELS = {"VMC2070", "VMC3070", "VMC2083", "VMC3083", "VMC2081", "VMC3081", "VMC2073", "VMC3073"}

# Path to the E3 Wired command list (sourced from Confluence)
_THIS_DIR = Path(__file__).resolve().parent
_E3_COMMANDS_JSON = _THIS_DIR / "e3_wired_commands.json"

# Fallback placeholder commands for non-E3 or if JSON missing
_PLACEHOLDER_COMMANDS = [
    {"name": "capture", "description": "Take a photo with the camera", "syntax": "capture [options]", "category": "imaging"},
    {"name": "record", "description": "Start/stop video recording", "syntax": "record [start|stop]", "category": "imaging"},
    {"name": "status", "description": "Get device status", "syntax": "status", "category": "system"},
    {"name": "settings", "description": "View/modify camera settings", "syntax": "settings [get|set <key> <value>]", "category": "config"},
    {"name": "reboot", "description": "Reboot the camera", "syntax": "reboot", "category": "system"},
    {"name": "logs", "description": "Retrieve device logs", "syntax": "logs [--lines <n>]", "category": "diagnostics"},
]


def _load_e3_wired_commands() -> list[dict[str, Any]]:
    """Load E3 Wired command list from JSON (from Confluence)."""
    if not _E3_COMMANDS_JSON.exists():
        return []
    try:
        with open(_E3_COMMANDS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        commands = data.get("commands") or []
        return [dict(c) for c in commands]
    except (json.JSONDecodeError, OSError):
        return []


def load_commands_from_confluence(model_name: str) -> list[dict[str, Any]]:
    """
    Return available commands for the specified camera model.

    For E3 Wired models (VMC2070/3070, VMC2083/3083, VMC2081/3081, VMC2073/3073), returns the
    command list loaded from e3_wired_commands.json (sourced from Arlo
    Confluence via arlochat MCP). For other models, returns placeholder
    commands.
    """
    model_upper = (model_name or "").strip().upper()
    if model_upper in E3_WIRED_MODELS:
        commands = _load_e3_wired_commands()
        if commands:
            return commands
    return _PLACEHOLDER_COMMANDS.copy()
