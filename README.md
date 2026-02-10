# Arlo Camera Control Terminal

Interactive terminal application for connecting to and controlling Arlo camera devices. Hackathon demo (Phase 1).

## Requirements

- Python 3.10+ (optional — the launcher can download it for you)
- For ADB: [Android SDK platform-tools](https://developer.android.com/studio/releases/platform-tools) with `adb` on your PATH (optional; connection will fail with a clear message if missing)
- For SSH: no extra tools; uses Paramiko

## Quick start (no Python or dependencies installed)

On **Windows**, use the launcher so Python and dependencies are set up automatically:

1. Open the `arlo-camera-terminal` folder.
2. Double-click **`run.bat`** (or in a terminal: `run.bat` or `powershell -File run.ps1`).

The first run will download a portable Python and install the required packages; later runs start the terminal immediately.

## Setup (manual)

If you already have Python installed:

```bash
cd arlo-camera-terminal
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

Or use **`run.bat`** / **`run.ps1`** so dependencies are installed automatically if missing.

## Demo Flow

1. On startup, the **models list** is shown (E3 Wired: VMC2070, VMC3070, VMC2083, VMC3083, VMC3081, VMC2081, VMC3073, VMC2073).
2. **Select a model** using the interactive prompt: use **arrow keys** to move or **type** to search/filter, then Enter to choose.
3. Choose connection method: **ADB** or **SSH** (or Back to cancel).
4. Enter connection parameters:
   - **ADB:** Connect the camera via USB first. You will be prompted for the **ADB shell auth password** only (no IP). The app runs `adb shell auth`, sends the password, then uses `adb shell` to run commands.
   - **SSH:** IP address, port (default 22), username, password.
5. After a successful connection, the prompt changes to `VMC3070>` (or the selected model). Type **help** to see commands.
6. Try device commands (e.g. **build_info**, **reboot**, **tar_logs**, **pull_logs** for E3 Wired) or **help** for the full list.
7. Type **disconnect** or **exit** to close the connection and exit. Type **back** to disconnect and return to model selection (models list is shown again).

### Example (no real device)

- Select a model from the list (arrows or type to filter) → choose **SSH** → enter any IP (e.g. `192.168.1.1`), port `22`, user `root`, password `test`. Connection will fail (e.g. timeout or refused); you can retry or cancel.
- For ADB: connect one camera via USB, enable USB debugging, then run the app and enter the device's shell auth password when prompted.
- For a successful demo without hardware, use SSH to a host that accepts the credentials.

## Confluence / MCP Integration

**E3 Wired (VMC2070, VMC3070, VMC2083, VMC3083, VMC2081, VMC3081, VMC2073, VMC3073):** The available command list (including **kvcmd** for key-value config) is loaded from `commands/e3_wired_commands.json`, populated from Arlo Confluence via arlochat MCP. Source: [Arlo E3 Wired - How to use CLI on Console](https://arlo.atlassian.net/wiki/spaces/AFS/pages/63449494/Arlo+E3+Wired+-+How+to+use+CLI+on+Console) and kvcmd docs. When you connect an E3 Wired camera (ADB, SSH, or UART), the terminal shows these CLI commands (e.g. `build_info`, `reboot`, `migrate`, `ptz`, `caliget`/`caliset`, `kvcmd`, `kvcmd_get`, `kvcmd_set`, `kvcmd_list`, `kvcmd_delete`). To refresh from Confluence, use arlochat MCP in Cursor with `confluence_search(cql='title ~ "E3 Wired" AND title ~ "CLI"')` and `confluence_search(cql='text ~ "kvcmd"')`, then update `e3_wired_commands.json` if needed.

**Other models:** Placeholder commands are used. The terminal app does not call MCP at runtime; MCP is used from Cursor to fetch Confluence content.

### Updating the E3 Wired models list from Confluence

Arlochat can fetch E3 Wired model info from Confluence so you can keep `models/camera_models.py` in sync. In Cursor, with the **user-arlochat** MCP server enabled, use the `confluence_search` tool with CQL. Example queries to try:

- **By product name:** `confluence_search(cql='text ~ "E3 Wired"')`
- **By model prefix:** `confluence_search(cql='text ~ "E3" AND text ~ "VMC"')`
- **By title:** `confluence_search(cql='title ~ "E3"')`

Then open the returned Confluence page(s), copy the list of E3 Wired model names/codes (e.g. VMC2070, VMC3070, …), and update the `CAMERA_MODELS` list in `models/camera_models.py`. Each entry needs: `name`, `display_name`, `supported_connections`, and `default_settings` (adb port, ssh port/username). If Confluence returns "fetch failed", the MCP or Confluence connection may be down; retry later or use Confluence in the browser and edit `camera_models.py` manually.

## Project Structure

- `main.py` – Entry point, main loop, connection flow.
- `models/` – Camera model definitions and connection config.
- `connections/` – ADB and SSH handlers.
- `commands/` – Command definitions (MCP integration point) and parser.
- `ui/` – Menus and prompts (rich, questionary).
- `utils/` – Validators and logger.

## License

Internal hackathon demo.
