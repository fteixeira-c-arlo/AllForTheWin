"""Automated update_url flow: collect inputs, Artifactory download, local server, send command to camera."""
from __future__ import annotations

import os
import re
from typing import Any, Callable, NamedTuple
from urllib.parse import quote

from core.artifactory_client import (
    list_available_firmware,
    resolve_repo_for_model,
)
from core.fw_setup_service import (
    DEFAULT_ARTIFACTORY_URL,
    compute_download_model,
    default_artifactory_url,
    default_fw_server_root,
    download_firmware_to_layout,
    extract_firmware_archive,
    flatten_firmware_archives,
    get_local_ipv4,
    is_firmware_archive,
    prepare_env_directories,
)
from core.local_server import (
    DEFAULT_PORT,
    check_server_status,
    get_running_server_url,
    start_http_server,
)
from utils.config_manager import (
    decode_token,
    get_config_path,
    load_config_file,
    save_config_file,
    update_last_used,
)
from interface.menus import console, show_error, show_success
from interface.prompts import (
    prompt_artifactory_base_url,
    prompt_artifactory_token,
    prompt_artifactory_username,
    prompt_confirm_proceed,
    prompt_ensure_fw_server_root,
    prompt_firmware_version_filter,
    prompt_fw_server_root,
    prompt_save_credentials_to_config,
    prompt_select_env_folder,
    prompt_select_firmware_version,
)


def _progress_callback(name: str, index: int, total: int) -> None:
    console.print(f"  [dim]\u2713[/] [dim]{name}[/] ({index}/{total})")


class _ArtifactoryCreds(NamedTuple):
    base_url: str
    token: str
    username: str | None


def _cli_step_credentials(model: dict) -> _ArtifactoryCreds | str:
    """Load or prompt Artifactory credentials. Returns creds or error / cancel token string."""
    model_name = (model or {}).get("name") or ""
    fw_models = (model or {}).get("fw_search_models") or ([model_name] if model_name else [])
    repo_for_device = resolve_repo_for_model(fw_models[0] if fw_models else model_name)
    config = None
    try:
        config = load_config_file()
    except ValueError as e:
        console.print(f"[yellow]Config file is corrupted: {e}[/]")
        console.print(f"   File: [dim]{get_config_path()}[/]")
        console.print("   Continuing with manual entry.\n")
        config = None

    if config:
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
        base_url = (art.get("base_url") or "").strip() or default_artifactory_url()
        if not base_url or "artifactory.example.com" in base_url:
            base_url = DEFAULT_ARTIFACTORY_URL
        console.print(f"[dim]Artifactory: {base_url}[/]\n")
        if config and token:
            try:
                update_last_used()
            except Exception:
                pass
        return _ArtifactoryCreds(base_url=base_url, token=token, username=username)

    console.print("Please provide the following information:\n")
    save_credentials = prompt_save_credentials_to_config(get_config_path())
    if save_credentials:
        console.print("[dim]Credentials will be saved after you enter them.[/]\n")

    base_url = default_artifactory_url()
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
        save_config_file(username or "", token, base_url, repo_for_device)
        console.print(f"\n[bold green]\u2713[/] [green]Configuration saved to {get_config_path()}[/]")
        console.print("[bold green]\u2713[/] [green]Credentials will be loaded automatically next time.[/]\n")
    else:
        console.print("\n[dim]No problem. You'll need to enter credentials manually next time.[/]\n")

    return _ArtifactoryCreds(base_url=base_url, token=token, username=username)


