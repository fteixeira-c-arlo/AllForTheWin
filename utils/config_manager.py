"""
Artifactory configuration file management.

Stores credentials in ~/.arlo_camera_config.json with base64-encoded token
and restricted file permissions (600). Not encrypted; avoid on shared systems.
"""
import base64
import json
import os
import stat
from datetime import datetime, timezone
from typing import Any

# Defaults aligned with app (artifactory_client / update_url_flow)
DEFAULT_BASE_URL = "https://artifactory.arlocloud.com"
DEFAULT_REPO = "camera-fw-generic-release-local"

CONFIG_FILE = os.path.expanduser("~/.arlo_camera_config.json")


def get_config_path() -> str:
    """Return the config file path (for display)."""
    return CONFIG_FILE


def load_config_file() -> dict[str, Any] | None:
    """
    Load configuration from file.

    Returns:
        Config dict with 'artifactory' (username, access_token, base_url, repo),
        'created_at', 'last_used'; or None if file doesn't exist or is invalid.
    """
    if not os.path.exists(CONFIG_FILE):
        return None
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
        if not config or "artifactory" not in config:
            return None
        art = config["artifactory"]
        if not isinstance(art, dict) or "username" not in art or "access_token" not in art:
            return None
        return config
    except (json.JSONDecodeError, KeyError, OSError) as e:
        # Caller can report that config is corrupted
        raise ValueError(str(e)) from e


def encode_token(token: str) -> str:
    """Base64-encode token (obfuscation only, not encryption)."""
    return base64.b64encode((token or "").encode()).decode()


def decode_token(encoded_token: str) -> str:
    """Decode access token from config file."""
    return base64.b64decode((encoded_token or "").encode()).decode()


def save_config_file(
    username: str,
    token: str,
    base_url: str | None = None,
    repo: str | None = None,
) -> None:
    """
    Save credentials to config file. Sets permissions to 600 (owner read/write only).
    """
    encoded = encode_token(token or "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    config_data = {
        "artifactory": {
            "username": (username or "").strip(),
            "access_token": encoded,
            "base_url": (base_url or "").strip() or DEFAULT_BASE_URL,
            "repo": (repo or "").strip() or DEFAULT_REPO,
        },
        "created_at": now,
        "last_used": now,
    }
    # Ensure parent dir exists (e.g. ~ on some setups)
    parent = os.path.dirname(CONFIG_FILE)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)
    try:
        os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def update_last_used() -> None:
    """Update last_used timestamp in existing config file."""
    try:
        config = load_config_file()
    except ValueError:
        return
    if not config:
        return
    config["last_used"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        os.chmod(CONFIG_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def delete_config_file() -> bool:
    """Remove config file. Returns True if deleted, False if it didn't exist."""
    if not os.path.exists(CONFIG_FILE):
        return False
    os.remove(CONFIG_FILE)
    return True


def config_exists() -> bool:
    """Return True if config file exists."""
    return os.path.exists(CONFIG_FILE)
