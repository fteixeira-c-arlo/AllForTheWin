"""Local firmware HTTP server for update_url flow."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import tarfile
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler
from typing import Any

try:
    import ctypes
except ImportError:
    ctypes = None  # type: ignore[misc, assignment]

# Default port and root; server state for stop/status
DEFAULT_PORT = 8000
_server: HTTPServer | None = None
_server_thread: threading.Thread | None = None
_server_root: str = ""
_served_directory: str = ""


def _fw_server_state_base_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    else:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return base


def _fw_server_state_path() -> str:
    """Persisted {pid, port, root} so a new window can detect another ArloHub's server."""
    base = _fw_server_state_base_dir()
    d = os.path.join(base, "ArloHub")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    return os.path.join(d, "fw_server_state.json")


def _legacy_fw_server_state_path() -> str:
    """Pre-rename installs wrote state under this path."""
    return os.path.join(_fw_server_state_base_dir(), "ArloShell", "fw_server_state.json")


def clear_fw_server_state_file() -> None:
    for path in (_fw_server_state_path(), _legacy_fw_server_state_path()):
        try:
            os.remove(path)
        except OSError:
            pass


def _write_fw_server_state(port: int, root: str) -> None:
    path = _fw_server_state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        return
    try:
        payload = {
            "pid": os.getpid(),
            "port": int(port),
            "root": os.path.abspath(root),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError:
        pass


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt" and ctypes is not None:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def read_fw_server_state() -> dict[str, Any] | None:
    """
    Last known firmware HTTP server: {pid, port, root}.
    Removes the file if the PID is gone (stale).
    """
    for path in (_fw_server_state_path(), _legacy_fw_server_state_path()):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        pid = int(data.get("pid") or 0)
        port = int(data.get("port") or 0)
        root = str(data.get("root") or "")
        if pid <= 0 or port <= 0 or not root:
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        if not _pid_exists(pid):
            try:
                os.remove(path)
            except OSError:
                pass
            continue
        return {"pid": pid, "port": port, "root": os.path.abspath(root)}
    return None


def get_in_process_server_root_abs() -> str:
    """Directory served by this process's firmware HTTP server, or empty if none."""
    if _server is None:
        return ""
    return os.path.abspath(_server_root)


def get_base_url_if_serving_root(desired_root: str) -> str | None:
    """
    If something is already serving desired_root on localhost, return http://localhost:<port>.
    Uses in-process server first, then persisted cross-session state + TCP check.
    """
    want = os.path.abspath(desired_root)
    if _server is not None and _server_root:
        if os.path.abspath(_server_root) == want:
            ok, url = get_running_server_url()
            if ok and url:
                return url.rstrip("/")
        return None
    st = read_fw_server_state()
    if st and os.path.abspath(str(st["root"])) == want and is_firmware_port_accepting_connections(
        int(st["port"])
    ):
        return f"http://localhost:{int(st['port'])}"
    return None


def firmware_folder_rename_blocked_reason(folder_abs: str) -> str | None:
    """
    If renaming folder_abs would conflict with an active firmware HTTP server, return a user message.
    """
    folder_abs = os.path.abspath(folder_abs)
    if _server is not None and _server_root:
        try:
            sr = os.path.abspath(_server_root)
            if os.path.commonpath([sr, folder_abs]) == sr:
                return (
                    "Can't rename this folder: the firmware HTTP server in this window is serving "
                    "this directory tree. Stop the server first (e.g. server stop or Stop server & close in the wizard)."
                )
        except ValueError:
            pass
    st = read_fw_server_state()
    if st and is_firmware_port_accepting_connections(int(st["port"])):
        try:
            sr = os.path.abspath(str(st["root"]))
            if os.path.commonpath([sr, folder_abs]) == sr:
                return (
                    f"Can't rename: a firmware server is still active on port {int(st['port'])} "
                    f"(PID {int(st['pid'])}), serving this tree. Stop it in the ArloHub window that "
                    "started the server, then retry."
                )
        except ValueError:
            pass
    return None


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
    Create under root: [server_folder]/archive/, [server_folder]/binaries/[model_name]/, [server_folder]/updaterules/.
    Only the connected device's VMC folder (model_name, e.g. VMC3070) is created under binaries/.
    fw_search_models is ignored for layout (kept for call-site compatibility).
    environment is the folder name as provided (case preserved); must be a single path segment.
    Returns (success, env_dir, binaries_dir, updaterules_dir, archive_dir).
    """
    _ = fw_search_models
    folder = (environment or "").strip()
    if not folder or folder in (".", "..") or os.sep in folder or (os.altsep and os.altsep in folder):
        return False, "Invalid server folder name.", "", "", ""
    env_dir = os.path.join(root, folder)
    updaterules_dir = os.path.join(env_dir, "updaterules")
    archive_dir = os.path.join(env_dir, "archive")
    binaries_dir = os.path.join(env_dir, "binaries", model_name)
    try:
        os.makedirs(binaries_dir, exist_ok=True)
        os.makedirs(updaterules_dir, exist_ok=True)
        os.makedirs(archive_dir, exist_ok=True)
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
    # Confirm the listening socket accepts connections (bind can succeed but serve path still fail).
    try:
        with socket.create_connection(("127.0.0.1", actual_port), timeout=3.0):
            pass
    except OSError as e:
        try:
            _server.shutdown()
        except Exception:
            pass
        if _server_thread:
            _server_thread.join(timeout=2.0)
        _server = None
        _server_thread = None
        _server_root = ""
        _served_directory = ""
        clear_fw_server_state_file()
        return False, f"Server did not accept connections on port {actual_port}: {e}"

    _write_fw_server_state(actual_port, abs_dir)
    return True, f"http://localhost:{actual_port}"


def stop_http_server() -> tuple[bool, str]:
    """Stop the local firmware server if running. Returns (success, message)."""
    global _server, _server_thread
    if _server is None:
        st = read_fw_server_state()
        if st and int(st["pid"]) == os.getpid():
            clear_fw_server_state_file()
        return True, "No server was running."
    try:
        _server.shutdown()
        _server = None
        if _server_thread:
            _server_thread.join(timeout=2.0)
        _server_thread = None
        clear_fw_server_state_file()
        return True, "Firmware server stopped."
    except Exception as e:
        return False, str(e)


def check_server_status() -> tuple[bool, str]:
    """Return (is_running, message)."""
    if _server is None:
        return False, "Firmware server is not running."
    return True, f"Firmware server is running (root: {_server_root})."


def is_firmware_port_accepting_connections(port: int | None = None) -> bool:
    """True if something accepts TCP connections on localhost:port (e.g. 8000)."""
    p = int(port or DEFAULT_PORT)
    try:
        with socket.create_connection(("127.0.0.1", p), timeout=0.25):
            return True
    except OSError:
        return False


def firmware_server_listener_summary() -> tuple[str, str, str]:
    """
    UI helper: distinguish this-process server vs another listener on the firmware port.

    Returns (dot_color_hint, primary_line, tooltip).
    dot_color_hint: "green" | "amber" | "gray"
    """
    running_here, _msg = check_server_status()
    if running_here:
        assert _server is not None
        _host, prt = _server.server_address
        return (
            "green",
            f"This session · serving on :{prt}",
            "The firmware HTTP server was started from this ArloHub window.",
        )
    st = read_fw_server_state()
    if st and is_firmware_port_accepting_connections(int(st["port"])):
        prt = int(st["port"])
        root_disp = str(st["root"])
        if len(root_disp) > 52:
            root_disp = "…" + root_disp[-49:]
        if int(st["pid"]) == os.getpid():
            return (
                "amber",
                f"State · :{prt} (same process, no server thread)",
                "Stale firmware server state was cleared or the server thread stopped unexpectedly. "
                "Try starting the server again.",
            )
        return (
            "amber",
            f"Other session · serving on :{prt} (PID {int(st['pid'])})",
            f"Another ArloHub process is serving {str(st['root'])}. Stop that window's server before "
            "renaming folders under that root, or manage the server from that window.",
        )
    if is_firmware_port_accepting_connections():
        return (
            "amber",
            f"Port {DEFAULT_PORT} in use (unknown process)",
            "Something is listening on the usual firmware port but there is no matching ArloHub state file. "
            "It may be another tool or an older ArloHub build. Folder renames under the FW root can still fail.",
        )
    return (
        "gray",
        "Firmware server off",
        "No firmware server in this window and no known listener on the default firmware port.",
    )


def firmware_rename_access_denied_user_hint() -> str:
    """Explain likely causes when renaming under the FW root fails with access denied."""
    if check_server_status()[0]:
        return (
            "This window still has the firmware server running. Stop it first (e.g. command "
            f"server stop), then rename."
        )
    st = read_fw_server_state()
    if st and is_firmware_port_accepting_connections(int(st["port"])):
        return (
            f"A firmware server is still active on port {int(st['port'])} (PID {int(st['pid'])}). "
            "Stop it in the ArloHub window that started it, then retry. If it still fails, close "
            "File Explorer on this folder."
        )
    if is_firmware_port_accepting_connections():
        return (
            f"Port {DEFAULT_PORT} is in use on this PC by another process—often a second ArloHub "
            "still running the firmware server over the same folder. Close or stop that instance, "
            "then retry. If it still fails, close File Explorer windows on this folder and wait a few seconds."
        )
    return (
        f"This window is not serving firmware and port {DEFAULT_PORT} is not accepting connections here. "
        "Access denied is usually File Explorer (folder open in another window), a short delay after a "
        "server stopped, or antivirus/indexing. Close Explorer on this path, wait a few seconds, retry."
    )
