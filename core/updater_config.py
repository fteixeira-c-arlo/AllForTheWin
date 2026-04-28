"""Persistent settings for the auto-updater.

Two pieces of state, stored as JSON in the user's app-state dir:

  channel:           which release stream this install follows
                     ("stable" | "beta" | "dev"). Default "stable".
  postponed_version: the version the user last clicked "Later" on.
  postponed_at:      ISO-8601 UTC timestamp of that click.

The file is best-effort: any parse error or I/O failure is swallowed and the
defaults are returned. The updater must never crash the app.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from core.user_paths import _app_state_root  # type: ignore[attr-defined]

Channel = Literal["stable", "beta", "dev"]
VALID_CHANNELS: tuple[Channel, ...] = ("stable", "beta", "dev")
DEFAULT_CHANNEL: Channel = "stable"
POSTPONE_HOURS = 24

_FILENAME = "updater.json"


def _config_path() -> Path:
    return Path(_app_state_root()) / _FILENAME


def _load() -> dict:
    p = _config_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    p = _config_path()
    try:
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def get_channel() -> Channel:
    raw = str(_load().get("channel") or DEFAULT_CHANNEL).lower()
    if raw not in VALID_CHANNELS:
        return DEFAULT_CHANNEL
    return raw  # type: ignore[return-value]


def set_channel(channel: str) -> None:
    if channel not in VALID_CHANNELS:
        raise ValueError(f"Invalid channel: {channel!r}; valid: {VALID_CHANNELS}")
    data = _load()
    data["channel"] = channel
    # Switching channels invalidates any postponed reminder for the old channel.
    data.pop("postponed_version", None)
    data.pop("postponed_at", None)
    _save(data)


def postpone(version: str) -> None:
    """Record that the user clicked 'Later' on this version."""
    data = _load()
    data["postponed_version"] = version
    data["postponed_at"] = datetime.now(timezone.utc).isoformat()
    _save(data)


def clear_postpone() -> None:
    data = _load()
    data.pop("postponed_version", None)
    data.pop("postponed_at", None)
    _save(data)


def is_postponed(version: str) -> bool:
    """True if `version` was postponed within the last POSTPONE_HOURS hours.

    A different (newer or older) `version` always returns False, so a freshly
    cut release surfaces the dialog even if the previous one was just postponed.
    """
    data = _load()
    pv = data.get("postponed_version")
    pa = data.get("postponed_at")
    if not pv or not pa or pv != version:
        return False
    try:
        when = datetime.fromisoformat(pa)
    except (TypeError, ValueError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(hours=POSTPONE_HOURS)


def disabled_via_env() -> bool:
    """Honor ARLOHUB_NO_UPDATE_CHECK regardless of stored config."""
    return os.environ.get("ARLOHUB_NO_UPDATE_CHECK", "").strip().lower() in ("1", "true", "yes")
