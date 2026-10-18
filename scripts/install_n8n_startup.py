#!/usr/bin/env python3
"""Register the n8n automation server as a Windows startup task.

Usage:
    python scripts/install_n8n_startup.py

What it does:
    - Registers a Windows Task Scheduler task (AtLogOn trigger).
    - The task starts scripts/Start_n8n.bat when you log in.
    - n8n starts 60 seconds after login (after the backend's 30-second delay).
    - No administrator privileges required.
    - Safe to run multiple times (updates the existing task).

To remove the task:
    python scripts/uninstall_n8n_startup.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "FindYourJobWithAI_N8n"
TASK_DESCRIPTION = (
    "Find Your Job With AI - n8n automation server. "
    "Starts automatically on login, 60 s after the backend. "
    "Managed by scripts/install_n8n_startup.py."
)

# 60 seconds so the backend (30 s delay) is ready before n8n tries to reach it.
STARTUP_DELAY_ISO = "PT60S"


def main() -> None:
    if sys.platform != "win32":
        print("ERROR: This script is for Windows only.")
        sys.exit(1)

    project_root = Path(__file__).resolve().parent.parent
    bat_file = project_root / "scripts" / "Start_n8n.bat"

    if not bat_file.exists():
        print(f"ERROR: Start script not found: {bat_file}")
        print("Expected: scripts/Start_n8n.bat")
        sys.exit(1)

    print(f"Project root : {project_root}")
    print(f"Start script : {bat_file}")
    print(f"Task name    : {TASK_NAME}")
    print()

    existing = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST"],
        capture_output=True, text=True,
    )
    if existing.returncode == 0:
        print(f"Task '{TASK_NAME}' already exists — it will be updated.")
    else:
        print(f"Registering new task '{TASK_NAME}' ...")

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
    print(f"  - n8n will start at http://localhost:5678  (after a {STARTUP_DELAY_ISO} delay)")
    print(f"  - The weekly digest workflow fires automatically at Monday 14:00 Europe/Paris")
    print()
    print("To test immediately without restarting:")
    print(f'  cmd /c "{bat_file}"')
    print()
    print("To view the task in Task Scheduler:")
    print(f"  taskschd.msc  →  Task Scheduler Library  →  {TASK_NAME}")
    print()
    print("To remove automatic startup:")
    print("  python scripts/uninstall_n8n_startup.py")


if __name__ == "__main__":
    main()
