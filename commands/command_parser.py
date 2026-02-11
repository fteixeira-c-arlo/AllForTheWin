"""Parse and execute user commands."""
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any, Callable

from commands.command_definitions import load_commands_from_confluence
from commands.log_parser import parse_line, write_html
from ui.menus import show_commands_table, show_connection_status, show_error, show_info
from ui.prompts import prompt_confirm_proceed, prompt_line, prompt_select_log_file
from utils.validators import validate_ipv4

# Default remote path for log archive on camera (BusyBox tar creates uncompressed .tar)
PULL_LOGS_REMOTE_PATH = "/tmp/allsystem.logs.tar"

# Action result: "continue" | "disconnect" | "exit" | "back"
SYSTEM_COMMANDS = [
    {"name": "help", "description": "Show available commands"},
    {"name": "status", "description": "Show connection status"},
    {"name": "stop_server", "description": "Stop the local firmware server"},
    {"name": "server_status", "description": "Check local firmware server status"},
    {"name": "update_url", "description": "Set or show FOTA update URL (update_url [url])"},
    {"name": "use_local_fw", "description": "Start local FW server (if needed) and set camera update_url to it"},
    {"name": "config_show", "description": "Show saved Artifactory credentials (no token)"},
    {"name": "config_update", "description": "Update saved Artifactory credentials"},
    {"name": "config_delete", "description": "Delete saved Artifactory credentials"},
    {"name": "tail_logs", "description": "Stream system log to a file and open in editor (tail_logs_stop to stop and save)"},
    {"name": "tail_logs_stop", "description": "Stop log streaming; logs are saved to the file"},
    {"name": "parse_logs", "description": "Stream and parse logs; use parse_logs_stop to stop and generate HTML report"},
    {"name": "parse_logs_stop", "description": "Stop log parsing and save HTML report"},
    {"name": "parse_log_file", "description": "Select a log file from arlo_logs folder and generate HTML parse report"},
    {"name": "export_logs_tftp", "description": "(UART only) Tar logs, then upload via TFTP; requires onboarded camera and TFTP server on same network"},
    {"name": "disconnect", "description": "Close connection and exit"},
    {"name": "exit", "description": "Close connection and exit"},
    {"name": "back", "description": "Disconnect and return to main menu"},
]


# Path of the log file when tail_logs or parse_logs is running
_tail_log_path: str | None = None
# True when current tail session is parse_logs (so stop generates HTML)
_parse_logs_mode: bool = False
# Accumulated parsed entries for parse_logs_stop (append from reader thread)
_parsed_entries: list[dict] = []
_parsed_entries_lock: threading.Lock = threading.Lock()


def _spawn_tail_viewer_terminal(log_path: str, title: str | None = None) -> None:
    """Open a new terminal window that follows the log file (tail -f style). Log is still saved to file when tail_logs_stop/parse_logs_stop is used."""
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


