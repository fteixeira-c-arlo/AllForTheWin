# ArloHub

**One tool. Any Arlo camera. One syntax.**

ArloHub is a developer tool for communicating with Arlo cameras over ADB, SSH, and UART. Every camera model has its own command syntax — ArloHub abstracts that away with a single, consistent command vocabulary that works regardless of which device is connected. You type the same command every time. ArloHub handles the translation.

---

## Features

- **Universal command vocabulary** — `update url`, `migrate`, `factory reset`, `version` work the same across all supported devices
- **Multi-transport support** — connect over ADB (USB), SSH, or UART
- **Auto-detection** — device model, firmware version, and environment are detected on connect
- **GUI + terminal** — full graphical interface with a persistent command session
- **Firmware tools** — Artifactory integration, local HTTP server, OTA flashing
- **Log tools** — live log streaming, log parsing, HTML report generation
- **Multi-device architecture** — add a new device via JSON only, no code changes required

---

## Project Structure

```
arlo-shell/
├── core/           # Command catalog, abstract dispatcher, Artifactory client, local server
├── transports/     # ADB, SSH, and UART connection handlers
├── interface/      # GUI window, menus, prompts
├── utils/          # Config manager, validators, logger
├── docs/           # Development and build documentation
├── installer/      # Inno Setup installer script
├── main_gui.py     # Application entry point
├── requirements.txt
└── build_installer.ps1
```

---

## Getting Started

**Requirements:** Python 3.10+

```bash
git clone https://github.com/<your-username>/arlo-shell.git
cd arlo-shell
pip install -r requirements.txt
python main_gui.py
```

**For ADB connections:** Install [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) and add `adb` to your PATH.

---

## Command Vocabulary

ArloHub uses a universal `noun verb` syntax. The same command works on every supported device — ArloHub translates it to the correct shell string at runtime.

### Firmware
| Command | Description |
|---|---|
| `update url <url>` | Set the firmware update URL and reboot |
| `update url get` | Read the current update URL |
| `update check` | Check for available update (notify only) |
| `update apply` | Force update check and install |
| `migrate <stage>` | Migrate to environment (`dev` / `qa` / `prod` / `ftrial`) and reboot |
| `flash` | FW Wizard flow via Artifactory download and local HTTP server (same as `fw_wizard`; no arguments) |
| `fw_wizard` | FW Wizard (CLI prompts): Artifactory download, local server, set `update_url` (same flow as **Tools → FW Wizard**) |

### Device
| Command | Description |
|---|---|
| `version` | Show firmware and build info |
| `info` | Show device and basestation info |
| `reboot` | Reboot the device |
| `factory reset` | Archive logs then factory reset |
| `serial` | Read device serial number |

### Logs
| Command | Description |
|---|---|
| `log level <level>` | Set log level (`trace` / `debug` / `info` / `warn` / `error`) |
| `log tail` | Stream live system log |
| `log tail stop` | Stop log streaming |
| `log parse` | Stream and parse logs live |
| `log parse stop` | Stop parsing and save HTML report |
| `log save` | Archive logs on device |
| `log pull [path]` | Archive and download logs to PC |
| `log export` | Upload logs via TFTP (UART only) |

### Manufacturing
| Command | Description |
|---|---|
| `mfg get <field>` | Read calibration field (`sn`, `mac`, `model_num`, etc.) |
| `mfg set <field> <value>` | Write calibration field |
| `mfg build` | Show full build info |

### Tools
| Command | Description |
|---|---|
| `fw local` | Start local firmware server and set camera URL |
| `server stop` | Stop the local firmware HTTP server |
| `server status` | Check firmware server status |
| `config show` | Show saved Artifactory credentials |
| `config update` | Update Artifactory credentials |
| `config delete` | Delete saved credentials |

---

## Adding a New Device

No Python changes required. Add a JSON file and register it:

1. Create `core/<device>_commands.json` — same schema as `core/e3_wired_commands.json`
2. For each command that maps to a universal abstract command, add `"abstract": "<command name>"`
3. Register it in `core/command_profiles.json`
4. Set `"command_profile": "<your_profile>"` in `models/camera_models.py` for the relevant models

