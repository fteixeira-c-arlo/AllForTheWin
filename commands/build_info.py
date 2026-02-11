"""Parse build_info and kvcmd/update_url output to extract device model (VMCXXXX), FW version, and env."""
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
    result: dict[str, Any] = {"model": None, "fw_version": None, "raw": raw_output or ""}
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

    return result


def parse_env_from_update_url(raw_output: str) -> str | None:
    """Parse env (qa/dev/prod/etc.) from arlocmd update_url output (URL or key=value)."""
    if not raw_output or not raw_output.strip():
        return None
    text = raw_output.strip()
    return _parse_env_from_url(text)


def detect_device(execute: Callable[[str, list[str]], tuple[bool, str]]) -> dict[str, Any]:
    """
    Run build_info and update_url (and optional kvcmd) on the device to detect model, FW, env.
    execute(cmd, args) -> (success, output).
    Returns dict with: model, fw_version, env, raw_build_info (for debugging).
    """
    result: dict[str, Any] = {
        "model": None,
        "fw_version": None,
        "env": None,
        "raw_build_info": "",
    }
    # 1) build_info
    ok, out = execute(BUILD_INFO_SHELL, [])
    if ok and out:
        result["raw_build_info"] = out
        parsed = parse_build_info(out)
        result["model"] = parsed.get("model")
        result["fw_version"] = parsed.get("fw_version")
    # 2) Env: try KV_BS_STAGE — use "kvcmd read" first (some FW use "read"), then "kvcmd get"
    for cmd in ("kvcmd read KV_BS_STAGE", "kvcmd get KV_BS_STAGE"):
        ok, out = execute(cmd, [])
        if ok and out:
            env = _parse_env_from_kv_bs_stage(out)
            if env:
                result["env"] = env
                break
    # 3) update_url as fallback for env
    if not result.get("env"):
        ok, out = execute(UPDATE_URL_SHELL, [])
        if ok and out:
            env = parse_env_from_update_url(out)
            if env:
                result["env"] = env
    # 4) Other kv keys as fallback (KV_UPDATE_URL, KV_MIGRATE_STAGE)
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
    return result
