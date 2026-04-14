@echo off
REM Legacy launcher name; forwards to run_arlohub.bat
call "%~dp0run_arlohub.bat" %*
exit /b %ERRORLEVEL%
