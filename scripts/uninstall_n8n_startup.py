#!/usr/bin/env python3
"""Remove the n8n automation server startup task left by an older version.

n8n support was removed in 2.0; this script stays so earlier installs can remove
the logon task that scripts/install_n8n_startup.py once registered.

Usage:
    python scripts/uninstall_n8n_startup.py

What it does:
    - Deletes the Windows Task Scheduler task created by install_n8n_startup.py.
    - n8n will no longer start automatically on login.
    - No administrator privileges required.
    - Safe to run even if the task does not exist.
"""

from __future__ import annotations

import subprocess
import sys

TASK_NAME = "FindYourJobWithAI_N8n"


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

    if result.returncode == 0:
        print(f"SUCCESS — Task '{TASK_NAME}' removed.")
        print("n8n will no longer start automatically on login.")
    else:
        query = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True,
        )
        if query.returncode != 0:
            print(f"Task '{TASK_NAME}' was not found (already removed or never installed).")
        else:
            print(f"ERROR: Failed to remove task '{TASK_NAME}'.")
            print((result.stdout + result.stderr).strip())
            sys.exit(1)


if __name__ == "__main__":
    main()
