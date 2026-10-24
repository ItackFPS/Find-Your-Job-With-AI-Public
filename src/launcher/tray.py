"""The app as a Windows notification-area (tray) application (update plan §C7, F2).

``python -m src.launcher tray`` (``pythonw``, no console) does what ``start`` does —
the same :class:`~src.launcher.run.Launcher`, same ownership rules, same stop
event — but the launcher runs in a worker thread while a tray icon owns the main
thread. So:

* closing the dashboard window closes only the window process (src/launcher/window.py):
  the backend, its scheduler and any LinkedIn/HelloWork scan keep running;
* the tray menu opens (or focuses) the window, pauses and resumes the scans, toggles
  "Start with Windows", opens the logs, and **Quit** stops everything the app started
  (scans stop at their next checkpoint);
* a backend that crashes is restarted once, and the user is told;
* a Windows notification says when a search finishes.

pystray and Pillow are imported only here, when the tray runs: nothing else in the
app (or its tests on Linux) needs them.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src.launcher import core
from src.launcher.autostart import Autostart
from src.launcher.run import Launcher

RUN_POLL_S = 30.0
HINT_MARKER = ".tray-hint-shown"


class RunWatcher:
    """Reports pipeline runs that complete while the tray app is running."""

    def __init__(self, backend_url: str | Callable[[], str | None],
                 fetch: Callable[[str], tuple[int, bytes] | None] | None = None) -> None:
        # a callable: the backend's port is known only once it answered with our token
        self._base = backend_url if callable(backend_url) else (lambda: backend_url)
        self._fetch = fetch or (lambda url: core.http_get(url, timeout_s=5.0))
        self._seen: str | None = None
        self._primed = False

    def poll(self) -> str | None:
        """A message for a run that completed since the last poll, else None.

        The first poll only records the latest run, so starting the app never
        announces an old run.
        """
        base = self._base()
        if base is None:
            return None
        result = self._fetch(f"{base}/api/pipeline/runs?limit=1")
        if result is None or result[0] != 200:
            return None
        try:
            runs = json.loads(result[1])
        except ValueError:
            return None
        latest = runs[0] if isinstance(runs, list) and runs else None
        if not isinstance(latest, dict):
            self._primed = True
            return None
        run_id, status = latest.get("id"), latest.get("status")
        if not self._primed:
            self._primed, self._seen = True, run_id if status in ("completed", "failed") else None
            return None
        if run_id == self._seen or status not in ("completed", "failed"):
            return None
        self._seen = run_id
        if status == "failed":
            return "A job search failed. Open the dashboard to see why."
        scored = (latest.get("stats") or {}).get("scored", 0)
        return f"Job search finished: {scored} offer(s) matched your profile."


class TrayApp:
    def __init__(self, launcher: Launcher, autostart: Autostart,
                 pystray: Any = None, image_loader: Callable[[Path], Any] | None = None,
                 watcher: RunWatcher | None = None,
                 alert: Callable[[str], None] | None = None) -> None:
        self.launcher = launcher
        self._alert = alert or _message_box
        self.autostart = autostart
        self._pystray = pystray
        self._load_image = image_loader
        self.watcher = watcher or RunWatcher(lambda: launcher.backend_url)
        self.icon: Any = None
        self.exit_code = 0
        self.scans_paused = False  # the scheduler's "Pause scans" state, as last seen
        self._ready = threading.Event()
        self._done = threading.Event()
        launcher.notify = self.notify

    # ---- actions (menu items) -----------------------------------------------------------

    def open_dashboard(self, *_args: object) -> None:
        url = self.launcher.backend_url
        if url is None:  # not answering with our token (yet): never open anything else
            self.notify("Find Your Job With AI is still starting; try again in a moment.")
            return
        self.launcher.open_window()

    def pause_scans(self, *_args: object) -> None:
        self._scheduler("pause", "Scans paused: running searches stop at their next checkpoint; "
                                 "none starts until you resume.")

    def resume_scans(self, *_args: object) -> None:
        self._scheduler("resume", "Scans resumed.")

    def _scheduler(self, action: str, done: str) -> None:
        url = self.launcher.backend_url
        if url is None:
            self.notify("Find Your Job With AI is still starting; try again in a moment.")
            return
        result = core.http_post(f"{url}/api/scheduler/{action}")
        if result is None or result[0] != 200:
            self.notify(f"Could not {action} the scans. See the logs folder.")
            return
        self.scans_paused = action == "pause"
        self.notify(done)
        if self.icon is not None and hasattr(self.icon, "update_menu"):
            self.icon.update_menu()

    def _read_paused(self) -> None:
        url = self.launcher.backend_url
        result = core.http_get(f"{url}/api/scheduler/status", timeout_s=5.0) if url else None
        if result is not None and result[0] == 200:
            try:
                self.scans_paused = bool(json.loads(result[1]).get("paused"))
            except (ValueError, AttributeError):
                pass

    def toggle_autostart(self, *_args: object) -> None:
        if self.autostart.enabled():
            self.autostart.disable()
        else:
            self.autostart.enable()

    def open_logs(self, *_args: object) -> None:
        logs = self.launcher.s.root / "logs"
        if hasattr(os, "startfile"):
            os.startfile(logs)  # noqa: S606 - the app's own logs folder

    def quit(self, *_args: object) -> None:
        self.launcher.out("Quit requested from the notification area.")
        self.launcher.request_stop()

    def notify(self, message: str) -> None:
        if self.icon is not None:
            try:
                self.icon.notify(message, core.APP_TITLE)
            except Exception:  # noqa: BLE001 - a notification must never stop the app
                pass

    # ---- lifecycle -------------------------------------------------------------------------

    def menu(self) -> Any:
        ps = self._pystray
        item = ps.MenuItem
        return ps.Menu(
            item("Open Find Your Job With AI", self.open_dashboard, default=True),
            item("Pause scans", self.pause_scans, visible=lambda _i: not self.scans_paused),
            item("Resume scans", self.resume_scans, visible=lambda _i: self.scans_paused),
            ps.Menu.SEPARATOR,
            item("Start with Windows", self.toggle_autostart,
                 checked=lambda _i: self.autostart.enabled(), visible=self.autostart.supported),
            item("Open logs folder", self.open_logs),
            ps.Menu.SEPARATOR,
            item("Quit", self.quit),
        )

    def _work(self) -> None:
        try:
            self.exit_code = self.launcher.run(on_ready=self._on_ready)
        except Exception:  # noqa: BLE001 - pythonw has no stderr: log it, never lose it
            self.exit_code = 1
            self.launcher.out("The app stopped on an unexpected error:\n" + traceback.format_exc())
        try:
            if self.exit_code != 0 and not self._ready.is_set():
                # A start that failed (prerequisites, port taken, build): without a
                # console the icon would just vanish, so say so before it does.
                self._alert("Find Your Job With AI could not start. The reason is in "
                            f"{self.launcher.s.logs_dir}.")
        finally:
            self._done.set()
            if self.icon is not None:
                self.icon.stop()

    def _on_ready(self) -> None:
        self._ready.set()
        self._read_paused()
        marker = self.launcher.s.logs_dir / HINT_MARKER
        if not marker.exists():
            # Record the hint before showing it: whoever sees the hint can rely on the marker.
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except OSError:
                pass
            self.notify("Find Your Job With AI keeps running here, in the notification area, so "
                        "searches continue when you close the window. Right-click the icon to quit.")
        threading.Thread(target=self._watch_runs, name="tray-runs", daemon=True).start()

    def _watch_runs(self) -> None:
        while not self._done.wait(RUN_POLL_S):
            message = self.watcher.poll()
            if message:
                self.notify(message)

    def run(self) -> int:
        if self._pystray is None:
            import pystray

            self._pystray = pystray
        if self._load_image is None:
            from PIL import Image

            self._load_image = Image.open
        icon_path = self.launcher.s.root / "FindYourJob.ico"
        if not icon_path.is_file():
            icon_path = Path(__file__).resolve().parents[2] / "packaging" / "windows" / "FindYourJob.ico"
        self.icon = self._pystray.Icon("find-your-job-with-ai", self._load_image(icon_path),
                                       core.APP_TITLE, self.menu())
        worker = threading.Thread(target=self._work, name="launcher", daemon=True)

        def setup(icon: Any) -> None:
            icon.visible = True
            worker.start()

        self.icon.run(setup=setup)
        # The icon stops when the launcher ends (Quit, the Stop shortcut, a fatal error).
        self.launcher.request_stop()
        self._done.wait(self.launcher.graceful_stop_s + 30)
        return self.exit_code


def _message_box(message: str) -> None:
    """A blocking Windows message box (the tray icon is about to disappear)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, core.APP_TITLE, 0x10 | 0x40000)  # error, topmost
    except Exception:  # noqa: BLE001 - best effort; the log has the reason
        pass
