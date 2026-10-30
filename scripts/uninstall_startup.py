#!/usr/bin/env python3
"""Remove the Find Your Job With AI backend startup task.

Usage:
    python scripts/uninstall_startup.py

What it does:
    - Deletes the Windows Task Scheduler task that scripts/install_startup.py (removed in
      2.0; use ``FindYourJob.cmd autostart on`` instead) created in 1.x.
    - The backend will no longer start automatically on login.
    - Running the backend manually (scripts/Start_backend.bat) still works.
    - No administrator privileges required.
    - Safe to run even if the task does not exist.
"""

from __future__ import annotations

import subprocess
import sys

TASK_NAME = "FindYourJobWithAI_Backend"


def main() -> None:
    if sys.platform != "win32":
        print("ERROR: This script is for Windows only.")
        sys.exit(1)

    print(f"Removing task '{TASK_NAME}' from Windows Task Scheduler ...")

    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
        capture_output=True,
        text=True,
    )

    output = (result.stdout + result.stderr).strip().lower()

    if result.returncode == 0:
        print(f"SUCCESS — Task '{TASK_NAME}' removed.")
        print("The backend will no longer start automatically on login.")
        print()
        print("You can still start the backend manually:")
        print("  scripts\\Start_backend.bat")
        print("  OR: python -m uvicorn src.api.app:app --port 8000")
    else:
        # Distinguish "task not found" from a real error by checking if the
        # task still exists — works on any Windows language.
        query = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True,
        )
        if query.returncode != 0:
            # Task doesn't exist → delete "failed" only because there was nothing to delete.
            print(f"Task '{TASK_NAME}' was not found (already removed or never installed).")
        else:
            print(f"ERROR: Failed to remove task '{TASK_NAME}'.")
            print((result.stdout + result.stderr).strip())
            sys.exit(1)


if __name__ == "__main__":
    main()
