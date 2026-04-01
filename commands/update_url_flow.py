"""Automated update_url flow: collect inputs, mock download, local server, send command to camera."""
import os
import socket
import sys
from typing import Callable

from commands.artifactory_client import (
    ARTIFACTORY_REPO,
    download_firmware,
    list_available_firmware,
)
from commands.local_server import (
    DEFAULT_PORT,
    FW_ENV_TAR_GZ_SUFFIXES,
    check_server_status,
    extract_firmware_tar_gz,
    extract_firmware_zip,
    get_running_server_url,
    setup_directory_structure,
    start_http_server,
)


def _is_firmware_archive(filename: str) -> bool:
    """True if filename is .zip or .<env>.tar.gz (qa, dev, prod, prod_signed, ftrial, ftrial_signed)."""
    n = filename.lower()
    if n.endswith(".zip"):
        return True
    return any(n.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES)
from utils.config_manager import (
    decode_token,
    get_config_path,
    load_config_file,
    save_config_file,
    update_last_used,
)
from ui.menus import console, show_error, show_success
from ui.prompts import (
    prompt_artifactory_base_url,
    prompt_artifactory_token,
    prompt_artifactory_username,
    prompt_confirm_proceed,
    prompt_firmware_version_filter,
    prompt_fw_server_root,
    prompt_save_credentials_to_config,
    prompt_select_binaries_folder,
    prompt_select_env_folder,
    prompt_select_firmware_version,
)

# Default root for local firmware server (FxTest layout per Confluence)
# Override with env FW_SERVER_ROOT if needed.
def _default_fw_server_root() -> str:
    if os.environ.get("FW_SERVER_ROOT"):
        return os.environ.get("FW_SERVER_ROOT", "")
    if sys.platform == "win32":
        return r"C:\FxTest\fw_server\local_server"
    return os.path.join(os.getcwd(), "local_fw_server")

# Artifactory base URL (override via env ARTIFACTORY_BASE_URL if needed)
DEFAULT_ARTIFACTORY_URL = "https://artifactory.arlocloud.com"


def _artifactory_base_url() -> str:
    return os.environ.get("ARTIFACTORY_BASE_URL", DEFAULT_ARTIFACTORY_URL)


