"""The dashboard in its own window process (update plan §C7, F2.2; owner decision K4).

``python -m src.launcher window --port N`` (started by the launcher, with the instance token in
``FYJ_INSTANCE_TOKEN``) shows the dashboard in a pywebview window on Microsoft Edge WebView2,
titled and iconed as the app. It is a separate process on purpose: closing the window ends only
this process, while the agent (tray, backend, scheduler, scans) keeps running.

* It loads nothing until ``/api/health`` on that port returns the same instance token: a foreign
  or stale process on the port is reported and never shown.
* One window per installation: a second request focuses the open window instead
  (a session-local named event, like the launcher's own single-instance check).
* Without pywebview or the WebView2 runtime it falls back to the Edge/Chrome ``--app`` window
  (``run.open_dashboard``), as before K4.

pywebview (and pythonnet, which its Windows backend needs) is imported only here, in the window
process: nothing else in the app, or its tests on Linux, needs it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from src.launcher import core
from src.launcher.core import Backend
from src.launcher.winproc import IS_WINDOWS, JobObject, StopEvent

WEBVIEW2_CLIENT = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # the Evergreen WebView2 runtime's id
WIDTH, HEIGHT, MIN_SIZE = 1280, 860, (900, 600)
WATCH_S = 3.0  # how often the window checks that its instance still answers


def window_event_name(root: Path) -> str:
    digest = hashlib.sha256(str(root).lower().encode()).hexdigest()[:16]
    return f"FindYourJobWithAI.window.{digest}"


def webview2_version() -> str | None:
    """The installed WebView2 runtime's version (per machine or per user), or None."""
    if sys.platform != "win32":
        return None
    import winreg

    keys = ((winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT}"))
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as key:
                version, _kind = winreg.QueryValueEx(key, "pv")
        except OSError:
            continue
        if version and version != "0.0.0.0":  # 0.0.0.0: an uninstalled runtime's leftover
            return str(version)
    return None


def webview_available() -> bool:
    """pywebview is installed and the WebView2 runtime it renders with is present."""
    return importlib.util.find_spec("webview") is not None and webview2_version() is not None


def icon_path(root: Path) -> Path | None:
    for candidate in (root / "FindYourJob.ico",
                      Path(__file__).resolve().parents[2] / "packaging" / "windows" / "FindYourJob.ico"):
        if candidate.is_file():
            return candidate
    return None


def show(url: str, root: Path, alive: Callable[[], bool] = lambda: True, webview: Any = None,
         poll_s: float = WATCH_S) -> None:
    """Show ``url`` in a pywebview window; returns when the user closes it, or once ``alive()``
    says the instance is gone (Quit, a crash): a window must not outlive the app it shows."""
    if webview is None:
        import webview  # noqa: PLC0415 - the window process only
    window = webview.create_window(core.APP_TITLE, url, width=WIDTH, height=HEIGHT, min_size=MIN_SIZE,
                                   text_select=True, zoomable=True)
    guard_navigation(window, url)

    closed = threading.Event()

    def watch() -> None:  # pywebview runs it in its own thread once the window exists
        while not closed.wait(poll_s):
            if not alive():
                window.destroy()
                return

    icon = icon_path(root)
    try:
        # private_mode=False + a storage path: what the dashboard keeps in the browser (localStorage,
        # e.g. the theme) survives a restart instead of being wiped with a private session
        webview.start(watch, gui="edgechromium", private_mode=False,
                      storage_path=str(root / "data" / "webview"), icon=str(icon) if icon else None)
    finally:
        closed.set()  # closed by the user: the watcher ends at once and the process can exit


def navigation_allowed(url: str, origin: str) -> bool:
    """Only the backend's own pages load in the app window (same scheme, host and port)."""
    target, home = urlsplit(url), urlsplit(origin)
    if target.scheme == "about":
        return target.path == "blank"
    return (target.scheme, target.hostname, target.port) == (home.scheme, home.hostname, home.port)


def guard_navigation(window: Any, origin: str, opener: Callable[[str], Any] = webbrowser.open) -> None:
    """Keep the app window on the backend's origin: any other http(s) page (a plain link to an offer,
    a redirect) opens in the default browser instead, and other schemes are refused. Links opening a
    new window already go to the default browser (pywebview's OPEN_EXTERNAL_LINKS_IN_BROWSER)."""

    def on_navigation(_sender: Any, args: Any) -> None:
        target = str(args.Uri)
        if navigation_allowed(target, origin):
            return
        args.Cancel = True
        if urlsplit(target).scheme in ("http", "https"):
            opener(target)

    def attach() -> None:  # the native WebView2 control exists once the form is built
        native = getattr(getattr(window, "native", None), "webview", None)
        if native is not None:
            native.NavigationStarting += on_navigation

    window.events.before_show += attach


def focus_existing() -> bool:
    """Bring the open app window (a visible top-level window titled as the app) to the front."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined,unused-ignore]

    def visit(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == core.APP_TITLE:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(proc(visit), 0)
    if not found:
        return False
    user32.ShowWindow(found[0], 9)  # SW_RESTORE
    user32.SetForegroundWindow(found[0])
    return True


def window_main(root: Path, port: int, token: str, *,
                show_window: Callable[[str, Path, Callable[[], bool]], None] | None = None,
                fallback: Callable[[str], None] | None = None,
                available: Callable[[], bool] = webview_available,
                alert: Callable[[str], None] | None = None) -> int:
    """The window process. 0 when shown (or handed to the fallback), 1 when another window of this
    installation is already open (it is focused), 3 when the backend is not this instance."""
    from src.launcher.run import Output, open_dashboard

    out = Output(root / "logs" / "launcher" / "window.log")
    url = f"http://{core.LOOPBACK}:{port}"
    verdict = core.probe_backend(port, token=token) if token else Backend.OTHER
    if verdict is not Backend.OURS:
        message = (f"{core.APP_TITLE}: the service on port {port} is not this app's running instance "
                   f"({verdict.value}); the window was not opened.")
        out(f"[XX] {message}")
        (alert or _message_box)(message)
        return 3
    single = StopEvent(window_event_name(root))
    if not single.create():
        focus_existing()
        return 1
    try:
        if available():
            out(f"Opening the app window (WebView2 {webview2_version() or '?'}) at {url}")
            (show_window or show)(url, root, _instance_alive(port, token))
            out("App window closed (the app keeps running in the notification area).")
            return 0
        out("pywebview or the WebView2 runtime is not available: opening an Edge/Chrome app window.")
        (fallback or (lambda u: open_dashboard(u, "app", out)))(url)
        return 0
    finally:
        single.close()


def _instance_alive(port: int, token: str, misses_allowed: int = 2) -> Callable[[], bool]:
    """False once the backend has stopped answering with the token (a few misses in a row, so
    a busy moment is not taken for an exit)."""
    misses = 0

    def alive() -> bool:
        nonlocal misses
        misses = 0 if core.probe_backend(port, timeout_s=5.0, token=token) is Backend.OURS else misses + 1
        return misses <= misses_allowed

    return alive


def window_command(port: int) -> list[str]:
    """The window process, under pythonw when there is one (no console window)."""
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if IS_WINDOWS and pythonw.is_file() else python
    return [str(exe), "-m", "src.launcher", "window", "--port", str(port)]


def open_app_window(root: Path, port: int, token: str, out: Callable[[str], None],
                    job: JobObject | None = None) -> None:
    """Focus the app window, or start its process. Started from the launcher, it goes in the
    launcher's kill-on-close job, so Quit closes it too; a second launch starts it detached, and
    it closes itself once the instance stops answering (``show``)."""
    if StopEvent(window_event_name(root)).exists():
        focus_existing()  # the window process is running: bring it forward, start no other
        return
    env = dict(os.environ)
    env[core.INSTANCE_TOKEN_ENV] = token  # never on the command line, which other accounts can read
    cmd = window_command(port)
    log = root / "logs" / "launcher" / "window-process.log"
    if job is not None:
        job.spawn(cmd, cwd=root, env=env, log_path=log)
    else:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as f:
            subprocess.Popen(cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,  # noqa: S603
                             stdout=f, stderr=subprocess.STDOUT, close_fds=True)
    out(f"Dashboard window: http://{core.LOOPBACK}:{port}")


def _message_box(message: str) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, core.APP_TITLE, 0x10 | 0x40000)  # type: ignore[attr-defined,unused-ignore]
    except Exception:  # noqa: BLE001 - best effort; window.log has the reason
        pass
