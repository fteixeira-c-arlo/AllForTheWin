"""Artifactory client for firmware download. Repo: camera-fw-generic-release-local."""
import base64
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Callable

# Spinner for single-line search progress
SPINNER_CHARS = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# #region agent log
_DEBUG_LOG_PATH = r"c:\Users\Arlo-Account\Documents\arlo_scrpits\.cursor\debug.log"
def _debug_log(msg: str, data: dict, hypothesis_id: str, run_id: str = "run1"):
    try:
        os.makedirs(os.path.dirname(_DEBUG_LOG_PATH), exist_ok=True)
        with open(_DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"timestamp": int(time.time() * 1000), "location": "artifactory_client.py:list_available_firmware", "message": msg, "data": data, "sessionId": "debug-session", "runId": run_id, "hypothesisId": hypothesis_id}) + "\n")
    except Exception:
        pass
# #endregion


def _parse_json_response(r) -> tuple[bool, dict | None, str]:
    """Parse response as JSON. Returns (ok, data, error_message). Handles empty/non-JSON body."""
    if not r.content or not r.text.strip():
        return False, None, "Artifactory returned an empty response. Check URL and path."
    try:
        return True, r.json(), ""
    except json.JSONDecodeError:
        content_type = r.headers.get("Content-Type", "").split(";")[0].strip()
        preview = (r.text or "").replace("\n", " ").strip()[:120]
        return False, None, (
            f"Artifactory returned non-JSON (Status {r.status_code}, Content-Type: {content_type}). "
            "Often this means: wrong base URL, expired/invalid token, or login required. "
            f"Body starts with: {preview!r}..."
        )

# Repo for camera firmware (Artifactory)
ARTIFACTORY_REPO = "camera-fw-generic-release-local"

# Artifactory folder names are 2K only (VMC3xxx). FHD models (VMC2xxx) share the same product folder.
# Map FHD model name -> 2K folder name for folder lookup.
FHD_TO_2K_ARTIFACTORY_FOLDER: dict[str, str] = {
    "VMC2070": "VMC3070",
    "VMC2083": "VMC3083",
    "VMC2081": "VMC3081",
    "VMC2073": "VMC3073",
}


def _artifactory_folder_for_model(model_name: str) -> str:
    """Return the Artifactory folder name for this model. FHD (2xxx) maps to 2K (3xxx) folder."""
    key = (model_name or "").strip().upper()
    return FHD_TO_2K_ARTIFACTORY_FOLDER.get(key, key)

# Fallback file names when Artifactory folder listing is unavailable or empty.
DEFAULT_BINARY_FILES = ["firmware.bin", "manifest.json", "checksum.txt"]
DEFAULT_UPDATERULE_FILES = ["updateRules.json"]


def _artifactory_api_base(base_url: str) -> str:
    """Return base URL with exactly one /artifactory for API paths (avoid double path)."""
    base = (base_url or "").rstrip("/")
    if not base:
        return base
    if base.endswith("/artifactory"):
        return base
    return f"{base}/artifactory"


def _auth_headers(access_token: str, username: str | None = None) -> dict[str, str]:
    """Headers for Artifactory. If username given, use Basic auth (username + API key); else Bearer + X-JFrog-Art-Api."""
    token = (access_token or "").strip()
    if (username or "").strip():
        creds = base64.b64encode(f"{username.strip()}:{token}".encode()).decode()
        return {"Authorization": f"Basic {creds}"}
    return {
        "Authorization": f"Bearer {token}",
        "X-JFrog-Art-Api": token,
    }


def _get_requests():
    try:
        import requests
        return requests
    except ImportError:
        return None


