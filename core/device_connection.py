"""
Unified connection detection: builds DeviceConnection + merged detect dict for the UI.

Routes by platform (amebapro2, gen5, linux) and integrates E3 Wired `detect_device` path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from core.build_info import detect_device as detect_device_e3_wired
from core.device_errors import UnknownDeviceError, UnsupportedConnectionError
from core.device_registry import lookup_registry_by_model_id

ExecuteFn = Callable[[str, list[str]], tuple[bool, str]]


@dataclass
class DeviceConnection:
    """Result of post-connect identification."""

    device: dict[str, Any]  # registry entry copy (may be empty dict if E3-only)
    connection_type: str  # uart | ssh | adb (lowercase)
    model_id: str | None
    firmware_version: str | None
    platform: str | None  # amebapro2 | gen5 | linux | e3_wired
    used_legacy_uart_baud: bool = False
    raw_detection: str = ""


def _norm_conn(t: str) -> str:
    return (t or "").strip().lower()


def _parse_amebapro2_build_info(text: str) -> tuple[str | None, str | None]:
    """Parse AGW_MODEL_ID and version from `build_info` on AmebaPro2."""
    if not text or not text.strip():
        return None, None
    model = None
    m = re.search(r"AGW_MODEL_ID\s*[=:]\s*(\S+)", text, re.IGNORECASE)
    if m:
        model = m.group(1).strip().upper()
    if not model:
        m = re.search(r"\b(VMC\d{4}[A-Z]?)\b", text, re.IGNORECASE)
        if m:
            model = m.group(1).upper()
    fw = None
    for pat in (
        r"(?:AGW\s+)?version\s*[=:]\s*(\S+)",
        r"fw(?:\s*_?version)?\s*[=:]\s*(\S+)",
        r"(\d+\.\d+\.\d+(?:\.\d+)?(?:_\d+)?(?:_[a-fA-F0-9]+)?)",
    ):
        m2 = re.search(pat, text, re.IGNORECASE)
        if m2:
            fw = m2.group(1).strip()
            break
    return model, fw


def _parse_gen5_model(text: str) -> tuple[str | None, str | None]:
    """From nvram show / boot-ish output: model + version hints."""
    if not text or not text.strip():
        return None, None
    model = None
    for pat in (
        r"\bmodel\s*[=:]\s*(\S+)",
        r"\b(VMC\d{4}[A-Z]?)\b",
        r"\b(AVD\d{4})\b",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            model = m.group(1).strip().upper()
            break
    fw = None
    m2 = re.search(r"version\s*[=:]\s*(\S+)", text, re.IGNORECASE)
    if m2:
        fw = m2.group(1).strip()
    return model, fw


def _parse_linux_model(text: str) -> tuple[str | None, str | None]:
    """From /etc/os-release or arlod -V."""
    if not text or not text.strip():
        return None, None
    model = None
    m = re.search(r"\b(VMC\d{4}[A-Z]?|AVD\d{4})\b", text, re.IGNORECASE)
    if m:
        model = m.group(1).upper()
    if not model:
        m = re.search(r'(?:^|\n)\s*MODEL_ID\s*=\s*"?([^"\n]+)"?', text, re.IGNORECASE)
        if m:
            model = m.group(1).strip().upper()
    fw = None
    m2 = re.search(
        r"(?:arlod|version)\s+([^\n]+)", text, re.IGNORECASE
    ) or re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", text)
    if m2:
        fw = m2.group(1).strip()
    return model, fw


def _parse_device_tree_model(text: str) -> str | None:
    t = (text or "").strip().strip("\x00")
    if not t:
        return None
    m = re.search(r"\b(VMC\d{4}[A-Z]?|AVD\d{4})\b", t, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


def detect_after_connect(
    execute: ExecuteFn,
    connection_type: str,
    *,
    selected_model: dict[str, Any] | None = None,
    used_legacy_uart_baud: bool = False,
) -> tuple[dict[str, Any], DeviceConnection]:
    """
    Run platform-appropriate detection. Returns (detect_dict_for_ui, DeviceConnection).

    detect_dict matches legacy keys: model, fw_version, serial, env, update_url_raw,
    raw_build_info, is_onboarded — best effort per platform.
    """
    ct = _norm_conn(connection_type)
    plat_hint = None
    if selected_model:
        plat_hint = (selected_model.get("platform") or "").strip().lower() or None

    dc = DeviceConnection(
        device={},
        connection_type=ct,
        model_id=None,
        firmware_version=None,
        platform=plat_hint,
        used_legacy_uart_baud=used_legacy_uart_baud,
    )

    result: dict[str, Any] = {
        "model": None,
        "fw_version": None,
        "serial": None,
        "env": None,
        "update_url_raw": "",
        "raw_build_info": "",
        "is_onboarded": None,
    }

    if plat_hint == "amebapro2":
        ok, out = execute("build_info", [])
        result["raw_build_info"] = out or ""
        mid, fw = _parse_amebapro2_build_info(out or "")
        result["model"] = mid
        result["fw_version"] = fw
        dc.model_id = mid
        dc.firmware_version = fw
        dc.platform = "amebapro2"
        dc.raw_detection = out or ""
        reg = lookup_registry_by_model_id(mid) if mid else None
        if reg:
            dc.device = dict(reg)
        elif mid:
            raise UnknownDeviceError(
                f"Unknown model ID from device: {mid!r}. Check registry or cable/baud rate."
            )
        return result, dc

    if plat_hint == "gen5":
        ok, out = execute("nvram show | grep model", [])
        raw = out or ""
        ok2, out2 = execute("nvram show | grep version", [])
        combined = raw + "\n" + (out2 or "")
        mid, fw = _parse_gen5_model(combined)
        if not mid:
            ok3, out3 = execute("nvram show", [])
            mid, fw2 = _parse_gen5_model(out3 or "")
            fw = fw or fw2
            combined = (out3 or "") + "\n" + combined
        result["model"] = mid
        result["fw_version"] = fw
        result["raw_build_info"] = combined.strip()
        dc.model_id = mid
        dc.firmware_version = fw
        dc.platform = "gen5"
        dc.raw_detection = combined
        reg = lookup_registry_by_model_id(mid) if mid else None
        if reg:
            dc.device = dict(reg)
        elif mid:
            raise UnknownDeviceError(
                f"Unknown model ID from device: {mid!r}. Check registry or nvram output."
            )
        return result, dc

    if plat_hint == "linux" and ct == "adb":
        ok, out = execute("cat /proc/device-tree/model", [])
        mid = _parse_device_tree_model(out or "")
        if not mid:
            ok2, out2 = execute("cat /etc/os-release", [])
            mid, _ = _parse_linux_model(out2 or "")
        result["model"] = mid
        dc.model_id = mid
        dc.platform = "linux"
        dc.raw_detection = (out or "").strip()
        reg = lookup_registry_by_model_id(mid) if mid else None
        if reg and not reg.get("adb_supported"):
            name = reg.get("display_name") or mid
            types_list = reg.get("connection_types") or []
            raise UnsupportedConnectionError(
                f"{name} does not support ADB connections. Supported methods: {', '.join(types_list)}"
            )
        if reg:
            dc.device = dict(reg)
        okv, outv = execute("arlod -V", [])
        if okv and outv:
            _, fw = _parse_linux_model(outv)
            result["fw_version"] = fw
            dc.firmware_version = fw
        if mid and not reg:
            if re.match(r"^(VMC|AVD)\d", mid, re.IGNORECASE):
                raise UnsupportedConnectionError(
                    f"Device {mid} does not support ADB. Use UART instead."
                )
            raise UnknownDeviceError(f"Unknown model ID from device: {mid!r}.")
        # Linux Kea: reuse E3-style kv/arlocmd where applicable
        merged = detect_device_e3_wired(execute)
        for k in ("env", "update_url_raw", "is_onboarded", "serial"):
            if merged.get(k) is not None:
                result[k] = merged.get(k)
        if not result.get("fw_version") and merged.get("fw_version"):
            result["fw_version"] = merged.get("fw_version")
            dc.firmware_version = merged.get("fw_version")
        if not result.get("raw_build_info") and merged.get("raw_build_info"):
            result["raw_build_info"] = merged.get("raw_build_info") or ""
        return result, dc

    if plat_hint == "linux" and ct in ("uart", "ssh"):
        ok, out = execute("cat /etc/os-release", [])
        mid, fw = _parse_linux_model(out or "")
        if not mid:
            ok2, out2 = execute("arlod -V", [])
            mid, fw = _parse_linux_model(out2 or (out or ""))
        result["model"] = mid
        result["fw_version"] = fw
        result["raw_build_info"] = (out or "").strip()
        dc.model_id = mid
        dc.firmware_version = fw
        dc.platform = "linux"
        dc.raw_detection = (out or "").strip()
        reg = lookup_registry_by_model_id(mid) if mid else None
        if reg:
            dc.device = dict(reg)
        elif mid:
            raise UnknownDeviceError(f"Unknown model ID from device: {mid!r}.")
        merged = detect_device_e3_wired(execute)
        for k in ("env", "update_url_raw", "is_onboarded", "serial"):
            if merged.get(k) is not None:
                result[k] = merged.get(k)
        return result, dc

    # Default: E3 Wired Linux path (existing behavior)
    merged = detect_device_e3_wired(execute)
    result.update(merged)
    mid = result.get("model")
    dc.model_id = str(mid) if mid else None
    dc.firmware_version = result.get("fw_version")
    dc.platform = "e3_wired"
    dc.raw_detection = result.get("raw_build_info") or ""
    reg = lookup_registry_by_model_id(str(mid)) if mid else None
    if reg:
        dc.device = dict(reg)
    return result, dc


def ensure_adb_allowed_for_selection(selected_model: dict[str, Any] | None) -> None:
    """Raise UnsupportedConnectionError if UI selected ADB for a non-ADB device."""
    if not selected_model:
        return
    if not (selected_model.get("adb_supported") is False):
        return
    name = selected_model.get("display_name") or selected_model.get("name") or "Device"
    conns = selected_model.get("supported_connections") or []
    raise UnsupportedConnectionError(
        f"{name} does not support ADB connections. Supported methods: {', '.join(str(c) for c in conns)}"
    )
