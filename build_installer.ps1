# Builds the Windows GUI app with PyInstaller, then compiles a single-file installer with Inno Setup 6.
# Writes release\ArloHub-Windows\Install-ArloHub.exe - zip that folder for testers.
#
# Prerequisites (machine that BUILDS the installer):
#   - Python 3.10+ on PATH
#   - pip install -r requirements.txt
#   - Inno Setup 6: https://jrsoftware.org/isinfo.php (optional; PyInstaller output still runs without it)
#
# Usage (from repo root):  powershell -ExecutionPolicy Bypass -File .\build_installer.ps1

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

Write-Host "=== ArloHub - build ===" -ForegroundColor Cyan

$req = Join-Path $Root "requirements.txt"
$spec = Join-Path $Root "ArloHub.spec"

# Read version from utils/version.py (single source of truth) and pass to Inno Setup.
$versionFile = Join-Path $Root "utils\version.py"
$AppVersion = "0.0.0"
if (Test-Path $versionFile) {
    $line = Get-Content $versionFile | Where-Object { $_ -match '^\s*__version__\s*=' } | Select-Object -First 1
    if ($line -and $line -match '"([^"]+)"') {
        $AppVersion = $matches[1]
    }
}
Write-Host "App version: $AppVersion" -ForegroundColor Gray

if (Get-Command py -ErrorAction SilentlyContinue) {
    py -3 -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Using: py -3" -ForegroundColor Gray
        Write-Host "Installing dependencies..." -ForegroundColor Cyan
        py -3 -m pip install -r $req -q
        Write-Host "Running PyInstaller (this may take several minutes)..." -ForegroundColor Cyan
        py -3 -m PyInstaller --clean --noconfirm $spec
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
if (-not (Test-Path (Join-Path $Root "dist\ArloHub\ArloHub.exe"))) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Write-Host "Python not found. Install Python 3.10+ and add 'py' or 'python' to PATH." -ForegroundColor Red
        exit 1
    }
    Write-Host "Using: python" -ForegroundColor Gray
    Write-Host "Installing dependencies..." -ForegroundColor Cyan
    python -m pip install -r $req -q
    Write-Host "Running PyInstaller (this may take several minutes)..." -ForegroundColor Cyan
    python -m PyInstaller --clean --noconfirm $spec
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$distExe = Join-Path $Root "dist\ArloHub\ArloHub.exe"
if (-not (Test-Path $distExe)) {
    Write-Host "Expected EXE not found: $distExe" -ForegroundColor Red
    exit 1
}
Write-Host "Built: $distExe" -ForegroundColor Green

# --- Smoke test: launch the freshly built EXE and verify it survives startup. ---
# Catches PyInstaller bundling regressions (e.g. missing yaml/.py files behind
# yaml/_yaml.pyd) BEFORE the installer is wrapped and shipped.
# The app's _fatal_startup() writes _internal\arlohub_last_error.txt next to
# the bundled scripts; presence of that file after launch == failed startup.
$bundleDir = Split-Path -Parent $distExe
$smokeErrFile = Join-Path $bundleDir "_internal\arlohub_last_error.txt"
$smokeWaitSec = 8

Write-Host "Smoke test: launching ArloHub.exe (waiting ${smokeWaitSec}s)..." -ForegroundColor Cyan
if (Test-Path $smokeErrFile) { Remove-Item $smokeErrFile -Force }

$smokeProc = $null
try {
    $smokeProc = Start-Process -FilePath $distExe -PassThru -ErrorAction Stop
} catch {
    Write-Host "Smoke test FAILED: could not launch $distExe — $_" -ForegroundColor Red
    exit 1
}

$smokeDeadline = (Get-Date).AddSeconds($smokeWaitSec)
while ((Get-Date) -lt $smokeDeadline -and -not $smokeProc.HasExited) {
    Start-Sleep -Milliseconds 250
}

$smokeCrashed = Test-Path $smokeErrFile
$smokeExitedEarly = $smokeProc.HasExited
$smokeExitCode = if ($smokeProc.HasExited) { $smokeProc.ExitCode } else { $null }

# Kill the GUI even if it survived; rebuild script must not block.
if (-not $smokeProc.HasExited) {
    Stop-Process -Id $smokeProc.Id -Force -ErrorAction SilentlyContinue
}
Get-Process -Name "ArloHub" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

if ($smokeCrashed) {
    Write-Host "Smoke test FAILED: app wrote $smokeErrFile during startup." -ForegroundColor Red
    Write-Host "--- arlohub_last_error.txt ---" -ForegroundColor Red
    Get-Content $smokeErrFile | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host "------------------------------" -ForegroundColor Red
    Write-Host "Refusing to wrap the installer around a broken build." -ForegroundColor Red
    exit 1
}
if ($smokeExitedEarly -and $smokeExitCode -ne 0) {
    Write-Host "Smoke test FAILED: ArloHub.exe exited early with code $smokeExitCode (no traceback file — likely a bootloader / DLL load failure)." -ForegroundColor Red
    exit 1
}
Write-Host "Smoke test passed." -ForegroundColor Green

$isccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Host ""
    Write-Host "Inno Setup 6 not found. Skipping single-file installer." -ForegroundColor Yellow
    Write-Host "To create ArloHub-Setup.exe for end users:" -ForegroundColor Yellow
    Write-Host "  1. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php" -ForegroundColor Yellow
    Write-Host "  2. Run: `"$($isccCandidates[0])`" `"$Root\installer\ArloCameraControl.iss`"" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "You can still ship the folder dist\ArloHub\ as a ZIP (run ArloHub.exe inside)." -ForegroundColor Yellow
    exit 0
}

$releaseDir = Join-Path $Root "release\ArloHub-Windows"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

$iss = Join-Path $Root "installer\ArloCameraControl.iss"
Write-Host "Compiling installer with Inno Setup (version $AppVersion)..." -ForegroundColor Cyan
& $iscc "/DMyAppVersion=$AppVersion" $iss
if ($LASTEXITCODE -ne 0) {
    Write-Host "ISCC failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

$setup = Join-Path $releaseDir "Install-ArloHub.exe"
if (Test-Path $setup) {
    Write-Host ""
    Write-Host "=== Done ===" -ForegroundColor Green
    Write-Host "Tester package folder: $releaseDir" -ForegroundColor Green
    Write-Host "  * Zip this folder and send it - they only run Install-ArloHub.exe" -ForegroundColor Green
    Write-Host "  * Commit release\ArloHub-Windows (Install-*.exe) if you use git for handoff" -ForegroundColor Green
} else {
    Write-Host "Installer not found at expected path: $setup" -ForegroundColor Yellow
}
