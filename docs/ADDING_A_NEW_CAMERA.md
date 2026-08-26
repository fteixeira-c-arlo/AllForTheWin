# Adding a New Camera (Dev/QA **and** Production listings)

This guide explains how to add a new camera (or basestation) to ArloHub so it
appears in **both** device listings of the **Connect to device** dialog:

- the **Dev / QA** tab, and
- the **Production** tab.

> **TL;DR**
> 1. **Register the device** in `core/device_registry.py` (or, for an E3 Wired
>    variant, in `core/camera_models.py`). This alone makes it show up in the
>    **Dev / QA** listing.
> 2. **Add production credentials** in `core/device_credentials.py` (a row with
>    `"stage": "prod"`) for a transport the device supports (**ADB** for cameras,
>    **SSH** for basestations). This is what makes it show up in the
>    **Production** listing.
> 3. (Recommended) add a `"stage": "dev_qa"` credential row too, and wire a
>    command profile so the universal command vocabulary works.

No Python/UI code changes are required — both listings are data-driven.

---

## 1. How the two listings are built

Both listings live in the **Connect to device** dialog
(`interface/gui_window.py`, `MainWindow._open_connect_dialog`). The dialog has
two tabs, added at [gui_window.py:4205](../interface/gui_window.py):

```python
tab_w.addTab(dev_tab,  "Dev / QA")
tab_w.addTab(prod_tab, "Production")
```

Both tabs iterate the **same** catalog returned by `get_models()`
(`core/camera_models.py`), but they filter it differently.

### Dev / QA listing — *every* registered model

The Dev/QA dropdown is populated with the full catalog, unfiltered
([gui_window.py:3755](../interface/gui_window.py)):

```python
models = get_models()
for m in models:
    device_combo.addItem(format_connect_dialog_device_label(m), m)
```

**Rule:** if a model is in the catalog, it appears in **Dev / QA**. Nothing else
is required.

`get_models()` (`core/camera_models.py`) returns:

- the E3 Wired product groups hard-coded in `CAMERA_MODEL_GROUPS`
  ([camera_models.py:19](../core/camera_models.py)), **plus**
- every entry in `DEVICE_REGISTRY`, converted via `registry_entry_to_camera_group()`
  ([camera_models.py:75](../core/camera_models.py) →
  [device_registry.py:211](../core/device_registry.py)).

### Production listing — only *production-capable* models

The Production dropdown is filtered by `_prod_transport_for_model()`
([gui_window.py:4008](../interface/gui_window.py)):

```python
def _prod_transport_for_model(m: dict | None) -> str:
    """Return 'ADB' for ADB-capable prod-credentialed devices,
       'SSH' for SSH-based prod basestations, else ''."""
    if not isinstance(m, dict):
        return ""
    if model_supports_adb(m) and resolve_production_adb_password(m):
        return "ADB"
    if "SSH" in connection_methods_upper(m) and resolve_production_ssh_password(m):
        return "SSH"
    return ""

# ... populate:
for m in models:
    if _prod_transport_for_model(m):
        prod_device_combo.addItem(format_connect_dialog_device_label(m), m)
```

**Rule:** a model appears in **Production** only if **both** are true:

1. it supports a production-capable transport, and
2. a **production credential** exists for that transport in
   `core/device_credentials.py`:

| Device kind | Required transport | Required credential |
|---|---|---|
| Camera | **ADB** (`adb_supported: true` + `"adb"` in `connection_types`) | a `device_credentials.py` row with `transport: "adb"`, `stage: "prod"` (or `"all"`) |
| Basestation / gateway | **SSH** (`"ssh"` in `connection_types`) | a `device_credentials.py` row with `transport: "uart_ssh"`, `stage: "prod"` (or `"all"`) |

The credential lookups are `resolve_production_adb_password()`
([device_credentials.py:249](../core/device_credentials.py)) and
`resolve_production_ssh_password()`
([device_credentials.py:293](../core/device_credentials.py)); both try the model's
primary `name` and every `fw_search_models` id against `stage="prod"` rows.

> ⚠️ **UART-only devices can never appear in Production.** The production connect
> flow needs either ADB (USB) or network SSH. A camera that lists only
> `connection_types: ["uart"]` will show in Dev/QA but be silently excluded from
> Production, even if it has a prod credential row.

---

## 2. Step-by-step

### Step 1 — Register the device → appears in **Dev / QA**

Most cameras and all basestations are registered in
`core/device_registry.py` by appending a `DeviceRegistryEntry` to
`DEVICE_REGISTRY` ([device_registry.py:33](../core/device_registry.py)).

