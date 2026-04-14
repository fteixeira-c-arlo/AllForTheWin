@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ArloHub — installing Python dependencies
echo.

set "PY_CMD="
py -3 --version >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
  python --version >nul 2>&1
  if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
  echo ERROR: Python was not found on this PC.
  echo.
  echo Install Python 3.10+ from https://www.python.org/downloads/
  echo During setup, enable "Add python.exe to PATH".
  echo Then run this script again.
  echo.
  pause
  exit /b 1
)

if not exist "requirements.txt" (
  echo ERROR: requirements.txt not found in "%CD%"
  pause
  exit /b 1
)

if not exist ".venv\" (
  echo Creating virtual environment in .venv ...
  %PY_CMD% -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv
    pause
    exit /b 1
  )
) else (
  echo Using existing .venv
)

call "%~dp0.venv\Scripts\activate.bat"
if errorlevel 1 (
  echo Failed to activate .venv
  pause
  exit /b 1
)

echo.
echo Upgrading pip ...
python -m pip install --upgrade pip
if errorlevel 1 (
  echo pip upgrade failed
  pause
  exit /b 1
)

echo.
echo Installing packages from requirements.txt ...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Package install failed
  pause
  exit /b 1
)

echo.
echo ---------------------------------------------------------------------------
echo Done. Dependencies are installed in .venv
echo.
echo Run the GUI:    double-click run_arlohub.bat
echo Or:              .venv\Scripts\python.exe main_gui.py  ^(same as run_arlohub.bat^)
echo.
echo Note: ADB-based features need Android platform-tools on your PATH.
echo ---------------------------------------------------------------------------
pause
exit /b 0
