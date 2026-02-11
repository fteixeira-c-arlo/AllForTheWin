"""Arlo Camera Control Terminal - Entry point."""
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

# Bootstrap: install deps on first run if needed
if os.environ.get("ARLO_SKIP_BOOTSTRAP") != "1":
    try:
        import rich  # noqa: F401
    except ImportError:
        req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        if os.path.isfile(req):
            subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req])
            os.environ["ARLO_SKIP_BOOTSTRAP"] = "1"
            os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])
        raise

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from rich.console import Console

from commands.build_info import detect_device
from commands.command_definitions import load_commands_from_confluence
from commands.command_parser import parse_and_execute, get_system_commands
from connections.adb_handler import ADBHandler
from connections.ssh_handler import SSHHandler
from connections.uart_handler import UARTHandler, list_uart_ports
from models.connection_config import ConnectionConfig
from ui.menus import (
    show_welcome,
    show_disconnected_help,
    show_commands_table,
    show_connected_device_banner,
    show_connection_methods,
    show_success,
    show_error,
)
from ui.prompts import (
    prompt_connection_method,
    prompt_adb_params,
    prompt_ssh_params,
    prompt_uart_params,
)

# Default connection settings when no model is selected (UART baud, SSH port, etc.)
DEFAULT_UART_BAUD = 115200
DEFAULT_SSH_PORT = 22

console = Console()


def _make_config(conn_type: str, settings: dict, device_id: str) -> ConnectionConfig:
    return ConnectionConfig(
        type=conn_type,
        settings=settings,
        status="connected",
        connected_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        device_identifier=device_id,
    )


def run_connection_flow() -> tuple[ConnectionConfig | None, Any, str]:
    """Prompt for connection method/params and connect. Returns (config, handle, reason): "ok"|"back"|"failed"."""
    method = prompt_connection_method()
    if method is None:
        return None, None, "back"

    if method == "UART":
        ports = list_uart_ports()
        if not ports:
            try:
                import serial  # noqa: F401
                show_error(
                    "No serial ports detected.",
                    "Connect the UART adapter, ensure drivers are installed, and try again (or run as administrator).",
                )
            except ImportError:
                show_error(
                    "pyserial not installed.",
                    "Install with: pip install pyserial",
                )
            return None, None, "failed"
        params = prompt_uart_params(default_baud=DEFAULT_UART_BAUD)
        if params is None:
            return None, None, "back"
        with console.status("[bold cyan]Connecting via UART...", spinner="dots"):
            handler = UARTHandler()
            ok, msg, settings = handler.connect(port=params["port"], baud_rate=params["baud_rate"])
        if ok and settings:
            cfg = _make_config("UART", settings, handler.device_identifier() or f"{params['port']}@{params['baud_rate']}")
            show_success(f"Connected via UART ({cfg.device_identifier})")
            console.print(f"[dim]Connection established at {cfg.connected_at}[/]")
            return cfg, handler, "ok"
        if "access denied" in (msg or "").lower() or "permission" in (msg or "").lower():
            show_error(msg)
        else:
            show_error(msg, "Check cable and port, or try 'back' to choose another method.")
        return None, None, "failed"

    if method == "ADB":
        params = prompt_adb_params()
        if params is None:
            return None, None, "back"
        with console.status("[bold cyan]Connecting via ADB (USB, shell auth)...", spinner="dots"):
            handler = ADBHandler()
            ok, msg, settings = handler.connect(password=params["password"])
        if ok and settings:
            device_id = settings.get("device_serial") or "USB"
            cfg = _make_config("ADB", settings, device_id)
            show_success(f"Connected via USB ({device_id})")
            console.print(f"[dim]Connection established at {cfg.connected_at}[/]")
            return cfg, handler, "ok"
        show_error(msg, "Connect camera via USB, ensure ADB is enabled, or try 'back' to choose another method.")
        return None, None, "failed"

    if method == "SSH":
        params = prompt_ssh_params(default_port=DEFAULT_SSH_PORT)
        if params is None:
            return None, None, "back"
        with console.status("[bold cyan]Connecting via SSH...", spinner="dots"):
            handler = SSHHandler()
            ok, msg, settings = handler.connect(
                ip_address=params["ip_address"],
                port=params["port"],
                username=params["username"],
                password=params.get("password", ""),
            )
        if ok and settings:
            device_id = f"{settings['ip_address']}:{settings['port']}"
            cfg = _make_config("SSH", settings, device_id)
            show_success(f"Connected at {device_id}")
            console.print(f"[dim]Connection established at {cfg.connected_at}[/]")
            return cfg, handler, "ok"
        show_error(msg, "Check credentials and network, or try 'back' to choose another method.")
        return None, None, "failed"

    return None, None, "back"


