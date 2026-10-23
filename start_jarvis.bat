@echo off
setlocal
title JARVIS Voice Assistant
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\activate.bat" (
    echo Jarvis virtual environment activation script is missing.
    echo Running setup.bat to repair the environment...
    call "%~dp0setup.bat"
)

call "%~dp0.venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Jarvis virtual environment is missing or failed to activate.
    pause
    exit /b 1
)

python --version >nul 2>&1
if errorlevel 1 (
    echo The Jarvis Python environment is unavailable. Run setup.bat to repair it.
    pause
    exit /b 1
)

python "%~dp0main.py" --ui %*