def _cli_step_pick_version_and_list(
    creds: _ArtifactoryCreds,
    fw_search_models: list[str],
    repo: str | None = None,
) -> tuple[str, str | None] | str:
    """
    Prompt version filter, list Artifactory, prompt firmware selection.
    Returns (version, selected_filename) or error / cancel string.
    """
    version_filter = prompt_firmware_version_filter()
    if not version_filter:
        return "cancelled"

    console.print()
    ok, available, file_meta, err = list_available_firmware(
        creds.base_url, creds.token, version_filter, fw_search_models, creds.username,
        repo=repo,
    )
    if not ok:
        show_error(err or "Failed to list firmware.")
        return err or "Failed to list firmware."

    selected_filename: str | None = None
    if available:
        console.print()
    if not available:
        console.print("[yellow]No firmware found matching your version.[/]")
        console.print(
            "[dim]Using your input as version for mock download. Configure ARTIFACTORY_BASE_URL for real Artifactory.[/]\n"
        )
        version = version_filter
    else:
        flat_fw = flatten_firmware_archives(available, file_meta)
        if not flat_fw:
            console.print("[yellow]No .zip or .<env>.tar.gz firmware found matching your version.[/]")
            console.print("[dim]Using your input as version for mock download.[/]\n")
            version = version_filter
            selected_filename = None
        else:
            choices = [
                (f"{row[0]} — {row[1]}", (row[0], row[1])) for row in flat_fw
            ]
            console.print(f"[bold]Firmware matching [cyan]{version_filter}[/] ({len(flat_fw)}):[/]")
            for i, row in enumerate(flat_fw, 1):
                folder, filename = row[0], row[1]
                console.print(f"   [cyan]{i}.[/] {folder} — {filename}")
            console.print()
            selected = prompt_select_firmware_version(choices=choices)
            if not selected:
                return "cancelled"
            version, selected_filename = selected

    return (version, selected_filename)


def _cli_step_pick_env_and_confirm(
    model_name: str,
    download_model: str,
    version: str,
    server_folder: str,
    repo: str | None = None,
) -> bool:
    active_repo = (repo or "").strip() or resolve_repo_for_model(model_name)
    console.print("\n[bold]Configuration Summary:[/]")
    console.print(
        f"   Model: [cyan]{model_name}[/]"
        + (f" (Artifactory download key: {download_model})" if download_model != model_name else "")
    )
    console.print(f"   Firmware Version: [cyan]{version}[/]")
    console.print(f"   Repo: [cyan]{active_repo}[/]")
    console.print(f"   Local server folder: [cyan]{server_folder}[/]")
    console.print(f"   Local Server URL: http://<this_pc>:{DEFAULT_PORT}/{quote(server_folder.strip(), safe='')}\n")
    return bool(prompt_confirm_proceed("Proceed with firmware download and server setup? (y/n):"))


def _cli_step_download_extract(
    creds: _ArtifactoryCreds,
    root: str,
    env: str,
    model_name: str,
    fw_search_models: list[str],
    download_model: str,
    version: str,
    selected_filename: str | None,
    repo: str | None = None,
) -> str | None:
    ok_setup, msg_or_env_dir, binaries_base, _primary_bin, updaterules_dir, archive_dir = prepare_env_directories(
        root, env, model_name, fw_search_models=fw_search_models
    )
    if not ok_setup:
        show_error(msg_or_env_dir)
        return msg_or_env_dir

    path_or_err = msg_or_env_dir
    server_folder = env.strip()
    binaries_dir_for_download = os.path.join(path_or_err, "binaries", model_name)

    console.print("\n[bold]Setting up local firmware server...[/]")
    show_success(
        f"Created {server_folder}/archive/, {server_folder}/binaries/{model_name}/ and {server_folder}/updaterules/"
    )

    console.print("\n[bold]Downloading firmware to archive folder...[/]")
    success, err = download_firmware_to_layout(
        creds.token,
        download_model,
        version,
        binaries_dir_for_download,
        updaterules_dir,
        archive_dir,
        creds.base_url,
        creds.username,
        selected_filename,
        progress_callback=_progress_callback,
        byte_progress_callback=None,
        repo=repo or resolve_repo_for_model(model_name),
    )
    if not success:
        show_error(err or "Download failed.")
        return err or "Download failed."
    console.print("  [bold green]\u2713[/] [green]Download complete (saved in archive/).[/]\n")

    if selected_filename:
        archive_path = os.path.abspath(os.path.join(archive_dir, selected_filename))
        rules_dir = os.path.abspath(updaterules_dir)
        if not os.path.isfile(archive_path):
            show_error(f"Archive file not found at: {archive_path}", "Download may have saved to a different path.")
            return "Archive not found for extraction."
        console.print(f"[bold]Extracting into binaries/{model_name}/[/]")
        bin_dir = os.path.abspath(os.path.join(binaries_base, model_name))
        lower = selected_filename.lower()
        if lower.endswith(".zip"):
            console.print("[bold]Extracting firmware zip...[/]")
        elif is_firmware_archive(selected_filename) or lower.endswith(".tar.gz"):
            console.print("[bold]Extracting firmware tar.gz...[/]")
        ok_extract, err_extract = extract_firmware_archive(archive_path, bin_dir, rules_dir)
        if not ok_extract:
            show_error(err_extract or "Extraction failed.")
            return err_extract or "Extraction failed."
        if lower.endswith(".zip") or ".tar.gz" in lower:
            console.print(
                "  [bold green]\u2713[/] [green].enc → "
                + f"{server_folder}/binaries/{model_name}/"
                + ", UpdateRules.json → updaterules/[/]\n"
            )

    show_success("Binaries and update rules in place")
    console.print(f"[dim]Path: {root}[/]\n")
    return None