def _error_message_for_status(r) -> str:
    """Build a user-friendly error message for non-2xx Artifactory responses."""
    status = r.status_code
    try:
        body = r.json()
        msg = body.get("message") or body.get("error") or ""
    except Exception:
        msg = (r.text or "").strip()[:200]
    if status == 401:
        return (
            "HTTP 401 Unauthorized. Artifactory rejected your credentials. "
            "Check that your access token is valid and not expired, and that you are using the correct username (if required). "
            "In Artifactory: User Profile → Edit → Generate API Key / Identity Token."
            + (f" Server: {msg}" if msg else "")
        )
    if status == 403:
        return (
            "HTTP 403 Forbidden. Your credentials are valid but you don't have permission to access this repository. "
            + (f"Server: {msg}" if msg else "")
        )
    if status == 404:
        return f"HTTP 404 Not Found. Path may not exist or repo name may be wrong. {msg}" if msg else "HTTP 404 Not Found."
    return f"HTTP {status}. {msg}" if msg else f"HTTP {status}"


def _artifact_path_for_version(model_name: str, version: str) -> str:
    """Build repo path segment: model/version or ENV/model/version when version is 'ENV/subfolder'.
    Uses 2K folder name for Artifactory (FHD models map to 2K folder)."""
    folder = _artifactory_folder_for_model(model_name)
    if "/" in version:
        env, version_part = version.split("/", 1)
        return f"{ARTIFACTORY_REPO}/{env}/{folder}/{version_part}"
    return f"{ARTIFACTORY_REPO}/{folder}/{version}"


def list_version_files(
    base_url: str,
    access_token: str,
    model_name: str,
    version: str,
    username: str | None = None,
    repo_folder_path: str | None = None,
) -> tuple[bool, list[str], str]:
    """
    List file names in Artifactory. When repo_folder_path is set, list repo/repo_folder_path;
    otherwise repo/model/version or repo/ENV/model/version. Returns (success, list_of_filenames, error_message).
    """
    requests = _get_requests()
    if not requests:
        return False, [], "requests library required for Artifactory download."

    api_base = _artifactory_api_base(base_url)
    if repo_folder_path:
        path = f"{ARTIFACTORY_REPO}/{repo_folder_path.strip('/')}"
    else:
        path = _artifact_path_for_version(model_name, version)
    url = f"{api_base}/api/storage/{path}"
    headers = _auth_headers(access_token, username)
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        ok, data, err = _parse_json_response(r)
        if not ok or data is None:
            return False, [], err or "Invalid response from Artifactory."
        children = data.get("children") or []
        files = []
        for c in children:
            if c.get("folder") is True:
                continue
            uri = (c.get("uri") or "").strip("/")
            if uri:
                files.append(uri)
        return True, files, ""
    except requests.exceptions.RequestException as e:
        return False, [], str(e)
    except Exception as e:
        return False, [], str(e)


def _list_artifactory_children(
    api_base: str,
    repo_path: str,
    headers: dict,
) -> tuple[bool, list[str], list[str], str]:
    """List folder contents in Artifactory. Returns (ok, dir_names, file_names, error)."""
    requests = _get_requests()
    if not requests:
        return False, [], [], "requests library required."
    url = f"{api_base}/api/storage/{repo_path}"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            err_msg = _error_message_for_status(r)
            return False, [], [], err_msg
        ok, data, err = _parse_json_response(r)
        if not ok or data is None:
            return False, [], [], err or "Invalid response."
    except requests.exceptions.RequestException as e:
        return False, [], [], str(e)
    children = data.get("children") or []
    dirs = []
    files = []
    for c in children:
        name = (c.get("uri") or "").strip("/")
        if not name:
            continue
        if c.get("folder") is True:
            dirs.append(name)
        else:
            files.append(name)
    return True, dirs, files, ""


