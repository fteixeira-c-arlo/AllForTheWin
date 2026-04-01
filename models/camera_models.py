"""Camera model definitions.

E3 Wired device data: populated from Arlo Essential (3rd Gen) wired/plug-in
product line. Models are grouped by product family (2K + FHD) for selection;
firmware search uses both model names when applicable.
"""
from typing import Any

# Shared connection defaults for E3 Wired
_DEFAULT_SETTINGS = {
    "adb": {"port": 5555},
    "ssh": {"port": 22, "username": "root"},
}

# E3 Wired model groups: one menu entry per product (2K + FHD grouped).
# name = primary model (2K where applicable); fw_search_models = list used for Artifactory search.
# command_profile = key in commands/command_profiles.json → which JSON loads device CLI commands.
CAMERA_MODEL_GROUPS = [
    {
        "name": "VMC3070",
        "display_name": "E3 Indoor 2K + FHD (VMC3070 / VMC2070) – Wired",
        "fw_search_models": ["VMC3070", "VMC2070"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
    {
        "name": "VMC3083",
        "display_name": "E3 Pan Tilt 2K + FHD (VMC3083 / VMC2083) – Wired",
        "fw_search_models": ["VMC3083", "VMC2083"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
    {
        "name": "VMC3081",
        "display_name": "E3 Pan Tilt 2K + FHD (VMC3081 / VMC2081) – Wired",
        "fw_search_models": ["VMC3081", "VMC2081"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
    {
        "name": "VMC3073",
        "display_name": "E3 Indoor 2K + FHD (VMC3073 / VMC2073) – Wired · codename Octopus",
        "fw_search_models": ["VMC3073", "VMC2073", "Octopus"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
]

# Flat list of all model names (for get_model_by_name and any code that expects single-model list)
CAMERA_MODELS = [
    {
        "name": m["name"],
        "display_name": m["display_name"],
        "supported_connections": m["supported_connections"],
        "default_settings": m["default_settings"],
        "fw_search_models": m["fw_search_models"],
        "command_profile": m.get("command_profile") or "none",
    }
    for m in CAMERA_MODEL_GROUPS
]


def format_supported_connections(supported: list[str] | None) -> str:
    """Human-readable connection list, e.g. 'ADB · SSH · UART'."""
    if not supported:
        return "—"
    order = ("UART", "ADB", "SSH")
    seen = {x.upper() for x in supported}
    parts = [x for x in order if x in seen]
    for x in supported:
        u = x.upper()
        if u not in parts and u in ("UART", "ADB", "SSH"):
            parts.append(u)
    return " · ".join(parts) if parts else " · ".join(supported)


def get_command_profile_for_model_name(model_name: str | None) -> str:
    """
    Resolve command profile id for a detected or selected model name.
    Unknown models get 'none' (no device-specific CLI catalog).
    """
    if not model_name or not str(model_name).strip():
        return "none"
    m = get_model_by_name(str(model_name).strip())
    if not m:
        return "none"
    return (m.get("command_profile") or "none").strip() or "none"


def get_models() -> list[dict[str, Any]]:
    """Return available camera model groups (2K + FHD grouped for E3 Wired)."""
    return [m.copy() for m in CAMERA_MODEL_GROUPS]


def get_model_by_name(name: str) -> dict[str, Any] | None:
    """Return model group dict by name (primary or any fw_search_models), case-insensitive."""
    name_upper = (name or "").strip().upper()
    for m in CAMERA_MODEL_GROUPS:
        if m["name"] == name_upper:
            return m.copy()
        if name_upper in [n.upper() for n in m.get("fw_search_models", [m["name"]])]:
            return m.copy()
    return None
