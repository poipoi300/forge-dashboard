@echo off
rem Launch the forge dashboard from cmd.exe or a double-click; run.ps1 does the work.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
exit /b %ERRORLEVEL%
