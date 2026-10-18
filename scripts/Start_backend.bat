@echo off
setlocal

REM Find Your Job With AI — backend startup script.
REM Used by Windows Task Scheduler (no --reload, no frontend).
REM Safe to run manually: double-click or call from a terminal.

REM Derive the project root from the location of this script.
set SCRIPT_DIR=%~dp0
set PROJECT_ROOT=%SCRIPT_DIR%..

REM Change to the project root so all relative paths (data/, config/, src/) work.
cd /d "%PROJECT_ROOT%"

REM ---- Python environment detection ----------------------------------------
REM Prefer a project-local virtual environment if one exists.
REM Fall back to the system/user Python on PATH.

if exist ".venv\Scripts\python.exe" (
    set PYTHON=".venv\Scripts\python.exe"
    echo [start_backend] Using venv: .venv\Scripts\python.exe
) else if exist "venv\Scripts\python.exe" (
    set PYTHON="venv\Scripts\python.exe"
    echo [start_backend] Using venv: venv\Scripts\python.exe
) else (
    set PYTHON=python
    echo [start_backend] No venv found — using Python from PATH.
)

REM ---- Log file ---------------------------------------------------------
REM Task Scheduler discards console output by default, so anything printed
REM before src.logging_config takes over (venv detection above, Python
REM import errors, etc.) would otherwise be lost with no record at all.
REM src/logging_config.py handles day-to-day application/scanner logging
REM once uvicorn actually starts; this is only a belt-and-suspenders capture
REM of the raw process output around that.
if not exist "logs\backend" mkdir "logs\backend"
set LAUNCH_LOG=logs\backend\launch-%date:~-4%-%date:~3,2%-%date:~0,2%.log

REM ---- Startup log ----------------------------------------------------------
echo [start_backend] Project root: %PROJECT_ROOT% >> "%LAUNCH_LOG%"
echo [start_backend] Starting uvicorn on port 8000 ... >> "%LAUNCH_LOG%"
echo [start_backend] Project root: %PROJECT_ROOT%
echo [start_backend] Starting uvicorn on port 8000 ...

REM ---- Launch uvicorn -------------------------------------------------------
REM --reload is intentionally OMITTED (development-only flag) -- running
REM with --reload here would let uvicorn's own file-watcher kill and
REM restart the worker process on every source-file change, silently
REM abandoning any in-flight scanner thread (see src/api/app.py's module
REM docstring, and docs/incidents for the investigation that traced a
REM production-shaped incident back to a --reload dev server doing exactly
REM this).
%PYTHON% -m uvicorn src.api.app:app --port 8000 >> "%LAUNCH_LOG%" 2>&1

REM If uvicorn exits with an error, pause so the error is visible.
if errorlevel 1 (
    echo [start_backend] uvicorn exited with error code %errorlevel%. >> "%LAUNCH_LOG%"
    echo [start_backend] uvicorn exited with error code %errorlevel%.
    echo [start_backend] Check "%LAUNCH_LOG%" and logs\backend\backend-*.log for details. Press any key to close.
    pause
)

endlocal
