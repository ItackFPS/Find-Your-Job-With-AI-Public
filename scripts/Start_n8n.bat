@echo off
setlocal

REM Find Your Job With AI — n8n startup script.
REM Used by Windows Task Scheduler (starts n8n automation server).
REM Safe to run manually: double-click or call from a terminal.
REM
REM n8n data (workflows, credentials, executions) is stored in:
REM   %USERPROFILE%\.n8n\
REM Do NOT set N8N_USER_FOLDER here — let n8n use its default location
REM so that existing workflows and credentials are preserved.

REM ---- Locate n8n executable ------------------------------------------------
REM Prefer the global npm install location on Windows.
REM Fall back to whatever is on PATH (covers nvm, fnm, winget installs, etc.).

set N8N_CMD=
if exist "%APPDATA%\npm\n8n.cmd" (
    set N8N_CMD="%APPDATA%\npm\n8n.cmd"
    echo [start_n8n] Found n8n at: %APPDATA%\npm\n8n.cmd
) else (
    where n8n >nul 2>&1
    if %errorlevel%==0 (
        set N8N_CMD=n8n
        echo [start_n8n] Using n8n from PATH.
    ) else (
        echo [start_n8n] ERROR: n8n not found.
        echo [start_n8n] Install it with:  npm install -g n8n
        pause
        exit /b 1
    )
)

REM ---- Local only -----------------------------------------------------------
REM n8n listens on every network interface by default, which puts its editor
REM and webhooks on the LAN (and on public Wi-Fi when a firewall rule allows
REM node.exe). The digest only needs this machine. It also sends anonymous
REM telemetry and checks for updates by default. A value you set beforehand
REM (environment or the n8n service settings) is kept.
if not defined N8N_LISTEN_ADDRESS set N8N_LISTEN_ADDRESS=127.0.0.1
if not defined N8N_DIAGNOSTICS_ENABLED set N8N_DIAGNOSTICS_ENABLED=false
if not defined N8N_VERSION_NOTIFICATIONS_ENABLED set N8N_VERSION_NOTIFICATIONS_ENABLED=false

REM ---- Startup log ----------------------------------------------------------
echo [start_n8n] Starting n8n on %N8N_LISTEN_ADDRESS%:5678 ...
echo [start_n8n] Dashboard: http://localhost:5678

REM ---- Launch n8n -----------------------------------------------------------
%N8N_CMD% start

REM If n8n exits with an error, pause so the error is visible.
if errorlevel 1 (
    echo [start_n8n] n8n exited with error code %errorlevel%.
    echo [start_n8n] Check the output above. Press any key to close.
    pause
)

endlocal
