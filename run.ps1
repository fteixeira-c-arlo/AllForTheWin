# Arlo Camera Terminal - Bootstrap launcher
# Runs the terminal, installing Python (embed) and dependencies if needed.
# Usage: .\run.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$PY_VERSION = "3.12.10"
$PY_EMBED_URL = "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-embed-amd64.zip"
$PY_EMBED_DIR = Join-Path $ScriptDir ".python"
$PY_EXE = Join-Path $PY_EMBED_DIR "python.exe"
$GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
$REQUIREMENTS = Join-Path $ScriptDir "requirements.txt"

function Find-Python {
    # Prefer system Python if it works
    foreach ($cmd in @("python", "py")) {
        try {
            $v = & $cmd -c "import sys; print(sys.executable)" 2>$null
            if ($v) { return $v.Trim() }
        } catch {}
    }
    if (Test-Path $PY_EXE) { return $PY_EXE }
    return $null
}

function Install-EmbeddedPython {
    Write-Host "Downloading Python $PY_VERSION (embed)..." -ForegroundColor Cyan
    $zipPath = Join-Path $ScriptDir "python-embed.zip"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $PY_EMBED_URL -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Host "Download failed. Ensure you have internet. Error: $_" -ForegroundColor Red
        exit 1
    }
    if (Test-Path $PY_EMBED_DIR) { Remove-Item $PY_EMBED_DIR -Recurse -Force }
    Expand-Archive -Path $zipPath -DestinationPath $ScriptDir -Force
    $extractedDir = Join-Path $ScriptDir "python-$PY_VERSION-embed-amd64"
    if (Test-Path $extractedDir) {
        Rename-Item $extractedDir $PY_EMBED_DIR -Force
    }
    Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
    # Enable site-packages (required for pip) in embeddable Python
    $pthFile = Get-ChildItem -Path $PY_EMBED_DIR -Filter "*.pth" | Select-Object -First 1
    if ($pthFile) {
        $content = Get-Content $pthFile.FullName -Raw
        if ($content -notmatch "import site") {
            Add-Content -Path $pthFile.FullName -Value "`nimport site"
        }
    }
    Write-Host "Python installed to $PY_EMBED_DIR" -ForegroundColor Green
}

function Install-PipAndDeps {
    param([string]$PythonExe)
    $getPip = Join-Path $ScriptDir "get-pip.py"
    if (-not (Test-Path $getPip)) {
        Write-Host "Downloading get-pip.py..." -ForegroundColor Cyan
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $GET_PIP_URL -OutFile $getPip -UseBasicParsing
    }
    Write-Host "Installing pip..." -ForegroundColor Cyan
    & $PythonExe $getPip --quiet 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $PythonExe $getPip
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }
    Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Cyan
    & $PythonExe -m pip install -r $REQUIREMENTS -q
    if ($LASTEXITCODE -ne 0) {
        & $PythonExe -m pip install -r $REQUIREMENTS
        if ($LASTEXITCODE -ne 0) { exit 1 }
    }
    Write-Host "Dependencies ready." -ForegroundColor Green
}

# Resolve Python
$pythonExe = Find-Python
if (-not $pythonExe) {
    Install-EmbeddedPython
    $pythonExe = $PY_EXE
}

# Ensure dependencies (pip + requirements)
$hasPip = $false
try {
    & $pythonExe -m pip --version 2>$null
    if ($LASTEXITCODE -eq 0) { $hasPip = $true }
} catch {}
if (-not $hasPip) {
    Install-PipAndDeps -PythonExe $pythonExe
} else {
    # Quick check: can we import rich?
    $ok = $false
    try {
        & $pythonExe -c "import rich, questionary" 2>$null
        if ($LASTEXITCODE -eq 0) { $ok = $true }
    } catch {}
    if (-not $ok) {
        Write-Host "Installing/updating dependencies..." -ForegroundColor Cyan
        & $pythonExe -m pip install -r $REQUIREMENTS -q
    }
}

# Run the terminal
& $pythonExe (Join-Path $ScriptDir "main.py") @args