def _cli_step_start_server_and_update_url(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
    root: str,
    server_folder: str,
) -> str | None:
    console.print("[bold]Starting local firmware server...[/]")
    ok, msg = start_http_server(root, DEFAULT_PORT)
    if not ok:
        show_error(msg)
        return msg
    base_url = msg.rstrip("/")
    local_ip = get_local_ipv4()
    host_part = base_url.replace("http://", "").replace("https://", "").strip("/")
    port = host_part.split(":")[1] if ":" in host_part else str(DEFAULT_PORT)
    firmware_url = f"http://{local_ip}:{port}/{quote(server_folder.strip(), safe='')}"
    show_success(f"Server started at {base_url}")
    console.print(f"[dim]Camera URL (use this): {firmware_url}[/]\n")

    console.print("[bold]Sending update_url command to camera...[/]")
    success, output = connection_execute("arlocmd update_url", [firmware_url])
    if success:
        show_success("Camera acknowledged update URL")
        console.print("[dim]Camera will check for updates from local server.[/]")
        console.print("[dim]Local server remains active during this session. Use 'stop_server' to stop.[/]\n")
        return None
    show_error(output or "Command failed.")
    if output and (
        "Device disconnected" in output
        or "Session expired" in output
        or "Login incorrect" in output
    ):
        return "disconnected"
    return output or "Command failed."


def run_update_url_flow(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
    model: dict,
    abstract_cli_args: list[str] | None = None,
) -> str | None:
    """
    Run the full FW Wizard flow: prompts, list FW from Artifactory, download, server start, send command.
    model: dict with "name", optional "fw_search_models" (list of models for Artifactory search, e.g. 2K + FHD).
    abstract_cli_args: when invoked via abstract ``flash``, user tokens after the command name (e.g. legacy
    ``ip`` / ``file``). This flow does not use them; non-empty values are rejected with a clear error.
    Returns None on success, or an error message string.
    """
    extra = [str(a).strip() for a in (abstract_cli_args or []) if str(a).strip()]
    if extra:
        return (
            "FW Wizard does not use IP or file arguments (Artifactory flow is interactive). "
            "Run flash or fw_wizard with no arguments. "
            f"Received: {' '.join(extra)}"
        )

    model_name = (model or {}).get("name") or "Camera"
    fw_search_models = (model or {}).get("fw_search_models") or [model_name]
    active_repo = resolve_repo_for_model(fw_search_models[0] if fw_search_models else model_name)

    console.print("\n[bold cyan]\u21C4 FW Wizard (Artifactory + Local Server)[/]")
    console.print(
        f"This will download FW from [bold]{active_repo}[/], set up a local server, and configure the device.\n"
    )

    creds_out = _cli_step_credentials(model)
    if isinstance(creds_out, str):
        return creds_out
    creds = creds_out

    pick_out = _cli_step_pick_version_and_list(creds, fw_search_models, repo=active_repo)
    if isinstance(pick_out, str):
        return pick_out
    version, selected_filename = pick_out

    download_model = compute_download_model(version, selected_filename, model_name)

    root = default_fw_server_root()
    ensured = prompt_ensure_fw_server_root(root)
    if not ensured:
        show_error("Firmware server folder was not created. Cancelled.")
        return "cancelled"
    root = ensured
    env = prompt_select_env_folder(root)
    if not env:
        if not os.path.isdir(root):
            show_error(f"FW server root not found: {root}")
        else:
            try:
                subdirs = [
                    d
                    for d in os.listdir(root)
                    if os.path.isdir(os.path.join(root, d)) and not d.startswith(".")
                ]
                if not subdirs:
                    show_error(
                        f"No folders found in {root}. Create at least one (e.g. qa, dev, prod) and try again."
                    )
            except OSError:
                pass
        return "cancelled"
    server_folder = env.strip()

    if not _cli_step_pick_env_and_confirm(
        model_name, download_model, version, server_folder, repo=active_repo
    ):
        return "cancelled"

    err_dl = _cli_step_download_extract(
        creds, root, env, model_name, fw_search_models, download_model, version,
        selected_filename, repo=active_repo,
    )
    if err_dl is not None:
        return err_dl

    return _cli_step_start_server_and_update_url(connection_execute, root, server_folder)


