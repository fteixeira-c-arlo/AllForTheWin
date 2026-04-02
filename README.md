# ArloShell

**One tool. Any Arlo camera. One syntax.**

ArloShell is a developer tool for communicating with Arlo cameras over ADB, SSH, and UART. Every camera model has its own command syntax — ArloShell abstracts that away with a single, consistent command vocabulary that works regardless of which device is connected. You type the same command every time. ArloShell handles the translation.

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

ArloShell uses a universal `noun verb` syntax. The same command works on every supported device — ArloShell translates it to the correct shell string at runtime.

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

Output: `dist/ArloShell/ArloShell.exe`

---

## License

Internal Arlo developer tool. Not for public distribution.
