#!/usr/bin/env python3
"""Register the Find Your Job With AI backend as a Windows startup task.

Usage:
    python scripts/install_startup.py

What it does:
    - Detects the project root and Python executable automatically.
    - Registers a Windows Task Scheduler task (AtLogOn trigger).
    - The task starts scripts/Start_backend.bat when you log in.
    - No administrator privileges required.
    - Safe to run multiple times (updates the existing task).

To remove the task:
    python scripts/uninstall_startup.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "FindYourJobWithAI_Backend"
TASK_DESCRIPTION = (
    "Find Your Job With AI - job-discovery backend. "
    "Starts automatically on login. Managed by scripts/install_startup.py."
)

# Delay after login before the backend starts (ISO 8601 duration).
# 30 seconds gives Windows time to finish its own startup tasks.
STARTUP_DELAY_ISO = "PT30S"


def main() -> None:
    if sys.platform != "win32":
        print("ERROR: This script is for Windows only.")
        sys.exit(1)

    project_root = Path(__file__).resolve().parent.parent
    bat_file = project_root / "scripts" / "Start_backend.bat"

    if not bat_file.exists():
        print(f"ERROR: Start script not found: {bat_file}")
        print("Expected: scripts/Start_backend.bat")
        sys.exit(1)

    print(f"Project root : {project_root}")
    print(f"Start script : {bat_file}")
    print(f"Task name    : {TASK_NAME}")
    print()

    # Check current state.
    existing = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        print(f"Task '{TASK_NAME}' already exists — it will be updated.")
    else:
        print(f"Registering new task '{TASK_NAME}' ...")

    # Use the Task Scheduler COM API directly.
    #
    # Register-ScheduledTask (the PS cmdlet) requires elevated privileges on
    # some Windows 10/11 configurations even for user-owned tasks (0x80070005).
    # The underlying COM API (Schedule.Service) works for standard users when
    # registering an AtLogOn task with TASK_LOGON_INTERACTIVE_TOKEN.
    #
    # COM constants:
    #   TASK_CREATE_OR_UPDATE    = 6
    #   TASK_LOGON_INTERACTIVE_TOKEN = 3
    #   TASK_TRIGGER_LOGON       = 9  (AtLogOn)
    #   TASK_ACTION_EXEC         = 0
    #   TASK_INSTANCES_IGNORE_NEW = 2
    bat_escaped = str(bat_file).replace("'", "''")
    root_escaped = str(project_root).replace("'", "''")
    desc_escaped = TASK_DESCRIPTION.replace("'", "''")

    ps_script = f"""
$ErrorActionPreference = 'Stop'
$svc = New-Object -ComObject "Schedule.Service"
$svc.Connect()
$root = $svc.GetFolder("\\")
$task = $svc.NewTask(0)
$task.RegistrationInfo.Description = '{desc_escaped}'
$task.RegistrationInfo.Author = $env:USERNAME
$task.Settings.StartWhenAvailable = $true
$task.Settings.DisallowStartIfOnBatteries = $false
$task.Settings.StopIfGoingOnBatteries = $false
$task.Settings.MultipleInstances = 2
$task.Settings.ExecutionTimeLimit = 'PT23H'
$task.Settings.RestartCount = 3
$task.Settings.RestartInterval = 'PT1M'
$task.Settings.Enabled = $true
$trig = $task.Triggers.Create(9)
$trig.UserId = "$env:USERDOMAIN\\$env:USERNAME"
$trig.Delay = '{STARTUP_DELAY_ISO}'
$trig.Enabled = $true
$act = $task.Actions.Create(0)
$act.Path = 'cmd.exe'
$act.Arguments = '/c "{bat_escaped}"'
$act.WorkingDirectory = '{root_escaped}'
$reg = $root.RegisterTaskDefinition('{TASK_NAME}', $task, 6, $null, $null, 3)
Write-Output "OK: $($reg.Name)"
"""

    result = subprocess.run(
        ["powershell", "-NonInteractive", "-Command", ps_script],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if result.returncode != 0:
        print("ERROR: Failed to register the task.")
        print("--- PowerShell output ---")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.stderr.strip():
            print(result.stderr.strip())
        print()
        print("Tip: Try running this script from your own terminal")
        print("     (PowerShell or Command Prompt, not from inside an IDE).")
        sys.exit(1)

    if result.stdout.strip():
        print(result.stdout.strip())

    print()
    print(f"SUCCESS — Task '{TASK_NAME}' is registered.")
    print()
    print("What happens next:")
    print(f"  - On your next login, Windows will run: {bat_file}")
    print(f"  - The backend will start at http://localhost:8000")
    print(f"  - The scheduler will start automatically after a {STARTUP_DELAY_ISO} delay")
    print()
    print("To test immediately without restarting:")
    print(f'  cmd /c "{bat_file}"')
    print()
    print("To view the task in Task Scheduler:")
    print(f"  taskschd.msc  →  Task Scheduler Library  →  {TASK_NAME}")
    print()
    print("To remove automatic startup:")
    print("  python scripts/uninstall_startup.py")


if __name__ == "__main__":
    main()
