"""
Canonical device registry: model IDs, platform, transports, UART settings, and command profiles.

E3 Wired models remain defined in camera_models.py; this registry adds Finch/Robin/Swallow,
Pro 5 (Phoenix/Griffin), Pro 6 (Kea), Lory, Parrot (AVD3001/AVD4001), and lookup helpers used after detection.
"""
from __future__ import annotations

from typing import Any, TypedDict


class DeviceRegistryEntry(TypedDict, total=False):
    model_ids: list[str]
    codename: str
    display_name: str
    platform: str  # amebapro2 | gen5 | linux | openwrt_qca
    device_kind: str  # "camera" (default) | "basestation"
    connection_types: list[str]  # uart, ssh, adb — priority order
    adb_supported: bool
    uart_baudrate: int
    enable_debug_method: str
    adb_auth_password: str
    notes: str
    command_profile: str


# Platforms that ship as base stations / gateways rather than cameras.
_BASESTATION_PLATFORMS: set[str] = {"openwrt_qca"}
# Model-id prefixes used by Arlo basestations / gateways (VMB = Video Management Base).
_BASESTATION_MODEL_PREFIXES: tuple[str, ...] = ("VMB",)


DEVICE_REGISTRY: list[DeviceRegistryEntry] = [
    {
        "model_ids": ["VMC2060", "VMC3060"],
        "codename": "finch",
        "display_name": "Arlo Essential II Wired (Finch)",
        "platform": "amebapro2",
        "connection_types": ["uart"],
        "adb_supported": False,
        "uart_baudrate": 921600,
        "enable_debug_method": "sync_button_6x",
        "command_profile": "amebapro2",
    },
    {
        "model_ids": ["VMC2050", "VMC3050", "VMC2052", "VMC3052"],
        "codename": "robin",
        "display_name": "Arlo Essential II Outdoor (Robin)",
        "platform": "amebapro2",
        "connection_types": ["uart"],
        "adb_supported": False,
        "uart_baudrate": 921600,
        "enable_debug_method": "sync_button_6x",
        "command_profile": "amebapro2",
    },
    {
        "model_ids": ["VMC2080", "VMC3080", "VMC2082", "VMC3082"],
        "codename": "swallow",
        "display_name": "Arlo Essential III Outdoor (Swallow)",
        "platform": "amebapro2",
        "connection_types": ["uart"],
        "adb_supported": False,
        "uart_baudrate": 921600,
        "enable_debug_method": "sync_button_6x",
        "command_profile": "amebapro2",
    },
    {
        "model_ids": ["VMC4041P"],
        "codename": "caracara",
        "display_name": "Arlo Pro 4 (Caracara)",
        "platform": "gen5",
        "connection_types": ["uart", "ssh"],
        "adb_supported": False,
        "uart_baudrate": 115200,
        "enable_debug_method": "sync_button_6x",
        "command_profile": "gen5",
    },
    {
        "model_ids": ["VMC4060", "VMC4060P", "VMC4061"],
        "codename": "phoenix_griffin",
        "display_name": "Arlo Pro 5 / Pro 5S (Phoenix/Griffin)",
        "platform": "gen5",
        "connection_types": ["uart", "ssh"],
        "adb_supported": False,
        "uart_baudrate": 115200,
        "enable_debug_method": "sync_button_6x",
        "notes": "VMC4061 = Griffin (HW rev >= H50), VMC4060/VMC4060P = Phoenix",
        "command_profile": "gen5",
    },
    {
        "model_ids": ["VMC4070P"],
        "codename": "kea",
        "display_name": "Arlo Pro 6 (Kea)",
        "platform": "linux",
        "connection_types": ["uart", "ssh", "adb"],
        "adb_supported": True,
        "adb_auth_password": "arlo",
        "uart_baudrate": 115200,
        "enable_debug_method": "sync_button_6x",
        "command_profile": "linux_kealory",
    },
    {
        "model_ids": ["AVD5001", "AVD6001"],
        "codename": "lory",
        "display_name": "Arlo Essential III Doorbell (Lory)",
        "platform": "linux",
        "connection_types": ["uart", "ssh"],
        "adb_supported": False,
        "uart_baudrate": 115200,
        "enable_debug_method": "uart_always_accessible",
        "command_profile": "linux_kealory",
    },
    {
        "model_ids": ["AVD4001", "AVD3001"],
        "codename": "parrot",
        "display_name": "Arlo Parrot (AVD4001 2K · AVD3001 FHD)",
        "platform": "amebapro2",
        "connection_types": ["uart"],
        "adb_supported": False,
        "uart_baudrate": 921600,
        "enable_debug_method": "sync_button_6x",
        "command_profile": "parrot",
    },
    {
        "model_ids": ["VMB4540"],
        "codename": "osprey",
        "display_name": "Arlo Pro3 SmartHub (VMB4540 Osprey)",
        "platform": "openwrt_qca",
        "device_kind": "basestation",
        "connection_types": ["ssh", "uart"],
        "adb_supported": False,
        "uart_baudrate": 115200,
        "enable_debug_method": "sync_button_11s",
        "notes": "OpenWrt + QCA4531 base station. SSH root password: 'ngbase' (dev/qa) or 'NX9PvLX2L3YvhjBjVLi68yBA8' (staging/ftrial/prod). Long-press sync button 11s to enable SSH on production firmware.",
        "command_profile": "osprey_smarthub",
    },
    {
        "model_ids": ["VMB5000"],
        "codename": "ultra_smarthub",
        "display_name": "Arlo Ultra SmartHub (VMB5000 Gen5)",
        "platform": "openwrt_qca",
        "device_kind": "basestation",
        "connection_types": ["ssh", "uart"],
        "adb_supported": False,
        "uart_baudrate": 115200,
        "enable_debug_method": "sync_button_11s",
        "notes": "OpenWrt + QCA Gen5 Ultra SmartHub. SSH root password: 'ngbase' (dev/qa) or 'nw2LuJ7syHKN9YUUHTfW7' / '9YGSCvF9VNLuwtjrYwW9KWPzc' (prod, latest/previous). Long-press sync button 11s to enable SSH on production firmware.",
        "command_profile": "none",
    },
]


