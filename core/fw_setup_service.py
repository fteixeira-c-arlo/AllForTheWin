"""Shared firmware wizard logic for CLI flow and GUI FW Wizard (no UI)."""
from __future__ import annotations

import json
import logging
import os
import re
import socket
import sys
from typing import Callable, Literal

_FW_LOCAL_DETECT_LOG = logging.getLogger("arlo_shell.fw_local_detect")
from urllib.parse import quote, unquote, urlparse

from core.artifactory_client import ARTIFACTORY_REPO, download_firmware, list_available_firmware
from core.local_server import (
    DEFAULT_PORT,
    FW_ENV_TAR_GZ_SUFFIXES,
    check_server_status,
    extract_firmware_tar_gz,
    extract_firmware_zip,
    get_base_url_if_serving_root,
    get_in_process_server_root_abs,
    get_running_server_url,
    setup_directory_structure,
    start_http_server,
)

# Default root for local firmware server (FxTest layout per Confluence)
def default_fw_server_root() -> str:
    if os.environ.get("FW_SERVER_ROOT"):
        return os.environ.get("FW_SERVER_ROOT", "")
    if sys.platform == "win32":
        return r"C:\FxTest\fw_server\local_server"
    return os.path.join(os.getcwd(), "local_fw_server")


DEFAULT_ARTIFACTORY_URL = "https://artifactory.arlocloud.com"


def default_artifactory_url() -> str:
    return os.environ.get("ARTIFACTORY_BASE_URL", DEFAULT_ARTIFACTORY_URL)


