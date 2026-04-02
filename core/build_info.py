"""Parse build_info and kvcmd/update_url output to extract device model (VMCXXXX), FW version, and env."""
import json
import re
from typing import Any, Callable


# Default shell commands for device detection (E3 Wired CLI)
BUILD_INFO_SHELL = "cli mfg build_info"
UPDATE_URL_SHELL = "arlocmd update_url"
# kvcmd keys for env: KV_BS_STAGE is the primary (output like "KV_BS_STAGE  [10]  <DFLT>  'qa'")
KV_KEYS_FOR_ENV = ("KV_BS_STAGE", "KV_UPDATE_URL", "KV_MIGRATE_STAGE")


def _parse_env_from_url(text: str) -> str | None:
    """Extract env from update URL path (e.g. .../qa, .../dev, .../prod, .../prod_signed, .../ftrial)."""
    if not text or not text.strip():
        return None
    # Match path segment that looks like env: /qa, /dev, /prod, /prod_signed, /ftrial
    m = re.search(r"/(?:qa|dev|prod(?:_signed)?|ftrial(?:_signed)?)(?:/|$)", text.strip(), re.IGNORECASE)
    if m:
        segment = m.group(0).strip("/").rstrip("/")
        return segment.lower().replace("_signed", "-signed") if segment else None
    return None


# Stage names we recognize (whole-word match for env)
_STAGE_PATTERN = re.compile(
    r"\b(qa|dev|prod(?:_signed)?|ftrial(?:_signed)?)\b",
    re.IGNORECASE,
)