def _search_firmware_aql(
    api_base: str,
    model_folder: str,
    headers: dict,
    version_filter: str,
) -> tuple[list[tuple[str, list[str]]] | None, str]:
    """
    Fast path: one AQL request to list all files under repo/model_folder.
    Returns (results, "") on success, (None, error) on failure. Caller can fall back to recursive walk.
    """
    requests = _get_requests()
    if not requests:
        return None, "requests library required."
    # AQL body is plain text: items.find({"repo":..., "path":...})
    aql_body = (
        f'items.find({{"repo":{{"$eq":"{ARTIFACTORY_REPO}"}},'
        f'"path":{{"$match":"{model_folder}*"}}}})'
    )
    url = f"{api_base}/api/search/aql"
    version_lower = (version_filter or "").strip().lower()
    try:
        r = requests.post(
            url,
            headers=headers,
            data=aql_body,
            timeout=30,
        )
        if r.status_code != 200:
            return None, f"AQL returned {r.status_code}"
        data = r.json()
        runs = data.get("results") or []
        by_path: dict[str, list[str]] = {}
        for item in runs:
            if not isinstance(item, dict):
                continue
            path = (item.get("path") or "").strip()
            name = (item.get("name") or "").strip()
            if not name or item.get("type") == "folder":
                continue
            if version_lower in name.lower() or version_lower in path.lower():
                by_path.setdefault(path, []).append(name)
        if not by_path:
            return [], ""
        results = [(p, sorted(names)) for p, names in sorted(by_path.items())]
        return results, ""
    except requests.exceptions.RequestException as e:
        return None, str(e)
    except (KeyError, TypeError, ValueError) as e:
        return None, str(e)


def find_model_folder(
    base_url: str,
    access_token: str,
    model_name: str,
    username: str | None = None,
) -> tuple[str | None, str]:
    """
    Stage 1: Search camera-fw-generic-release-local for a folder matching the device model.
    Updates progress on a single line with spinner. Returns (repo_relative_path or None, error).
    """
    requests = _get_requests()
    if not requests:
        return None, "requests library required for Artifactory."
    api_base = _artifactory_api_base(base_url)
    headers = _auth_headers(access_token, username)
    repo_path = ARTIFACTORY_REPO
    model_upper = (model_name or "").strip().upper()
    # Artifactory folders are 2K only; FHD (2070/2083/2081/2073) -> use 2K folder (3070/3083/3081/3073)
    folder_to_find = _artifactory_folder_for_model(model_upper)
    spinner_idx = [0]  # use list so inner fn can mutate

    def write_progress(msg: str) -> None:
        sys.stdout.write(f"\r{msg}                    ")
        sys.stdout.flush()

    try:
        ok, dirs, _files, err = _list_artifactory_children(api_base, repo_path, headers)
        if not ok:
            write_progress("")
            sys.stdout.write(f"\r❌ Failed to list Artifactory: {err}\n")
            sys.stdout.flush()
            return None, err
        folders_checked = 0
        for folder in dirs:
            sp = SPINNER_CHARS[spinner_idx[0] % len(SPINNER_CHARS)]
            spinner_idx[0] += 1
            write_progress(f"Searching for {model_name} folder...  {sp} ({folders_checked} folders checked)")
            folders_checked += 1
            if folder_to_find in folder.upper():
                full = f"{repo_path}/{folder}"
                sys.stdout.write(f"\r✓ Found: {full}/\n")
                sys.stdout.flush()
                return folder, ""
        write_progress("")
        sys.stdout.write(
            f"\r❌ Model folder not found for {model_name}\n\n"
            "Possible reasons:\n"
            "  - Model name doesn't match folder name in Artifactory\n"
            "  - Model not in camera-fw-generic-release-local\n"
        )
        sys.stdout.flush()
        return None, f"Model folder not found for {model_name}"
    except requests.exceptions.RequestException as e:
        write_progress("")
        sys.stdout.write(f"\r❌ Failed to connect to Artifactory: {e}\n")
        sys.stdout.flush()
        return None, str(e)