def get_local_ipv4() -> str:
    """Return local IPv4 address for URL (camera must reach this). Fallback to localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def is_firmware_archive(filename: str) -> bool:
    """True if filename is .zip or .<env>.tar.gz (qa, dev, prod, prod_signed, ftrial, ftrial_signed)."""
    n = filename.lower()
    if n.endswith(".zip"):
        return True
    return any(n.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES)


def flatten_firmware_archives(
    available: list[tuple[str, list[str]]],
) -> list[tuple[str, str]]:
    """Build (folder_path, filename) list for .zip / env .tar.gz only."""
    available_fw = [(folder, [f for f in files if is_firmware_archive(f)]) for folder, files in available]
    available_fw = [(folder, files) for folder, files in available_fw if files]
    return [(folder, fn) for folder, files in available_fw for fn in files]


def scan_local_firmware_archives(
    fw_root: str,
    server_folder: str,
    version_filter: str,
) -> list[tuple[str, str]]:
    """
    List (version_path, filename) for archives under <fw_root>/<server_folder>/archive/.
    Runs before Artifactory search; rows use version_path prefix 'local/<folder>/' so the wizard
    can tell them from remote paths. version_filter: non-empty means filename must contain it (case-insensitive).
    """
    root = os.path.abspath(fw_root)
    folder = sanitize_server_folder_name((server_folder or "").strip())
    if not folder:
        return []
    arch = os.path.join(root, folder, "archive")
    if not os.path.isdir(arch):
        return []
    vf = (version_filter or "").strip().lower()
    out: list[tuple[str, str]] = []
    try:
        names = sorted(os.listdir(arch), key=str.lower)
    except OSError:
        return []
    for fn in names:
        if not is_firmware_archive(fn):
            continue
        if vf and vf not in fn.lower():
            continue
        out.append((f"local/{folder}/{fn}", fn))
    return out


def compute_download_model(version: str, selected_filename: str | None, model_name: str) -> str:
    """Folder name under binaries/ for Artifactory download target."""
    if not selected_filename:
        return model_name
    return version.split("/")[0] if "/" in version else version


def sanitize_server_folder_name(name: str) -> str | None:
    """
    Single path segment under the FW server root. No slashes or Windows-forbidden chars.
    Returns None if invalid.
    """
    s = (name or "").strip()
    if not s or s in (".", ".."):
        return None
    forbidden = '\\/:*?"<>|'
    if any(c in s for c in forbidden):
        return None
    if os.sep in s or (os.altsep and os.altsep in s):
        return None
    return s


_VMC_BIN_SUBDIR_RE = re.compile(r"^VMC\d{4}$", re.I)


def dir_has_enc_files(path: str) -> bool:
    """True if directory tree contains at least one .enc file."""
    if not os.path.isdir(path):
        return False
    try:
        for _root, _dirs, files in os.walk(path):
            if any(f.lower().endswith(".enc") for f in files):
                return True
    except OSError:
        pass
    return False


def vmc_binaries_folder_name_for_device(model_name: str) -> str:
    """Local server binaries/ subfolder name for the connected device (VMC#### when known)."""
    n = (model_name or "").strip().upper()
    if re.match(r"^VMC\d{4}$", n):
        return n
    return (model_name or "Camera").strip() or "Camera"


def should_filter_firmware_folders_by_camera(
    *,
    connected: bool,
    profile_e3_wired: bool,
    model_name: str,
) -> bool:
    """When True, Local Server shows only folders that match this camera's model."""
    if not connected or not profile_e3_wired:
        return False
    n = (model_name or "").strip().upper()
    return bool(re.match(r"^VMC\d{4}$", n))


def _folder_haystack_for_model_detection(folder_abs: str) -> str:
    """Concatenate updaterules JSON text and archive filenames for substring model checks."""
    parts: list[str] = []
    rules = os.path.join(folder_abs, "updaterules")
    if os.path.isdir(rules):
        try:
            for fn in sorted(os.listdir(rules)):
                if not fn.lower().endswith(".json"):
                    continue
                fp = os.path.join(rules, fn)
                if not os.path.isfile(fp):
                    continue
                try:
                    with open(fp, encoding="utf-8") as f:
                        parts.append(f.read())
                except OSError:
                    pass
        except OSError:
            pass
    arch = os.path.join(folder_abs, "archive")
    if os.path.isdir(arch):
        try:
            parts.extend(os.listdir(arch))
        except OSError:
            pass
    return "\n".join(parts).upper()


def _needles_match_haystack(needles: set[str], haystack_upper: str) -> bool:
    for n in needles:
        t = (n or "").strip().upper()
        if len(t) < 4:
            continue
        if t in haystack_upper:
            return True
    return False


def folder_matches_connected_camera(
    server_folder_abs: str,
    camera_vmc: str,
    *,
    search_aliases: list[str] | None = None,
) -> bool:
    """
    True if this server env folder contains firmware usable for the connected camera.

    Uses binaries/VMC####/.enc first, then other VMC#### trees, then updaterules/archive text
    (includes codenames from search_aliases, e.g. Octopus for VMC3073 family).
    """
    folder_abs = os.path.abspath(server_folder_abs)
    vmc_u = (camera_vmc or "").strip().upper()
    if not re.match(r"^VMC\d{4}$", vmc_u):
        return True

    needles: set[str] = {vmc_u}
    for a in search_aliases or []:
        s = (a or "").strip().upper()
        if s:
            needles.add(s)

    if not folder_has_firmware_artifacts(folder_abs):
        return False

    bin_target = os.path.join(folder_abs, "binaries", vmc_u)
    if os.path.isdir(bin_target) and dir_has_enc_files(bin_target):
        return True

    bin_root = os.path.join(folder_abs, "binaries")
    vmc_subdirs_with_enc: set[str] = set()
    if os.path.isdir(bin_root):
        try:
            for name in os.listdir(bin_root):
                if not _VMC_BIN_SUBDIR_RE.match(name or ""):
                    continue
                p = os.path.join(bin_root, name)
                if os.path.isdir(p) and dir_has_enc_files(p):
                    vmc_subdirs_with_enc.add(name.upper())
        except OSError:
            pass

    if vmc_subdirs_with_enc:
        return vmc_u in vmc_subdirs_with_enc

    hay = _folder_haystack_for_model_detection(folder_abs)
    return _needles_match_haystack(needles, hay)


def folder_has_firmware_artifacts(server_folder_dir: str) -> bool:
    """True if this server folder already has firmware files (overwrite warning)."""
    if not os.path.isdir(server_folder_dir):
        return False
    archive = os.path.join(server_folder_dir, "archive")
    binaries = os.path.join(server_folder_dir, "binaries")
    rules = os.path.join(server_folder_dir, "updaterules")
    try:
        if os.path.isdir(archive):
            for f in os.listdir(archive):
                fl = f.lower()
                if fl.endswith((".zip", ".tar.gz")) or any(fl.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES):
                    return True
        if os.path.isdir(binaries):
            for root, _dirs, files in os.walk(binaries):
                for f in files:
                    if f.lower().endswith(".enc"):
                        return True
        if os.path.isdir(rules):
            for f in os.listdir(rules):
                if f.lower().endswith(".json"):
                    return True
    except OSError:
        pass
    return False


def _normalize_fw_version_token(s: str) -> str:
    """Lowercase, strip; first dotted numeric run (1.300, 1.300.0.42, etc.)."""
    t = (s or "").strip().lower()
    t = re.sub(r"\s+", "", t)
    m = re.search(r"(\d+(?:\.\d+)+)", t)
    return m.group(1) if m else t


def _fw_build_tokens_compatible(sel_token: str, loc_token: str) -> bool:
    """True if selection and local version refer to the same build (prefix / exact on dotted tokens)."""
    s = (sel_token or "").strip().lower()
    l = (loc_token or "").strip().lower()
    if not s or not l:
        return False
    if s == l:
        return True
    if l.startswith(s + ".") or s.startswith(l + "."):
        return True
    return False


def _archive_suggests_selected_build(
    archive_dir: str,
    selected_archive_name: str | None,
    selected_version_path: str,
) -> bool:
    """True if any file in archive/ plausibly is the same build as the Artifactory selection."""
    if not os.path.isdir(archive_dir):
        return False
    sel_name = (selected_archive_name or "").strip()
    sel_low = sel_name.lower()
    path_tok = _normalize_fw_version_token(selected_version_path)
    name_tok = _normalize_fw_version_token(selected_archive_name or "")
    sel_tok = path_tok or name_tok
    try:
        names = os.listdir(archive_dir)
    except OSError:
        return False
    for fn in names:
        low = fn.lower()
        if not (low.endswith(".zip") or ".tar.gz" in low):
            continue
        if sel_low and low == sel_low:
            return True
        if sel_low and sel_low in low or low in sel_low:
            return True
        arch_tok = _normalize_fw_version_token(fn)
        if sel_tok and arch_tok and _fw_build_tokens_compatible(sel_tok, arch_tok):
            return True
    return False


def _list_files_in_archive_dir(server_folder_dir: str) -> list[str]:
    arch = os.path.join(server_folder_dir, "archive")
    if not os.path.isdir(arch):
        return []
    try:
        return sorted(os.listdir(arch))
    except OSError:
        return []


def debug_probe_local_firmware_folder(
    tag: str,
    server_folder_abs: str,
    *,
    selected_version_path: str,
    selected_archive_name: str | None,
) -> None:
    """
    Console + logger probe for skip-download debugging (items 1–4).
    Typical layout after a successful download + extract: archive/ (.zip or .tar.gz),
    binaries/<VMC>/ (*.enc), updaterules/ (*update*rule*.json with version fields).
    """
    root = os.path.abspath(server_folder_abs)

    def out(msg: str) -> None:
        print(msg, flush=True)
        _FW_LOCAL_DETECT_LOG.info(msg)

    out(f"[FW_LOCAL_DETECT] ========== {tag} ==========")
    out(f"[FW_LOCAL_DETECT] 1. Target server folder (absolute): {root}")

    out("[FW_LOCAL_DETECT] 2. Directory tree (depth-limited):")
    if not os.path.isdir(root):
        out(f"[FW_LOCAL_DETECT]    (folder does not exist yet: {root!r})")
    else:
        max_depth = 6
        max_entries = 400
        n = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root):
            if truncated:
                break
            depth = dirpath[len(root) :].count(os.sep)
            if depth >= max_depth:
                dirnames[:] = []
                continue
            rel = os.path.relpath(dirpath, root)
            disp = "." if rel in (".", "") else rel
            out(f"[FW_LOCAL_DETECT]    {disp}/")
            for fn in sorted(filenames):
                n += 1
                if n > max_entries:
                    out(f"[FW_LOCAL_DETECT]    ... ({max_entries}+ entries, truncated)")
                    truncated = True
                    dirnames[:] = []
                    break
                out(f"[FW_LOCAL_DETECT]       {fn}")

    has_art = folder_has_firmware_artifacts(root)
    local_label = firmware_folder_version_label(root)
    arch_files = _list_files_in_archive_dir(root)
    archive_dir = os.path.join(root, "archive")
    archive_path_hit = bool(
        selected_archive_name
        and os.path.isfile(os.path.join(archive_dir, selected_archive_name))
    )
    archive_heuristic = _archive_suggests_selected_build(
        archive_dir, selected_archive_name, selected_version_path
    )
    combined_sel = f"{selected_version_path or ''}/{selected_archive_name or ''}"
    sel_tok = (
        _normalize_fw_version_token(combined_sel)
        or _normalize_fw_version_token(selected_version_path)
        or _normalize_fw_version_token(selected_archive_name or "")
    )
    loc_tok = _normalize_fw_version_token(local_label) if local_label and local_label != "—" else ""
    ver_tok_match = bool(
        sel_tok and loc_tok and (_fw_build_tokens_compatible(sel_tok, loc_tok) or sel_tok == loc_tok)
    )

    out("[FW_LOCAL_DETECT] 3. What we check for 'already have this build':")
    out(
        "[FW_LOCAL_DETECT]    - folder_has_firmware_artifacts() "
        "(archive/*.zip|*.tar.gz, binaries/**/*.enc, updaterules/*.json)"
    )
    out(
        "[FW_LOCAL_DETECT]    - Exact file: archive/<selected_archive_filename> on disk "
        f"=> {archive_path_hit!r}"
    )
    out(f"[FW_LOCAL_DETECT]    - Archive heuristic (name/token vs archive/*) => {archive_heuristic!r}")
    out(
        "[FW_LOCAL_DETECT]    - Version: firmware_folder_version_label() "
        "(updaterules JSON version* fields, else semver in archive filename) "
        f"=> {local_label!r}"
    )
    out(
        "[FW_LOCAL_DETECT]    - Normalized semver compare "
        f"selection({selected_version_path!r} / {selected_archive_name!r}) "
        f"sel_tok={sel_tok!r} vs local_tok={loc_tok!r} => {ver_tok_match!r}"
    )
    if arch_files:
        out(f"[FW_LOCAL_DETECT]    - archive/ listing: {arch_files!r}")

    cls = classify_local_firmware_vs_selection(root, selected_version_path, selected_archive_name)
    exact = cls == "exact_match"
    found = has_art
    if cls == "empty":
        reason = "no firmware artifacts in folder (or folder missing)"
    elif cls == "exact_match":
        if archive_path_hit:
            reason = "selected archive filename already present under archive/"
        elif archive_heuristic:
            reason = "archive/ file matches selection by name or version token heuristic"
        elif ver_tok_match:
            reason = "local version token matches selection (JSON/archive-derived vs path/filename)"
        else:
            reason = "classified exact_match"
    else:
        reason = f"firmware present but not matched to selection (local label {local_label!r})"

    out(
        f"[FW_LOCAL_DETECT] 4. Firmware found: {found!s}; "
        f"exact match vs selection: {exact!s}; classification={cls!r}; reason: {reason}"
    )
    out(f"[FW_LOCAL_DETECT] ========== end {tag} ==========")


