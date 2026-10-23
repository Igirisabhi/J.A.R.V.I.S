@echo off
title Stop Jarvis
echo Terminating Jarvis core...
powershell -NoProfile -Command "$root = [IO.Path]::GetFullPath('%~dp0'); $file = Join-Path $root 'jarvis.pid'; $mutex = $null; try { $mutex = [Threading.Mutex]::OpenExisting('JarvisCore_SingleInstance_Session_Mutex') } catch {}; if ($mutex) { try { if (Test-Path -LiteralPath $file) { $id = 0; try { $id = [int](Get-Content -LiteralPath $file -Raw).Trim() } catch {}; if (Get-Process -Id $id -ErrorAction SilentlyContinue) { & taskkill.exe /PID $id /T /F | Out-Null; if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) { Write-Error ('Could not stop Jarvis process ' + $id); exit 1 } } } } finally { $mutex.Dispose() } }; Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue"
if errorlevel 1 (
    echo Jarvis is still running. Close it from the account that started it, then retry.
    exit /b 1
)
echo Jarvis core has been stopped.