```python
{
    "model_ids": ["VMC5040", "VMC5040P"],   # every SKU that reports this device
    "codename": "hawk",
    "display_name": "Arlo Pro 7 (Hawk)",
    "platform": "linux",                     # amebapro2 | gen5 | linux | openwrt_qca
    "connection_types": ["uart", "ssh", "adb"],  # priority order; MUST include adb or ssh for Production
    "adb_supported": True,
    "adb_auth_password": "arlo",             # optional ADB shell auth hint
    "uart_baudrate": 115200,
    "enable_debug_method": "sync_button_6x",
    "command_profile": "linux_kealory",      # or "none" if no CLI catalog yet
},
```

Field notes:

- **`model_ids`** — list every model number the device may report. Lookups
  (credentials, firmware search) match against all of them.
- **`connection_types`** — to be **Production-eligible**, include `"adb"`
  (cameras) or `"ssh"` (basestations). `"uart"` alone is Dev/QA-only.
- **`adb_supported`** — must be `True` for the ADB production path
  (`model_supports_adb()` returns `False` when this is `False`).
- **`device_kind`** — usually inferred. `openwrt_qca` platform or a `VMB*`
  model id ⇒ `"basestation"`; otherwise `"camera"`
  (`get_device_kind()`, [device_registry.py:179](../core/device_registry.py)).
  Set it explicitly only to override the inference.
- **`command_profile`** — see [Step 3](#step-3--optional-wire-the-command-vocabulary).

> **E3 Wired variants only:** if your new SKU is another Arlo Essential 3 *wired*
> product, add it to `CAMERA_MODEL_GROUPS` in `core/camera_models.py`
> ([camera_models.py:19](../core/camera_models.py)) instead, following the
> existing entries (`platform: "e3_wired"`). Everything else goes in the registry.

At this point the device is already selectable in the **Dev / QA** tab.

### Step 2 — Add credentials → appears in **Production**

Add one or more `CredentialRecord` entries to `DEVICE_CREDENTIALS` in
`core/device_credentials.py` ([device_credentials.py:21](../core/device_credentials.py)).
You need at least a **`prod`** row for the device's production transport, and
you should also add a **`dev_qa`** row so Dev/QA connections auto-fill the
password.

**Camera (ADB) example:**

```python
{
    "model_ids": ["VMC5040", "VMC5040P"],
    "stage": "dev_qa",           # dev_qa | prod | all
    "transport": "adb",          # uart_ssh | adb | uboot
    "username": "",
    "password": "arlo",
    "note": "Hawk Dev/QA ADB shell auth",
},
{
    "model_ids": ["VMC5040", "VMC5040P"],
    "stage": "prod",             # <-- this row unlocks the Production listing
    "transport": "adb",
    "username": "",
    "password": "<prod-adb-shell-password>",
    "note": "Hawk Production ADB shell auth",
},
```

**Basestation (SSH) example:**

```python
{
    "model_ids": ["VMB6000"],
    "stage": "dev_qa",
    "transport": "uart_ssh",
    "username": "root",
    "password": "ngbase",
    "note": "New SmartHub Dev/QA UART/SSH",
},
{
    "model_ids": ["VMB6000"],
    "stage": "prod",             # <-- this row unlocks the Production listing
    "transport": "uart_ssh",
    "username": "root",
    "password": "<prod-ssh-root-password>",
    "note": "New SmartHub Production UART/SSH",
},
```

Notes:

- `stage` must be one of `dev_qa`, `prod`, or `all`. An `all` row satisfies both
  environments (used e.g. for U-Boot and Kea ADB).
- `transport` must be `adb` for the camera production path or `uart_ssh` for the
  SSH production path. A `uboot` row does **not** count toward the Production
  listing.
- Use the real lab passwords per environment. **Never invent a value** — leave a
  clear `note` and coordinate with the firmware/lab owners if you don't have the
  production secret yet (the device simply won't list under Production until a
  real prod credential is present).

### Step 3 — (Optional) Wire the command vocabulary

Adding the device to the listings does **not** by itself give it the universal
`noun verb` commands (`version`, `factory reset`, `update url`, …). To enable
those, point the device at a **command profile**:

1. Reuse an existing profile by setting `"command_profile"` on the registry
   entry (e.g. `"linux_kealory"`, `"gen5"`, `"amebapro2"`), **or**
2. Create a new profile: add `core/<device>_commands.json`, register it in
   `core/command_profiles.json`, and reference its id.

Full details: [`core/HOW_TO_ADD_DEVICE_COMMANDS.txt`](../core/HOW_TO_ADD_DEVICE_COMMANDS.txt).
Use `"command_profile": "none"` if there's no CLI catalog yet (the device still
lists and connects; only device-specific commands are unavailable).

### Step 4 — Firmware repo (automatic)

