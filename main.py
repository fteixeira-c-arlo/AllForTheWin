"""
Arlo Camera Control Terminal - Entry point.
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

# Bootstrap: if dependencies are missing, install and re-run (once)
if os.environ.get("ARLO_SKIP_BOOTSTRAP") != "1":
    try:
        import rich  # noqa: F401
    except ImportError:
        _script_dir = os.path.dirname(os.path.abspath(__file__))
        _req = os.path.join(_script_dir, "requirements.txt")
        if os.path.isfile(_req):
            print("Installing dependencies (first run)...")
            r = subprocess.call([sys.executable, "-m", "pip", "install", "-r", _req], cwd=_script_dir)
            if r != 0:
                print("Failed to install dependencies. Run: pip install -r requirements.txt")
                sys.exit(1)
            os.environ["ARLO_SKIP_BOOTSTRAP"] = "1"
            os.execv(sys.executable, [sys.executable, __file__] + sys.argv[1:])
        raise

# Use UTF-8 for console output on Windows so Rich can render check/cross symbols
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from typing import Any

from rich.console import Console

from commands.command_definitions import load_commands_from_confluence
from commands.command_parser import parse_and_execute, get_system_commands
from connections.adb_handler import ADBHandler
from connections.ssh_handler import SSHHandler
from connections.uart_handler import UARTHandler, list_uart_ports
from models.camera_models import get_models, get_model_by_name
from models.connection_config import ConnectionConfig
from ui.menus import (
    show_welcome,
    show_models_table,
    show_models_section,
    show_disconnected_help,
    show_connection_methods,
    show_commands_table,
    show_success,
    show_error,
)
from ui.prompts import prompt_connection_method, prompt_adb_params, prompt_ssh_params, prompt_uart_params, prompt_select_model

console = Console()


def run_connection_flow(current_model: dict[str, Any]) -> tuple[ConnectionConfig | None, Any, str]:
    """
    Prompt for connection method and params, then connect.
    Returns (ConnectionConfig, connection_handle, reason).
    reason: "ok" on success, "back" if user chose back, "failed" on connection failure.
    """
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
        defaults = current_model.get("default_settings", {}).get("uart", {})
        params = prompt_uart_params(default_baud=defaults.get("baud_rate", 115200))
        if params is None:
            return None, None, "back"
        with console.status("[bold cyan]Connecting via UART...", spinner="dots"):
            handler = UARTHandler()
            ok, msg, settings = handler.connect(port=params["port"], baud_rate=params["baud_rate"])
        if ok and settings:
            cfg = ConnectionConfig(
                type="UART",
                settings=settings,
                status="connected",
                connected_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                device_identifier=handler.device_identifier() or f"{params['port']}@{params['baud_rate']}",
            )
            show_success(f"Successfully connected to {current_model['name']} via UART ({cfg.device_identifier})")
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
            cfg = ConnectionConfig(
                type="ADB",
                settings=settings,
                status="connected",
                connected_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                device_identifier=device_id,
            )
            show_success(f"Successfully connected to {current_model['name']} via USB ({device_id})")
            console.print(f"[dim]Connection established at {cfg.connected_at}[/]")
            return cfg, handler, "ok"
        show_error(msg, "Connect camera via USB, ensure ADB is enabled, or try 'back' to choose another method.")
        return None, None, "failed"

    if method == "SSH":
        defaults = current_model.get("default_settings", {}).get("ssh", {})
        params = prompt_ssh_params(default_port=defaults.get("port", 22))
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
            cfg = ConnectionConfig(
                type="SSH",
                settings=settings,
                status="connected",
                connected_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                device_identifier=f"{settings['ip_address']}:{settings['port']}",
            )
            show_success(f"Successfully connected to {current_model['name']} at {cfg.device_identifier}")
            console.print(f"[dim]Connection established at {cfg.connected_at}[/]")
            return cfg, handler, "ok"
        show_error(msg, "Check credentials and network, or try 'back' to choose another method.")
        return None, None, "failed"

    return None, None, "back"


def main() -> None:
    models = get_models()
    show_welcome()

    current_model: dict[str, Any] | None = None
    connection_config: ConnectionConfig | None = None
    connection_handle: Any = None  # ADBHandler or SSHHandler
    device_commands: list[dict] = []

    while True:
        if connection_config and connection_handle:
            # Connected: command loop
            prompt = f"{current_model['name']}> "
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Goodbye![/]")
                break
            if not line.strip():
                continue
            action, message = parse_and_execute(
                line,
                current_model,
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
                current_model = None
                device_commands = []
                console.print("[dim]Disconnected. Returned to main menu.[/]\n")
                continue
            if action == "disconnected":
                if connection_handle:
                    connection_handle.disconnect()
                connection_config = None
                connection_handle = None
                model_name = (current_model or {}).get("name", "Camera")
                current_model = None
                device_commands = []
                show_error(f"{model_name} disconnected from the PC.", "Returned to device list.")
                console.print()
                show_models_table(models)
                continue
            continue

        # Not connected: command prompt (models | exit)
        show_disconnected_help()
        try:
            line = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye![/]")
            break

        if line in ("exit", "quit", "q", "x"):
            console.print("[dim]Goodbye![/]")
            break

        if line in ("models", "m", "select", "s"):
            show_models_section(models)
            try:
                current_model = prompt_select_model(models)
            except (EOFError, KeyboardInterrupt):
                continue
            if not current_model or not isinstance(current_model, dict) or "name" not in current_model:
                continue

            show_success(f"Selected: {current_model['name']} ({current_model['display_name']})")
            # Run connection flow (with retry loop on failure only)
            while True:
                cfg, handle, reason = run_connection_flow(current_model)
                if reason == "back":
                    current_model = None
                    break
                if cfg is not None and handle is not None:
                    connection_config = cfg
                    connection_handle = handle
                    device_commands = load_commands_from_confluence(current_model["name"])
                    full = device_commands + [
                        {"name": c["name"], "description": c["description"]}
                        for c in get_system_commands()
                    ]
                    show_commands_table(full, include_system=True)
                    break
                # Connection failed - offer retry
                retry = input("Retry connection? [y/N]: ").strip().lower()
                if retry not in ("y", "yes"):
                    current_model = None
                    break
            continue

        if line:
            show_error("Unknown command.", "Type 's' to start device selection, 'x' to close.")
        continue


if __name__ == "__main__":
    main()