def _get_local_ipv4() -> str:
    """Return local IPv4 address for URL (camera must reach this). Fallback to localhost."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _progress_callback(name: str, index: int, total: int) -> None:
    console.print(f"  [dim]\u2713[/] [dim]{name}[/] ({index}/{total})")


def run_update_url_flow(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
    model: dict,
) -> str | None:
    """
    Run the full fw_setup flow: prompts, list FW from Artifactory, download, server start, send command.
    model: dict with "name", optional "fw_search_models" (list of models for Artifactory search, e.g. 2K + FHD).
    Returns None on success, or an error message string.
    """
    model_name = (model or {}).get("name") or "Camera"
    fw_search_models = (model or {}).get("fw_search_models") or [model_name]

    console.print("\n[bold cyan]\u21C4 Firmware Setup (Artifactory + Local Server)[/]")
    console.print("This will download FW from [bold]camera-fw-generic-release-local[/], set up a local server, and configure the camera.\n")

    # --- Config file: load saved credentials or offer to save ---
    config = None
    try:
        config = load_config_file()
    except ValueError as e:
        console.print(f"[yellow]Config file is corrupted: {e}[/]")
        console.print(f"   File: [dim]{get_config_path()}[/]")
        console.print("   Continuing with manual entry.\n")
        config = None

    if config:
        # Use saved credentials
        art = config["artifactory"]
        username = (art.get("username") or "").strip() or None
        try:
            token = decode_token(art.get("access_token") or "")
        except Exception:
            console.print("[yellow]Saved token could not be decoded. Enter credentials manually.[/]\n")
            token = prompt_artifactory_token()
            if not token:
                return "cancelled"
            username = prompt_artifactory_username() or username
        else:
            token = (token or "").strip()
            if not token:
                token = prompt_artifactory_token()
                if not token:
                    return "cancelled"
                username = prompt_artifactory_username() or username
            else:
                console.print("[bold green]\u2713[/] [green]Found saved credentials in config file[/]")
                console.print(f"   Username: [cyan]{username or '(none)'}[/]")
                console.print("   Token: [dim]****...****[/]\n")
                console.print("[dim]Using saved credentials. To change them, run: [bold]config_update[/][/]\n")
        base_url = (art.get("base_url") or "").strip() or _artifactory_base_url()
        if not base_url or "artifactory.example.com" in base_url:
            base_url = DEFAULT_ARTIFACTORY_URL
        console.print(f"[dim]Artifactory: {base_url}[/]\n")
    else:
        # No config: optionally offer to save after we have credentials
        console.print("Please provide the following information:\n")
        save_credentials = prompt_save_credentials_to_config(get_config_path())
        if save_credentials:
            console.print("[dim]Credentials will be saved after you enter them.[/]\n")

        # Artifactory base URL
        base_url = _artifactory_base_url()
        if not base_url or "artifactory.example.com" in base_url:
            console.print(f"[dim]Default: {DEFAULT_ARTIFACTORY_URL}. Override with ARTIFACTORY_BASE_URL or enter below.[/]\n")
            base_url = prompt_artifactory_base_url(base_url or DEFAULT_ARTIFACTORY_URL)
            if not base_url:
                return "cancelled"
        else:
            console.print(f"[dim]Artifactory: {base_url}[/]")

        token = prompt_artifactory_token()
        if not token:
            return "cancelled"
        username = prompt_artifactory_username()

        if save_credentials:
            save_config_file(username or "", token, base_url, ARTIFACTORY_REPO)
            console.print(f"\n[bold green]\u2713[/] [green]Configuration saved to {get_config_path()}[/]")
            console.print("[bold green]\u2713[/] [green]Credentials will be loaded automatically next time.[/]\n")
        else:
            console.print("\n[dim]No problem. You'll need to enter credentials manually next time.[/]\n")

    # Update last_used when using saved config successfully (optional, non-blocking)
    if config and token:
        try:
            update_last_used()
        except Exception:
            pass

    version_filter = prompt_firmware_version_filter()
    if not version_filter:
        return "cancelled"

    # Search across all models in group (2K + FHD) when fw_search_models has multiple entries
    console.print()
    ok, available, err = list_available_firmware(base_url, token, version_filter, fw_search_models, username)
    if not ok:
        show_error(err or "Failed to list firmware.")
        return err or "Failed to list firmware."

    selected_filename: str | None = None
    if available:
        console.print()
    if not available:
        console.print("[yellow]No firmware found matching your version.[/]")
        console.print("[dim]Using your input as version for mock download. Configure ARTIFACTORY_BASE_URL for real Artifactory.[/]\n")
        version = version_filter
    else:
        # Show .zip and .<env>.tar.gz firmware (env: qa, dev, prod, prod_signed, ftrial, ftrial_signed); one line per file
        available_fw = [(folder, [f for f in files if _is_firmware_archive(f)]) for folder, files in available]
        available_fw = [(folder, files) for folder, files in available_fw if files]
        flat_fw: list[tuple[str, str]] = [(folder, fn) for folder, files in available_fw for fn in files]
        if not flat_fw:
            console.print("[yellow]No .zip or .<env>.tar.gz firmware found matching your version.[/]")
            console.print("[dim]Using your input as version for mock download.[/]\n")
            version = version_filter
            selected_filename = None
        else:
            choices = [(f"{folder} — {filename}", (folder, filename)) for folder, filename in flat_fw]
            console.print(f"[bold]Firmware matching [cyan]{version_filter}[/] ({len(flat_fw)}):[/]")
            for i, (folder, filename) in enumerate(flat_fw, 1):
                console.print(f"   [cyan]{i}.[/] {folder} — {filename}")
            console.print()
            selected = prompt_select_firmware_version(choices=choices)
            if not selected:
                return "cancelled"
            version, selected_filename = selected

    # When a file was selected, version is the repo folder path (e.g. VMC3073/release-MR5); use first segment for download/extract target
    download_model = (version.split("/")[0] if "/" in version else version) if selected_filename else model_name

    # Select folder for update URL from folders in local_server (e.g. C:\FxTest\fw_server\local_server)
    root = _default_fw_server_root()
    env = prompt_select_env_folder(root)
    if not env:
        if not os.path.isdir(root):
            show_error(f"FW server root not found: {root}")
        else:
            try:
                subdirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")]
                if not subdirs:
                    show_error(f"No folders found in {root}. Create at least one (e.g. qa, dev, prod) and try again.")
            except OSError:
                pass
        return "cancelled"
    env_lower = env.lower()

    # Summary
    console.print("\n[bold]Configuration Summary:[/]")
    console.print(f"   Model: [cyan]{model_name}[/]" + (f" (download: {download_model})" if download_model != model_name else ""))
    console.print(f"   Firmware Version: [cyan]{version}[/]")
    console.print(f"   Repo: [cyan]{ARTIFACTORY_REPO}[/]")
    console.print(f"   Local Server: http://<this_pc>:{DEFAULT_PORT}/{env_lower}\n")
    if not prompt_confirm_proceed("Proceed with firmware download and server setup? (y/n):"):
        return "cancelled"

    ok, path_or_err, binaries_dir, updaterules_dir, archive_dir = setup_directory_structure(
        root, env, model_name, fw_search_models=fw_search_models
    )
    if not ok:
        show_error(path_or_err)
        return path_or_err

    # Use binaries dir for the selected file's model (2K or FHD folder)
    binaries_dir_for_download = os.path.join(path_or_err, "binaries", download_model)

    console.print("\n[bold]Setting up local firmware server...[/]")
    show_success(f"Created {env_lower}/archive/, {env_lower}/binaries/{{{', '.join(fw_search_models)}}}/ and {env_lower}/updaterules/")

    console.print("\n[bold]Downloading firmware to archive folder...[/]")
    success, err = download_firmware(
        token,
        download_model,
        version,
        binaries_dir_for_download,
        updaterules_dir,
        base_url=base_url,
        username=username,
        progress_callback=_progress_callback,
        files_allowlist=[selected_filename] if selected_filename else None,
        repo_folder_path=version if selected_filename else None,
        archive_dir=archive_dir,
    )
    if not success:
        show_error(err or "Download failed.")
        return err or "Download failed."
    console.print("  [bold green]\u2713[/] [green]Download complete (saved in archive/).[/]\n")

    # Extract from archive folder: .enc -> user-chosen binaries/MODEL (2K or FHD), UpdateRules.json -> updaterules
    if selected_filename:
        archive_path = os.path.abspath(os.path.join(archive_dir, selected_filename))
        rules_dir = os.path.abspath(updaterules_dir)
        if not os.path.isfile(archive_path):
            show_error(f"Archive file not found at: {archive_path}", "Download may have saved to a different path.")
            return "Archive not found for extraction."
        # Prompt user to choose which model folder (2K or FHD) in the local server to extract .enc into
        binaries_base = os.path.join(path_or_err, "binaries")
        console.print("[bold]Local server model folders (2K / FHD):[/]")
        chosen_folder = prompt_select_binaries_folder(binaries_base, env_lower)
        if not chosen_folder:
            return "cancelled"
        bin_dir = os.path.abspath(os.path.join(binaries_base, chosen_folder))
        lower = selected_filename.lower()
        if lower.endswith(".zip"):
            console.print("[bold]Extracting firmware zip...[/]")
            ok_extract, err_extract = extract_firmware_zip(archive_path, bin_dir, rules_dir)
        elif any(lower.endswith(s) for s in FW_ENV_TAR_GZ_SUFFIXES) or lower.endswith(".tar.gz"):
            console.print("[bold]Extracting firmware tar.gz...[/]")
            ok_extract, err_extract = extract_firmware_tar_gz(archive_path, bin_dir, rules_dir)
        else:
            ok_extract, err_extract = True, ""
        if not ok_extract:
            show_error(err_extract or "Extraction failed.")
            return err_extract or "Extraction failed."
        if lower.endswith(".zip") or lower.endswith(".tar.gz"):
            console.print("  [bold green]\u2713[/] [green].enc → " + f"{env_lower}/binaries/{chosen_folder}/" + ", UpdateRules.json → updaterules/[/]\n")

    show_success("Binaries and update rules in place")
    console.print(f"[dim]Path: {root}[/]\n")

    console.print("[bold]Starting local firmware server...[/]")
    ok, msg = start_http_server(root, DEFAULT_PORT)
    if not ok:
        show_error(msg)
        return msg
    base_url = msg.rstrip("/")
    local_ip = _get_local_ipv4()
    # Extract port from base_url (e.g. http://localhost:8000 -> 8000)
    host_part = base_url.replace("http://", "").replace("https://", "").strip("/")
    port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
    # Camera URL: http://localipv4:8000/qa (ends on env folder; camera finds binaries/ and updaterules/)
    firmware_url = f"http://{local_ip}:{port}/{env_lower}"
    show_success(f"Server started at {base_url}")
    console.print(f"[dim]Camera URL (use this): {firmware_url}[/]\n")

    console.print("[bold]Sending update_url command to camera...[/]")
    shell_cmd = "arlocmd update_url"
    success, output = connection_execute(shell_cmd, [firmware_url])
    if success:
        show_success("Camera acknowledged update URL")
        console.print("[dim]Camera will check for updates from local server.[/]")
        console.print("[dim]Local server remains active during this session. Use 'stop_server' to stop.[/]\n")
        return None
    show_error(output or "Command failed.")
    if output and "Device disconnected" in output:
        return "disconnected"
    return output or "Command failed."


def run_use_local_fw_server(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
) -> str | None:
    """
    Use existing local FW server: start server if needed, then set camera update_url to it.
    For when the user already has the FW server directory with correct files (qa/dev/prod).
    Returns None on success, or an error message string (or "cancelled" / "disconnected").
    """
    console.print("\n[bold cyan]Use local FW server[/]")
    console.print("Start the local firmware server (if not running) and set the camera update URL to it.\n")

    running, running_msg = check_server_status()
    base_url: str
    port: str
    root: str

    if running:
        ok, url = get_running_server_url()
        if not ok or not url:
            show_error(running_msg or "Could not get server URL.")
            return running_msg or "Could not get server URL."
        base_url = url.rstrip("/")
        host_part = base_url.replace("http://", "").replace("https://", "").strip("/")
        port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
        console.print(f"[dim]Server already running at {base_url}[/]\n")
        root = _default_fw_server_root()
    else:
        root = prompt_fw_server_root(_default_fw_server_root())
        if not root:
            return "cancelled"
        if not os.path.isdir(root):
            show_error(f"Directory does not exist: {root}")
            return f"Directory does not exist: {root}"
        ok, msg = start_http_server(root, DEFAULT_PORT)
        if not ok:
            show_error(msg)
            return msg
        base_url = msg.rstrip("/")
        host_part = base_url.replace("http://", "").replace("https://", "").strip("/")
        port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
        show_success(f"Server started at {base_url}\n")

    env = prompt_select_env_folder(root)
    if not env:
        if not os.path.isdir(root):
            show_error(f"FW server root not found: {root}")
        else:
            try:
                subdirs = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")]
                if not subdirs:
                    show_error(f"No folders found in {root}. Create at least one (e.g. qa, dev, prod) and try again.")
            except OSError:
                pass
        return "cancelled"
    env_lower = env.lower()
    local_ip = _get_local_ipv4()
    firmware_url = f"http://{local_ip}:{port}/{env_lower}"

    console.print(f"[dim]Camera URL: {firmware_url}[/]\n")
    console.print("[bold]Sending update_url command to camera...[/]")
    success, output = connection_execute("arlocmd update_url", [firmware_url])
    if success:
        show_success("Camera acknowledged update URL")
        console.print("[dim]Camera will check for updates from local server.[/]\n")
        return None
    show_error(output or "Command failed.")
    if output and "Device disconnected" in output:
        return "disconnected"
    return output or "Command failed."


def run_stop_server() -> str:
    """Stop the local firmware server. Returns message for user."""
    from commands.local_server import stop_http_server
    ok, msg = stop_http_server()
    if ok:
        return msg
    return f"Error: {msg}"


def run_server_status() -> str:
    """Return server status message."""
    running, msg = check_server_status()
    if running:
        return msg
    return msg