def classify_local_firmware_vs_selection(
    server_folder_abs: str,
    selected_version_path: str,
    selected_archive_name: str | None,
) -> Literal["empty", "exact_match", "different_present"]:
    """
    empty: no prior firmware tree content.
    exact_match: same archive on disk or same normalized version as selection.
    different_present: tree has firmware but not the selected build.
    """
    root = os.path.abspath(server_folder_abs)
    if not folder_has_firmware_artifacts(root):
        return "empty"

    archive_dir = os.path.join(root, "archive")
    if selected_archive_name and os.path.isfile(os.path.join(archive_dir, selected_archive_name)):
        return "exact_match"

    if _archive_suggests_selected_build(archive_dir, selected_archive_name, selected_version_path):
        return "exact_match"

    local_label = firmware_folder_version_label(root)
    combined_sel = f"{selected_version_path or ''}/{selected_archive_name or ''}"
    sel_tok = (
        _normalize_fw_version_token(combined_sel)
        or _normalize_fw_version_token(selected_version_path)
        or _normalize_fw_version_token(selected_archive_name or "")
    )
    loc_tok = _normalize_fw_version_token(local_label) if local_label and local_label != "—" else ""
    if sel_tok and loc_tok and (
        sel_tok == loc_tok or _fw_build_tokens_compatible(sel_tok, loc_tok)
    ):
        return "exact_match"

    return "different_present"