def try_handle_fw_wizard_command(
    cmd: str,
    connection_execute: Callable[[str, list[str]], tuple[bool, str]] | None,
    model: dict[str, Any],
) -> tuple[str, str | None] | None:
    """
    If cmd is fw_wizard, run the text-based flow and return parse_and_execute-style (action, message).
    Otherwise return None so the caller can continue dispatch.
    """
    if cmd != "fw_wizard":
        return None
    if not connection_execute:
        show_error("Connect to the camera first to run fw_wizard.")
        return ("continue", None)
    try:
        err = run_update_url_flow(connection_execute, model)
    except (KeyboardInterrupt, EOFError):
        return ("continue", None)
    except Exception as e:
        show_error("fw_wizard failed.", str(e))
        return ("continue", None)
    if err is None:
        return ("continue", None)
    if err == "disconnected":
        return ("disconnected", None)
    if err == "cancelled":
        return ("continue", None)
    return ("continue", None)


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
        root = default_fw_server_root()
    else:
        root = prompt_fw_server_root(default_fw_server_root())
        if not root:
            return "cancelled"
        ensured = prompt_ensure_fw_server_root(root)
        if not ensured:
            return "cancelled"
        root = ensured
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
    server_folder = env.strip()
    local_ip = get_local_ipv4()
    firmware_url = f"http://{local_ip}:{port}/{quote(server_folder, safe='')}"

    console.print(f"[dim]Camera URL: {firmware_url}[/]\n")
    console.print("[bold]Sending update_url command to camera...[/]")
    success, output = connection_execute("arlocmd update_url", [firmware_url])
    if success:
        show_success("Camera acknowledged update URL")
        console.print("[dim]Camera will check for updates from local server.[/]\n")
        return None
    show_error(output or "Command failed.")
    if output and (
        "Device disconnected" in output
        or "Session expired" in output
        or "Login incorrect" in output
    ):
        return "disconnected"
    return output or "Command failed."


_OSPREY_VZDAEMON_ENV_PATH = "/tmp/media/nand/config/arlo/env/vzdaemon.env"
_OSPREY_VZDAEMON_ENV_DIR = "/tmp/media/nand/config/arlo/env"

# Sentinels we echo from the remote shell so Python can tell real success / failure
# apart regardless of exit code. Using unique strings avoids any risk of output
# containing them naturally.
_ARLO_WRITE_OK = "__ARLO_WRITE_OK__"
_ARLO_WRITE_FAILED = "__ARLO_WRITE_FAILED__"
_ARLO_NO_FILE = "__ARLO_NO_FILE__"


