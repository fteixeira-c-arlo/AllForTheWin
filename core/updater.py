"""Auto-updater client.

Polls GitHub Releases for a newer version on the user's selected channel,
downloads the Inno Setup installer to %TEMP%, verifies SHA256, then launches
it silently. The installer overwrites files in-place and relaunches ArloHub
via a [Run] entry guarded by `skipifnotsilent`.

Channels (configured via core/updater_config.set_channel):

  stable  Tags like  v1.0.5         -> /releases/latest (no prereleases)
  beta    Tags like  v1.0.5-beta.1  -> filtered from /releases (prerelease=true)
  dev     Tags like  v1.0.5-dev.1   -> filtered from /releases (prerelease=true)

Pure-Python (no Qt). The Qt-side glue lives in interface/update_dialog.py.

Disable at runtime by setting env var ARLOHUB_NO_UPDATE_CHECK=1.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

from core.updater_config import (
    Channel,
    disabled_via_env,
    get_channel,
)
from utils.version import __version__

GITHUB_REPO = "fteixeira-c-arlo/AllForTheWin"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_API_LIST = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30"
MANIFEST_NAME = "latest.json"
INSTALLER_NAME = "Install-ArloHub.exe"
NETWORK_TIMEOUT = 10
DOWNLOAD_TIMEOUT = 60

_CHANNEL_TAG_PATTERN = {
    "beta": re.compile(r"-beta\.\d+", re.IGNORECASE),
    "dev": re.compile(r"-dev\.\d+", re.IGNORECASE),
}


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    url: str
    sha256: str
    size: int
    notes: str
    channel: Channel


def _channel_version_key(s: str) -> tuple[int, ...]:
    """Comparable tuple within a single channel.

    Stable: 'v1.0.5'         -> (1, 0, 5)
    Beta:   'v1.0.5-beta.7'  -> (1, 0, 5, 7)
    Dev:    'v1.0.5-dev.12'  -> (1, 0, 5, 12)

    Tuple comparison works across the same channel because all tags in a
    channel have the same shape. Stable vs prerelease comparison is undefined
    here -- callers should filter by channel first.
    """
    raw = (s or "0").lstrip("v").strip()
    if "-" in raw:
        main, _, pre = raw.partition("-")
    else:
        main, pre = raw, ""
    parts: list[int] = []
    for seg in main.split("."):
        digits = ""
        for c in seg:
            if c.isdigit():
                digits += c
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    if pre:
        for seg in pre.split("."):
            try:
                parts.append(int(seg))
                break
            except ValueError:
                continue
    return tuple(parts)


def is_newer(remote: str, local: str = __version__) -> bool:
    return _channel_version_key(remote) > _channel_version_key(local)


def is_disabled() -> bool:
    return disabled_via_env()


def _matches_channel(tag: str, channel: Channel) -> bool:
    if channel == "stable":
        # Stable tags must NOT contain a prerelease suffix.
        return "-" not in tag
    pat = _CHANNEL_TAG_PATTERN.get(channel)
    return bool(pat and pat.search(tag))


def _release_for_channel(channel: Channel) -> Optional[dict]:
    """Returns the GitHub release JSON for the latest entry on the requested channel.

    Stable uses /releases/latest (which excludes prereleases by definition).
    Beta/dev list /releases and pick the highest version matching the tag pattern.

    Raises on network / HTTP errors. Returns None only when there is genuinely
    no matching release on the channel.
    """
    if channel == "stable":
        r = requests.get(
            GITHUB_API_LATEST,
            timeout=NETWORK_TIMEOUT,
            headers={"Accept": "application/vnd.github+json"},
        )
        r.raise_for_status()
        release = r.json()
        tag = str(release.get("tag_name") or "")
        if not _matches_channel(tag, "stable"):
            return None
        return release

    r = requests.get(
        GITHUB_API_LIST,
        timeout=NETWORK_TIMEOUT,
        headers={"Accept": "application/vnd.github+json"},
    )
    r.raise_for_status()
    releases = r.json()
    candidates = [
        rel
        for rel in releases
        if not rel.get("draft")
        and _matches_channel(str(rel.get("tag_name") or ""), channel)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda rel: _channel_version_key(str(rel.get("tag_name") or "")),
        reverse=True,
    )
    return candidates[0]


def fetch_latest(channel: Optional[Channel] = None) -> Optional[UpdateInfo]:
    """Returns UpdateInfo if a newer release exists on the requested channel, else None.

    `channel` defaults to the value persisted by updater_config. Raises on
    network / HTTP / parse errors so the caller can distinguish "up to date"
    (return None) from "couldn't reach GitHub" (raises). Background callers
    that want silent fallback should wrap in try/except (see check_async).
    """
    if is_disabled():
        return None

    ch: Channel = channel or get_channel()
    release = _release_for_channel(ch)
    if not release:
        return None

    assets = release.get("assets") or []
    manifest_asset = next((a for a in assets if a.get("name") == MANIFEST_NAME), None)
    if not manifest_asset:
        return None

    m = requests.get(manifest_asset["browser_download_url"], timeout=NETWORK_TIMEOUT)
    m.raise_for_status()
    manifest = m.json()

    version = str(manifest.get("version") or "").strip()
    url = str(manifest.get("url") or "").strip()
    sha256 = str(manifest.get("sha256") or "").strip().lower()
    if not version or not url or not sha256:
        return None
    if not is_newer(version):
        return None

    manifest_channel = str(manifest.get("channel") or ch).lower()
    if manifest_channel not in ("stable", "beta", "dev"):
        manifest_channel = ch

    return UpdateInfo(
        version=version,
        url=url,
        sha256=sha256,
        size=int(manifest.get("size") or 0),
        notes=str(release.get("body") or ""),
        channel=manifest_channel,  # type: ignore[arg-type]
    )


def check_async(callback: Callable[[Optional[UpdateInfo]], None]) -> None:
    """Run fetch_latest in a daemon thread; invoke callback with the result.

    The callback is called from the worker thread. Qt callers should pass a
    Signal.emit so Qt marshals the call to the GUI thread (see update_dialog.py).
    Errors are swallowed (callback receives None) -- intended for the silent
    startup check. Use check_async_verbose for the manual menu path so you can
    distinguish "up to date" from "network failed".
    """

    def _run() -> None:
        try:
            info = fetch_latest()
        except Exception:
            info = None
        try:
            callback(info)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="arlohub-update-check").start()


def check_async_verbose(
    callback: Callable[[Optional[UpdateInfo], Optional[str]], None],
) -> None:
    """Like check_async, but also surfaces errors so the manual UI can show them.

    callback receives (info, error_message). Exactly one of the two is non-None:
      - (UpdateInfo, None)    -> a newer version exists
      - (None, None)          -> already on the latest version
      - (None, "<message>")   -> network or API failed (user should retry)
    """

    def _run() -> None:
        info: Optional[UpdateInfo]
        err: Optional[str]
        try:
            info = fetch_latest()
            err = None
        except Exception as e:
            info = None
            err = str(e) or e.__class__.__name__
        try:
            callback(info, err)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="arlohub-update-check-verbose").start()


def download(
    info: UpdateInfo,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Path:
    """Stream the installer to %TEMP%/ArloHubUpdate/. Verifies SHA256.

    Raises RuntimeError on hash mismatch (after deleting the partial file).
    """
    tmp_dir = Path(tempfile.gettempdir()) / "ArloHubUpdate"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_dir / INSTALLER_NAME

    h = hashlib.sha256()
    with requests.get(info.url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or info.size or 0)
        downloaded = 0
        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                h.update(chunk)
                downloaded += len(chunk)
                if progress is not None:
                    try:
                        progress(downloaded, total)
                    except Exception:
                        pass

    actual = h.hexdigest().lower()
    if actual != info.sha256:
        try:
            target.unlink()
        except OSError:
            pass
        raise RuntimeError(
            f"Integridade do download falhou (SHA256). Esperado: {info.sha256}, recebido: {actual}"
        )

    return target


def launch_installer(installer_path: Path) -> None:
    """Launch the Inno Setup installer detached, in very-silent mode.

    Flags:
      /VERYSILENT          - no wizard UI
      /SUPPRESSMSGBOXES    - skip confirmation message boxes
      /CLOSEAPPLICATIONS   - use Restart Manager to close apps holding files
      /NORESTART           - never reboot the OS

    The .iss has a [Run] entry with `skipifnotsilent` that relaunches ArloHub
    after the silent install completes, so the user sees a brief gap and the
    app comes back on the new version.
    """
    if sys.platform != "win32":
        raise RuntimeError("Auto-update is Windows-only")

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    subprocess.Popen(
        [
            str(installer_path),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/CLOSEAPPLICATIONS",
            "/NORESTART",
        ],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