def get_system_commands() -> list[dict]:
    """Return list of system command definitions for display."""
    return SYSTEM_COMMANDS.copy()


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
    parts = line.split()
    cmd = (parts[0] or "").lower()
    args = parts[1:] if len(parts) > 1 else []
    # Aliases for fw_setup (automated flow)
    if cmd in ("upd_url", "fw_url"):
        cmd = "fw_setup"

    all_cmds = device_commands + SYSTEM_COMMANDS
    cmd_names = [c["name"].lower() for c in all_cmds]

    if cmd in ("help", "?"):
        full = list(device_commands) + [
            {"name": c["name"], "description": c["description"]} for c in SYSTEM_COMMANDS
        ]
        show_commands_table(full, include_system=True)
        return "continue", None

    if cmd == "status":
        show_connection_status(
            connection_type, device_identifier, model_name, connected_at
        )
        return "continue", None

    if cmd in ("disconnect", "exit", "quit", "x"):
        return "exit", None

    if cmd == "back":
        return "back", None

    if cmd == "stop_server":
        from commands.update_url_flow import run_stop_server
        return "continue", run_stop_server()

    if cmd == "server_status":
        from commands.update_url_flow import run_server_status
        return "continue", run_server_status()

    if cmd == "use_local_fw":
        if not connection_execute:
            show_error("Connect to the camera first to use use_local_fw.")
            return "continue", None
        try:
            from commands.update_url_flow import run_use_local_fw_server
            err = run_use_local_fw_server(connection_execute)
        except (KeyboardInterrupt, EOFError):
            return "continue", None
        except Exception as e:
            show_error("use_local_fw failed.", str(e))
            return "continue", None
        if err is None:
            return "continue", None
        if err == "disconnected":
            return "disconnected", None
        if err == "cancelled":
            return "continue", None
        return "continue", None

    if cmd == "config_show":
        from commands.config_commands import run_config_show
        run_config_show()
        return "continue", None

    if cmd == "config_update":
        from commands.config_commands import run_config_update
        run_config_update()
        return "continue", None

    if cmd == "config_delete":
        from commands.config_commands import run_config_delete
        run_config_delete()
        return "continue", None

    if cmd == "tail_logs":
        start_tail = getattr(connection_handle, "start_tail_logs_to_file", None) if connection_handle else None
        if not start_tail or not callable(start_tail):
            show_error("Connect to the camera first (ADB, SSH, or UART) to use tail_logs.")
            return "continue", None
        if _tail_log_path:
            show_error(
                "A tail or parse_logs session is already running. Use tail_logs_stop or parse_logs_stop first."
            )
            return "continue", None
        try:
            log_dir = os.path.join(os.getcwd(), "arlo_logs")
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = os.path.join(log_dir, f"system_log_{stamp}.log")
            result = start_tail(log_path)
            if not isinstance(result, (tuple, list)) or len(result) != 2:
                show_error("Failed to start tail_logs (unexpected response).")
                return "continue", None
            ok, err = result[0], result[1]
            if not ok:
                show_error(err or "Failed to start tail_logs.")
                return "continue", None
            _tail_log_path = log_path
            _parse_logs_mode = False
            _spawn_tail_viewer_terminal(log_path)
            return "continue", (
                f"Log is being written to [bold]{log_path}[/]. "
                "Use [bold]tail_logs_stop[/] to stop and save."
            )
        except OSError as e:
            show_error(str(e))
            return "continue", None
        except Exception as e:
            show_error(f"tail_logs failed: {e}")
            return "continue", None

    if cmd == "tail_logs_stop":
        stop_tail = getattr(connection_handle, "stop_tail_logs", None) if connection_handle else None
        if stop_tail and callable(stop_tail):
            stop_tail()
        path = _tail_log_path
        _tail_log_path = None
        _parse_logs_mode = False
        if path:
            return "continue", f"Stopped. Logs saved to [bold]{path}[/]"
        return "continue", "No tail_logs session was running."

    if cmd == "parse_logs":
        start_tail = getattr(connection_handle, "start_tail_logs_to_file", None) if connection_handle else None
        if not start_tail or not callable(start_tail):
            show_error("Connect to the camera first (ADB, SSH, or UART) to use parse_logs.")
            return "continue", None
        if _tail_log_path:
            show_error(
                "A tail or parse_logs session is already running. Use tail_logs_stop or parse_logs_stop first."
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
                show_error("Failed to start parse_logs (unexpected response).")
                return "continue", None
            ok, err = result[0], result[1]
            if not ok:
                show_error(err or "Failed to start parse_logs.")
                return "continue", None
            _tail_log_path = log_path
            _parse_logs_mode = True
            _spawn_tail_viewer_terminal(log_path, title="Parse logs - live view")
            return "continue", (
                "Parsing logs. Live view opened in another terminal. Use [bold]parse_logs_stop[/] to stop and generate HTML report."
            )
        except OSError as e:
            show_error(str(e))
            return "continue", None
        except Exception as e:
            show_error(f"parse_logs failed: {e}")
            return "continue", None

    if cmd == "parse_logs_stop":
        if not _tail_log_path or not _parse_logs_mode:
            return "continue", "No parse_logs session was running."
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
        if report_path:
            return "continue", f"Stopped. Report saved to [bold]{report_path}[/]"
        return "continue", "Stopped. (Report could not be saved.)"

    if cmd == "parse_log_file":
        log_dir = os.path.join(os.getcwd(), "arlo_logs")
        if not os.path.isdir(log_dir):
            show_error("arlo_logs folder not found.", "Run tail_logs or parse_logs first to create log files, or create arlo_logs manually.")
            return "continue", None
        files_in_dir = [f for f in os.listdir(log_dir) if os.path.isfile(os.path.join(log_dir, f)) and not f.startswith(".")]
        if not files_in_dir:
            show_error("No log files in arlo_logs.", "Run tail_logs or parse_logs to capture logs first.")
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

    # export_logs_tftp: UART only — check onboarded + online, tar logs, then tftp upload
    if cmd == "export_logs_tftp":
        if connection_type.upper() != "UART":
            show_error("export_logs_tftp is only available over UART.", "Connect via UART and try again.")
            return "continue", None
        if not connection_execute:
            show_error("Not connected. Connect via UART first.")
            return "continue", None
        # 1) Check KV_BS_CLAIMED == 1 (onboarded) — run kvcmd and show what was read
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
                f"KV_BS_CLAIMED must be 1. kvcmd returned: {repr(raw)}. Onboard the camera first, then run export_logs_tftp again.",
            )
            return "continue", None
        # 2) Check camera is online and running (quick command)
        ok, out = connection_execute("arlocmd device_info", [])
        if not ok:
            if out and "Device disconnected" in (out or ""):
                return "disconnected", None
            show_error("Camera did not respond (device_info). Ensure the device is online and running.", out or "")
            return "continue", None
        # 3) Tar logs (creates .tar.gz); execute() waits for shell prompt
        tar_cmd = "tar -czvf /tmp/allsystem.logs.tar.gz /userdata/logs /tmp/logs/system-log_V1_0"
        ok, out = connection_execute(tar_cmd, [])
        if not ok:
            if out and "Device disconnected" in (out or ""):
                return "disconnected", None
            show_error("Tar failed.", out or "Check that paths exist on device.")
            return "continue", None
        # 4) Prompt user: TFTP server running and camera on same network?
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
        # 5) Run tftp -p -l allsystem.logs.tar.gz <ip> on device (file in /tmp/)
        tftp_cmd = f"tftp -p -l /tmp/allsystem.logs.tar.gz {ip}"
        ok, out = connection_execute(tftp_cmd, [])
        if not ok:
            if out and "Device disconnected" in (out or ""):
                return "disconnected", None
            show_error("TFTP upload failed.", out or "Check network and TFTP server.")
            return "continue", None
        return "continue", f"TFTP upload completed. File sent to {ip} as allsystem.logs.tar.gz"

    # update_url [url]: set or show camera FOTA update URL (arlocmd update_url)
    if cmd == "update_url":
        if connection_execute:
            success, output = connection_execute("arlocmd update_url", args)
            if success:
                if args:
                    return "continue", (output or f"Update URL set to {args[0]}.")
                return "continue", output or "Current update URL shown above."
            if output and output.strip() == "Device disconnected.":
                return "disconnected", None
            show_error(output or "Command failed.")
            return "continue", None
        show_error("Connect to the camera first to use update_url.")
        return "continue", None

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
            # fw_setup: automated flow (Artifactory, local server, send update_url to camera)
            if cmd == "fw_setup" and connection_execute:
                from commands.update_url_flow import run_update_url_flow
                err = run_update_url_flow(connection_execute, model)
                if err is None:
                    return "continue", None
                if err == "disconnected":
                    return "disconnected", None
                if err == "cancelled":
                    return "continue", None
                return "continue", None
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
