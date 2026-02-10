"""Config file commands: config_show, config_update, config_delete."""
from ui.menus import console, show_error, show_success
from ui.prompts import (
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


def run_config_show() -> str | None:
    """Show current Artifactory config (no token). Returns message or None."""
    console.print("\n[bold cyan]\u2699 Artifactory Configuration[/]\n")
    try:
        config = load_config_file()
    except ValueError as e:
        show_error(f"Config file is corrupted: {e}", f"File: {get_config_path()}")
        return None
    if not config:
        console.print("No configuration file found.")
        console.print(f"To create one, run [bold]fw_setup[/] and choose to save credentials.\n")
        return None
    art = config["artifactory"]
    console.print(f"Configuration file: [dim]{get_config_path()}[/]")
    console.print(f"Username: [cyan]{art.get('username', '')}[/]")
    console.print("Token: [dim]****...****[/]")
    console.print(f"Base URL: [cyan]{art.get('base_url', '')}[/]")
    console.print(f"Repository: [cyan]{art.get('repo', '')}[/]")
    console.print(f"Created: [dim]{config.get('created_at', 'Unknown')}[/]")
    console.print(f"Last used: [dim]{config.get('last_used', 'Unknown')}[/]\n")
    return None


def run_config_update() -> str | None:
    """Update saved Artifactory credentials. Returns message or None."""
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
    save_config_file((username or "").strip(), token, base_url, DEFAULT_REPO)
    show_success("Configuration updated successfully.")
    console.print()
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
