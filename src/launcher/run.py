"""Start, watch and stop the application's processes; report their state.

Ownership rule: the launcher stops only what it started itself (children in its
kill-on-close job). A backend that was already running — the Task Scheduler
startup tasks, a terminal — is reused and reported as "external", never stopped.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from datetime import datetime
from pathlib import Path

from src.launcher import core
from src.launcher.core import Backend, Level, Settings
from src.launcher.winproc import (
    IS_WINDOWS,
    JobObject,
    StopEvent,
    process_alive,
    process_creation_time,
    request_graceful_stop,
)

MARK = {Level.OK: "[ok]", Level.INFO: "[..]", Level.WARN: "[!!]", Level.ERROR: "[XX]"}
GRACEFUL_STOP_S = 20.0
# a launch while the previous launcher is still stopping waits for it, then starts the app
STOPPING_WAIT_S = GRACEFUL_STOP_S + 40.0
STARTING_GRACE_S = 30.0  # how long a port that is open but silent may take to answer


class Output:
    """Console lines, mirrored to logs/launcher/launcher.log (no secrets are ever printed)."""

    def __init__(self, log_path: Path | None) -> None:
        self.log_path = log_path

    def __call__(self, line: str = "") -> None:
        print(line, flush=True)
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
        except OSError:
            pass


def print_checks(out: Output, checks: list[core.Check]) -> None:
    for c in checks:
        out(f"  {MARK[c.level]} {c.name:<10} {c.message}")
        if c.fix and c.level >= Level.WARN:
            out(f"       fix: {c.fix}")


# -- status ------------------------------------------------------------------------------------


def _owned_alive(state: core.State | None, name: str) -> core.Owned | None:
    if state is None or name not in state.components:
        return None
    owned = state.components[name]
    return owned if process_alive(owned.pid, owned.created) else None


def status_lines(settings: Settings) -> list[tuple[Level, str, str]]:
    state = core.State.load(settings.state_file)
    if state and not process_alive(state.launcher_pid, state.launcher_created):
        state = None  # left over from a launcher that was killed: its children died with it
    rows: list[tuple[Level, str, str]] = []

    backend = core.probe_backend(settings.backend_port)
    owned = _owned_alive(state, "backend")
    if backend is Backend.OURS:
        who = (f"started by the launcher, pid {owned.pid}" if owned else
               "started outside the launcher, e.g. by the FindYourJobWithAI_Backend startup task")
        rows.append((Level.OK, "backend", f"running at {settings.backend_url} ({who})"))
    elif backend is Backend.STARTING:
        rows.append((Level.INFO, "backend", f"port {settings.backend_port} open, not answering yet"))
    elif backend is Backend.FOREIGN:
        rows.append((Level.ERROR, "backend",
                     f"port {settings.backend_port} is used by another program"))
    else:
        rows.append((Level.INFO, "backend", "not running"))

    fe = core.frontend_state(settings.root)
    if fe.built:
        rows.append((Level.WARN if fe.stale else Level.OK, "dashboard",
                     "built, older than its sources" if fe.stale else "built, up to date"))
    else:
        rows.append((Level.WARN, "dashboard", "not built"))

    llm = settings.llm_endpoint
    where = "" if core.is_loopback_url(llm) else " -- NOT a local address"
    if core.probe_llm(llm):
        rows.append((Level.OK if not where else Level.WARN, "local LLM", f"answering at {llm}{where}"))
    else:
        rows.append((Level.INFO, "local LLM",
                     f"not reachable at {llm}{where} (only AI-enabled runs need it; "
                     "the launcher never starts it)"))
    if state is not None:
        rows.append((Level.INFO, "launcher", f"running ({state.mode} mode, pid {state.launcher_pid})"))
    return rows


def cmd_status(settings: Settings, out: Output) -> int:
    out(f"{core.APP_TITLE} -- status")
    for level, name, text in status_lines(settings):
        out(f"  {MARK[level]} {name:<10} {text}")
    return 0


def cmd_stop(settings: Settings, out: Output, timeout_s: float = 60.0) -> int:
    event = StopEvent(settings.stop_event_name)
    if not event.signal_existing():
        out("No launcher is running for this folder; nothing to stop.")
        if core.probe_backend(settings.backend_port) is Backend.OURS:
            out("The backend is running, but it was not started by the launcher (for example the")
            out("FindYourJobWithAI_Backend startup task). The launcher never stops processes it")
            out("did not start: use Task Scheduler, or scripts\\uninstall_startup.py to remove the task.")
        return 0
    out("Stop requested; waiting for the launcher to shut its services down ...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = core.State.load(settings.state_file)
        if state is None or not process_alive(state.launcher_pid, state.launcher_created):
            out("Stopped.")
            return 0
        time.sleep(0.5)
    out("The launcher did not stop in time; close its window to stop everything it started.")
    return 1


# -- start / dev -------------------------------------------------------------------------------


def find_app_browser() -> str | None:
    """Edge or Chrome, for a chrome-less ``--app`` window; None if neither is found."""
    if sys.platform != "win32":
        return None
    import winreg

    for exe in ("msedge.exe", "chrome.exe"):
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                    path = winreg.QueryValue(k, None)
            except OSError:
                continue
            if path and Path(path.strip('"')).is_file():
                return path.strip('"')
    return None


def open_dashboard(url: str, window: str, out: Output) -> None:
    if window == "none":
        return
    browser = find_app_browser() if window == "app" else None
    if browser:
        subprocess.Popen([browser, f"--app={url}"], stdin=subprocess.DEVNULL,  # noqa: S603
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    else:
        webbrowser.open(url)
    out(f"Dashboard: {url}")


class Launcher:
    def __init__(self, settings: Settings, out: Output, *, mode: str,
                 window: str, reload: bool = False) -> None:
        self.s = settings
        self.out = out
        self.mode = mode
        self.window = window
        self.reload = reload
        self.job: JobObject | None = None
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.event = StopEvent(settings.stop_event_name)
        self.stop_requested = False
        self.graceful_stop_s = GRACEFUL_STOP_S
        self._previous_handlers: dict[int, Any] = {}
        self.state = core.State(launcher_pid=os.getpid(),
                                launcher_created=process_creation_time(os.getpid()),
                                mode=mode)

    # ---- lifecycle

    def run(self) -> int:
        out = self.out
        out(f"{core.APP_TITLE} -- starting ({self.mode} mode)")
        checks = core.prerequisite_checks(self.s.root)
        if self.mode == "dev":  # the dev server replaces the build
            checks = [c for c in checks if c.name != "dashboard"]
        print_checks(out, checks)
        if any(c.level is Level.ERROR for c in checks):
            out("")
            out("Cannot start: fix the [XX] items above, then run this again.")
            return 2
        if not self.event.create():
            if not self.event.owner_stopping():
                out("The app is already running (another launcher window owns it); opening the dashboard.")
                open_dashboard(self._dashboard_url(), self.window, out)
                return 0
            # Stop, then Start at once: opening the dashboard now would show a backend about to exit
            out("The app is still stopping; starting it again once it has stopped ...")
            deadline = time.monotonic() + STOPPING_WAIT_S
            while not self.event.create():
                if time.monotonic() > deadline:
                    out(f"It has not stopped after {STOPPING_WAIT_S:.0f}s; try again in a moment.")
                    return 1
                time.sleep(0.5)
        self._install_signal_handlers()
        self.state.save(self.s.state_file)
        self.job = JobObject()
        try:
            code = self._start_all()
            if code == 0:
                code = self._watch()
            elif self.stop_requested:
                code = 0  # stopped while starting (Ctrl+C, window closed): not a failure
            return code
        except KeyboardInterrupt:
            return 0
        finally:
            self.shutdown()

    def _start_all(self) -> int:
        if self.mode == "start" and not self._ensure_frontend():
            return 1
        if not self._ensure_backend():
            return 1
        if self.mode == "dev" and not self._start_vite():
            return 1
        self._report_llm()
        open_dashboard(self._dashboard_url(), self.window, self.out)
        self.out("")
        for level, name, text in status_lines(self.s):
            self.out(f"  {MARK[level]} {name:<10} {text}")
        self.out("")
        self.out(f"Logs: {self.s.logs_dir}  (backend log: {self.s.root / 'logs' / 'backend'})")
        self.out("Press Ctrl+C, close this window, or run 'FindYourJob.cmd stop' to stop "
                 "everything this window started.")
        return 0

    def _watch(self) -> int:
        while not self.stop_requested:
            if self.event.wait(1.0):
                self.out("Stop requested.")
                return 0
            for name, proc in list(self.children.items()):
                if proc.poll() is None:
                    continue
                del self.children[name]
                self.out(f"[!!] {name} exited unexpectedly (code {proc.returncode}). Last log lines:")
                for line in core.log_tail(self._log(name)):
                    self.out(f"     {line}")
                if name == "backend":
                    self.out("The dashboard cannot work without the backend; stopping.")
                    return 1
        return 0

    def shutdown(self) -> None:
        # reverse start order: the dev server before the backend it calls
        for name in reversed(list(self.children)):
            proc = self.children[name]
            if proc.poll() is not None:
                continue
            self.out(f"Stopping {name} ...")
            request_graceful_stop(proc)
            try:
                proc.wait(timeout=self.graceful_stop_s)
            except subprocess.TimeoutExpired:
                self.out(f"[!!] {name} did not stop within {self.graceful_stop_s:.0f}s; terminating it.")
                proc.kill()
        if self.job is not None:
            self.job.terminate_all()
            self.job.close()
            self.job = None
        self.children.clear()
        try:
            self.s.state_file.unlink()
        except OSError:
            pass
        self.event.close()
        self._restore_signal_handlers()

    # ---- components

    def _ensure_frontend(self) -> bool:
        fe = core.frontend_state(self.s.root)
        if fe.built and not fe.stale:
            return True
        if not fe.can_build:
            if fe.built:
                self.out("[!!] dashboard build is out of date and cannot be rebuilt here; using it anyway.")
                return True
            return False  # reported by the prerequisite checks
        assert fe.node is not None
        self.out("Building the dashboard (frontend/dist) ...")
        proc = self._spawn("frontend-build", core.frontend_build_command(self.s.root, fe.node),
                           cwd=self.s.root / "frontend", own=False)
        try:
            code = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = -1
        if code == 0:
            self.out("[ok] dashboard built")
            return True
        self.out(f"[XX] dashboard build failed (code {code}). Last log lines:")
        for line in core.log_tail(self._log("frontend-build")):
            self.out(f"     {line}")
        if fe.built:
            self.out("     continuing with the previous build")
            return True
        return False

    def _ensure_backend(self) -> bool:
        port = self.s.backend_port
        state = core.probe_backend(port)
        deadline = time.monotonic() + STARTING_GRACE_S
        while state is Backend.STARTING and time.monotonic() < deadline:
            time.sleep(0.5)  # a server that has just bound the port answers soon
            state = core.probe_backend(port)
        if state is Backend.OURS:
            self._reusing_backend()
            return True
        if state is not Backend.FREE:
            self.out(f"[XX] port {port} is already used by another program.")
            self.out(f"     fix: stop that program, or choose another port: FindYourJob.cmd --port {port + 1}")
            return False
        self.out(f"Starting the backend on {self.s.backend_url} ...")
        cmd = core.backend_command(sys.executable, port, reload=self.reload)
        proc = self._spawn("backend", cmd, cwd=self.s.root, port=port)
        # uvicorn binds the port only after the app's startup: a backend started at the
        # same moment elsewhere (the logon task) can win the port, and ours then exits.
        # Ready means OUR child answers (same pid), not merely "the app answers".
        lost_race = lambda: core.probe_backend(port) is Backend.OURS  # noqa: E731
        ok = self._wait_until(lambda: core.backend_pid(port) in self._owned_pids(proc), "backend",
                              proc, self.s.startup_timeout_s, exit_explained=lost_race)
        if ok and proc.poll() is None:
            self.out("[ok] backend ready")
            return True
        if proc.poll() is not None and lost_race():
            self.state.components.pop("backend", None)
            self.state.save(self.s.state_file)
            self._reusing_backend()
            return True
        return False

    def _owned_pids(self, proc: subprocess.Popen[bytes]) -> set[int]:
        return (self.job.pids() if self.job is not None else set()) | {proc.pid}

    def _reusing_backend(self) -> None:
        self.out(f"[ok] backend already running at {self.s.backend_url}; reusing it "
                 "(this window will not stop it)")

    def _start_vite(self) -> bool:
        fe = core.frontend_state(self.s.root)
        if not fe.can_build or fe.node is None:
            self.out("[XX] dev mode needs Node.js and the frontend packages.")
            self.out("     fix: cd frontend && npm ci")
            return False
        port = self.s.vite_port
        if core.port_in_use(port):
            self.out(f"[XX] port {port} (dev server) is already in use.")
            return False
        env = _environ()
        env["FYJ_API_URL"] = self.s.backend_url
        self.out(f"Starting the dashboard dev server on http://{core.LOOPBACK}:{port} ...")
        proc = self._spawn("vite", core.vite_dev_command(self.s.root, fe.node, port),
                           cwd=self.s.root / "frontend", port=port, env=env)
        return self._wait_until(lambda: core.port_in_use(port), "vite", proc, 60.0)

    def _report_llm(self) -> None:
        llm = self.s.llm_endpoint
        if not core.is_loopback_url(llm):
            self.out(f"[!!] PHASE2_LLM_ENDPOINT points to a non-local address ({llm}). AI-enabled "
                     "runs refuse it and run without AI, unless FYJ_ALLOW_REMOTE_LLM=1.")

    # ---- helpers

    def _dashboard_url(self) -> str:
        if self.mode == "dev":
            return f"http://{core.LOOPBACK}:{self.s.vite_port}"
        return self.s.backend_url

    def _log(self, name: str) -> Path:
        return self.s.logs_dir / f"{name}.log"

    def _spawn(self, name: str, cmd: list[str], *, cwd: Path, port: int | None = None,
               env: dict[str, str] | None = None, own: bool = True) -> subprocess.Popen[bytes]:
        assert self.job is not None
        log = self._log(name)
        core.rotate_log(log)
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} launcher: "
                    f"starting {name} =====\n")
        proc = self.job.spawn(cmd, cwd=cwd, env=env or _environ(), log_path=log)
        if own:
            self.children[name] = proc
            self.state.components[name] = core.Owned(proc.pid, process_creation_time(proc.pid), port)
            self.state.save(self.s.state_file)
        return proc

    def _wait_until(self, ready: Callable[[], bool], name: str, proc: subprocess.Popen[bytes],
                    timeout_s: float, fatal: bool = True,
                    exit_explained: Callable[[], bool] | None = None) -> bool:
        """Wait for ``ready()``. If the child exits first, report it with its last log
        lines — unless ``exit_explained()`` says the exit is expected (caller handles it)."""
        deadline = time.monotonic() + timeout_s
        next_note = time.monotonic() + 15
        while time.monotonic() < deadline and not self.stop_requested:
            if proc.poll() is not None:
                if exit_explained is not None and exit_explained():
                    self.children.pop(name, None)
                    return False
                self.out(f"[{'XX' if fatal else '!!'}] {name} exited during startup "
                         f"(code {proc.returncode}). Last log lines:")
                for line in core.log_tail(self._log(name)):
                    self.out(f"     {line}")
                self.children.pop(name, None)
                return False
            if ready():
                return True
            if time.monotonic() > next_note:
                self.out(f"     still waiting for {name} ...")
                next_note = time.monotonic() + 15
            time.sleep(0.5)
        if not self.stop_requested:
            self.out(f"[{'XX' if fatal else '!!'}] {name} did not become ready within {timeout_s:.0f}s; "
                     f"see {self._log(name)}")
        return False

    def _install_signal_handlers(self) -> None:
        def request_stop(_signum: int, _frame: object) -> None:
            self.stop_requested = True

        # Window close / logoff / shutdown arrive as SIGBREAK on Windows.
        for name in ("SIGBREAK", "SIGTERM"):
            if hasattr(signal, name):
                sig = getattr(signal, name)
                self._previous_handlers[sig] = signal.signal(sig, request_stop)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._previous_handlers.items():
            signal.signal(sig, handler)
        self._previous_handlers.clear()


def _environ() -> dict[str, str]:
    return dict(os.environ)


# -- shortcut ----------------------------------------------------------------------------------


def shortcut_paths() -> dict[str, Path]:
    appdata = Path(os.environ.get("APPDATA", ""))
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    return {
        "start-menu": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{core.APP_TITLE}.lnk",
        "desktop": home / "Desktop" / f"{core.APP_TITLE}.lnk",
    }


def cmd_shortcut(settings: Settings, out: Output, where: str, remove: bool) -> int:
    """Create or remove a per-user shortcut to FindYourJob.cmd (only when asked)."""
    if not IS_WINDOWS:
        out("Shortcuts are only supported on Windows.")
        return 2
    path = shortcut_paths()[where]
    if remove:
        try:
            path.unlink()
            out(f"Removed {path}")
        except FileNotFoundError:
            out(f"No shortcut at {path}")
        return 0
    target = settings.root / "FindYourJob.cmd"
    # Values travel as environment variables, never spliced into the script text.
    script = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:FYJ_LNK);"
              "$s.TargetPath=$env:FYJ_TARGET;$s.WorkingDirectory=$env:FYJ_DIR;"
              "$s.Description='Find Your Job With AI';$s.Save()")
    env = _environ()
    env.update(FYJ_LNK=str(path), FYJ_TARGET=str(target), FYJ_DIR=str(settings.root))
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(  # noqa: S603
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        out(f"Could not create the shortcut: {result.stderr.strip()}")
        return 1
    out(f"Created {path}")
    return 0
