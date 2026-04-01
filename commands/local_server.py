"""Local firmware HTTP server for update_url flow."""
import os
import shutil
import socket
import tempfile
import threading
import tarfile
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Any

# Default port and root; server state for stop/status
DEFAULT_PORT = 8000
_server: HTTPServer | None = None
_server_thread: threading.Thread | None = None
_server_root: str = ""
_served_directory: str = ""


def _make_handler(directory: str) -> type[SimpleHTTPRequestHandler]:
    """Handler that serves from directory (no process chdir)."""
    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=directory, **kwargs)
        def log_message(self, format: str, *args: Any) -> None:
            pass
    return _Handler


def _find_free_port(start: int = 8000, max_tries: int = 100) -> int | None:
    """Return a free port >= start or None."""
    for i in range(max_tries):
        port = start + i
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("", port))
                return port
        except OSError:
            continue
    return None


def get_running_server_url() -> tuple[bool, str]:
    """If server is running, return (True, base_url e.g. http://localhost:8000). Else (False, message)."""
    if _server is None:
        return False, "Server is not running."
    _host, port = _server.server_address
    return True, f"http://localhost:{port}"


def extract_firmware_zip(
    zip_path: str,
    binaries_dir: str,
    updaterules_dir: str,
) -> tuple[bool, str]:
    """
    Extract a firmware .zip and place .enc files into binaries_dir and update-rules .json (as-is) into updaterules_dir.
    Walks the extracted content (including subdirs) to find .enc and update-rules JSON (case-insensitive).
    Returns (success, error_message).
    """
    if not os.path.isfile(zip_path) or not zip_path.lower().endswith(".zip"):
        return False, "Not a .zip file or file missing."
    try:
        os.makedirs(binaries_dir, exist_ok=True)
        os.makedirs(updaterules_dir, exist_ok=True)
    except OSError as e:
        return False, str(e)

    with tempfile.TemporaryDirectory(prefix="fw_extract_") as tmpdir:
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile as e:
            return False, f"Invalid or corrupted zip: {e}"
        except OSError as e:
            return False, str(e)

        enc_copied, rules_copied, err = _extract_enc_and_rules_from_dir(tmpdir, binaries_dir, updaterules_dir)
        if err:
            return False, err
        if enc_copied == 0 and rules_copied == 0:
            return False, "No .enc files or UpdateRules.json found inside the zip."

    # Remove the .zip from binaries_dir so server only serves .enc (and manifest if any)
    try:
        if os.path.abspath(os.path.dirname(zip_path)) == os.path.abspath(binaries_dir):
            os.remove(zip_path)
    except OSError:
        pass  # non-fatal

    return True, ""


# .<env>.tar.gz suffixes for firmware archives (qa, dev, prod, prod_signed, ftrial, ftrial_signed)
FW_ENV_TAR_GZ_SUFFIXES = (
    ".qa.tar.gz",
    ".dev.tar.gz",
    ".prod.tar.gz",
    ".prod_signed.tar.gz",
    ".ftrial.tar.gz",
    ".ftrial_signed.tar.gz",
)


def _is_update_rules_file(name: str) -> bool:
    """True if filename looks like update rules (UpdateRules.json, update_rules.json, etc.)."""
    n = name.lower()
    return n.endswith(".json") and "update" in n and "rule" in n


def _extract_enc_and_rules_from_dir(tmpdir: str, binaries_dir: str, updaterules_dir: str) -> tuple[int, int, str]:
    """Walk tmpdir, copy .enc to binaries_dir and update-rules .json (original name) to updaterules_dir. Returns (enc_count, rules_count, error)."""
    enc_copied = 0
    rules_copied = 0
    binaries_dir = os.path.abspath(binaries_dir)
    updaterules_dir = os.path.abspath(updaterules_dir)
    os.makedirs(binaries_dir, exist_ok=True)
    os.makedirs(updaterules_dir, exist_ok=True)
    for root_dir, _dirs, files in os.walk(tmpdir):
        for name in files:
            src = os.path.join(root_dir, name)
            if not os.path.isfile(src):
                continue
            if name.lower().endswith(".enc"):
                dest = os.path.join(binaries_dir, name)
                try:
                    shutil.copy2(src, dest)
                    enc_copied += 1
                except OSError as e:
                    return 0, 0, str(e)
            elif _is_update_rules_file(name):
                # Keep original filename (e.g. UpdateRules.json, update_rule.json)
                dest = os.path.join(updaterules_dir, name)
                try:
                    shutil.copy2(src, dest)
                    rules_copied += 1
                except OSError as e:
                    return 0, 0, str(e)
    return enc_copied, rules_copied, ""


