"""Parse and execute user commands."""
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from core.abstract_dispatcher import (
    execute_abstract_command,
    load_abstract_definitions,
)

# Optional (GUI): live tail view in-app instead of spawning an external terminal.
_tail_live_view_start: Callable[[str, str], None] | None = None
_tail_live_view_stop: Callable[[str], None] | None = None


def set_tail_live_view_handlers(
    start: Callable[[str, str], None] | None,
    stop: Callable[[str], None] | None,
) -> None:
    """When start/stop are set (e.g. GUI), log tail / log parse use them instead of an external tail window."""
    global _tail_live_view_start, _tail_live_view_stop
    _tail_live_view_start = start
    _tail_live_view_stop = stop

from core.log_parser import parse_line, write_html
from core.update_url_flow import try_handle_fw_wizard_command
from interface.menus import (
    show_abstract_commands_section,
    show_commands_table,
    show_connection_status,
    show_error,
    show_info,
)
from interface.prompts import prompt_confirm_proceed, prompt_line, prompt_select_log_file
from utils.validators import validate_ipv4

_COMMAND_PARSER_DIR = Path(__file__).resolve().parent
ABSTRACT_DEFINITIONS: list[dict] = load_abstract_definitions(
    str(_COMMAND_PARSER_DIR / "abstract_command_definitions.json")
)