def rename_server_folder(root: str, old_name: str, new_name: str) -> tuple[bool, str]:
    """Rename a folder under the FW server root. Returns (ok, error_message)."""
    o = sanitize_server_folder_name(old_name)
    n = sanitize_server_folder_name(new_name)
    if not o:
        return False, "Invalid current folder name."
    if not n:
        return False, "Invalid new folder name."
    if o == n:
        return True, ""
    op = os.path.join(root, o)
    np = os.path.join(root, n)
    if not os.path.isdir(op):
        return False, f"Folder not found: {o}"
    if os.path.exists(np):
        return False, f"A folder named {n!r} already exists."
    try:
        os.rename(op, np)
    except OSError as e:
        return False, str(e)
    return True, ""


def list_environment_folders(root: str) -> list[str]:
    """Subdirectory names under FW server root (user-defined server folders)."""
    if not os.path.isdir(root):
        return []
    try:
        names = [
            d
            for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")
        ]
        return sorted(names, key=str.lower)
    except OSError:
        return []


def search_firmware_archives(
    base_url: str,
    token: str,
    version_filter: str,
    fw_search_models: list[str],
    username: str | None = None,
) -> tuple[bool, list[tuple[str, str]], str]:
    """
    Query Artifactory and return flat (repo_folder_path, filename) archives.
    version_filter may be empty (matches broadly in client-side filter).
    """
    ok, available, err = list_available_firmware(
        base_url, token, version_filter, fw_search_models, username
    )
    if not ok:
        return False, [], err or "Search failed."
    flat = flatten_firmware_archives(available)
    return True, flat, ""


