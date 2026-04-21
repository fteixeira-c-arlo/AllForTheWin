"""
Unified connection detection: builds DeviceConnection + merged detect dict for the UI.

Routes by platform (amebapro2, gen5, linux) and integrates E3 Wired `detect_device` path.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from core.build_info import (
    _kv_bs_claimed_indicates_onboarded,
    _parse_env_from_kv_bs_stage,
    detect_device as detect_device_e3_wired,
    parse_build_info,
    parse_env_from_isp_or_kv_text,
    parse_onboarded_from_device_info_text,
)
from core.device_errors import UnknownDeviceError, UnsupportedConnectionError
from core.device_registry import lookup_registry_by_model_id

ExecuteFn = Callable[[str, list[str]], tuple[bool, str]]

logger = logging.getLogger(__name__)

# Essential II Outdoor (Robin): default ISP hibernate drops the session after ~30 min idle.
ROBIN_CODENAME = "robin"
_ROBIN_HIBERNATE_CMD = "cli_agw_enable_hibernate"
_ROBIN_HIBERNATE_OFF_ARGS = ["0"]


@dataclass
class DeviceConnection:
    """Result of post-connect identification."""

    device: dict[str, Any]  # registry entry copy (may be empty dict if E3-only)
    connection_type: str  # uart | ssh | adb (lowercase)
    model_id: str | None
    firmware_version: str | None
    platform: str | None  # amebapro2 | gen5 | linux | e3_wired
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
        m = re.search(r"\b(VMC\d{4}[A-Z]?|AVD\d{4})\b", text, re.IGNORECASE)
        if m:
            model = m.group(1).upper()
    fw = None
    # Prefer dotted numeric builds first so we don't grab symbol names like agw_get_hardware_version
    # from help text or mixed ISP output.
    m_sem = re.search(
        r"(\d+\.\d+\.\d+(?:\.\d+)?(?:_\d+)?(?:_[a-fA-F0-9]+)?)",
        text,
        re.IGNORECASE,
    )
    if m_sem:
        fw = m_sem.group(1).strip()
    if not fw:
        for pat in (
            r"(?:AGW\s+)?version\s*[=:]\s*(\S+)",
            r"fw(?:\s*_?version)?\s*[=:]\s*(\S+)",
        ):
            m2 = re.search(pat, text, re.IGNORECASE)
            if not m2:
                continue
            cand = m2.group(1).strip()
            if re.search(r"\d", cand):
                fw = cand
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


def _robin_hibernate_disable_applies(
    dc: DeviceConnection, selected_model: dict[str, Any] | None
) -> bool:
    """
    Send hibernate off only for Robin. Prefer registry from detected model_id; if the device
    did not report a model, fall back to the user's Connect selection (UART without build_info).
    """
    dev_cod = (dc.device.get("codename") or "").strip().lower()
    if dev_cod:
        return dev_cod == ROBIN_CODENAME
    sel = selected_model or {}
    return (sel.get("codename") or "").strip().lower() == ROBIN_CODENAME


def _maybe_disable_robin_hibernate(
    execute: ExecuteFn,
    dc: DeviceConnection,
    selected_model: dict[str, Any] | None,
    result: dict[str, Any],
) -> None:
    if not _robin_hibernate_disable_applies(dc, selected_model):
        return
    ok, out = execute(_ROBIN_HIBERNATE_CMD, _ROBIN_HIBERNATE_OFF_ARGS)
    preview = (out or "").replace("\r", "")[:800]
    logger.info(
        "Robin: %s %s → ok=%s output=%r",
        _ROBIN_HIBERNATE_CMD,
        " ".join(_ROBIN_HIBERNATE_OFF_ARGS),
        ok,
        preview,
    )
    msgs: list[str] = []
    if ok:
        msgs.append("Robin detected — hibernate disabled to keep camera awake")
    else:
        logger.warning(
            "Robin: hibernate disable failed (continuing session). output=%r", preview
        )
        msgs.append(
            "Robin detected — could not disable hibernate (session continues); "
            "see application log for device output."
        )
    prev = result.get("post_connect_messages")
    if isinstance(prev, list):
        result["post_connect_messages"] = prev + msgs
    else:
        result["post_connect_messages"] = msgs


def _lory_fw_version_plausible(v: Any) -> bool:
    """Reject syslog / shell junk (e.g. lone '>') mistaken for a version string."""
    if v is None or not isinstance(v, str):
        return False
    s = v.strip()
    if len(s) < 3 or not re.search(r"\d", s):
        return False
    if len(s) <= 2:
        return False
    alnumish = sum(1 for c in s if c.isalnum() or c in "._-+")
    if alnumish < len(s) * 0.3 and len(s) < 8:
        return False
    return True


def _extract_lory_fw_from_text(text: str) -> str | None:
    """Best-effort FW string from noisy `info` output (semantic version + optional suffix)."""
    if not text or not str(text).strip():
        return None
    t = str(text)
    for pat in (
        r"\b(\d+\.\d+\.\d+(?:\.\d+)?(?:_[a-zA-Z0-9]+)?)\b",
        r"\b(\d+\.\d+\.\d+[._][a-zA-Z0-9_.-]+)\b",
        r"(?im)^VERSION_ID\s*=\s*(\S+)",
        r"(?im)^VERSION\s*=\s*(\S+)",
    ):
        m = re.search(pat, t)
        if m:
            cand = m.group(1).strip().strip('"')
            if _lory_fw_version_plausible(cand):
                return cand
    return None


def _parse_lory_info_output(raw: str) -> dict[str, Any]:
    """Parse `info` shell output on Lory (may be key=value lines or JSON)."""
    text = raw or ""
    out: dict[str, Any] = dict(parse_build_info(text))
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            root = json.loads(stripped)
        except json.JSONDecodeError:
            root = None
        if isinstance(root, dict):
            if not out.get("model"):
                mid = root.get("model_id") or root.get("model") or root.get("MODEL_ID")
                if isinstance(mid, str) and mid.strip():
                    out["model"] = mid.strip().upper()
            if not out.get("fw_version"):
                ver = root.get("fw_version") or root.get("version") or root.get("firmware_version")
                if isinstance(ver, str) and ver.strip():
                    out["fw_version"] = ver.strip()
            if not out.get("serial"):
                sn = root.get("serial") or root.get("serial_number") or root.get("ssn")
                if isinstance(sn, str) and sn.strip():
                    out["serial"] = sn.strip()
    if not _lory_fw_version_plausible(out.get("fw_version")):
        out.pop("fw_version", None)
        alt = _extract_lory_fw_from_text(text)
        if alt:
            out["fw_version"] = alt
    envx = parse_env_from_isp_or_kv_text(text)
    if envx:
        out["env"] = envx
    ob = parse_onboarded_from_device_info_text(text)
    if ob is not None:
        out["is_onboarded"] = ob
    return out


def _parse_device_tree_model(text: str) -> str | None:
    t = (text or "").strip().strip("\x00")
    if not t:
        return None
    m = re.search(r"\b(VMC\d{4}[A-Z]?|AVD\d{4})\b", t, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return None


_LORY_MODEL_IDS = frozenset({"AVD5001", "AVD6001"})


def selection_is_lory(sel: dict[str, Any] | None) -> bool:
    """True when Connect selection is Lory (codename, registry copy, or known model IDs)."""
    if not isinstance(sel, dict) or not sel:
        return False
    if (sel.get("codename") or "").strip().lower() == "lory":
        return True
    reg = sel.get("registry_entry")
    if isinstance(reg, dict) and (reg.get("codename") or "").strip().lower() == "lory":
        return True
    name = (sel.get("name") or "").strip().upper()
    if name in _LORY_MODEL_IDS:
        return True
    for x in sel.get("fw_search_models") or []:
        if str(x).strip().upper() in _LORY_MODEL_IDS:
            return True
    return False


def detect_after_connect(
    execute: ExecuteFn,
    connection_type: str,
    *,
    selected_model: dict[str, Any] | None = None,
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

    # Lory is Linux shell + `info` only; missing/empty `platform` used to skip the linux branch and
    # fall through to E3 `cli mfg build_info` — never do that for Lory UART/SSH.
    if selection_is_lory(selected_model if isinstance(selected_model, dict) else None) and ct in (
        "uart",
        "ssh",
    ):
        plat_hint = "linux"

    dc = DeviceConnection(
        device={},
        connection_type=ct,
        model_id=None,
        firmware_version=None,
        platform=plat_hint,
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
        # Env: ISP `update_url`, then KV keys, then `arlogw migrate` (no args shows stage on some FW).
        ok_u, out_u = execute("update_url", [])
        if out_u and str(out_u).strip():
            u = str(out_u).strip()
            result["update_url_raw"] = u[:500]
            env = parse_env_from_isp_or_kv_text(u)
            if env:
                result["env"] = env
        if not result.get("env"):
            for kv_cmd in (
                "kvread -s KV_BS_STAGE",
                "kvread -s KV_UPDATE_URL",
                "kvread -s KV_MIGRATE_STAGE",
            ):
                ok_kv, out_kv = execute(kv_cmd, [])
                if not ok_kv or not (out_kv or "").strip():
                    continue
                env = parse_env_from_isp_or_kv_text(out_kv)
                if env:
                    result["env"] = env
                    break
        if not result.get("env"):
            ok_m, out_m = execute("arlogw migrate", [])
            if ok_m and (out_m or "").strip():
                env = parse_env_from_isp_or_kv_text(out_m)
                if env:
                    result["env"] = env
        _maybe_disable_robin_hibernate(execute, dc, selected_model, result)
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
        ok2: bool | None = None
        out2 = ""
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
        sel = selected_model or {}
        is_lory = selection_is_lory(sel)
        if is_lory:
            # Lory: device identity comes from `info` only — no E3 build_info / arlocmd device_info / bs_info.
            merged = {
                "model": None,
                "fw_version": None,
                "serial": None,
                "env": None,
                "update_url_raw": "",
                "raw_build_info": "",
                "is_onboarded": None,
            }
            ok_li, out_li = execute("info", [])
            if ok_li and (out_li or "").strip():
                parsed_li = _parse_lory_info_output(out_li)
                if parsed_li.get("model"):
                    merged["model"] = parsed_li["model"]
                if parsed_li.get("fw_version"):
                    merged["fw_version"] = parsed_li["fw_version"]
                if parsed_li.get("serial"):
                    merged["serial"] = parsed_li["serial"]
                if parsed_li.get("env"):
                    merged["env"] = parsed_li["env"]
                if parsed_li.get("is_onboarded") is not None:
                    merged["is_onboarded"] = parsed_li["is_onboarded"]
                merged["raw_build_info"] = (out_li or "").strip()
        else:
            merged = detect_device_e3_wired(execute)
        for k in ("env", "update_url_raw", "is_onboarded", "serial"):
            if merged.get(k) is not None:
                result[k] = merged.get(k)
        if is_lory:
            if merged.get("model"):
                result["model"] = merged["model"]
                dc.model_id = merged["model"]
            if merged.get("fw_version"):
                result["fw_version"] = merged["fw_version"]
                dc.firmware_version = merged["fw_version"]
            if merged.get("raw_build_info"):
                result["raw_build_info"] = merged["raw_build_info"]
                dc.raw_detection = merged["raw_build_info"]
            reg_li = lookup_registry_by_model_id(str(result["model"])) if result.get("model") else None
            if reg_li:
                dc.device = dict(reg_li)
            # Env / claimed from KV (Lory does not use E3 build_info / bs_info; `info` may omit these).
            if not result.get("env"):
                for kv_cmd in ("kvcmd read KV_BS_STAGE", "kvcmd get KV_BS_STAGE"):
                    ok_kv, out_kv = execute(kv_cmd, [])
                    if not ok_kv or not (out_kv or "").strip():
                        continue
                    env = _parse_env_from_kv_bs_stage(out_kv or "")
                    if env:
                        result["env"] = env
                        break
            if result.get("is_onboarded") is None:
                ok_c, out_c = execute("kvcmd read KV_BS_CLAIMED", [])
                if not ok_c or not (out_c or "").strip():
                    ok_c, out_c = execute("kvcmd get KV_BS_CLAIMED", [])
                if ok_c and out_c:
                    if _kv_bs_claimed_indicates_onboarded(out_c):
                        result["is_onboarded"] = True
                    else:
                        z = (out_c or "").strip()
                        zlines = [ln.strip() for ln in z.splitlines() if ln.strip()]
                        if z == "0" or (zlines and zlines[-1] == "0"):
                            result["is_onboarded"] = False
            if not result.get("fw_version") or not _lory_fw_version_plausible(result.get("fw_version")):
                m = re.search(r"(?im)^VERSION=(\S+)", out or "")
                if m:
                    v = m.group(1).strip().strip('"')
                    if _lory_fw_version_plausible(v):
                        result["fw_version"] = v
                        dc.firmware_version = v
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
    name = selected_model.get("display_name") or selected_model.get("name") or "Device"
    if selected_model.get("adb_supported") is False:
        conns = selected_model.get("supported_connections") or []
        raise UnsupportedConnectionError(
            f"{name} does not support ADB connections. Supported methods: {', '.join(str(c) for c in conns)}"
        )
    ctypes = selected_model.get("connection_types")
    if isinstance(ctypes, list) and len(ctypes) > 0:
        lowered = [str(c).strip().lower() for c in ctypes if str(c).strip()]
        if "adb" not in lowered:
            raise UnsupportedConnectionError(f"{name} does not support ADB. Use UART.")
