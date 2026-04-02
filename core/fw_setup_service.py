"""Shared firmware-setup logic for CLI flow and GUI wizard (no UI)."""
from __future__ import annotations

import os
import socket
import sys
from typing import Callable
from urllib.parse import quote

from core.artifactory_client import ARTIFACTORY_REPO, download_firmware, list_available_firmware
from core.local_server import (
    DEFAULT_PORT,
    FW_ENV_TAR_GZ_SUFFIXES,
    check_server_status,
    extract_firmware_tar_gz,
    extract_firmware_zip,
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
    running, _ = check_server_status()
    if running:
        ok_u, url = get_running_server_url()
        if not ok_u or not url:
            return False, "Could not read running server URL.", ""
        base_url = url.rstrip("/")
    else:
        ok, msg = start_http_server(root, DEFAULT_PORT)
        if not ok:
            return False, msg or "Failed to start server.", ""
        base_url = msg.rstrip("/")
    local_ip = get_local_ipv4()
    host_part = base_url.replace("http://", "").replace("https://", "").strip("/")
    port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
    seg = quote((server_folder or "").strip(), safe="")
    firmware_url = f"http://{local_ip}:{port}/{seg}"
    return True, "", firmware_url


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