def main() -> None:
    show_welcome()

    connection_config: ConnectionConfig | None = None
    connection_handle: Any = None
    device_commands: list[dict] = []
    # Detected from build_info + kvcmd/update_url after connect (model, fw_version, env)
    detected_device: dict[str, Any] = {}

    while True:
        if connection_config and connection_handle:
            # Connected: command loop; prompt uses detected model or "Device"
            prompt_name = (detected_device.get("model") or "Device").strip() or "Device"
            prompt = f"{prompt_name}> "
            # Minimal model dict for parse_and_execute (status, fw_setup, etc.)
            current_model_dict = {
                "name": prompt_name,
                "fw_search_models": [prompt_name] if detected_device.get("model") else [],
            }
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/]")
                break
            if not line.strip():
                continue
            action, message = parse_and_execute(
                line,
                current_model_dict,
                connection_config.type,
                connection_config.device_identifier or "",
                connection_config.connected_at,
                device_commands,
                connection_handle.execute if connection_handle else None,
                connection_pull_file=getattr(connection_handle, "pull_file", None),
                pull_logs_local_dir=os.getcwd(),
                connection_get_tail_logs_command=getattr(connection_handle, "get_tail_logs_command", None) if connection_handle else None,
                connection_handle=connection_handle,
            )
            if message:
                console.print(f"[green]{message}[/]")
            if action == "exit":
                if connection_handle:
                    connection_handle.disconnect()
                console.print("[dim]Disconnecting...[/]")
                show_success("Connection closed")
                console.print("\n[dim]Goodbye![/]")
                break
            if action == "back":
                if connection_handle:
                    connection_handle.disconnect()
                connection_config = None
                connection_handle = None
                detected_device = {}
                device_commands = []
                console.print("[dim]Disconnected. Returned to main menu.[/]\n")
                continue
            if action == "disconnected":
                if connection_handle:
                    connection_handle.disconnect()
                connection_config = None
                connection_handle = None
                model_name = detected_device.get("model") or "Camera"
                detected_device = {}
                device_commands = []
                show_error(f"{model_name} disconnected from the PC.", "Returning to connection page.")
                console.print()
                # Send user back to connection page (connection method selection)
                show_connection_methods()
                while True:
                    cfg, handle, reason = run_connection_flow()
                    if reason == "back":
                        break
                    if cfg is not None and handle is not None:
                        connection_config = cfg
                        connection_handle = handle
                        with console.status("[bold cyan]Detecting device (build_info + kvcmd)...", spinner="dots"):
                            detected_device = detect_device(handle.execute)
                        model_for_commands = detected_device.get("model") or "Device"
                        device_commands = load_commands_from_confluence(model_for_commands)
                        full = device_commands + [
                            {"name": c["name"], "description": c["description"]}
                            for c in get_system_commands()
                        ]
                        show_connected_device_banner(
                            detected_device.get("model"),
                            detected_device.get("fw_version"),
                            detected_device.get("env"),
                            cfg.type,
                            cfg.device_identifier or "",
                            commands=full,
                            include_system_commands=True,
                        )
                        break
                    retry = input("Retry connection? [y/N]: ").strip().lower()
                    if retry not in ("y", "yes"):
                        break
                continue
            continue

        show_disconnected_help()
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/]")
            break

        if line in ("exit", "quit", "q", "x"):
            console.print("[dim]Goodbye![/]")
            break

        if line in ("connect", "c", "select", "s"):
            while True:
                cfg, handle, reason = run_connection_flow()
                if reason == "back":
                    break
                if cfg is not None and handle is not None:
                    connection_config = cfg
                    connection_handle = handle
                    # Auto-detect model, FW, env from build_info + kvcmd/update_url
                    with console.status("[bold cyan]Detecting device (build_info + kvcmd)...", spinner="dots"):
                        detected_device = detect_device(handle.execute)
                    # Load commands by detected model (E3 Wired list if model known, else placeholder)
                    model_for_commands = detected_device.get("model") or "Device"
                    device_commands = load_commands_from_confluence(model_for_commands)
                    full = device_commands + [
                        {"name": c["name"], "description": c["description"]}
                        for c in get_system_commands()
                    ]
                    show_connected_device_banner(
                        detected_device.get("model"),
                        detected_device.get("fw_version"),
                        detected_device.get("env"),
                        cfg.type,
                        cfg.device_identifier or "",
                        commands=full,
                        include_system_commands=True,
                    )
                    break
                retry = input("Retry connection? [y/N]: ").strip().lower()
                if retry not in ("y", "yes"):
                    break
            continue

        if line:
            show_error("Unknown command.", "Type 's' to connect, 'x' to close.")


if __name__ == "__main__":
    main()