def _norm_model(s: str) -> str:
    return (s or "").strip().upper()


def lookup_registry_by_model_id(model_id: str | None) -> DeviceRegistryEntry | None:
    """Return registry entry when model_id matches any model_ids entry (case-insensitive)."""
    if not model_id:
        return None
    key = _norm_model(model_id)
    for entry in DEVICE_REGISTRY:
        for mid in entry.get("model_ids") or []:
            if _norm_model(str(mid)) == key:
                return entry
    return None


def get_registry_model_ids_flat() -> list[str]:
    """All model IDs across registry entries."""
    out: list[str] = []
    for entry in DEVICE_REGISTRY:
        for mid in entry.get("model_ids") or []:
            if mid and mid not in out:
                out.append(str(mid).upper())
    return out


def get_device_kind(model_id: str | None) -> str:
    """
    Classify a model ID as ``"camera"`` or ``"basestation"``.

    Resolution order:
      1. ``device_kind`` field on the matching registry entry (if set).
      2. Platform-based inference (``openwrt_qca`` etc. → basestation).
      3. Model-ID prefix heuristic (``VMB*`` → basestation).
      4. Fallback to ``"camera"``.

    Used by the firmware pipeline to pick the correct Artifactory repo
    (``camera-fw-generic-release-local`` vs ``gateway-fw-generic-release-local``).
    """
    key = _norm_model(model_id)
    entry = lookup_registry_by_model_id(key)
    if entry:
        kind = (entry.get("device_kind") or "").strip().lower()
        if kind in ("camera", "basestation"):
            return kind
        platform = (entry.get("platform") or "").strip().lower()
        if platform in _BASESTATION_PLATFORMS:
            return "basestation"
    if key and any(key.startswith(prefix) for prefix in _BASESTATION_MODEL_PREFIXES):
        return "basestation"
    return "camera"


def is_basestation_model(model_id: str | None) -> bool:
    """Return True when ``model_id`` is classified as a basestation / gateway."""
    return get_device_kind(model_id) == "basestation"


def registry_entry_to_camera_group(entry: DeviceRegistryEntry) -> dict[str, Any]:
    """Build a CAMERA_MODEL_GROUPS-style dict for the connect UI."""
    mids = entry.get("model_ids") or []
    primary = mids[0] if mids else "UNKNOWN"
    conns = entry.get("connection_types") or []
    supported = [c.upper() for c in conns]
    baud = int(entry.get("uart_baudrate") or 115200)
    ds: dict[str, Any] = {
        "ssh": {"port": 22, "username": "root"},
    }
    if entry.get("adb_supported"):
        ds["adb"] = {"port": 5555}
    return {
        "name": primary,
        "display_name": entry.get("display_name") or primary,
        "fw_search_models": [str(m).upper() for m in mids],
        "supported_connections": supported,
        "connection_types": [str(c).lower() for c in conns],
        "default_settings": ds,
        "command_profile": entry.get("command_profile") or "none",
        "platform": entry.get("platform"),
        "codename": entry.get("codename"),
        "adb_supported": bool(entry.get("adb_supported")),
        "default_uart_baud": baud,
        "enable_debug_method": entry.get("enable_debug_method"),
        "device_kind": get_device_kind(primary),
        "registry_entry": dict(entry),
    }
