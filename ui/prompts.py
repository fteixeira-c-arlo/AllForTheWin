"""User input prompts."""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import questionary
from questionary import Choice, Style

from commands.camera_models import format_supported_connections
from utils.validators import validate_ipv4, validate_port

if TYPE_CHECKING:
    from ui.gui_bridge import GuiBridge

_gui_prompt_bridge: GuiBridge | None = None


def set_gui_prompt_bridge(bridge: GuiBridge | None) -> None:
    global _gui_prompt_bridge
    _gui_prompt_bridge = bridge


def _gb() -> GuiBridge | None:
    return _gui_prompt_bridge


custom_style = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:green bold"),
])


def prompt_select_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prompt to select a camera model with arrows or type-to-search. Returns selected model dict or None to exit."""
    b = _gb()
    if b is not None:
        items = [
            (
                f"{m['name']} — {m['display_name']} ({format_supported_connections(m.get('supported_connections'))})",
                m,
            )
            for m in models
        ]
        items.append(("Exit", None))
        return b.ask_select("Select model:", items)
    choices = [
        Choice(
            title=f"{m['name']} — {m['display_name']} ({format_supported_connections(m.get('supported_connections'))})",
            value=m,
        )
        for m in models
    ] + [
        Choice(title="Exit", value=None),
    ]
    return questionary.select(
        "Select model:",
        choices=choices,
        style=custom_style,
    ).ask()


def prompt_line(prompt_text: str, default: str = "") -> str:
    """Read a single line from user (for main loop)."""
    b = _gb()
    if b is not None:
        r = b.ask_text(prompt_text, default)
        return (r if r is not None else "") or ""
    return questionary.text(prompt_text, default=default, style=custom_style).ask() or ""


def prompt_select_log_file(log_dir: str) -> str | None:
    """List log files in log_dir (e.g. arlo_logs), let user pick one. Returns full path or None to cancel."""
    if not os.path.isdir(log_dir):
        return None
    files = [
        f for f in os.listdir(log_dir)
        if os.path.isfile(os.path.join(log_dir, f)) and not f.startswith(".")
    ]
    if not files:
        return None
    # Sort by modification time, newest first
    paths = [os.path.join(log_dir, f) for f in files]
    paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    choices = [Choice(title=os.path.basename(p), value=p) for p in paths]
    choices.append(Choice(title="Cancel", value=None))
    b = _gb()
    if b is not None:
        items = [(os.path.basename(p), p) for p in paths]
        items.append(("Cancel", None))
        return b.ask_select("Select log file to parse:", items)
    return questionary.select("Select log file to parse:", choices=choices, style=custom_style).ask()


def prompt_connection_method(supported: list[str] | None = None) -> str | None:
    """
    Prompt for connection method: UART, ADB, or SSH.
    If supported is set (e.g. ['ADB','UART'] from the selected device), only those options appear.
    Returns 'UART', 'ADB', 'SSH' or None to cancel.
    """
    all_methods = [
        ("UART (serial)", "UART"),
        ("ADB (USB)", "ADB"),
        ("SSH", "SSH"),
    ]
    if supported:
        allow = {x.upper() for x in supported}
        items = [(label, val) for label, val in all_methods if val in allow]
        if not items:
            items = list(all_methods)
    else:
        items = list(all_methods)

    b = _gb()
    if b is not None:
        gui_items = list(items) + [("Back", "back")]
        sel = b.ask_select("Connection method:", gui_items)
        if sel == "back" or sel is None:
            return None
        return sel
    choices = [questionary.Choice(label, value=val) for label, val in items]
    choices.append(questionary.Choice("Back", value="back"))
    choice = questionary.select(
        "Connection method:",
        choices=choices,
        style=custom_style,
    ).ask()
    if choice == "back":
        return None
    return choice


def prompt_adb_params() -> dict | None:
    """Prompt for ADB connection: password only (USB connection, adb shell auth). Returns dict or None to cancel."""
    b = _gb()
    if b is not None:
        password = b.ask_password("ADB shell auth password:")
        if password is None:
            return None
        return {"password": password}
    password = questionary.password(
        "ADB shell auth password:",
        style=custom_style,
    ).ask()
    if password is None:
        return None
    return {"password": password}


def prompt_uart_params(default_baud: int = 115200) -> dict | None:
    """Prompt for UART: select port from list, then baud rate. Returns dict with port, baud_rate or None to cancel."""
    from connections.uart_handler import list_uart_ports

    ports = list_uart_ports()
    if not ports:
        return None
    b = _gb()
    if b is not None:
        items = [(f"{desc} ({port})", port) for port, desc in ports]
        port = b.ask_select("Serial port:", items)
        if port is None:
            return None
        baud_str = b.ask_text("Baud rate:", str(default_baud))
        if baud_str is None:
            return None
        baud_str = (baud_str or str(default_baud)).strip()
        if not baud_str.isdigit() or int(baud_str) <= 0:
            from ui.menus import show_error

            show_error("Enter a positive baud rate.")
            return None
        return {"port": port, "baud_rate": int(baud_str)}
    choices = [Choice(title=f"{desc} ({port})", value=port) for port, desc in ports]
    port = questionary.select("Serial port:", choices=choices, style=custom_style).ask()
    if port is None:
        return None
    baud_str = questionary.text(
        "Baud rate:",
        default=str(default_baud),
        validate=lambda x: (x or "").strip().isdigit() and int((x or "").strip()) > 0 or "Enter a positive number.",
        style=custom_style,
    ).ask()
    if baud_str is None:
        return None
    baud = int((baud_str or str(default_baud)).strip())
    return {"port": port, "baud_rate": baud}


def prompt_ssh_params(default_port: int = 22) -> dict | None:
    """Prompt for SSH connection parameters. Returns dict or None to cancel."""
    b = _gb()
    if b is not None:
        from ui.menus import show_error

        ip = None
        while True:
            raw = b.ask_text("IP Address:", "")
            if raw is None:
                return None
            ok, err = validate_ipv4(raw.strip())
            if ok:
                ip = raw.strip()
                break
            show_error(err or "Invalid IP address.")
        port_str = b.ask_text(f"Port [{default_port}]:", str(default_port))
        if port_str is None:
            return None
        ok_p, err_p, port = validate_port(
            (port_str or str(default_port)).strip() or str(default_port), default_port
        )
        if not ok_p:
            show_error(err_p or "Invalid port.")
            return None
        username = b.ask_text("Username:", "root")
        if username is None:
            return None
        password = b.ask_password("Password:")
        if password is None:
            return None
        return {
            "ip_address": ip,
            "port": port or default_port,
            "username": username.strip(),
            "password": password,
        }
    ip = questionary.text(
        "IP Address:",
        validate=lambda x: True if validate_ipv4(x)[0] else validate_ipv4(x)[1],
        style=custom_style,
    ).ask()
    if ip is None:
        return None
    port_str = questionary.text(
        f"Port [{default_port}]:",
        default=str(default_port),
        validate=lambda x: True if validate_port(x, default_port)[0] else validate_port(x, default_port)[1],
        style=custom_style,
    ).ask()
    if port_str is None:
        return None
    _, _, port = validate_port(port_str.strip() or str(default_port), default_port)
    username = questionary.text("Username:", default="root", style=custom_style).ask()
    if username is None:
        return None
    password = questionary.password("Password:", style=custom_style).ask()
    if password is None:
        return None
    return {
        "ip_address": ip.strip(),
        "port": port or default_port,
        "username": username.strip(),
        "password": password,
    }


# --- Firmware update_url flow ---


def prompt_artifactory_base_url(default: str = "") -> str | None:
    """Prompt for Artifactory base URL. Default is Arlo Artifactory. Returns URL or None to cancel."""
    b = _gb()
    if b is not None:
        from ui.menus import show_error

        d = default or "https://artifactory.arlocloud.com"
        while True:
            url = b.ask_text("Artifactory base URL:", d)
            if url is None:
                return None
            u = (url or "").strip()
            if u and (u.startswith("http://") or u.startswith("https://")):
                return u.rstrip("/") or None
            show_error("Enter a valid URL (http:// or https://).")
    url = questionary.text(
        "Artifactory base URL:",
        default=default or "https://artifactory.arlocloud.com",
        validate=lambda x: bool((x or "").strip()) and ((x or "").startswith("http://") or (x or "").startswith("https://")) or "Enter a valid URL (http:// or https://).",
        style=custom_style,
    ).ask()
    if url is None:
        return None
    return (url or "").strip().rstrip("/") or None


def prompt_artifactory_username() -> str | None:
    """Prompt for Artifactory username (optional; use for Basic auth if server returns HTML)."""
    b = _gb()
    if b is not None:
        u = b.ask_text(
            "Artifactory username (optional - try if you get HTML/login errors):",
            "",
        )
        if u is None:
            return None
        return (u or "").strip() or None
    u = questionary.text(
        "Artifactory username (optional - try if you get HTML/login errors):",
        default="",
        style=custom_style,
    ).ask()
    if u is None:
        return None
    return (u or "").strip() or None


def prompt_artifactory_token() -> str | None:
    """Prompt for Artifactory access token (hidden). Returns token or None to cancel."""
    b = _gb()
    if b is not None:
        token = b.ask_password("Artifactory Access Token (or API key):")
        if token is None:
            return None
        token = (token or "").strip()
        return token or None
    token = questionary.password(
        "Artifactory Access Token (or API key):",
        style=custom_style,
    ).ask()
    if token is None:
        return None
    token = (token or "").strip()
    if not token:
        return None
    return token


def prompt_firmware_version_filter() -> str | None:
    """Prompt for version number to search in Artifactory (e.g. 5.0.18). Returns filter or None."""
    b = _gb()
    if b is not None:
        from ui.menus import show_error

        while True:
            version = b.ask_text(
                "Firmware version to search (e.g. 5.0.18 or 5.0.18_9a7a4d7):",
                "",
            )
            if version is None:
                return None
            v = (version or "").strip()
            if v:
                return v
            show_error("Enter a version number.")
    version = questionary.text(
        "Firmware version to search (e.g. 5.0.18 or 5.0.18_9a7a4d7):",
        default="",
        validate=lambda x: bool((x or "").strip()) or "Enter a version number.",
        style=custom_style,
    ).ask()
    if version is None:
        return None
    return (version or "").strip() or None


def prompt_select_firmware_version(
    versions: list[str] | None = None,
    choices: list[tuple[str, Any]] | None = None,
) -> Any | None:
    """Prompt to select one firmware. choices=(display_title, value). Returns selected value or None."""
    b = _gb()
    if b is not None:
        if choices:
            if not choices:
                return None
            if len(choices) == 1:
                return choices[0][1]
            return b.ask_select("Select firmware to download:", list(choices))
        if versions:
            if len(versions) == 1:
                return versions[0]
            items = [(v, v) for v in versions]
            return b.ask_select("Select firmware to download:", items)
        return None
    if choices:
        if not choices:
            return None
        if len(choices) == 1:
            return choices[0][1]
        q_choices = [Choice(title=title, value=value) for title, value in choices]
    elif versions:
        if len(versions) == 1:
            return versions[0]
        q_choices = [Choice(title=v, value=v) for v in versions]
    else:
        return None
    return questionary.select("Select firmware to download:", choices=q_choices, style=custom_style).ask()


def prompt_fw_server_root(default: str = "") -> str | None:
    """Prompt for local FW server root (directory that contains qa/dev/prod folders). Returns path or None to cancel."""
    b = _gb()
    if b is not None:
        path = b.ask_text(
            "FW server root (directory with env folders, e.g. qa/, dev/):",
            default or "",
        )
        if path is None:
            return None
        return (path or "").strip().rstrip("/") or None
    path = questionary.text(
        "FW server root (directory with env folders, e.g. qa/, dev/):",
        default=default or "",
        style=custom_style,
    ).ask()
    if path is None:
        return None
    return (path or "").strip().rstrip("/") or None


def prompt_select_env_folder(base_path: str) -> str | None:
    """
    List subdirectories of base_path (e.g. C:\\FxTest\\fw_server\\local_server) and let user pick one.
    Used for update URL folder (env) in fw_setup. Returns folder name (e.g. qa, dev, prod) or None to cancel.
    """
    if not base_path or not os.path.isdir(base_path):
        return None
    subdirs = sorted(
        d for d in os.listdir(base_path)
        if os.path.isdir(os.path.join(base_path, d)) and not d.startswith(".")
    )
    if not subdirs:
        return None
    b = _gb()
    if b is not None:
        items = [(d, d) for d in subdirs]
        return b.ask_select("Select folder for update URL:", items)
    choices = [Choice(title=d, value=d) for d in subdirs]
    return questionary.select(
        "Select folder for update URL:",
        choices=choices,
        style=custom_style,
    ).ask()


def prompt_confirm_proceed(message: str = "Proceed? (y/n):") -> bool:
    """Ask for y/n confirmation. Returns True for yes, False for no/cancel."""
    b = _gb()
    if b is not None:
        return b.ask_confirm(message, default=False)
    a = questionary.confirm(message, default=False, style=custom_style).ask()
    return a is True


def _model_resolution_label(model_name: str) -> str:
    """Return '2K' or 'FHD' for display based on model number (VMC3xxx = 2K, VMC2xxx = FHD)."""
    name = (model_name or "").upper()
    if name.startswith("VMC3"):
        return "2K"
    if name.startswith("VMC2"):
        return "FHD"
    return ""


def prompt_select_binaries_folder(
    binaries_base_path: str,
    env_folder_name: str,
) -> str | None:
    """
    Prompt user to choose which model folder (2K or FHD) to extract .enc files into.
    Shows the exact folders present under binaries_base_path (e.g. qa/binaries/VMC3073).
    Returns the chosen folder name (e.g. 'VMC3073') or None to cancel.
    """
    if not os.path.isdir(binaries_base_path):
        return None
    subdirs = sorted(
        d for d in os.listdir(binaries_base_path)
        if os.path.isdir(os.path.join(binaries_base_path, d))
    )
    if not subdirs:
        return None
    # Relative path for display: env_folder_name/binaries/MODEL; label 2K/FHD when known
    def choice_title(model_dir: str) -> str:
        label = _model_resolution_label(model_dir)
        path = f"{env_folder_name}/binaries/{model_dir}"
        return f"{model_dir} ({label}) — {path}" if label else f"{model_dir} — {path}"

    b = _gb()
    if b is not None:
        items = [(choice_title(d), d) for d in subdirs]
        return b.ask_select("Extract .enc files to which model folder (2K or FHD)?", items)
    choices = [Choice(title=choice_title(d), value=d) for d in subdirs]
    return questionary.select(
        "Extract .enc files to which model folder (2K or FHD)?",
        choices=choices,
        style=custom_style,
    ).ask()


def prompt_save_credentials_to_config(config_path: str) -> bool:
    """Ask if user wants to save Artifactory credentials to config file. Returns True for yes."""
    b = _gb()
    if b is not None:
        msg = (
            f"Would you like to save your Artifactory credentials to a config file?\n"
            f"This will allow automatic login in future sessions.\n\n"
            f"Credentials will be stored in: {config_path}\n\n"
            "Save credentials?"
        )
        return b.ask_confirm(msg, default=False)
    return questionary.confirm(
        f"Would you like to save your Artifactory credentials to a config file?\n"
        f"This will allow automatic login in future sessions.\n\n"
        f"Credentials will be stored in: {config_path}\n\n"
        "Save credentials?",
        default=False,
        style=custom_style,
    ).ask() is True
