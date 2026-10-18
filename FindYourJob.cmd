@echo off
setlocal
REM Find Your Job With AI -- the one entry point on Windows.
REM Double-click it, or run "FindYourJob.cmd --help" for start/dev/status/stop/shortcut.
REM It starts the backend (and builds the dashboard when needed), opens the dashboard,
REM and stops only what it started when you press Ctrl+C or close this window.

title Find Your Job With AI
cd /d "%~dp0"

set "FYJ_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%FYJ_PY%" (
    echo Find Your Job With AI is not set up yet: no Python environment in "%~dp0.venv".
    echo.
    echo First-time setup, from this folder:
    echo   py -3.12 -m venv .venv
    echo   .venv\Scripts\python -m pip install -e .
    echo Then build the dashboard once ^(needs Node.js 22 LTS or 20.19+^):
    echo   cd frontend ^&^& npm ci ^&^& npm run build
    echo.
    echo See README.md, "Install".
    if "%~1"=="" pause
    exit /b 2
)

"%FYJ_PY%" -m src.launcher %*
set "FYJ_RC=%ERRORLEVEL%"
REM Keep a double-clicked window open on failure so the message can be read.
if not "%FYJ_RC%"=="0" if "%~1"=="" pause
exit /b %FYJ_RC%