Firmware search/download automatically picks the right Artifactory repo from the
device kind (`resolve_repo_for_model()`,
[artifactory_client.py:51](../core/artifactory_client.py)):

- cameras → `camera-fw-generic-release-local`
- basestations/gateways → `gateway-fw-generic-release-local`

So as long as `device_kind` resolves correctly (Step 1), no firmware wiring is
needed. The **firmware environment** (dev/qa/prod folders) is a *separate axis*
chosen inside the FW Wizard / local-server tools per download — it is not part
of the device listing.

### Step 5 — Verify

1. `python main_gui.py`
2. Open **Connect to device**.
3. **Dev / QA** tab: your device is in the dropdown.
4. **Production** tab: your device is in the dropdown (if Step 2's `prod`
   credential + Step 1's ADB/SSH transport are both present).
5. Optional smoke test of the label/ids:

   ```bash
   python -c "from core.camera_models import get_models; print([m['name'] for m in get_models()])"
   python -c "from core.device_credentials import resolve_production_adb_password, resolve_production_ssh_password; from core.camera_models import get_model_by_name; m=get_model_by_name('VMC5040'); print('ADB', resolve_production_adb_password(m), '| SSH', resolve_production_ssh_password(m))"
   ```

---

## 3. Worked example — new camera "Hawk" (VMC5040)

**`core/device_registry.py`** — append to `DEVICE_REGISTRY`:

```python
{
    "model_ids": ["VMC5040", "VMC5040P"],
    "codename": "hawk",
    "display_name": "Arlo Pro 7 (Hawk)",
    "platform": "linux",
    "connection_types": ["uart", "ssh", "adb"],
    "adb_supported": True,
    "adb_auth_password": "arlo",
    "uart_baudrate": 115200,
    "enable_debug_method": "sync_button_6x",
    "command_profile": "linux_kealory",
},
```

**`core/device_credentials.py`** — append to `DEVICE_CREDENTIALS`:

```python
{
    "model_ids": ["VMC5040", "VMC5040P"],
    "stage": "dev_qa", "transport": "adb",
    "username": "", "password": "arlo",
    "note": "Hawk Dev/QA ADB shell auth",
},
{
    "model_ids": ["VMC5040", "VMC5040P"],
    "stage": "prod", "transport": "adb",
    "username": "", "password": "<prod-adb-shell-password>",
    "note": "Hawk Production ADB shell auth",
},
```

Result: **Hawk** now appears in **both** the Dev/QA and Production listings, uses
the `linux_kealory` command vocabulary, and pulls firmware from the camera repo.

---

## 4. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Device missing from **both** tabs | Not registered | Add it to `DEVICE_REGISTRY` (or `CAMERA_MODEL_GROUPS` for E3 Wired). |
| Shows in **Dev / QA** but **not Production** | No `stage: "prod"` credential for a supported transport | Add a `prod` row in `device_credentials.py` with `transport: "adb"` (camera) or `"uart_ssh"` (basestation). |
| Shows in Dev/QA, has a prod credential, still not in Production | Transport mismatch: device is UART-only, or `adb_supported: false`, or credential `transport` is `uboot` | Add `"adb"`/`"ssh"` to `connection_types` (and `adb_supported: true` for ADB); use `transport: "adb"`/`"uart_ssh"` on the prod row. |
| In Production but "No production credentials configured" on connect | `prod` row `password` is empty | Provide the real prod password (empty passwords are ignored by `resolve_production_*`). |
| Connects but no device commands | `command_profile` is `"none"` or unmapped | Wire a profile per `core/HOW_TO_ADD_DEVICE_COMMANDS.txt`. |
| Firmware search hits the wrong repo | `device_kind` misclassified | Set `device_kind` explicitly, or verify the `VMB*` / `openwrt_qca` heuristic. |

---

## 5. File reference map

| Concern | File | Symbol |
|---|---|---|
| Device catalog (both listings) | `core/camera_models.py` | `CAMERA_MODEL_GROUPS`, `get_models()` |
| Registry (most devices) | `core/device_registry.py` | `DEVICE_REGISTRY`, `registry_entry_to_camera_group()` |
| Camera vs basestation | `core/device_registry.py` | `get_device_kind()`, `is_basestation_model()` |
| Credentials (Production gate) | `core/device_credentials.py` | `DEVICE_CREDENTIALS`, `resolve_production_adb_password()`, `resolve_production_ssh_password()` |
| Connect dialog (the two tabs) | `interface/gui_window.py` | `_open_connect_dialog()`, `_prod_transport_for_model()` |
| Firmware repo selection | `core/artifactory_client.py` | `resolve_repo_for_model()` |
| Command vocabulary | `core/command_profiles.json`, `core/<device>_commands.json` | see `core/HOW_TO_ADD_DEVICE_COMMANDS.txt` |