def _normalize_quotes(text: str) -> str:
    """Replace Unicode curly/smart quotes with ASCII so regex matches."""
    for uq, aq in [("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"')]:
        text = text.replace(uq, aq)
    return text


def _parse_env_from_kv_bs_stage(text: str) -> str | None:
    """Parse env from kvcmd get/read KV_BS_STAGE output.
    Handles: \"KV_BS_STAGE  [10]  <DFLT>  'qa'\", or just \"qa\", or table output.
    """
    if not text or not text.strip():
        return None
    raw = _normalize_quotes(text.strip())
    # 1) Quoted value: 'qa', \"qa\", 'dev', etc. (ASCII or normalized curly)
    m = re.search(r"['\"](qa|dev|prod(?:_signed)?|ftrial(?:_signed)?)['\"]", raw, re.IGNORECASE)
    if m:
        return m.group(1).lower().replace("_signed", "-signed")
    # 2) Whole-word stage name anywhere (e.g. bare "qa" or in table)
    m = _STAGE_PATTERN.search(raw)
    if m:
        return m.group(1).lower().replace("_signed", "-signed")
    # 3) Last token that looks like a stage (for "key  [10]  <DFLT>  qa" without quotes)
    tokens = raw.split()
    for t in reversed(tokens):
        t = t.strip("'\",;")
        if re.match(r"^(qa|dev|prod|ftrial)(_signed)?$", t, re.IGNORECASE):
            return t.lower().replace("_signed", "-signed")
    return None


def parse_build_info(raw_output: str) -> dict[str, Any]:
    """
    Parse raw build_info output (e.g. from `cli mfg build_info`).
    Returns dict with:
      - model: str | None  (e.g. VMC3070)
      - fw_version: str | None  (e.g. 1.2.3 or AGW version)
      - raw: str  (original output for fallback display)
    """
    result: dict[str, Any] = {"model": None, "fw_version": None, "serial": None, "raw": raw_output or ""}
    if not raw_output or not raw_output.strip():
        return result

    text = raw_output.strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Model: VMC + 4 digits (e.g. VMC3070, model_num: VMC3070, Model: VMC3070)
    model_pattern = re.compile(r"\b(VMC\d{4})\b", re.IGNORECASE)
    for line in lines:
        m = model_pattern.search(line)
        if m:
            result["model"] = m.group(1).upper()
            break
    if not result["model"]:
        m = model_pattern.search(text)
        if m:
            result["model"] = m.group(1).upper()

    # FW / version: common patterns (AGW version, version:, fw:, firmware:, etc.)
    version_patterns = [
        re.compile(r"(?:AGW\s+)?version\s*[=:]\s*([^\s]+)", re.IGNORECASE),
        re.compile(r"fw(?:\s*_?version)?\s*[=:]\s*([^\s]+)", re.IGNORECASE),
        re.compile(r"firmware\s*[=:]\s*([^\s]+)", re.IGNORECASE),
        re.compile(r"build(?:\s*_?version)?\s*[=:]\s*([^\s]+)", re.IGNORECASE),
        re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)", re.IGNORECASE),  # fallback: first x.y.z
    ]
    for line in lines:
        for pat in version_patterns:
            m = pat.search(line)
            if m:
                val = m.group(1).strip()
                if val and not val.lower().startswith(("key", "value", "serial")):
                    result["fw_version"] = val
                    break
        if result["fw_version"]:
            break
    if not result["fw_version"]:
        m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", text)
        if m:
            result["fw_version"] = m.group(1)

    serial_patterns = [
        re.compile(r"(?i)serial(?:\s*(?:number|#|num))?\s*[=:]\s*([^\s,;|]+)"),
        re.compile(r"(?i)ssn\s*[=:]\s*([^\s,;|]+)"),
        re.compile(r"(?i)\bsn\s*[=:]\s*([^\s,;|]+)"),
    ]
    for line in lines:
        for pat in serial_patterns:
            m2 = pat.search(line)
            if m2:
                result["serial"] = m2.group(1).strip().strip("'\"")
                break
        if result.get("serial"):
            break

    return result


def _kv_bs_claimed_indicates_onboarded(raw_output: str) -> bool:
    """True if kvcmd KV_BS_CLAIMED style output indicates value 1 (matches command_parser heuristic)."""
    if not raw_output:
        return False
    text = raw_output.strip()
    if text == "1":
        return True
    if "KV_BS_CLAIMED" in raw_output and ("=1" in raw_output or ": 1" in raw_output or ":1" in raw_output):
        return True
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        if line == "1":
            return True
    if lines and lines[-1] == "1":
        return True
    return False


def parse_onboarded_from_device_info_text(text: str) -> bool | None:
    """
    Parse arlocmd device_info / bs_info (or similar) output for claimed or onboarded flags.
    Returns True if either is true, False if both appear explicitly false, None if unclear.
    """
    if not text or not str(text).strip():
        return None
    t = str(text)

    try:
        root = json.loads(t.strip())
        if isinstance(root, dict):
            c, o = root.get("claimed"), root.get("onboarded")
            if c is True or o is True:
                return True
            if c is False and o is False:
                return False
    except json.JSONDecodeError:
        pass

    # Embedded JSON objects (scan each {...} block)
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", t, re.DOTALL):
        chunk = m.group(0)
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        c = data.get("claimed")
        o = data.get("onboarded")
        if c is True or o is True:
            return True
        if c is False and o is False:
            return False

    # Line / loose patterns (e.g. key: value, "claimed": true)
    if re.search(r'(?i)"claimed"\s*:\s*true', t) or re.search(r'(?i)"onboarded"\s*:\s*true', t):
        return True
    if re.search(r'(?i)(?<![\w-])claimed(?![\w-])\s*[:=]\s*(?:true|1|yes)\b', t) or re.search(
        r'(?i)(?<![\w-])onboarded(?![\w-])\s*[:=]\s*(?:true|1|yes)\b', t
    ):
        return True
    if re.search(r'(?i)"claimed"\s*:\s*false', t) and re.search(r'(?i)"onboarded"\s*:\s*false', t):
        if not re.search(r'(?i)"claimed"\s*:\s*true|"onboarded"\s*:\s*true', t):
            return False

    return None


def parse_env_from_update_url(raw_output: str) -> str | None:
    """Parse env (qa/dev/prod/etc.) from arlocmd update_url output (URL or key=value)."""
    if not raw_output or not raw_output.strip():
        return None
    text = raw_output.strip()
    return _parse_env_from_url(text)


def detect_device(execute: Callable[[str, list[str]], tuple[bool, str]]) -> dict[str, Any]:
    """
    Run build_info, kvcmd, device_info/bs_info, and optional KV_BS_CLAIMED on the device.
    execute(cmd, args) -> (success, output).
    Returns dict with: model, fw_version, serial, env, update_url_raw, raw_build_info, is_onboarded (bool | None).
    """
    result: dict[str, Any] = {
        "model": None,
        "fw_version": None,
        "serial": None,
        "env": None,
        "update_url_raw": "",
        "raw_build_info": "",
        "is_onboarded": None,
    }
    # 1) build_info
    ok, out = execute(BUILD_INFO_SHELL, [])
    if ok and out:
        result["raw_build_info"] = out
        parsed = parse_build_info(out)
        result["model"] = parsed.get("model")
        result["fw_version"] = parsed.get("fw_version")
        result["serial"] = parsed.get("serial")
    # 2) Env: try KV_BS_STAGE — use "kvcmd read" first (some FW use "read"), then "kvcmd get"
    for cmd in ("kvcmd read KV_BS_STAGE", "kvcmd get KV_BS_STAGE"):
        ok, out = execute(cmd, [])
        if ok and out:
            env = _parse_env_from_kv_bs_stage(out)
            if env:
                result["env"] = env
                break
    # 3) Other kv keys as fallback (KV_UPDATE_URL, KV_MIGRATE_STAGE)
    for key in KV_KEYS_FOR_ENV:
        if key == "KV_BS_STAGE":
            continue  # already tried above
        if result.get("env"):
            break
        ok, out = execute("kvcmd get", [key])
        if not ok or not out:
            continue
        env = _parse_env_from_url(out) or (
            _parse_env_from_kv_bs_stage(out) or (out.strip() if out.strip() and len(out.strip()) < 20 else None)
        )
        if env:
            result["env"] = env

    # update_url: stage/env fallback and camera URL line for UI
    ok_u, out_u = execute(UPDATE_URL_SHELL, [])
    if ok_u and out_u:
        u = (out_u or "").strip()
        result["update_url_raw"] = u[:500]
        if not result.get("env"):
            env = parse_env_from_update_url(u)
            if env:
                result["env"] = env

    # Onboarded / claimed: device_info + bs_info (same sources as abstract "info"), then KV_BS_CLAIMED fallback
    info_blob_parts: list[str] = []
    ok_di, out_di = execute("arlocmd device_info", [])
    if ok_di and out_di:
        info_blob_parts.append(out_di)
    ok_bs, out_bs = execute("arlocmd bs_info", [])
    if ok_bs and out_bs:
        info_blob_parts.append(out_bs)
    combined_info = "\n".join(info_blob_parts)
    if combined_info.strip():
        parsed_ob = parse_onboarded_from_device_info_text(combined_info)
        if parsed_ob is True:
            result["is_onboarded"] = True
        elif parsed_ob is False:
            result["is_onboarded"] = False
    if result.get("is_onboarded") is None:
        ok_kv, out_kv = execute("kvcmd get KV_BS_CLAIMED", [])
        if ok_kv and out_kv:
            if _kv_bs_claimed_indicates_onboarded(out_kv):
                result["is_onboarded"] = True
            else:
                z = (out_kv or "").strip()
                zlines = [ln.strip() for ln in z.splitlines() if ln.strip()]
                if z == "0" or (zlines and zlines[-1] == "0"):
                    result["is_onboarded"] = False

    return result