def get_visible_commands(device_commands: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split the device catalog for display only.

    Returns (abstract_command_definitions, advanced_raw_device_commands).
    Entries with a non-empty ``abstract`` field are implementation steps for an abstract
    command and are omitted from the second list; they remain in the full catalog for execution.
    """
    advanced: list[dict] = []
    for c in device_commands:
        if not isinstance(c, dict):
            continue
        link = c.get("abstract")
        if link is not None and str(link).strip():
            continue
        advanced.append(c)
    return (list(ABSTRACT_DEFINITIONS), advanced)


# Default remote path for log archive on camera (BusyBox tar creates uncompressed .tar)
PULL_LOGS_REMOTE_PATH = "/tmp/allsystem.logs.tar"

# Action result: "continue" | "disconnect" | "exit" | "back"
# command_profiles: None = show for every device profile; else list of profile ids (e.g. e3_wired).
SYSTEM_COMMANDS = [
    {"name": "help", "description": "Show available commands", "command_profiles": None},
    {"name": "status", "description": "Show connection status", "command_profiles": None},
    {"name": "server stop", "description": "Stop the local firmware HTTP server", "command_profiles": ["e3_wired"]},
    {"name": "server status", "description": "Check whether the local firmware HTTP server is running", "command_profiles": ["e3_wired"]},
    {"name": "fw local", "description": "Start local firmware server and set camera update URL to it", "command_profiles": ["e3_wired"]},
    {
        "name": "fw_wizard",
        "description": "FW Wizard (CLI prompts): Artifactory download, local server, set camera update URL. In the GUI prefer Tools → FW Wizard.",
        "command_profiles": ["e3_wired"],
    },
    {"name": "config_show", "description": "Show saved Artifactory credentials (no token)", "command_profiles": None},
    {"name": "config_update", "description": "Update saved Artifactory credentials", "command_profiles": None},
    {"name": "config_delete", "description": "Delete saved Artifactory credentials", "command_profiles": None},
    {"name": "log tail", "description": "Stream device system log live; use log tail stop to stop", "command_profiles": ["e3_wired"]},
    {"name": "log tail stop", "description": "Stop log streaming and save the log file", "command_profiles": ["e3_wired"]},
    {"name": "log parse", "description": "Stream and parse logs live; use log parse stop to save HTML report", "command_profiles": ["e3_wired"]},
    {"name": "log parse stop", "description": "Stop log parsing and save HTML report", "command_profiles": ["e3_wired"]},
    {"name": "parse_log_file", "description": "Select a log file from arlo_logs folder and generate HTML parse report", "command_profiles": None},
    {"name": "log export", "description": "Archive logs on device and upload via TFTP (UART only)", "command_profiles": ["e3_wired"]},
    {"name": "disconnect", "description": "Close connection and exit", "command_profiles": None},
    {"name": "exit", "description": "Close connection and exit", "command_profiles": None},
    {"name": "back", "description": "Disconnect and return to main menu", "command_profiles": None},
]


# Underscore names remapped to canonical multi-word tool vocabulary (full line, lowercased).
_TOOL_NAME_ALIASES: dict[str, str] = {
    "tail_logs": "log tail",
    "tail_logs_stop": "log tail stop",
    "parse_logs": "log parse",
    "parse_logs_stop": "log parse stop",
    "export_logs_tftp": "log export",
    "use_local_fw": "fw local",
    "stop_server": "server stop",
    "server_status": "server status",
}


_MULTIWORD_E3_TOOL_NAMES = frozenset(
    {
        "log tail",
        "log tail stop",
        "log parse",
        "log parse stop",
        "log export",
        "fw local",
        "server stop",
        "server status",
    }
)


def get_tools_for_profile(command_profile: str, connection_type: str = "") -> list[dict]:
    """E3-wired multi-word system tools (prefix-matched like abstract commands)."""
    pid = (command_profile or "none").strip() or "none"
    _ = connection_type  # reserved for transport-specific tool visibility
    out: list[dict] = []
    for c in SYSTEM_COMMANDS:
        name = (c.get("name") or "").strip()
        if name not in _MULTIWORD_E3_TOOL_NAMES:
            continue
        prof = c.get("command_profiles")
        if not (isinstance(prof, (list, tuple, set)) and pid in prof):
            continue
        out.append(c)
    return sorted(out, key=lambda d: len((d.get("name") or "").strip()), reverse=True)


# Path of the log file when log tail or log parse is running
_tail_log_path: str | None = None
# True when current tail session is log parse (so stop generates HTML)
_parse_logs_mode: bool = False
# Accumulated parsed entries for log parse stop (append from reader thread)
_parsed_entries: list[dict] = []
_parsed_entries_lock: threading.Lock = threading.Lock()


def _spawn_tail_viewer_terminal(log_path: str, title: str | None = None) -> None:
    """Open a new terminal window that follows the log file (tail -f style). Log is still saved when log tail stop / log parse stop is used."""
    if title is None:
        title = "Tail logs - system-log_V1_0"
    try:
        if sys.platform == "win32":
            # Use a temp PowerShell script to avoid quoting issues; -Wait keeps window open and streams new lines
            log_dir = os.path.dirname(log_path)
            script_path = os.path.join(log_dir, "_tail_view.ps1")
            path_in_script = log_path.replace("'", "''")
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(f"Get-Content -LiteralPath '{path_in_script}' -Wait\n")
            subprocess.Popen(
                ["cmd", "/c", "start", title, "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path],
                cwd=os.getcwd(),
            )
        else:
            term = shutil.which("gnome-terminal") or shutil.which("xterm") or shutil.which("x-terminal-emulator")
            if term:
                cmd = f"tail -f {shutil.quote(log_path)}; exec bash"
                if "gnome-terminal" in term:
                    subprocess.Popen([term, "--", "bash", "-c", cmd])
                else:
                    subprocess.Popen([term, "-e", cmd])
            else:
                subprocess.Popen(["xterm", "-e", f"tail -f {shutil.quote(log_path)}; exec bash"])
    except Exception:
        pass


def _strip_command_profile_meta(cmd: dict) -> dict:
    return {k: v for k, v in cmd.items() if k != "command_profiles"}


def get_system_commands() -> list[dict]:
    """Return all system command definitions for display (no profile filter)."""
    return [_strip_command_profile_meta(dict(c)) for c in SYSTEM_COMMANDS]


def get_system_commands_for_profile(command_profile: str) -> list[dict]:
    """System commands visible for this device command profile (e.g. e3_wired vs none)."""
    pid = (command_profile or "none").strip() or "none"
    out: list[dict] = []
    for c in SYSTEM_COMMANDS:
        prof = c.get("command_profiles")
        if prof is None:
            out.append(_strip_command_profile_meta(dict(c)))
        elif isinstance(prof, (list, tuple, set)) and pid in prof:
            out.append(_strip_command_profile_meta(dict(c)))
    return out


def _is_kv_bs_claimed_one(raw_output: str) -> bool:
    """Return True if kvcmd get KV_BS_CLAIMED output indicates value 1 (onboarded)."""
    if not raw_output:
        return False
    text = raw_output.strip()
    # Whole output is just "1"
    if text == "1":
        return True
    # Key=value or "Key: value" style
    if "KV_BS_CLAIMED" in raw_output and ("=1" in raw_output or ": 1" in raw_output or ":1" in raw_output):
        return True
    # Any line is exactly "1" (handles raw UART buffer with echo + value + prompt)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines:
        if line == "1":
            return True
    # Last non-empty line is "1" (common for CLI that prints only the value)
    if lines and lines[-1] == "1":
        return True
    return False


def _similar_commands(name: str, commands: list[dict]) -> list[str]:
    """Return command names that are similar to name (simple substring/prefix)."""
    name_lower = name.lower()
    out = []
    for c in commands:
        cn = c["name"].lower()
        if name_lower in cn or cn in name_lower:
            out.append(c["name"])
    return out[:5]


def _match_abstract_prefix(parts: list[str]) -> tuple[str, list[str]] | None:
    """
    If the leading tokens match a known abstract command name, return (abstract_name, remaining_args).
    Longer names win so e.g. 'update url get' is matched before 'update url'.
    """
    if not parts or not ABSTRACT_DEFINITIONS:
        return None
    defs = sorted(
        ABSTRACT_DEFINITIONS,
        key=lambda d: len((d.get("name") or "").strip()),
        reverse=True,
    )
    for d in defs:
        name = (d.get("name") or "").strip()
        if not name:
            continue
        name_words = name.split()
        if len(parts) < len(name_words):
            continue
        if [p.lower() for p in parts[: len(name_words)]] != [
            w.lower() for w in name_words
        ]:
            continue
        return name, parts[len(name_words) :]
    return None


def _match_tool_prefix(parts: list[str], tools: list[dict]) -> tuple[str, list[str]] | None:
    """
    If leading tokens match a tool entry's name, return (tool_name, remaining_args).
    Longer names win (same rules as _match_abstract_prefix).
    """
    if not parts or not tools:
        return None
    for t in tools:
        name = (t.get("name") or "").strip()
        if not name:
            continue
        name_words = name.split()
        if len(parts) < len(name_words):
            continue
        if [p.lower() for p in parts[: len(name_words)]] != [
            w.lower() for w in name_words
        ]:
            continue
        return name, parts[len(name_words) :]
    return None


def _abstract_help_arg_suffix(arg_specs: list[Any]) -> str:
    """Format abstract `args` JSON field for help, e.g. ' <url>' or ' <ssid> <password> [<security>]'"""
    if not arg_specs:
        return ""
    parts: list[str] = []
    for spec in arg_specs:
        s = str(spec).strip()
        if not s:
            continue
        optional = s.endswith("?")
        base = s[:-1].strip() if optional else s
        if not base:
            continue
        if optional:
            parts.append(f"[<{base}>]")
        else:
            parts.append(f"<{base}>")
    return (" " + " ".join(parts)) if parts else ""


def _abstract_help_transport_tag(restriction: Any) -> str:
    if restriction is None or restriction == "":
        return ""
    r = str(restriction).strip().lower()
    if r == "no_uart":
        return " [no UART]"
    if r == "adb_only":
        return " [ADB only]"
    return ""


def _abstract_help_lines(definitions: list[dict]) -> list[str]:
    """One line per abstract command: name, args, description, optional transport tag."""
    lines: list[str] = []
    for d in definitions:
        if not isinstance(d, dict):
            continue
        name = (d.get("name") or "").strip()
        if not name:
            continue
        desc = (d.get("description") or "").strip()
        arg_specs = d.get("args") or []
        if not isinstance(arg_specs, list):
            arg_specs = []
        args_suffix = _abstract_help_arg_suffix(arg_specs)
        tag = _abstract_help_transport_tag(d.get("transport_restriction"))
        lines.append(f"{name}{args_suffix}  —  {desc}{tag}")
    return lines


def get_abstract_command_help_lines() -> list[str]:
    """Lines for the primary «Commands» section (same as help output)."""
    return _abstract_help_lines(ABSTRACT_DEFINITIONS)


def _run_push_arlod(
    connection_handle: Any,
    connection_type: str,
    abstract_args: list[str],
) -> tuple[str, str | None]:
    """
    ADB-only flow: auth (password arlo), push binary, killall, chmod, start arlod in background.
    """
    if (connection_type or "").strip().upper() != "ADB":
        show_error(
            "push arlod requires an ADB connection.",
            "Connect via ADB and try again.",
        )
        return "continue", None

    if not abstract_args or not str(abstract_args[0]).strip():
        show_error(
            "push arlod requires a local file path.",
            "Usage: push arlod <local_path>",
        )
        return "continue", None

    local_path = os.path.expanduser(str(abstract_args[0]).strip())
    if not os.path.isfile(local_path):
        show_error(f"Local file not found: {local_path}")
        return "continue", None

    from transports.adb_handler import ADBHandler

    if not isinstance(connection_handle, ADBHandler) or not connection_handle.is_connected():
        show_error(
            "push arlod requires an active ADB session.",
            "Connect to the camera over ADB first.",
        )
        return "continue", None

    serial = connection_handle.device_identifier()
    if not serial:
        show_error("No ADB device serial is available. Reconnect and try again.")
        return "continue", None

    adb = ADBHandler._adb_cmd()

    def _disconnected(stderr: str) -> bool:
        s = (stderr or "").strip().lower()
        if not s:
            return False
        return (
            "device offline" in s
            or "no devices/emulators found" in s
            or "device not found" in s
            or ("device '" in s and "not found" in s)
        )

    # 1) adb shell auth + password arlo
    try:
        proc = subprocess.Popen(
            [adb, "-s", serial, "shell", "auth"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        out, err = proc.communicate(input="arlo\n", timeout=30)
    except FileNotFoundError:
        show_error("adb not found.", "Install Android platform-tools and add adb to PATH.")
        return "continue", None
    except subprocess.TimeoutExpired:
        show_error("push arlod failed at step 1/5 (adb shell auth).", "Authentication timed out.")
        return "continue", None
    except Exception as e:
        show_error(f"push arlod failed at step 1/5 (adb shell auth): {e}")
        return "continue", None

    auth_combined = ((out or "") + (err or "")).strip()
    if proc.returncode != 0:
        show_error(
            "push arlod failed at step 1/5 (adb shell auth).",
            auth_combined or "Authentication failed.",
        )
        return "continue", None
    show_info("push arlod: step 1/5 — adb shell auth succeeded.")

    # 2) adb push
    try:
        r = subprocess.run(
            [adb, "-s", serial, "push", local_path, "/userdata/arlod"],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        show_error("adb not found.", "Install Android platform-tools and add adb to PATH.")
        return "continue", None
    except subprocess.TimeoutExpired:
        show_error(
            "push arlod failed at step 2/5 (adb push).",
            "Push timed out.",
        )
        return "continue", None
    except Exception as e:
        show_error(f"push arlod failed at step 2/5 (adb push): {e}")
        return "continue", None

    stderr = (r.stderr or "").strip()
    if _disconnected(stderr):
        return "disconnected", None
    if r.returncode != 0:
        show_error(
            "push arlod failed at step 2/5 (adb push).",
            (r.stderr or r.stdout or "adb push failed.").strip(),
        )
        return "continue", None
    show_info("push arlod: step 2/5 — file pushed to /userdata/arlod.")

    shell_steps: list[tuple[str, str]] = [
        ("killall arlod", "3/5 — stopped existing arlod (killall)."),
        ("chmod u+x /userdata/arlod", "4/5 — set execute permission on /userdata/arlod."),
        ("/userdata/arlod &", "5/5 — started arlod in the background."),
    ]
    for shell_cmd, progress_msg in shell_steps:
        try:
            r = subprocess.run(
                [adb, "-s", serial, "shell", shell_cmd],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            show_error(
                f"push arlod failed at step {shell_cmd!r}.",
                "Command timed out.",
            )
            return "continue", None
        except Exception as e:
            show_error(f"push arlod failed running adb shell {shell_cmd!r}: {e}")
            return "continue", None

        if _disconnected(r.stderr or ""):
            return "disconnected", None
        if r.returncode != 0:
            detail = (r.stderr or r.stdout or "Command failed.").strip()
            show_error(
                f"push arlod failed at adb shell: {shell_cmd}",
                detail,
            )
            return "continue", None
        show_info(f"push arlod: step {progress_msg}")

    return "continue", "push arlod completed successfully."


def parse_and_execute(
    line: str,
    model: dict[str, Any],
    connection_type: str,
    device_identifier: str,
    connected_at: str | None,
    device_commands: list[dict],
    connection_execute: Callable[[str, list[str]], tuple[bool, str]] | None,
    connection_pull_file: Callable[[str, str], tuple[bool, str]] | None = None,
    pull_logs_local_dir: str | None = None,
    connection_get_tail_logs_command: Callable[[], str | None] | None = None,
    connection_handle: Any = None,
    command_profile: str = "none",
) -> tuple[str, str | None]:
    """
    Parse user input and execute. Returns (action, message).
    action: "continue" | "disconnected" | "exit" | "back"
    message: optional output to show (e.g. success text or error).
    "disconnected" means the camera was disconnected from the PC; caller should return to device list.
    """
    global _tail_log_path, _parse_logs_mode, _parsed_entries
    line = line.strip()
    if not line:
        return "continue", None
    model_name = (model or {}).get("name") or "Camera"
    profile = (command_profile or (model or {}).get("command_profile") or "none").strip() or "none"
    parts = line.split()
    _raw_line = " ".join(parts).lower().strip()
    if _raw_line in _TOOL_NAME_ALIASES:
        parts = _TOOL_NAME_ALIASES[_raw_line].split()

    _tool_hit = _match_tool_prefix(parts, get_tools_for_profile(profile, connection_type))
    if _tool_hit is not None:
        tool_name, _tool_args = _tool_hit

        if tool_name == "server stop":
            from core.update_url_flow import run_stop_server

            return "continue", run_stop_server()

        if tool_name == "server status":
            from core.update_url_flow import run_server_status

            return "continue", run_server_status()

        if tool_name == "fw local":
            if not connection_execute:
                show_error("Connect to the camera first to use fw local.")
                return "continue", None
            try:
                from core.update_url_flow import run_use_local_fw_server

                err = run_use_local_fw_server(connection_execute)
            except (KeyboardInterrupt, EOFError):
                return "continue", None
            except Exception as e:
                show_error("fw local failed.", str(e))
                return "continue", None
            if err is None:
                return "continue", None
            if err == "disconnected":
                return "disconnected", None
            if err == "cancelled":
                return "continue", None
            return "continue", None

        if tool_name == "log tail":
            start_tail = getattr(connection_handle, "start_tail_logs_to_file", None) if connection_handle else None
            if not start_tail or not callable(start_tail):
                show_error("Connect to the camera first (ADB, SSH, or UART) to use log tail.")
                return "continue", None
            if _tail_log_path:
                show_error(
                    "A log tail or log parse session is already running. Use log tail stop or log parse stop first."
                )
                return "continue", None
            try:
                log_dir = os.path.join(os.getcwd(), "arlo_logs")
                os.makedirs(log_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_path = os.path.join(log_dir, f"system_log_{stamp}.log")
                result = start_tail(log_path)
                if not isinstance(result, (tuple, list)) or len(result) != 2:
                    show_error("Failed to start log tail (unexpected response).")
                    return "continue", None
                ok, err = result[0], result[1]
                if not ok:
                    show_error(err or "Failed to start log tail.")
                    return "continue", None
                _tail_log_path = log_path
                _parse_logs_mode = False
                if _tail_live_view_start:
                    _tail_live_view_start(log_path, "Tail logs")
                else:
                    _spawn_tail_viewer_terminal(log_path)
                hint = (
                    "Live view: in-app tab."
                    if _tail_live_view_start
                    else "Live view: external terminal."
                )
                return "continue", (
                    f"Log is being written to [bold]{log_path}[/]. {hint} "
                    "Use [bold]log tail stop[/] to stop and save."
                )
            except OSError as e:
                show_error(str(e))
                return "continue", None
            except Exception as e:
                show_error(f"log tail failed: {e}")
                return "continue", None

        if tool_name == "log tail stop":
            stop_tail = getattr(connection_handle, "stop_tail_logs", None) if connection_handle else None
            if stop_tail and callable(stop_tail):
                stop_tail()
            path = _tail_log_path
            _tail_log_path = None
            _parse_logs_mode = False
            if path and _tail_live_view_stop:
                _tail_live_view_stop(path)
            if path:
                return "continue", f"Stopped. Logs saved to [bold]{path}[/]"
            return "continue", "No log tail session was running."

        if tool_name == "log parse":
            start_tail = getattr(connection_handle, "start_tail_logs_to_file", None) if connection_handle else None
            if not start_tail or not callable(start_tail):
                show_error("Connect to the camera first (ADB, SSH, or UART) to use log parse.")
                return "continue", None
            if _tail_log_path:
                show_error(
                    "A log tail or log parse session is already running. Use log tail stop or log parse stop first."
                )
                return "continue", None
            try:
                log_dir = os.path.join(os.getcwd(), "arlo_logs")
                os.makedirs(log_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                log_path = os.path.join(log_dir, f"system_log_parse_{stamp}.log")
                _parsed_entries = []

                def on_line(line: str) -> None:
                    with _parsed_entries_lock:
                        _parsed_entries.append(parse_line(line))

                result = start_tail(log_path, line_callback=on_line)
                if not isinstance(result, (tuple, list)) or len(result) != 2:
                    show_error("Failed to start log parse (unexpected response).")
                    return "continue", None
                ok, err = result[0], result[1]
                if not ok:
                    show_error(err or "Failed to start log parse.")
                    return "continue", None
                _tail_log_path = log_path
                _parse_logs_mode = True
                if _tail_live_view_start:
                    _tail_live_view_start(log_path, "Parse logs (live)")
                else:
                    _spawn_tail_viewer_terminal(log_path, title="Parse logs - live view")
                hint = (
                    "Live view: in-app tab."
                    if _tail_live_view_start
                    else "Live view: external terminal."
                )
                return "continue", (
                    f"Parsing logs. {hint} Use [bold]log parse stop[/] to stop and generate HTML report."
                )
            except OSError as e:
                show_error(str(e))
                return "continue", None
            except Exception as e:
                show_error(f"log parse failed: {e}")
                return "continue", None

        if tool_name == "log parse stop":
            if not _tail_log_path or not _parse_logs_mode:
                return "continue", "No log parse session was running."
            path_for_ui = _tail_log_path
            stop_tail = getattr(connection_handle, "stop_tail_logs", None) if connection_handle else None
            if stop_tail and callable(stop_tail):
                stop_tail()
            report_path = ""
            try:
                log_dir = os.path.join(os.getcwd(), "arlo_logs")
                os.makedirs(log_dir, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                report_path = os.path.join(log_dir, f"parsed_{stamp}.html")
                with _parsed_entries_lock:
                    entries_snapshot = list(_parsed_entries)
                write_html(entries_snapshot, report_path, title="Log parse report")
            except Exception as e:
                show_error(f"Failed to write HTML report: {e}")
            finally:
                _tail_log_path = None
                _parse_logs_mode = False
                with _parsed_entries_lock:
                    _parsed_entries.clear()
                if path_for_ui and _tail_live_view_stop:
                    _tail_live_view_stop(path_for_ui)
            if report_path:
                return "continue", f"Stopped. Report saved to [bold]{report_path}[/]"
            return "continue", "Stopped. (Report could not be saved.)"

        if tool_name == "log export":
            if connection_type.upper() != "UART":
                show_error("log export is only available over UART.", "Connect via UART and try again.")
                return "continue", None
            if not connection_execute:
                show_error("Not connected. Connect via UART first.")
                return "continue", None
            ok, out = connection_execute("kvcmd get KV_BS_CLAIMED", [])
            raw = (out or "").strip()
            show_info(f"kvcmd get KV_BS_CLAIMED → {repr(raw) if raw else '(empty)'}")
            if not ok:
                if out and "Device disconnected" in (out or ""):
                    return "disconnected", None
                show_error("Could not read KV_BS_CLAIMED.", out or "Command failed.")
                return "continue", None
            if not _is_kv_bs_claimed_one(out or ""):
                show_error(
                    "Camera is not onboarded.",
                    f"KV_BS_CLAIMED must be 1. kvcmd returned: {repr(raw)}. Onboard the camera first, then run log export again.",
                )
                return "continue", None
            ok, out = connection_execute("arlocmd device_info", [])
            if not ok:
                if out and "Device disconnected" in (out or ""):
                    return "disconnected", None
                show_error("Camera did not respond (device_info). Ensure the device is online and running.", out or "")
                return "continue", None
            tar_cmd = "tar -czvf /tmp/allsystem.logs.tar.gz /userdata/logs /tmp/logs/system-log_V1_0"
            ok, out = connection_execute(tar_cmd, [])
            if not ok:
                if out and "Device disconnected" in (out or ""):
                    return "disconnected", None
                show_error("Tar failed.", out or "Check that paths exist on device.")
                return "continue", None
            ip = prompt_line(
                "Is the TFTP server running? Is the camera connected to the same network as the TFTP server? "
                "Enter the TFTP server's local IPv4 (e.g. 192.168.1.100), or leave empty to cancel:",
                default="",
            ).strip()
            if not ip:
                return "continue", "Export cancelled (no TFTP address)."
            valid, err = validate_ipv4(ip)
            if not valid:
                show_error(err)
                return "continue", None
            if not prompt_confirm_proceed(f"Upload /tmp/allsystem.logs.tar.gz to {ip} via TFTP? (y/n):"):
                return "continue", "Upload cancelled."
            tftp_cmd = f"tftp -p -l /tmp/allsystem.logs.tar.gz {ip}"
            ok, out = connection_execute(tftp_cmd, [])
            if not ok:
                if out and "Device disconnected" in (out or ""):
                    return "disconnected", None
                show_error("TFTP upload failed.", out or "Check network and TFTP server.")
                return "continue", None
            return "continue", f"TFTP upload completed. File sent to {ip} as allsystem.logs.tar.gz"

        show_error(f"Unhandled tool command {tool_name!r}.")
        return "continue", None

    abstract_hit = _match_abstract_prefix(parts)
    if abstract_hit is not None:
        abstract_name, abstract_args = abstract_hit

        def _execute_shell(shell_line: str) -> tuple[bool, str]:
            if not connection_execute:
                return False, "Not connected."
            return connection_execute(shell_line, [])

        try:
            abstract_out = execute_abstract_command(
                abstract_name,
                abstract_args,
                ABSTRACT_DEFINITIONS,
                device_commands,
                _execute_shell,
                connection_type,
                connection_execute=connection_execute,
                model=model,
            )
        except ValueError as e:
            show_error(str(e))
            return "continue", None
        except RuntimeError as e:
            err_text = str(e)
            if "Device disconnected" in err_text:
                return "disconnected", None
            show_error(err_text)
            return "continue", None
        if abstract_out is not None:
            combined = "\n".join(s for s in abstract_out if (s or "").strip())
            return "continue", combined or None

        if abstract_name.strip().lower() == "push arlod":
            return _run_push_arlod(connection_handle, connection_type, abstract_args)

    cmd = (parts[0] or "").lower()
    args = parts[1:] if len(parts) > 1 else []

    system_cmds = get_system_commands_for_profile(profile)
    all_cmds = device_commands + system_cmds
    cmd_names = [c["name"].lower() for c in all_cmds]

    if cmd in ("help", "?", "--help"):
        show_abstract_commands_section(_abstract_help_lines(ABSTRACT_DEFINITIONS))
        _, advanced_device = get_visible_commands(device_commands)
        full = list(advanced_device) + [
            {"name": c["name"], "description": c["description"]} for c in system_cmds
        ]
        show_commands_table(
            full,
            include_system=True,
            device_profile=profile,
            section_heading="Advanced",
        )
        return "continue", None

    if cmd == "status":
        ob = model.get("is_onboarded")
        is_ob = ob if isinstance(ob, bool) else None
        show_connection_status(
            connection_type, device_identifier, model_name, connected_at, is_onboarded=is_ob
        )
        return "continue", None

    if cmd in ("disconnect", "exit", "quit", "x"):
        return "exit", None

    if cmd == "back":
        return "back", None

    if cmd == "config_show":
        from core.config_commands import run_config_show
        run_config_show()
        return "continue", None

    if cmd == "config_update":
        from core.config_commands import run_config_update
        run_config_update()
        return "continue", None

    if cmd == "config_delete":
        from core.config_commands import run_config_delete
        run_config_delete()
        return "continue", None

    _fw_wizard_result = try_handle_fw_wizard_command(cmd, connection_execute, model)
    if _fw_wizard_result is not None:
        return _fw_wizard_result

    if cmd == "parse_log_file":
        log_dir = os.path.join(os.getcwd(), "arlo_logs")
        if not os.path.isdir(log_dir):
            show_error("arlo_logs folder not found.", "Run log tail or log parse first to create log files, or create arlo_logs manually.")
            return "continue", None
        files_in_dir = [f for f in os.listdir(log_dir) if os.path.isfile(os.path.join(log_dir, f)) and not f.startswith(".")]
        if not files_in_dir:
            show_error("No log files in arlo_logs.", "Run log tail or log parse to capture logs first.")
            return "continue", None
        selected = prompt_select_log_file(log_dir)
        if selected is None:
            return "continue", None
        try:
            with open(selected, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            show_error(f"Cannot read file: {e}")
            return "continue", None
        entries = [parse_line(ln) for ln in lines]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.splitext(os.path.basename(selected))[0]
        report_path = os.path.join(log_dir, f"parsed_{stamp}_{base}.html")
        try:
            write_html(entries, report_path, title=f"Log parse: {os.path.basename(selected)}")
        except OSError as e:
            show_error(f"Cannot write report: {e}")
            return "continue", None
        return "continue", f"Report saved to [bold]{report_path}[/] ([dim]{len(entries)} lines[/])"

    if cmd not in cmd_names:
        similar = _similar_commands(cmd, all_cmds)
        msg = f"Unknown command '{cmd}'."
        if similar:
            msg += f" Did you mean: {', '.join(similar)}?"
        show_error(msg, "Type 'help' for available commands.")
        return "continue", None

    # Device command: use "shell" for E3 Wired (full cli command), else command name + args
    for c in device_commands:
        if c["name"].lower() == cmd:
            # pull_logs: download log archive from camera to PC (no shell command)
            if cmd == "pull_logs":
                if connection_type.upper() == "UART":
                    show_error("pull_logs is not supported over UART.", "Connect via ADB to download the log archive.")
                    return "continue", None
                if connection_pull_file:
                    local_dir = pull_logs_local_dir or os.getcwd()
                    local_path = os.path.join(local_dir, "allsystem.logs.tar")
                    if args:
                        local_path = args[0]
                    success, output = connection_pull_file(PULL_LOGS_REMOTE_PATH, local_path)
                    if success:
                        return "continue", output
                    if output and output.strip() == "Device disconnected.":
                        return "disconnected", None
                    show_error(output or "Pull failed.")
                    return "continue", None
                show_error("Pull not available for this connection.", "Use ADB or SSH.")
                return "continue", None
            if connection_execute:
                base_cmd = c.get("shell") or cmd
                success, output = connection_execute(base_cmd, args)
                if success:
                    return "continue", output or f"Command '{cmd}' executed successfully."
                # Camera disconnected from PC (USB unplugged or network lost)
                if output and output.strip() == "Device disconnected.":
                    return "disconnected", None
                show_error(output or "Command failed.")
                return "continue", None
            # Placeholder: no real device, just echo success
            if cmd == "capture":
                return "continue", "Image captured successfully (placeholder). Saved to: /tmp/capture_placeholder.jpg"
            if cmd == "record":
                return "continue", "Recording started (placeholder)."
            return "continue", f"Command '{cmd}' executed (placeholder)."

    return "continue", None
