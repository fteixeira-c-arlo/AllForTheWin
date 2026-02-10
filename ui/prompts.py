"""User input prompts."""
from typing import Any

import questionary
from questionary import Choice, Style

from utils.validators import validate_ipv4, validate_port, validate_firmware_version

custom_style = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:green bold"),
])


def prompt_select_model(models: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Prompt to select a camera model with arrows or type-to-search. Returns selected model dict or None to exit."""
    choices = [
        Choice(title=f"{m['name']} — {m['display_name']}", value=m)
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
    return questionary.text(prompt_text, default=default, style=custom_style).ask() or ""


def prompt_connection_method() -> str | None:
    """Prompt for connection method: UART, ADB, or SSH. Returns 'UART', 'ADB', 'SSH' or None to cancel."""
    choice = questionary.select(
        "Connection method:",
        choices=[
            questionary.Choice("UART (serial)", value="UART"),
            questionary.Choice("ADB (USB)", value="ADB"),
            questionary.Choice("SSH", value="SSH"),
            questionary.Choice("Back to model selection", value="back"),
        ],
        style=custom_style,
    ).ask()
    if choice == "back":
        return None
    return choice


def prompt_adb_params() -> dict | None:
    """Prompt for ADB connection: password only (USB connection, adb shell auth). Returns dict or None to cancel."""
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

ENVIRONMENTS = ["QA", "DEV", "PROD_SIGNED"]


def prompt_artifactory_base_url(default: str = "") -> str | None:
    """Prompt for Artifactory base URL. Default is Arlo Artifactory. Returns URL or None to cancel."""
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


def prompt_firmware_version() -> str | None:
    """Prompt for firmware version (X.X.XX_XXXXXXX). Returns version or None to cancel."""
    version = questionary.text(
        "Firmware Version:",
        default="",
        validate=lambda x: validate_firmware_version(x)[0] or validate_firmware_version(x)[1],
        style=custom_style,
    ).ask()
    if version is None:
        return None
    return (version or "").strip() or None


def prompt_firmware_version_filter() -> str | None:
    """Prompt for version number to search in Artifactory (e.g. 5.0.18). Returns filter or None."""
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
    choices: list[tuple[str, str]] | None = None,
) -> str | None:
    """Prompt to select one firmware. choices=(display_title, value) e.g. ('release-MR5 - file.tar.gz', 'release-MR5'). Returns selected value or None."""
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
    path = questionary.text(
        "FW server root (directory with env folders, e.g. qa/, dev/):",
        default=default or "",
        style=custom_style,
    ).ask()
    if path is None:
        return None
    return (path or "").strip().rstrip("/") or None


def prompt_environment() -> str | None:
    """Prompt for environment (QA / DEV / PROD_SIGNED). Returns env or None to cancel."""
    choice = questionary.select(
        "Environment:",
        choices=[
            Choice("QA", value="QA"),
            Choice("DEV", value="DEV"),
            Choice("PROD_SIGNED", value="PROD_SIGNED"),
        ],
        style=custom_style,
    ).ask()
    if choice is None:
        return None
    if isinstance(choice, str):
        return choice
    return None


def prompt_confirm_proceed(message: str = "Proceed? (y/n):") -> bool:
    """Ask for y/n confirmation. Returns True for yes, False for no/cancel."""
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
    import os
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

    choices = [Choice(title=choice_title(d), value=d) for d in subdirs]
    return questionary.select(
        "Extract .enc files to which model folder (2K or FHD)?",
        choices=choices,
        style=custom_style,
    ).ask()


def prompt_save_credentials_to_config(config_path: str) -> bool:
    """Ask if user wants to save Artifactory credentials to config file. Returns True for yes."""
    return questionary.confirm(
        f"Would you like to save your Artifactory credentials to a config file?\n"
        f"This will allow automatic login in future sessions.\n\n"
        f"Credentials will be stored in: {config_path}\n\n"
        "Save credentials?",
        default=False,
        style=custom_style,
    ).ask() is True
