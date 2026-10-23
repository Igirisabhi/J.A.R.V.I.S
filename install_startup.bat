@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo      Installing JARVIS Windows Startup Shortcut
echo ============================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_startup.ps1"
echo.
pause
