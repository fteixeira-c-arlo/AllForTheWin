@echo off
REM Arlo Camera Terminal - Double-click or run from cmd to start.
REM Installs Python and dependencies automatically if needed.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 pause
