@echo off
setlocal
cd /d "%~dp0"
echo Removing JARVIS Windows Startup Shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.SpecialFolders('Startup'); Remove-Item -Path (Join-Path $s 'JARVIS.lnk') -ErrorAction SilentlyContinue; Write-Host 'Startup shortcut removed.'"
echo.
pause