def _shell_single_quote(value: str) -> str:
    """Single-quote a string for POSIX shell (works on BusyBox ash on Osprey)."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _parse_current_vz_update_url(raw_output: str) -> str:
    """Extract the value of ``vz_update_url`` from a grep output (or empty).

    The real file on the Osprey SmartHub stores the key as a shell export line:

        export vz_update_url='https://arloupdates.arlo.com/arlo/fw/fw_deployed/qa'

    We also tolerate the legacy/unquoted form ``vz_update_url=VALUE`` and an
    optional double-quoted form just in case. The returned value is always
    unquoted (the surrounding quotes, if any, are stripped).
    """
    if not raw_output:
        return ""
    for ln in raw_output.splitlines():
        s = ln.strip()
        # Strip an optional leading "export "
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if not s.startswith("vz_update_url="):
            continue
        value = s[len("vz_update_url="):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        return value.strip()
    return ""


# Characters allowed inside a vz_update_url we embed into a shell command.
# Intentionally strict: this set contains ONLY RFC-3986 reserved/unreserved
# chars that are also shell-harmless when single-quoted. No ; & $ ` " ' \ | < >
# ( ) { } [ ] * ! space / tab / newline. Covers every realistic update URL we
# care about: http[s]://host[:port]/env[?key=val[&...]][#frag].
_URL_SAFE_RE = re.compile(r"^[A-Za-z0-9:/?=&.,_~%+#@\-]+$")


def _validate_update_url(url: str) -> str | None:
    """Return an error message if the URL is unsafe to embed in our shell; else None.

    ``&`` is allowed because we only ever expand the URL inside double quotes
    (for ``sed``) or single quotes (for ``grep``/``printf``) — in both of
    those contexts ``&`` is a literal character, not a shell metacharacter.
    """
    if not url:
        return "URL must not be empty."
    if len(url) > 512:
        return "URL is too long (>512 chars)."
    if not _URL_SAFE_RE.match(url):
        return (
            "URL contains unsupported characters. Allowed: letters, digits, "
            "and : / ? = & . , _ ~ % + # @ -"
        )
    return None


def _transport_lost_message(output: str) -> bool:
    o = output or ""
    return (
        "Device disconnected" in o
        or "Session expired" in o
        or "Login incorrect" in o
        or "Connection closed" in o
    )


def run_osprey_set_update_url(
    connection_execute: Callable[[str, list[str]], tuple[bool, str]],
) -> str | None:
    """
    Implements the "Change Update URL" tool for Osprey SmartHub (VMB4540).

    Mirrors the manual SSH procedure the user does by hand, but replaces the
    interactive ``vi`` + ``:%s|...|`` + ``:wq`` dance with a headless
    ``sed -i`` that makes the SAME on-disk change.  ``vi`` needs a PTY (which
    paramiko's ``exec_command`` does not give us); ``sed -i`` on BusyBox
    produces a byte-identical result and is much more reliable.

    Steps (matches the spec 1:1; steps 5–7 collapse into a sed that has the
    same effect as vi + :%s|...| + :wq):

      1. cd / stat ``/tmp/media/nand/config/arlo/env/vzdaemon.env``.
      2. ``ls``; if vzdaemon.env missing, show the File-Not-Found alert and
         abort.
      3. ``grep "vz_update_url" vzdaemon.env`` and parse the value between the
         single quotes for the popup prefill (falling back to empty).
      4. Popup for the new URL (prefilled from step 3).
      5–7. ``sed -i`` replaces (or inserts) the canonical
         ``export vz_update_url='<NEW_URL>'`` line, then we verify via
         ``grep -Fxq`` that the file actually contains that exact line.
      8. ``check_configs --backup`` to persist the change to flash.
      9. Confirm popup; on yes, ``reboot``.

    Returns None on real success, or one of "cancelled" / "disconnected" /
    error message on failure.  ``check_configs`` and ``reboot`` are ONLY
    reached when the readback verification succeeded.
    """
    from interface.prompts import prompt_line

    console.print("\n[bold cyan]Change Update URL (VMB4540)[/]")
    console.print(
        f"Edits [cyan]{_OSPREY_VZDAEMON_ENV_PATH}[/], saves with "
        "[cyan]check_configs --backup[/], then offers to reboot the hub.\n"
    )

    def _run(label: str, shell: str) -> tuple[bool, str, bool]:
        """Run ``shell`` over the existing SSH session, log both the command
        and the raw output verbatim, and return (ok, output, transport_lost).

        Verbose per-step printing is deliberate: when the button 'looks like
        it does nothing', the user can see the exact command we sent and the
        exact reply the hub gave (including stderr from sed/grep/printf).
        """
        console.print(f"[dim]VMB4540> {shell}[/]")
        try:
            ok_, out_ = connection_execute(shell, [])
        except Exception as exc:  # pragma: no cover - defensive
            console.print(f"[red]{label} raised: {exc}[/]")
            return False, str(exc), False
        raw = out_ or ""
        if raw.strip():
            console.print(raw.rstrip())
        else:
            console.print("[dim](no output)[/]")
        if _transport_lost_message(raw):
            return ok_, raw, True
        return ok_, raw, False

    # ----- Step 1 + 2 : cd + ls, confirm vzdaemon.env is there -------------
    # We fuse the "cd + ls" into one ``ls`` on the absolute directory path.
    # The output is parsed for the literal filename; a missing file becomes
    # the File-Not-Found alert from the spec.
    ok, ls_out, lost = _run(
        "ls env dir",
        f"ls {_OSPREY_VZDAEMON_ENV_DIR}",
    )
    if lost:
        return "disconnected"
    if not ok and not (ls_out or "").strip():
        show_error(
            "File Not Found",
            f"ls {_OSPREY_VZDAEMON_ENV_DIR} returned no output. "
            "Operation aborted.",
        )
        return "ls failed on device."
    present = any(
        (line.strip() == "vzdaemon.env") or line.strip().endswith(" vzdaemon.env")
        for line in (ls_out or "").splitlines()
    )
    if not present:
        show_error(
            "File Not Found",
            "vzdaemon.env was not found in "
            f"{_OSPREY_VZDAEMON_ENV_DIR}. Operation aborted.",
        )
        return "vzdaemon.env not found."

    # ----- Step 3 : read the current value for the popup prefill ----------
    # Use the exact command from the spec: no anchor, so ``export vz_update_url='...'``
    # as well as legacy ``vz_update_url=...`` both match. Parsing handles
    # the single-quoted form.
    _ok, cur_out, lost = _run(
        "grep vz_update_url",
        f'grep "vz_update_url" {_OSPREY_VZDAEMON_ENV_PATH} || echo __ARLO_NO_MATCH__',
    )
    if lost:
        return "disconnected"
    current_url = ""
    if "__ARLO_NO_MATCH__" not in (cur_out or ""):
        current_url = _parse_current_vz_update_url(cur_out or "")
    if current_url:
        console.print(f"[dim]Current vz_update_url: {current_url}[/]")
    else:
        console.print(
            "[yellow]vz_update_url line not found or not parseable; "
            "input will start empty but you can still proceed.[/]"
        )

    # ----- Step 4 : popup for the new URL (trim + validate) ---------------
    while True:
        new_url = prompt_line(
            "New value for vz_update_url (leave empty to cancel):",
            default=current_url,
        )
        new_url = (new_url or "").strip()
        if not new_url:
            console.print("Cancelled.\n")
            return "cancelled"
        err = _validate_update_url(new_url)
        if err is None:
            break
        show_error("Value cannot be empty or must be a valid URL.", err)
        # Re-prompt so the user can fix the value without restarting the flow.
        current_url = new_url

    if new_url == _parse_current_vz_update_url(cur_out or ""):
        console.print(
            "[dim]New URL matches current value \u2014 nothing to change.[/]\n"
        )
        return "cancelled"

    console.print(f"[dim]New vz_update_url: {new_url}[/]")

    # ----- Steps 5–7 : headless equivalent of vi + :%s|...|...| + :wq -----
    #
    # The canonical line we want on disk is ALWAYS:
    #
    #     export vz_update_url='<NEW_URL>'
    #
    # We run TWO sed substitutions back-to-back so we rewrite the line
    # regardless of whether it previously had the ``export`` prefix or not.
    # If neither form existed, the file is untouched by sed and we append
    # the canonical line instead. Finally we verify with ``grep -Fxq`` that
    # the EXACT expected line is present before we dare to check_configs
    # or reboot.
    target_line = f"export vz_update_url='{new_url}'"
    target_line_q = _shell_single_quote(target_line)

    # 5a. sed -i (two -e so we catch both "export X=..." and bare "X=..." rows)
    ok, sed_out, lost = _run(
        "sed -i",
        (
            f'sed -i '
            f'-e "s|^export vz_update_url=.*$|{target_line}|" '
            f'-e "s|^vz_update_url=.*$|{target_line}|" '
            f"{_OSPREY_VZDAEMON_ENV_PATH}"
        ),
    )
    if lost:
        return "disconnected"
    if not ok:
        show_error(
            "sed -i failed on the device.",
            (sed_out or "").strip()
            or "No stderr captured. The file may be on a read-only filesystem.",
        )
        return sed_out or "sed -i failed."

    # 5b. Append the canonical line only if sed matched nothing.
    ok, app_out, lost = _run(
        "append if missing",
        (
            f"grep -Fxq {target_line_q} {_OSPREY_VZDAEMON_ENV_PATH} "
            f"|| printf '%s\\n' {target_line_q} >> {_OSPREY_VZDAEMON_ENV_PATH}"
        ),
    )
    if lost:
        return "disconnected"
    if not ok:
        show_error(
            "Could not append vz_update_url to vzdaemon.env.",
            (app_out or "").strip() or "No stderr captured.",
        )
        return app_out or "append failed."

    # 5c. Explicit readback verification. This is what guarantees that the
    # file REALLY contains the expected line before we touch check_configs
    # or reboot the hub.
    ok, ver_out, lost = _run(
        "verify write",
        (
            f"if grep -Fxq {target_line_q} {_OSPREY_VZDAEMON_ENV_PATH}; "
            f"then echo {_ARLO_WRITE_OK}; "
            f"else echo {_ARLO_WRITE_FAILED}; fi"
        ),
    )
    if lost:
        return "disconnected"
    if _ARLO_WRITE_OK not in (ver_out or "") or _ARLO_WRITE_FAILED in (ver_out or ""):
        show_error(
            "vzdaemon.env readback does NOT contain the new line.",
            "The write appeared to run but the file does not show "
            f"'{target_line}'. check_configs and reboot were skipped. "
            "See the command log above for the device's actual stdout/stderr.",
        )
        return "vzdaemon.env update could not be verified."

    # 5d. Show the AFTER content so the user can see the change on screen.
    _run("cat vzdaemon.env (after)", f"cat {_OSPREY_VZDAEMON_ENV_PATH}")
    show_success("vzdaemon.env updated and verified on device.")

    # ----- Step 8 : check_configs --backup --------------------------------
    ok, out, lost = _run("check_configs --backup", "check_configs --backup")
    if lost:
        return "disconnected"
    if not ok:
        show_error(
            "check_configs --backup failed \u2014 the change is NOT yet "
            "persisted to flash.",
            (out or "").strip() or "no stderr captured",
        )
        return out or "check_configs --backup failed."
    show_success("check_configs --backup completed.\n")

    # ----- Step 9 : confirm reboot, then reboot ---------------------------
    if not prompt_confirm_proceed(
        "Reboot Basestation? The update URL has been changed and config "
        "backed up. The basestation will now reboot to apply changes. "
        "Continue? (y/n):"
    ):
        console.print(
            "[dim]File change + backup are already persisted. "
            "Skipping reboot per user choice.[/]\n"
        )
        return None

    console.print("[bold]Rebooting hub...[/]")
    ok, out, lost = _run("reboot", "reboot")
    if lost or ok:
        show_success(
            "Basestation is rebooting. SSH session will disconnect.\n"
        )
        return None
    show_error("reboot command failed.", (out or "").strip() or "no output")
    return out or "Command failed."


def run_stop_server() -> str:
    """Stop the local firmware server. Returns message for user."""
    from core.local_server import stop_http_server
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