def download_firmware_to_layout(
    token: str,
    download_model: str,
    version: str,
    binaries_dir_for_download: str,
    updaterules_dir: str,
    archive_dir: str,
    base_url: str,
    username: str | None,
    selected_filename: str | None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    byte_progress_callback: Callable[[int, int | None], None] | None = None,
) -> tuple[bool, str]:
    """Download selected archive + sidecar files into archive/updaterules/binaries staging."""
    return download_firmware(
        token,
        download_model,
        version,
        binaries_dir_for_download,
        updaterules_dir,
        base_url=base_url,
        username=username,
        progress_callback=progress_callback,
        byte_progress_callback=byte_progress_callback,
        files_allowlist=[selected_filename] if selected_filename else None,
        repo_folder_path=version if selected_filename else None,
        archive_dir=archive_dir,
    )


def extract_firmware_archive(
    archive_path: str,
    chosen_binaries_dir: str,
    updaterules_dir: str,
) -> tuple[bool, str]:
    """Extract .zip or env .tar.gz into chosen binaries folder and updaterules."""
    lower = archive_path.lower()
    if lower.endswith(".zip"):
        return extract_firmware_zip(archive_path, chosen_binaries_dir, updaterules_dir)
    if any(lower.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES) or lower.endswith(".tar.gz"):
        return extract_firmware_tar_gz(archive_path, chosen_binaries_dir, updaterules_dir)
    return True, ""


def ensure_server_and_camera_url(root: str, server_folder: str) -> tuple[bool, str, str]:
    """
    Start the local HTTP server if needed. Returns (ok, error_or_empty, camera_fota_url).
    camera_fota_url is http://<lan-ip>:<port>/<encoded-folder>; folder name preserves case.
    """
    root_abs = os.path.abspath(root)
    base_url = get_base_url_if_serving_root(root_abs)
    if base_url:
        base_url = base_url.rstrip("/")
    else:
        running, _ = check_server_status()
        if running:
            if get_in_process_server_root_abs() != root_abs:
                return (
                    False,
                    "This window already has a firmware server running from a different root. "
                    "Stop it (server stop) before serving another root.",
                    "",
                )
            ok_u, url = get_running_server_url()
            if not ok_u or not url:
                return False, "Could not read running server URL.", ""
            base_url = url.rstrip("/")
        else:
            ok, msg = start_http_server(root_abs, DEFAULT_PORT)
            if not ok:
                return False, msg or "Failed to start server.", ""
            base_url = msg.rstrip("/")
    local_ip = get_local_ipv4()
    host_part = base_url.replace("http://", "").replace("https://", "").strip("/")
    port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
    seg = quote((server_folder or "").strip(), safe="")
    firmware_url = f"http://{local_ip}:{port}/{seg}"
    return True, "", firmware_url


