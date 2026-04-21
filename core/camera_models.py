"""Camera model definitions.

E3 Wired device data: populated from Arlo Essential (3rd Gen) wired/plug-in
product line. Models are grouped by product family (2K + FHD) for selection;
firmware search uses both model names when applicable.
"""
from typing import Any

from core.device_registry import DEVICE_REGISTRY, registry_entry_to_camera_group

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
        "codename": "dolphin",
        "display_name": "Arlo Essential 3 Indoor Wired (Dolphin)",
        "fw_search_models": ["VMC3070", "VMC2070"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "connection_types": ["uart", "ssh", "adb"],
        "adb_supported": True,
        "platform": "e3_wired",
        "default_uart_baud": 1_500_000,
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
    {
        "name": "VMC3073",
        "codename": "octopus",
        "display_name": "Arlo Essential 3 Indoor Wired PTZ (Octopus)",
        "fw_search_models": ["VMC3073", "VMC2073", "Octopus"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "connection_types": ["uart", "ssh", "adb"],
        "adb_supported": True,
        "platform": "e3_wired",
        "default_uart_baud": 1_500_000,
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
    {
        "name": "VMC3081",
        "codename": "orca",
        "display_name": "Arlo Essential 3 Outdoor Wired (Orca)",
        "fw_search_models": ["VMC3081", "VMC2081"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "connection_types": ["uart", "ssh", "adb"],
        "adb_supported": True,
        "platform": "e3_wired",
        "default_uart_baud": 1_500_000,
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
    {
        "name": "VMC3083",
        "codename": "jellyfish",
        "display_name": "Arlo Essential 3 Outdoor Wired PTZ (Jellyfish)",
        "fw_search_models": ["VMC3083", "VMC2083"],
        "supported_connections": ["ADB", "SSH", "UART"],
        "connection_types": ["uart", "ssh", "adb"],
        "adb_supported": True,
        "platform": "e3_wired",
        "default_uart_baud": 1_500_000,
        "default_settings": _DEFAULT_SETTINGS,
        "command_profile": "e3_wired",
    },
]

# Finch, Robin, Swallow, Pro 5, Pro 6, Lory, Parrot — from device_registry (platform + UART defaults).
CAMERA_MODEL_GROUPS = CAMERA_MODEL_GROUPS + [
    registry_entry_to_camera_group(e) for e in DEVICE_REGISTRY
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


def _codename_title(cod: str) -> str:
    return cod.replace("_", " ").strip().title()


def format_connect_dialog_device_label(m: dict[str, Any]) -> str:
    """Dropdown label: codename + model ID pair (E3 Wired and registry); fallback name + connections."""
    conns = format_supported_connections(m.get("supported_connections"))
    plat = (m.get("platform") or "").strip().lower()
    cod = (m.get("codename") or "").strip()
    primary = str(m.get("name") or "").strip().upper()
    ids_upper = [
        str(x).strip().upper()
        for x in (m.get("fw_search_models") or [])
        if str(x).strip()
    ]
    hw_ids = [x for x in ids_upper if x.startswith(("VMC", "AVD"))]
    if not hw_ids:
        hw_ids = ids_upper

    if plat == "e3_wired" and cod:
        vmcs = [x for x in hw_ids if x.startswith("VMC")]
        if primary and len(vmcs) >= 2:
            others = [x for x in vmcs if x != primary]
            if others:
                return f"{_codename_title(cod)} — {primary} / {others[0]}"
    elif cod:
        # Registry (Finch, Robin, Caracara, Kea, …): same Codename — ID / ID pattern as E3, plus transports.
        if primary and len(hw_ids) >= 2:
            others = [x for x in hw_ids if x != primary]
            if others:
                return f"{_codename_title(cod)} — {primary} / {others[0]}  ({conns})"
        if primary:
            return f"{_codename_title(cod)} — {primary}  ({conns})"
        if hw_ids:
            return f"{_codename_title(cod)} — {hw_ids[0]}  ({conns})"
        return f"{_codename_title(cod)}  ({conns})"

    name = str(m.get("name") or "")
    return f"{name}  ({conns})"


def connection_methods_upper(m: dict[str, Any] | None) -> list[str]:
    """Ordered connection keys as UART, ADB, SSH (subset), from registry-style data."""
    if not m:
        return []
    ct = m.get("connection_types")
    if isinstance(ct, list) and len(ct) > 0:
        return [str(c).strip().upper() for c in ct if str(c).strip()]
    return [str(x).strip().upper() for x in (m.get("supported_connections") or [])]


def model_supports_adb(m: dict[str, Any] | None) -> bool:
    if not m or m.get("adb_supported") is False:
        return False
    return "ADB" in connection_methods_upper(m)


def default_uart_baud_for_model_group(m: dict[str, Any] | None) -> int | None:
    """UART default baud from model group dict (registry + E3), or None if unspecified."""
    if not isinstance(m, dict):
        return None
    v = m.get("default_uart_baud")
    if v is None:
        return None
    try:
        b = int(v)
    except (TypeError, ValueError):
        return None
    return b if b >= 1 else None


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