See `core/HOW_TO_ADD_DEVICE_COMMANDS.txt` for full details.

---

## Currently Supported Devices

| Device | Profile | Transports |
|---|---|---|
| E3 Wired (VMC3070, VMC3073, VMC3081, VMC3083 + FHD variants) | `e3_wired` | ADB, SSH, UART |

---

## Building the Installer

```powershell
pip install -r requirements.txt
.\build_installer.ps1
```

Output: `dist/ArloHub/ArloHub.exe`

---

## Releases & Auto-Update

ArloHub ships an in-app updater that polls GitHub Releases on startup and from `Help → Check for updates…`. When a newer version is published on the user's channel, a dialog shows release notes plus "Install now" — the app downloads `Install-ArloHub.exe`, verifies its SHA256, runs it silently, and relaunches itself on the new version. Clicking "Later" suppresses the prompt for that same version for 24 hours.

### Channels

Three release streams keep production testers isolated from experimental builds. Set the channel from `Help → Update channel`. It's persisted in `%LOCALAPPDATA%/ArloHub/updater.json`.

| Channel | Tag pattern | GitHub flag | Audience |
|---|---|---|---|
| `stable` (default) | `v1.0.5` | regular release | production testers |
| `beta` | `v1.0.5-beta.1` | prerelease | early-access testers |
| `dev` | `v1.0.5-dev.1` | prerelease | internal/dogfood |

`stable` clients query `/releases/latest` (which excludes prereleases by definition). `beta`/`dev` clients list all releases and pick the highest tag matching their channel pattern. A user on `stable` will never see `-beta`/`-dev` builds.

### Cutting a release

Tag locally and push — the GitHub Action does the rest:

```bash
# Stable
git tag v1.0.5 && git push origin v1.0.5

# Beta (early access)
git tag v1.0.5-beta.1 && git push origin v1.0.5-beta.1

# Dev (dogfood)
git tag v1.0.5-dev.1 && git push origin v1.0.5-dev.1
```

The workflow:
1. Parses the tag → derives version + channel + prerelease flag
2. Rewrites `utils/version.py` from the tag
3. Builds the installer via `build_installer.ps1`
4. Computes SHA256, writes `latest.json` (with `channel` field)
5. Publishes the GitHub Release, marked `prerelease=true` for beta/dev

### Disabling the updater locally

Set `ARLOHUB_NO_UPDATE_CHECK=1` to skip both the startup check and the manual menu (useful when QA-ing against an old release).

### Code signing (optional)

Builds are unsigned. SmartScreen / Defender may show a warning on first download. To enable Authenticode signing:

1. Buy a code-signing certificate (.pfx) from a trusted CA
2. Store it as a base64 GitHub secret named `SIGNING_CERT_PFX_BASE64` plus the password as `SIGNING_CERT_PASSWORD`
3. Add a step before "Generate latest.json manifest" in `.github/workflows/release.yml`:
   ```yaml
   - name: Sign installer
     shell: pwsh
     run: |
       $bytes = [Convert]::FromBase64String($env:CERT_B64)
       Set-Content -Path cert.pfx -Value $bytes -AsByteStream
       & "$env:ProgramFiles(x86)\Windows Kits\10\bin\x64\signtool.exe" sign `
         /f cert.pfx /p $env:CERT_PASS /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
         release\ArloHub-Windows\Install-ArloHub.exe
       Remove-Item cert.pfx
     env:
       CERT_B64: ${{ secrets.SIGNING_CERT_PFX_BASE64 }}
       CERT_PASS: ${{ secrets.SIGNING_CERT_PASSWORD }}
   ```

### Components
- `utils/version.py` — single source of truth for the running version (re-exported as `core.app_metadata.APP_VERSION`)
- `core/updater.py` — channel-aware fetch, SHA256-verified download
- `core/updater_config.py` — channel preference + remind-later state (`%LOCALAPPDATA%/ArloHub/updater.json`)
- `interface/update_dialog.py` — PySide6 dialog shown when an update is available
- `installer/ArloCameraControl.iss` — `[Run]` entry with `skipifnotsilent` relaunches the app after silent install

---

## License

Internal Arlo developer tool. Not for public distribution.