def firmware_folder_version_label(folder_path: str) -> str:
    """Best-effort version string from UpdateRules JSON or archive filenames."""
    rules_dir = os.path.join(folder_path, "updaterules")
    if os.path.isdir(rules_dir):
        for name in sorted(os.listdir(rules_dir)):
            low = name.lower()
            if not low.endswith(".json") or "update" not in low or "rule" not in low:
                continue
            jpath = os.path.join(rules_dir, name)
            try:
                with open(jpath, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key in (
                        "version",
                        "Version",
                        "firmwareVersion",
                        "FirmwareVersion",
                        "fw_version",
                    ):
                        v = data.get(key)
                        if v is not None and str(v).strip():
                            return str(v).strip()
            except (OSError, json.JSONDecodeError, TypeError):
                continue
    arch = os.path.join(folder_path, "archive")
    if os.path.isdir(arch):
        best = ""
        for name in os.listdir(arch):
            low = name.lower()
            if not (low.endswith(".zip") or ".tar.gz" in low):
                continue
            m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", name)
            if m:
                return m.group(1)
            if len(name) > len(best):
                best = name
        if best:
            return best
    return "—"


def firmware_folder_model_label(folder_path: str) -> str:
    """VMC / product folder name under binaries/ that contains .enc files (same logic as Local Server cards)."""
    bin_root = os.path.join(folder_path, "binaries")
    if not os.path.isdir(bin_root):
        return "—"
    try:
        subdirs = sorted(
            (n for n in os.listdir(bin_root) if os.path.isdir(os.path.join(bin_root, n))),
            key=str.lower,
        )
    except OSError:
        return "—"
    for name in subdirs:
        p = os.path.join(bin_root, name)
        try:
            for _r, _d, files in os.walk(p):
                if any(f.lower().endswith(".enc") for f in files):
                    return name
        except OSError:
            continue
    return subdirs[0] if subdirs else "—"


def scan_firmware_folders_with_versions(server_root: str, vmc_model: str) -> list[tuple[str, str]]:
    """
    (folder_name, version_label) for subdirs that look like firmware trees for this device.
    """
    root = os.path.abspath(server_root)
    if not os.path.isdir(root):
        return []
    vmc = (vmc_model or "").strip().upper()
    try:
        names = sorted(
            (d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")),
            key=str.lower,
        )
    except OSError:
        return []
    out: list[tuple[str, str]] = []
    for name in names:
        p = os.path.join(root, name)
        bin_vm = os.path.join(p, "binaries", vmc)
        if os.path.isdir(bin_vm) or folder_has_firmware_artifacts(p):
            out.append((name, firmware_folder_version_label(p)))
    return out


def active_folder_from_camera_update_url(raw_update_url: str, folder_names: list[str]) -> str | None:
    """Match camera update_url output to a server folder name (last path segment)."""
    raw = (raw_update_url or "").strip()
    if not raw:
        return None
    try:
        path = urlparse(raw).path or ""
    except Exception:
        path = ""
    parts = [p for p in path.split("/") if p]
    seg = parts[-1] if parts else ""
    if not seg:
        return None
    seg_decoded = unquote(seg)
    for f in folder_names:
        if f.lower() == seg_decoded.lower():
            return f
    return None


def build_camera_fota_url_for_folder(server_root: str, folder_name: str) -> tuple[bool, str, str]:
    """(ok, err, url) when a server is already running for server_root."""
    root_abs = os.path.abspath(server_root)
    base = get_base_url_if_serving_root(root_abs)
    if not base:
        running, _ = check_server_status()
        if running and get_in_process_server_root_abs() == root_abs:
            ok_u, url = get_running_server_url()
            if ok_u and url:
                base = url.rstrip("/")
    if not base:
        return False, "No firmware server is running for this server root.", ""
    local_ip = get_local_ipv4()
    host_part = base.replace("http://", "").replace("https://", "").strip("/")
    port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
    seg = quote((folder_name or "").strip(), safe="")
    return True, "", f"http://{local_ip}:{port}/{seg}"


def prepare_env_directories(
    root: str,
    env: str,
    model_name: str,
    fw_search_models: list[str],
) -> tuple[bool, str, str, str, str, str]:
    """
    Create server-folder layout under root. Returns (ok, env_dir, binaries_base, binaries_primary_dir, updaterules_dir, archive_dir).
    binaries_primary_dir is <folder>/binaries/<model_name> for the connected device (VMCxxxx).
    fw_search_models is only passed through for API compatibility with setup_directory_structure.
    """
    ok, path_or_err, binaries_dir, updaterules_dir, archive_dir = setup_directory_structure(
        root, env, model_name, fw_search_models=fw_search_models
    )
    if not ok:
        return False, path_or_err, "", "", "", ""
    env_dir = path_or_err
    binaries_base = os.path.join(env_dir, "binaries")
    return True, env_dir, binaries_base, binaries_dir, updaterules_dir, archive_dir
