@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo This project needs a virtual environment with dependencies.
  echo Run setup_dependencies.bat first, then try again.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "%~dp0main_gui.py"
set "_EC=%ERRORLEVEL%"
if %_EC% neq 0 (
  echo.
  echo ArloHub exited with an error ^(code %_EC%^).
  echo If a dialog appeared, check arlohub_last_error.txt in this folder.
  echo.
  pause
)
exit /b %_EC%
