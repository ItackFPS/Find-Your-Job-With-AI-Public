"""Start with Windows: one per-user ``Run`` registry value, nothing else.

``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run`` needs no administrator
rights and is what Windows' own "Startup apps" settings page lists and can turn
off. The value starts the tray app with no window (``tray --window none``): the
backend and its scheduled scans run, and the dashboard opens from the tray icon.

Only ever changed when the user asks (tray menu or ``python -m src.launcher
autostart on|off``); the uninstaller turns it off.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "FindYourJobWithAI"


def command(root: Path) -> str:
    """The command line Windows runs at logon for the app installed in ``root``."""
    runtime = root / "runtime" / "pythonw.exe"
    python = runtime if runtime.is_file() else Path(sys.executable).with_name("pythonw.exe")
    return f'"{python}" -m src.launcher tray --window none'


class Autostart:
    """Read and change the Run value. The registry functions are injectable for tests."""

    def __init__(self, root: Path,
                 read: Callable[[], str | None] | None = None,
                 write: Callable[[str], None] | None = None,
                 delete: Callable[[], None] | None = None) -> None:
        self.root = root
        self._read = read or _read_value
        self._write = write or _write_value
        self._delete = delete or _delete_value

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def enabled(self) -> bool:
        """True only when the value starts *this* installation (another copy's is not ours)."""
        try:
            return self._read() == command(self.root)
        except OSError:
            return False

    def enable(self) -> None:
        self._write(command(self.root))

    def disable(self) -> None:
        """Remove the value if it starts this installation; another copy's is left alone."""
        try:
            current = self._read()
        except OSError:
            return
        if current == command(self.root):
            self._delete()


def _read_value() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    return str(value)


def _write_value(value: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, value)


def _delete_value() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
