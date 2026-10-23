@echo off
setlocal
title Jarvis Setup
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 (
    echo Python Launcher was not found. Install Python 3.12 with the Windows Python Launcher, then run setup.bat again.
    exit /b 1
)

set NEED_VENV=
if not exist ".venv\Scripts\python.exe" set NEED_VENV=1
if not exist ".venv\Scripts\activate.bat" set NEED_VENV=1
if defined NEED_VENV (
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Could not create or repair .venv. Confirm Python 3.12 is installed with: py -3.12 --version
        exit /b 1
    )
)

".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 (
    echo Existing .venv is not Python 3.12. It was left untouched. Rename it manually if you want setup.bat to create a new one.
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
if not exist ".env" (
    if exist ".env.example" (
        copy /Y ".env.example" ".env" >nul
        if errorlevel 1 (
            echo Could not create .env from .env.example.
            exit /b 1
        )
    )
)


set NEED_TTSENV=
if not exist ".ttsenv\Scripts\python.exe" set NEED_TTSENV=1
if not exist ".ttsenv\Scripts\activate.bat" set NEED_TTSENV=1
if defined NEED_TTSENV (
    py -3.12 -m venv .ttsenv
    if errorlevel 1 (
        echo Could not create or repair .ttsenv. Confirm Python 3.12 is installed.
        exit /b 1
    )
)

".ttsenv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)"
if errorlevel 1 (
    echo Existing .ttsenv cannot run Python 3.12. It was left untouched.
    echo Install Python 3.12 so py -3.12 works, then rename .ttsenv and run setup.bat again.
    exit /b 1
)

".ttsenv\Scripts\python.exe" -m pip install -r tts-requirements.txt
if errorlevel 1 exit /b 1

echo Jarvis dependencies are installed. Start the app with start_jarvis.bat.
exit /b 0
