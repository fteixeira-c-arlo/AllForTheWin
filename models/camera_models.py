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
CAMERA_MODEL_GROUPS = [
    {
        "name": "VMC3070",
        "display_name": "E3 Indoor 2K + FHD (VMC3070 / VMC2070) – Wired",
        "fw_search_models": ["VMC3070", "VMC2070"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
    },
    {
        "name": "VMC3083",
        "display_name": "E3 Pan Tilt 2K + FHD (VMC3083 / VMC2083) – Wired",
        "fw_search_models": ["VMC3083", "VMC2083"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
    },
    {
        "name": "VMC3081",
        "display_name": "E3 Pan Tilt 2K + FHD (VMC3081 / VMC2081) – Wired",
        "fw_search_models": ["VMC3081", "VMC2081"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
    },
    {
        "name": "VMC3073",
        "display_name": "E3 Indoor 2K + FHD (VMC3073 / VMC2073) – Wired",
        "fw_search_models": ["VMC3073", "VMC2073"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
    },
    {
        "name": "Octopus",
        "display_name": "Octopus (E3 Wired test device – build_info + ASCII banner)",
        "fw_search_models": ["Octopus"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "default_settings": _DEFAULT_SETTINGS,
    },
]

# Flat list of all model names (for get_model_by_name and any code that expects single-model list)
CAMERA_MODELS = [
    {"name": m["name"], "display_name": m["display_name"], "supported_connections": m["supported_connections"], "default_settings": m["default_settings"], "fw_search_models": m["fw_search_models"]}
    for m in CAMERA_MODEL_GROUPS
]


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
