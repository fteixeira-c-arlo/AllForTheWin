"""Config file commands: config_show, config_update, config_delete."""
from typing import Callable

from core.artifactory_client import (
    CAMERA_FIRMWARE_REPO,
    GATEWAY_FIRMWARE_REPO,
    resolve_repo_for_model,
)
from interface.menus import console, show_error, show_success
from interface.prompts import (
    prompt_artifactory_base_url,
    prompt_artifactory_token,
    prompt_artifactory_username,
    prompt_confirm_proceed,
)
from utils.config_manager import (
    DEFAULT_BASE_URL,
    DEFAULT_REPO,
    get_config_path,
    load_config_file,
    save_config_file,
    delete_config_file as config_delete_file,
    config_exists,
)

_VZDAEMON_ENV_PATH = "/tmp/media/nand/config/arlo/env/vzdaemon.env"


def _ssh_device_online_status(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
) -> tuple[str, str]:
    """Run a quick cloud-connectivity probe on the device over SSH.

    Returns (label, detail) where label is one of "online", "offline", "unknown".
    A single short ping to 8.8.8.8 is enough to tell us whether the device has
    working WAN/internet access (and therefore can reach Arlo cloud services).
    """
    shell = (
        "ping -c 1 -W 2 8.8.8.8 >/dev/null 2>&1 "
        "&& echo __ARLO_ONLINE__ || echo __ARLO_OFFLINE__"
    )
    try:
        ok, out = connection_execute(shell, [])
    except Exception as e:
        return "unknown", f"probe failed: {e}"
    text = (out or "").strip()
    if not ok and not text:
        return "unknown", "no response from device"
    if "__ARLO_ONLINE__" in text:
        return "online", "ping 8.8.8.8 succeeded"
    if "__ARLO_OFFLINE__" in text:
        return "offline", "ping 8.8.8.8 failed"
    return "unknown", text or "unexpected output"


def _ssh_read_vz_update_url(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
) -> tuple[str | None, str | None]:
    """Read ``vz_update_url`` from vzdaemon.env on the device over SSH.

    Returns (value, error). ``value`` is the raw URL (possibly empty string
    when the key is present but unset); ``error`` is a short human-readable
    reason when the file/key cannot be read.
    """
    shell = (
        f"[ -f {_VZDAEMON_ENV_PATH} ] "
        f"&& grep '^vz_update_url=' {_VZDAEMON_ENV_PATH} "
        f"|| echo __ARLO_NO_FILE__"
    )
    try:
        ok, out = connection_execute(shell, [])
    except Exception as e:
        return None, f"read failed: {e}"
    text = (out or "").strip()
    if "__ARLO_NO_FILE__" in text:
        return None, f"{_VZDAEMON_ENV_PATH} not found on device"
    if not ok and not text:
        return None, "no response from device"
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("vz_update_url="):
            return s[len("vz_update_url="):].strip(), None
    return None, "vz_update_url not set in vzdaemon.env"


def _active_repo_for_model(model_name: str | None) -> str | None:
    """Active repo for the currently connected device, or None when unknown."""
    name = (model_name or "").strip()
    if not name:
        return None
    return resolve_repo_for_model(name)


def run_config_show(
    model_name: str | None = None,
    connection_type: str | None = None,
    connection_execute: Callable[[str, list[str]], tuple[bool, str]] | None = None,
) -> str | None:
    """Show current Artifactory config (no token).

    The "Active repo" line is driven by the currently connected device
    (``camera-fw-generic-release-local`` vs ``gateway-fw-generic-release-local``)
    so cameras and base stations display the correct source, independent of
    whatever repo happened to be persisted in the config file.

    When connected over SSH, two extra device-side facts are probed and
    appended to the output: the device's online/offline status (from a
    short ping to 8.8.8.8) and the current ``vz_update_url`` value read
    from ``/tmp/media/nand/config/arlo/env/vzdaemon.env``.
    Returns message or None.
    """
    console.print("\n[bold cyan]\u2699 Artifactory Configuration[/]\n")
    try:
        config = load_config_file()
    except ValueError as e:
        show_error(f"Config file is corrupted: {e}", f"File: {get_config_path()}")
        return None
    active_repo = _active_repo_for_model(model_name)
    if not config:
        console.print("No configuration file found.")
        console.print("To create one, run [bold]fw_wizard[/] and choose to save credentials.\n")
        if active_repo:
            console.print(
                f"Active repo for [cyan]{(model_name or '').strip()}[/]: [cyan]{active_repo}[/]\n"
            )
        _show_ssh_device_extras(connection_type, connection_execute)
        return None
    art = config["artifactory"]
    console.print(f"Configuration file: [dim]{get_config_path()}[/]")
    console.print(f"Username: [cyan]{art.get('username', '')}[/]")
    console.print("Token: [dim]****...****[/]")
    console.print(f"Base URL: [cyan]{art.get('base_url', '')}[/]")
    if active_repo:
        console.print(
            f"Active repo (for [cyan]{(model_name or '').strip()}[/]): [cyan]{active_repo}[/]"
        )
    else:
        console.print(
            "Active repo: [dim]connect a device first "
            f"({CAMERA_FIRMWARE_REPO} for cameras, {GATEWAY_FIRMWARE_REPO} for basestations)[/]"
        )
    saved_repo = (art.get("repo") or "").strip()
    if saved_repo and active_repo and saved_repo != active_repo:
        console.print(
            f"[dim]Saved repo in config file: {saved_repo} "
            "(informational; the active repo above is what's used for firmware ops).[/]"
        )
    elif saved_repo and not active_repo:
        console.print(f"[dim]Saved repo in config file: {saved_repo}[/]")
    console.print(f"Created: [dim]{config.get('created_at', 'Unknown')}[/]")
    console.print(f"Last used: [dim]{config.get('last_used', 'Unknown')}[/]\n")
    _show_ssh_device_extras(connection_type, connection_execute)
    return None