def extract_firmware_tar_gz(
    tar_path: str,
    binaries_dir: str,
    updaterules_dir: str,
) -> tuple[bool, str]:
    """
    Extract a firmware .<env>.tar.gz and place .enc files into binaries_dir and update-rules .json (as-is) into updaterules_dir.
    Returns (success, error_message).
    """
    tar_path_lower = tar_path.lower()
    if not os.path.isfile(tar_path):
        return False, "File missing."
    if not any(tar_path_lower.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES) and not tar_path_lower.endswith(".tar.gz"):
        return False, "Not a .tar.gz firmware archive."
    try:
        os.makedirs(binaries_dir, exist_ok=True)
        os.makedirs(updaterules_dir, exist_ok=True)
    except OSError as e:
        return False, str(e)

    with tempfile.TemporaryDirectory(prefix="fw_extract_") as tmpdir:
        try:
            with tarfile.open(tar_path, "r:gz") as tf:
                tf.extractall(tmpdir)
        except (tarfile.TarError, OSError) as e:
            return False, f"Invalid or corrupted tar.gz: {e}"

        enc_copied, rules_copied, err = _extract_enc_and_rules_from_dir(tmpdir, binaries_dir, updaterules_dir)
        if err:
            return False, err
        if enc_copied == 0 and rules_copied == 0:
            return False, "No .enc files or UpdateRules.json found inside the archive."

    try:
        if os.path.abspath(os.path.dirname(tar_path)) == os.path.abspath(binaries_dir):
            os.remove(tar_path)
    except OSError:
        pass
    return True, ""


def setup_directory_structure(
    root: str,
    environment: str,
    model_name: str,
    fw_search_models: list[str] | None = None,
) -> tuple[bool, str, str, str, str]:
    """
    Create under root: [env]/archive/, [env]/binaries/[model]/ for each model, and [env]/updaterules/.
    When fw_search_models is provided (e.g. 2K + FHD), creates binaries/<m> for each m; otherwise binaries/<model_name>.
    Returns (success, env_dir, binaries_dir, updaterules_dir, archive_dir). binaries_dir is for model_name (primary).
    """
    env_lower = environment.lower()
    env_dir = os.path.join(root, env_lower)
    models_to_create = list(fw_search_models) if fw_search_models else [model_name]
    updaterules_dir = os.path.join(env_dir, "updaterules")
    archive_dir = os.path.join(env_dir, "archive")
    try:
        for m in models_to_create:
            bin_dir = os.path.join(env_dir, "binaries", m)
            os.makedirs(bin_dir, exist_ok=True)
        os.makedirs(updaterules_dir, exist_ok=True)
        os.makedirs(archive_dir, exist_ok=True)
        binaries_dir = os.path.join(env_dir, "binaries", model_name)
        return True, env_dir, binaries_dir, updaterules_dir, archive_dir
    except OSError as e:
        return False, str(e), "", "", ""


def start_http_server(directory: str, port: int | None = None) -> tuple[bool, str]:
    """
    Start HTTP server in a background thread serving directory.
    Returns (success, message). Message is URL or error.
    """
    global _server, _server_thread, _server_root, _served_directory
    if _server is not None:
        return False, "Server is already running. Use stop_server first."

    port = port or DEFAULT_PORT
    abs_dir = os.path.abspath(directory)
    if not os.path.isdir(abs_dir):
        return False, f"Directory does not exist: {abs_dir}"

    actual_port = _find_free_port(port)
    if actual_port is None:
        return False, f"No free port in range {port}-{port + 99}."

    try:
        _server = HTTPServer(("", actual_port), _make_handler(abs_dir))
    except OSError as e:
        return False, f"Failed to start server: {e}"

    _server_root = abs_dir
    _served_directory = abs_dir

    def serve() -> None:
        assert _server is not None
        _server.serve_forever()

    _server_thread = threading.Thread(target=serve, daemon=True)
    _server_thread.start()
    return True, f"http://localhost:{actual_port}"


def stop_http_server() -> tuple[bool, str]:
    """Stop the local firmware server if running. Returns (success, message)."""
    global _server, _server_thread
    if _server is None:
        return True, "No server was running."
    try:
        _server.shutdown()
        _server = None
        if _server_thread:
            _server_thread.join(timeout=2.0)
        _server_thread = None
        return True, "Firmware server stopped."
    except Exception as e:
        return False, str(e)


def check_server_status() -> tuple[bool, str]:
    """Return (is_running, message)."""
    if _server is None:
        return False, "Firmware server is not running."
    return True, f"Firmware server is running (root: {_server_root})."