def find_firmware_version_in_model(
    base_url: str,
    access_token: str,
    model_folder: str,
    version_filter: str,
    username: str | None = None,
) -> tuple[list[tuple[str, list[str]]], str]:
    """
    Stage 2: Search inside the model folder for firmware files matching version_filter.
    Tries fast AQL search first (one request); falls back to parallel recursive walk.
    Returns (list of (repo_relative_folder_path, [filenames]), error).
    """
    requests = _get_requests()
    if not requests:
        return [], "requests library required for Artifactory."
    api_base = _artifactory_api_base(base_url)
    headers = _auth_headers(access_token, username)
    version_lower = (version_filter or "").strip().lower()
    base_path = f"{ARTIFACTORY_REPO}/{model_folder}"

    def write_progress(msg: str) -> None:
        sys.stdout.write(f"\r{msg}                    ")
        sys.stdout.flush()

    # Fast path: single AQL request
    aql_results, aql_err = _search_firmware_aql(api_base, model_folder, headers, version_filter)
    if aql_results is not None:
        if aql_results:
            sys.stdout.write(f"\r✓ Found: {len(aql_results)} folder(s) with matching firmware\n")
            sys.stdout.flush()
        return aql_results, ""
    # Fallback: parallel recursive walk (single executor, parallel across folders)
    results: list[tuple[str, list[str]]] = []
    results_lock = Lock()
    max_workers = 10

    def walk(path: str, executor: ThreadPoolExecutor | None) -> None:
        ok, dirs, files, _ = _list_artifactory_children(api_base, path, headers)
        if not ok:
            return
        matching = [f for f in files if version_lower in f.lower()]
        if matching:
            repo_relative = path.replace(f"{ARTIFACTORY_REPO}/", "", 1) if path.startswith(ARTIFACTORY_REPO + "/") else path
            with results_lock:
                results.append((repo_relative, sorted(matching)))
        if not dirs:
            return
        if executor:
            for sub in dirs:
                executor.submit(walk, f"{path}/{sub}", executor)

    try:
        write_progress(f"Searching for version {version_filter}...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executor.submit(walk, base_path, executor)
        if results:
            sys.stdout.write(f"\r✓ Found: {len(results)} folder(s) with matching firmware\n")
            sys.stdout.flush()
        else:
            write_progress("")
            sys.stdout.write(
                f"\r❌ Version {version_filter} not found in {base_path}/\n\n"
                "Possible reasons:\n"
                "  - Version not yet published\n"
                "  - Version number incorrect\n\n"
                "Try: Double-check version number format\n"
            )
            sys.stdout.flush()
        return results, ""
    except requests.exceptions.RequestException as e:
        write_progress("")
        sys.stdout.write(f"\r❌ Failed during search: {e}\n")
        sys.stdout.flush()
        return [], str(e)


def list_available_firmware(
    base_url: str,
    access_token: str,
    version_filter: str,
    model_name: str | list[str],
    username: str | None = None,
) -> tuple[bool, list[tuple[str, list[str]]], str]:
    """
    Two-stage search: find model folder(s), then find firmware version in each.
    model_name can be a single model (e.g. "VMC3081") or a list for 2K + FHD (e.g. ["VMC3073", "VMC2073"]).
    Returns (success, list of (repo_relative_folder_path, matching_filenames), error).
    """
    models_to_search = [model_name] if isinstance(model_name, str) else list(model_name)
    if not models_to_search:
        return False, [], "No model(s) provided for search."
    all_results: list[tuple[str, list[str]]] = []
    last_err = ""
    seen_folders: set[str] = set()  # Artifactory has one folder per 2K; FHD maps to same folder, skip duplicate search
    for m in models_to_search:
        mn = (m or "").strip()
        if not mn:
            continue
        model_folder, err = find_model_folder(base_url, access_token, mn, username)
        if not model_folder:
            last_err = err or f"Model folder not found for {mn}."
            continue
        if model_folder in seen_folders:
            continue
        seen_folders.add(model_folder)
        results, err2 = find_firmware_version_in_model(
            base_url, access_token, model_folder, version_filter, username
        )
        if err2:
            last_err = err2
            continue
        all_results.extend(results)
    if not all_results:
        return False, [], last_err or "No firmware found for any of the given models."
    return True, all_results, ""


def construct_download_url(
    base_url: str,
    repo_path: str,
    environment: str,
    version: str,
) -> str:
    """Build Artifactory URL for environment/version. For Phase 2 real requests."""
    base = base_url.rstrip("/")
    repo = repo_path.strip("/")
    return f"{base}/{repo}/{environment}/{version}/"


def _is_archive_filename(name: str) -> bool:
    """True if filename looks like a firmware archive (.zip or .tar.gz)."""
    n = name.lower()
    if n.endswith(".zip"):
        return True
    return n.endswith(".tar.gz")


def download_firmware(
    access_token: str,
    model_name: str,
    version: str,
    binaries_dir: str,
    updaterules_dir: str,
    base_url: str = "https://artifactory.arlocloud.com",
    username: str | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    files_allowlist: list[str] | None = None,
    repo_folder_path: str | None = None,
    archive_dir: str | None = None,
) -> tuple[bool, str]:
    """
    Download firmware from Artifactory. When archive_dir is set, .zip/.tar.gz files go there;
    otherwise into binaries_dir. UpdateRules.json always goes to updaterules_dir.
    Returns (success, error_message).
    """
    requests = _get_requests()
    if not requests:
        return False, "requests library required for Artifactory download. pip install requests"

    os.makedirs(binaries_dir, exist_ok=True)
    os.makedirs(updaterules_dir, exist_ok=True)
    if archive_dir:
        os.makedirs(archive_dir, exist_ok=True)

    if repo_folder_path:
        ok, file_names, list_err = list_version_files(
            base_url, access_token, model_name, version, username, repo_folder_path=repo_folder_path
        )
        path_prefix = f"{ARTIFACTORY_REPO}/{repo_folder_path.strip('/')}"
    else:
        ok, file_names, list_err = list_version_files(base_url, access_token, model_name, version, username)
        path_prefix = _artifact_path_for_version(model_name, version)
    if not ok or not file_names:
        file_names = list(DEFAULT_BINARY_FILES) + list(DEFAULT_UPDATERULE_FILES)
    if files_allowlist:
        allow_set = set(files_allowlist)
        file_names = [f for f in file_names if f in allow_set]
        if not file_names:
            return False, "Selected file(s) not found in Artifactory folder."
    # Route each file: updateRules.json -> updaterules_dir; archives -> archive_dir if set; else binaries_dir
    to_download: list[tuple[str, str]] = []  # (filename, dest_path)
    for name in file_names:
        if name.lower() == "updaterules.json" or ("updaterule" in name.lower() and name.endswith(".json")):
            to_download.append((name, os.path.join(updaterules_dir, name)))
        elif archive_dir and _is_archive_filename(name):
            to_download.append((name, os.path.join(archive_dir, name)))
        else:
            to_download.append((name, os.path.join(binaries_dir, name)))

    api_base = _artifactory_api_base(base_url)
    headers = _auth_headers(access_token, username)
    total = len(to_download)
    for i, (filename, dest_path) in enumerate(to_download):
        if progress_callback:
            progress_callback(filename, i + 1, total)
        url = f"{api_base}/{path_prefix}/{filename}"
        try:
            r = requests.get(url, headers=headers, stream=True, timeout=60)
            if r.status_code == 404:
                continue  # skip missing file
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        except requests.exceptions.RequestException as e:
            return False, f"Download failed for {filename}: {e}"
        except OSError as e:
            return False, f"Failed to write {filename}: {e}"

    return True, ""


def verify_access_token(base_url: str, token: str) -> tuple[bool, str]:
    """Phase 1: accept any non-empty token. Phase 2: call Artifactory API."""
    if not (token or "").strip():
        return False, "Access token cannot be empty."
    return True, ""
