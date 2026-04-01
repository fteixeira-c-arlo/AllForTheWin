# PyInstaller spec — build: pyinstaller arlo_camera_terminal.spec
# Produces dist/ArloCameraTerminal/ with ArloCameraTerminal.exe (console app; Rich + questionary UI).

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

block_cipher = None

_project_root = Path(SPEC).resolve().parent

datas = [
    (str(_project_root.joinpath("commands", "e3_wired_commands.json")), "commands"),
    (str(_project_root.joinpath("commands", "command_profiles.json")), "commands"),
]
binaries = []
hiddenimports = []

for pkg in ("rich", "questionary", "prompt_toolkit"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "paramiko",
    "cryptography",
    "cryptography.hazmat.backends.openssl.backend",
    "serial",
    "requests",
]

a = Analysis(
    [str(_project_root.joinpath("main.py"))],
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

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ArloCameraTerminal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
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
    name="ArloCameraTerminal",
)