def _show_ssh_device_extras(
    connection_type: str | None,
    connection_execute: Callable[[str, list[str]], tuple[bool, str]] | None,
) -> None:
    """Print device online status + vz_update_url when connected via SSH.

    No-op for non-SSH sessions (ADB/UART) and when no execute fn is available,
    so the plain Artifactory view is unchanged in those modes.
    """
    if (connection_type or "").upper() != "SSH" or connection_execute is None:
        return

    console.print("[bold cyan]\u2699 Device status (via SSH)[/]")

    status, detail = _ssh_device_online_status(connection_execute)
    if status == "online":
        status_markup = "[green]online[/]"
    elif status == "offline":
        status_markup = "[red]offline[/]"
    else:
        status_markup = "[yellow]unknown[/]"
    console.print(f"Connection status: {status_markup} [dim]({detail})[/]")

    value, err = _ssh_read_vz_update_url(connection_execute)
    if err:
        console.print(f"vz_update_url: [yellow]n/a[/] [dim]({err})[/]\n")
    elif not value:
        console.print("vz_update_url: [dim](empty)[/]\n")
    else:
        console.print(f"vz_update_url: [cyan]{value}[/]\n")


def run_config_update(model_name: str | None = None) -> str | None:
    """Update saved Artifactory credentials.

    When a device is connected, the repo saved alongside the credentials is the
    one that matches the device kind; otherwise the previously saved repo is
    kept (falling back to the camera repo default).
    Returns message or None.
    """
    console.print("\n[bold cyan]\u2699 Update Artifactory Configuration[/]\n")
    try:
        config = load_config_file()
    except ValueError:
        config = None
    if config:
        art = config["artifactory"]
        console.print("Current configuration:")
        console.print(f"   Username: [cyan]{art.get('username', '')}[/]")
        console.print("   Token: [dim]****...****[/]")
        console.print(f"   Base URL: [cyan]{art.get('base_url', '')}[/]\n")
        if not prompt_confirm_proceed("Update credentials? (y/n):"):
            console.print("Configuration unchanged.\n")
            return None
    username = prompt_artifactory_username()
    if username is None:
        return "cancelled"
    token = prompt_artifactory_token()
    if not token:
        return "cancelled"
    base_url = prompt_artifactory_base_url(default=DEFAULT_BASE_URL)
    if base_url is None:
        return "cancelled"
    active_repo = _active_repo_for_model(model_name)
    if not active_repo and config:
        active_repo = ((config.get("artifactory") or {}).get("repo") or "").strip() or None
    repo_to_save = active_repo or DEFAULT_REPO
    save_config_file((username or "").strip(), token, base_url, repo_to_save)
    show_success("Configuration updated successfully.")
    console.print(f"[dim]Saved repo: {repo_to_save}[/]\n")
    return None


def run_config_delete() -> str | None:
    """Delete saved Artifactory config file. Returns message or None."""
    console.print("\n[bold cyan]\u2699 Delete Artifactory Configuration[/]\n")
    if not config_exists():
        console.print("No configuration file found.\n")
        return None
    console.print(f"This will delete: [dim]{get_config_path()}[/]")
    console.print("You will need to enter credentials manually in future sessions.\n")
    if not prompt_confirm_proceed("Delete configuration? (y/n):"):
        console.print("Configuration kept.\n")
        return None
    if config_delete_file():
        show_success(f"Deleted config file: {get_config_path()}")
    else:
        console.print("Config file was not found.\n")
    return None
