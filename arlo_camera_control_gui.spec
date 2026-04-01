# PyInstaller spec — GUI (PySide6)
# Build: pyinstaller --clean --noconfirm arlo_camera_control_gui.spec
# Output: dist/ArloCameraControl/ArloCameraControl.exe
#
# Do NOT use collect_all("PySide6"): that bundles every Qt module (WebEngine, QML, 3D, …)
# and inflates the folder to ~700MB+. This app only needs QtCore / QtGui / QtWidgets.

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

_project_root = Path(SPEC).resolve().parent

datas = [
    (str(_project_root / "commands" / "e3_wired_commands.json"), "commands"),
    (str(_project_root / "commands" / "command_profiles.json"), "commands"),
    (str(_project_root / "docs" / "e3_wired_cli_reference.md"), "docs"),
]
binaries = []
hiddenimports = []

# Terminal stack only — small compared to Qt.
for pkg in ("rich", "questionary", "prompt_toolkit"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += [
    "shiboken6",
    "paramiko",
    "cryptography",
    "cryptography.hazmat.backends.openssl.backend",
    "serial",
    "requests",
    "urllib3",
    "certifi",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

a = Analysis(
    [str(_project_root / "main_gui.py")],
    pathex=[str(_project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)


def _trim_qt_translation_qm(analysis_datas):
    """Keep English Qt .qm files only; drop other locales to save space."""
    out = []
    for entry in analysis_datas:
        src = entry[1] if len(entry) > 1 else None
        if src is None:
            out.append(entry)
            continue
        path = str(src).replace("\\", "/")
        if "/translations/" in path and path.lower().endswith(".qm"):
            if "en" not in Path(src).name.lower():
                continue
        out.append(entry)
    return out


a.datas = _trim_qt_translation_qm(a.datas)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ArloCameraControl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ArloCameraControl",
)
