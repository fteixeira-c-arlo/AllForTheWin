# Builds the Windows GUI app with PyInstaller, then compiles a single-file installer with Inno Setup 6.
# Writes release\ArloCameraControl-Windows\Install-ArloCameraControl.exe — zip that folder for testers.
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

Write-Host "=== Arlo Camera Control — build ===" -ForegroundColor Cyan

$req = Join-Path $Root "requirements.txt"
$spec = Join-Path $Root "arlo_camera_control_gui.spec"

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
if (-not (Test-Path (Join-Path $Root "dist\ArloCameraControl\ArloCameraControl.exe"))) {
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

$distExe = Join-Path $Root "dist\ArloCameraControl\ArloCameraControl.exe"
if (-not (Test-Path $distExe)) {
    Write-Host "Expected EXE not found: $distExe" -ForegroundColor Red
    exit 1
}
Write-Host "Built: $distExe" -ForegroundColor Green

$isccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
)
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $iscc) {
    Write-Host ""
    Write-Host "Inno Setup 6 not found. Skipping single-file installer." -ForegroundColor Yellow
    Write-Host "To create ArloCameraControl-Setup.exe for end users:" -ForegroundColor Yellow
    Write-Host "  1. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php" -ForegroundColor Yellow
    Write-Host "  2. Run: `"$($isccCandidates[0])`" `"$Root\installer\ArloCameraControl.iss`"" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "You can still ship the folder dist\ArloCameraControl\ as a ZIP (run ArloCameraControl.exe inside)." -ForegroundColor Yellow
    exit 0
}

$releaseDir = Join-Path $Root "release\ArloCameraControl-Windows"
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null

$iss = Join-Path $Root "installer\ArloCameraControl.iss"
Write-Host "Compiling installer with Inno Setup..." -ForegroundColor Cyan
& $iscc $iss
if ($LASTEXITCODE -ne 0) {
    Write-Host "ISCC failed." -ForegroundColor Red
    exit $LASTEXITCODE
}

$setup = Join-Path $releaseDir "Install-ArloCameraControl.exe"
if (Test-Path $setup) {
    Write-Host ""
    Write-Host "=== Done ===" -ForegroundColor Green
    Write-Host "Tester package folder: $releaseDir" -ForegroundColor Green
    Write-Host "  • Zip this folder and send it — they only run Install-ArloCameraControl.exe" -ForegroundColor Green
    Write-Host "  • Commit release\ArloCameraControl-Windows (Install-*.exe) if you use git for handoff" -ForegroundColor Green
} else {
    Write-Host "Installer not found at expected path: $setup" -ForegroundColor Yellow
}
