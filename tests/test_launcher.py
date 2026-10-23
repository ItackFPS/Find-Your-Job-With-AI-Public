"""The Windows launcher (src/launcher): probes, ownership, start/stop lifecycle.

The real backend is never started here: the lifecycle tests swap in a tiny HTTP
server that answers /api/health like the backend does, run from a temporary root.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.launcher import core, run
from src.launcher.core import Backend, Level, Settings
from src.launcher.winproc import IS_WINDOWS, JobObject, StopEvent, process_alive, process_creation_time

windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows job objects / named events")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """In-process HTTP server answering fixed (status, body) per path."""

    def __init__(self, routes: dict[str, tuple[int, bytes]]) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, body = routes.get(self.path, (404, b'{"detail": "Not Found"}'))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    servers: list[_Server] = []

    def make(routes: dict[str, tuple[int, bytes]]) -> _Server:
        s = _Server(routes)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


# -- configuration -------------------------------------------------------------------------


def test_settings_precedence(tmp_path):
    (tmp_path / ".env").write_text("FYJ_PORT=8100\nPHASE2_LLM_ENDPOINT=http://localhost:11434/\n",
                                   encoding="utf-8")
    s = Settings.load(tmp_path, environ={})
    assert (s.backend_port, s.llm_endpoint) == (8100, "http://localhost:11434")
    assert Settings.load(tmp_path, environ={"FYJ_PORT": "8200"}).backend_port == 8200
    assert Settings.load(tmp_path, environ={}, backend_port=9000).backend_port == 9000


def test_settings_bad_port_falls_back(tmp_path):
    for bad in ("eighty", "70000"):
        s = Settings.load(tmp_path, environ={"FYJ_PORT": bad})
        assert s.backend_port == core.DEFAULT_BACKEND_PORT


def test_stop_event_name_is_per_checkout(tmp_path):
    a = Settings(root=tmp_path / "a").stop_event_name
    b = Settings(root=tmp_path / "b").stop_event_name
    assert a != b and "\\" not in a


# -- probes --------------------------------------------------------------------------------


def test_probe_backend_free():
    assert core.probe_backend(free_port(), timeout_s=0.5) is Backend.FREE


def test_probe_backend_ours(server):
    s = server({"/api/health": (200, json.dumps({"status": "ok", "app": core.APP_ID}).encode())})
    assert core.probe_backend(s.port) is Backend.OURS


def test_probe_backend_recognises_backend_without_health(server):
    # an older backend answers unknown paths with the SPA page; its OpenAPI title names it
    s = server({"/api/health": (200, b"<!doctype html><html></html>"),
                "/api/openapi.json": (200, json.dumps({"info": {"title": core.APP_TITLE}}).encode())})
    assert core.probe_backend(s.port) is Backend.OURS


def test_probe_backend_reads_a_large_openapi_document(server):
    # the real document is far larger than one probe read (regression: it was truncated)
    doc = {"info": {"title": core.APP_TITLE}, "paths": {f"/api/p{i}": {} for i in range(20000)}}
    body = json.dumps(doc).encode()
    assert len(body) > 200_000
    s = server({"/api/health": (200, b"<!doctype html>"), "/api/openapi.json": (200, body)})
    assert core.probe_backend(s.port) is Backend.OURS


def test_probe_backend_foreign(server):
    s = server({"/api/health": (200, b'{"status": "ok"}')})
    assert core.probe_backend(s.port) is Backend.FOREIGN


def test_probe_llm(server):
    s = server({"/v1/models": (200, b'{"data": []}')})
    assert core.probe_llm(f"http://127.0.0.1:{s.port}/")
    assert not core.probe_llm(f"http://127.0.0.1:{free_port()}", timeout_s=0.5)


@pytest.mark.parametrize("url,expected", [
    ("http://localhost:1234", True), ("http://127.0.0.1:11434", True), ("http://[::1]:1234", True),
    ("http://192.168.1.10:1234", False), ("https://api.example.com", False),
])
def test_is_loopback_url(url, expected):
    assert core.is_loopback_url(url) is expected


# -- prerequisites and commands ------------------------------------------------------------


def _frontend(root: Path, *, built: bool, deps: bool) -> Path:
    fe = root / "frontend"
    (fe / "src").mkdir(parents=True)
    (fe / "src" / "main.tsx").write_text("x", encoding="utf-8")
    if deps:
        vite = fe / "node_modules" / "vite" / "bin" / "vite.js"
        vite.parent.mkdir(parents=True)
        vite.write_text("", encoding="utf-8")
    if built:
        (fe / "dist").mkdir()
        (fe / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    return fe


def test_frontend_stale_when_sources_newer(tmp_path):
    fe = _frontend(tmp_path, built=True, deps=True)
    old = time.time() - 100
    os.utime(fe / "dist" / "index.html", (old, old))
    assert core.frontend_state(tmp_path).stale
    os.utime(fe / "src" / "main.tsx", (old - 100, old - 100))
    assert not core.frontend_state(tmp_path).stale


def test_unbuilt_dashboard_without_node_is_an_error(tmp_path, monkeypatch):
    _frontend(tmp_path, built=False, deps=False)
    monkeypatch.setattr(core.shutil, "which", lambda _name: None)
    checks = {c.name: c for c in core.prerequisite_checks(tmp_path)}
    assert checks["dashboard"].level is Level.ERROR
    assert "Node.js" in checks["dashboard"].fix


def test_unbuilt_dashboard_with_node_is_built(tmp_path, monkeypatch):
    _frontend(tmp_path, built=False, deps=True)
    monkeypatch.setattr(core.shutil, "which", lambda _name: "node")
    checks = {c.name: c for c in core.prerequisite_checks(tmp_path)}
    assert checks["dashboard"].level is Level.INFO


def test_backend_binds_loopback_only():
    cmd = core.backend_command("C:\\Program Files\\Python\\python.exe", 8123)
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert cmd[0] == "C:\\Program Files\\Python\\python.exe"  # one argv item: spaces are safe
    assert "--reload" not in cmd


def test_backend_stops_within_the_launchers_grace_period():
    # an open dashboard keeps its live-events stream open: uvicorn waited for it until the launcher
    # killed the backend after GRACEFUL_STOP_S (seen in the 1.0.0 walkthrough)
    cmd = core.backend_command("python", 8123)
    assert float(cmd[cmd.index("--timeout-graceful-shutdown") + 1]) < run.GRACEFUL_STOP_S


def test_state_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "state.json"
    st = core.State(launcher_pid=1, launcher_created=2, mode="start",
                    components={"backend": core.Owned(3, 4, 8000)})
    st.save(path)
    loaded = core.State.load(path)
    assert loaded is not None and loaded.components["backend"].port == 8000
    path.write_text("{not json", encoding="utf-8")
    assert core.State.load(path) is None


def test_rotate_log_keeps_one_generation(tmp_path):
    log = tmp_path / "backend.log"
    log.write_bytes(b"x" * 20)
    core.rotate_log(log, max_bytes=10)
    assert not log.exists() and (tmp_path / "backend.log.1").read_bytes() == b"x" * 20


def test_cli_defaults_to_start():
    from src.launcher.__main__ import parse

    args = parse(["--port", "8100"])
    assert (args.command, args.port) == ("start", 8100)
    assert parse(["status"]).command == "status"


# -- process ownership ---------------------------------------------------------------------


def test_process_alive_checks_creation_time():
    me = os.getpid()
    created = process_creation_time(me)
    assert process_alive(me, created)
    if created is not None:
        assert not process_alive(me, created + 1)  # same pid, different process


@windows_only
def test_job_kills_child_and_grandchild_on_close(tmp_path):
    marker = tmp_path / "grandchild.pid"
    script = (
        "import subprocess, sys, time, pathlib\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path(r'{marker}').write_text(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    job = JobObject()
    child = job.spawn([sys.executable, "-c", script], cwd=tmp_path, env=dict(os.environ),
                      log_path=tmp_path / "child.log")
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    grandchild = int(marker.read_text())
    assert process_alive(grandchild, None)
    job.close()  # what happens when the launcher dies, however it dies
    child.wait(timeout=10)
    deadline = time.monotonic() + 10
    while process_alive(grandchild, None) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not process_alive(grandchild, None)


@windows_only
def test_job_lists_children_and_grandchildren(tmp_path):
    job = JobObject()
    try:
        child = job.spawn([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path,
                          env=dict(os.environ), log_path=tmp_path / "c.log")
        assert child.pid in job.pids()  # a venv's python.exe adds the real interpreter too
    finally:
        job.close()


@windows_only
def test_stop_event_single_instance_and_signal(tmp_path):
    name = Settings(root=tmp_path).stop_event_name
    owner = StopEvent(name)
    assert owner.create()
    try:
        assert not StopEvent(name).create()  # a second launcher is refused
        assert not owner.wait(0.05)
        assert StopEvent(name).signal_existing()
        assert owner.wait(1.0)
    finally:
        owner.close()
    assert not StopEvent(name).signal_existing()  # nothing left to signal


@windows_only
def test_a_stopping_owner_is_told_apart_from_a_running_one(tmp_path):
    name = Settings(root=tmp_path).stop_event_name
    assert not StopEvent(name).owner_stopping()               # nobody owns it
    owner = StopEvent(name)
    assert owner.create()
    try:
        assert not StopEvent(name).owner_stopping()           # running
        StopEvent(name).signal_existing()
        assert StopEvent(name).owner_stopping()               # stop requested, not finished
    finally:
        owner.close()


@windows_only
def test_a_start_while_the_app_is_stopping_waits_then_starts(tmp_path, monkeypatch):
    # seen in the 1.0.0 walkthrough: Stop, then Start a few seconds later -> "already running,
    # opening the dashboard", then the stopping launcher killed the backend: nothing was left running
    port = free_port()
    old = StopEvent(Settings(root=tmp_path).stop_event_name)
    assert old.create()
    old.signal_existing()                                      # the old launcher is stopping...
    threading.Timer(1.5, old.close).start()                    # ...and finishes 1.5 s later
    launcher = _launcher(tmp_path, port, monkeypatch)
    started: list[bool] = []
    monkeypatch.setattr(run.Launcher, "_start_all", lambda self: started.append(True) or 1)
    monkeypatch.setattr(run, "open_dashboard", lambda *a, **k: pytest.fail("opened a dying dashboard"))
    assert launcher.run() == 1                                 # _start_all's code: it did start
    assert started == [True]


# -- lifecycle -----------------------------------------------------------------------------

FAKE_BACKEND = """\
import http.server, json, os, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ok = self.path == "/api/health"
        health = {"status": "ok", "app": "find-your-job-with-ai", "pid": os.getpid()}
        body = json.dumps(health if ok else {}).encode()
        self.send_response(200 if ok else 404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def _launcher(tmp_path: Path, port: int, monkeypatch) -> run.Launcher:
    script = tmp_path / "fake_backend.py"
    script.write_text(FAKE_BACKEND, encoding="utf-8")
    monkeypatch.setattr(core, "backend_command",
                        lambda python, p, reload=False: [python, str(script), str(p)])
    monkeypatch.setattr(core, "prerequisite_checks", lambda root: [])
    monkeypatch.setattr(run.Launcher, "_ensure_frontend", lambda self: True)
    settings = Settings(root=tmp_path, backend_port=port,
                        llm_endpoint=f"http://127.0.0.1:{free_port()}", startup_timeout_s=60)
    launcher = run.Launcher(settings, run.Output(None), mode="start", window="none")
    launcher.graceful_stop_s = 5
    return launcher


def test_launcher_starts_owns_and_stops_backend(tmp_path, monkeypatch):
    port = free_port()
    launcher = _launcher(tmp_path, port, monkeypatch)
    seen: dict[str, object] = {}

    def stop_when_ready() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if core.probe_backend(port, timeout_s=0.5) is Backend.OURS and "backend" in launcher.children:
                seen["pid"] = launcher.children["backend"].pid
                seen["state"] = core.State.load(launcher.s.state_file)
                break
            time.sleep(0.2)
        launcher.stop_requested = True

    threading.Thread(target=stop_when_ready, daemon=True).start()
    assert launcher.run() == 0
    state = seen["state"]
    assert isinstance(state, core.State) and state.components["backend"].pid == seen["pid"]
    assert not process_alive(int(seen["pid"]), None)          # stopped with the launcher
    assert core.probe_backend(port, timeout_s=0.5) is Backend.FREE
    assert not launcher.s.state_file.exists()


def test_launcher_reuses_running_backend_and_leaves_it_running(tmp_path, monkeypatch, server):
    external = server({"/api/health": (200, json.dumps({"app": core.APP_ID}).encode())})
    launcher = _launcher(tmp_path, external.port, monkeypatch)
    threading.Timer(1.0, lambda: setattr(launcher, "stop_requested", True)).start()
    assert launcher.run() == 0
    assert launcher.children == {}
    assert core.probe_backend(external.port) is Backend.OURS  # not ours to stop


def test_launcher_refuses_port_held_by_another_program(tmp_path, monkeypatch, server):
    other = server({"/api/health": (200, b'{"hello": "world"}')})
    launcher = _launcher(tmp_path, other.port, monkeypatch)
    assert launcher.run() == 1
    assert launcher.children == {}


def test_launcher_reports_backend_crash_at_startup(tmp_path, monkeypatch):
    launcher = _launcher(tmp_path, free_port(), monkeypatch)
    monkeypatch.setattr(core, "backend_command",
                        lambda python, p, reload=False: [python, "-c", "print('boom'); raise SystemExit(3)"])
    lines: list[str] = []
    launcher.out = lambda line="": lines.append(line)  # type: ignore[assignment]
    assert launcher.run() == 1
    assert any("exited during startup (code 3)" in line for line in lines)
    assert any("boom" in line for line in lines)


def test_stop_requested_while_backend_starts_is_a_clean_exit(tmp_path, monkeypatch):
    """Ctrl+C or closing the window before the backend is ready is not a failure:
    exit code 0 (so FindYourJob.cmd does not pause), and the child is still stopped."""
    launcher = _launcher(tmp_path, free_port(), monkeypatch)
    monkeypatch.setattr(core, "backend_command",  # starts, never answers
                        lambda python, p, reload=False: [python, "-c", "import time; time.sleep(60)"])
    seen: dict[str, int] = {}

    def stop_once_spawned() -> None:
        deadline = time.monotonic() + 30
        while "backend" not in launcher.children and time.monotonic() < deadline:
            time.sleep(0.05)
        seen["pid"] = launcher.children["backend"].pid
        launcher.stop_requested = True

    threading.Thread(target=stop_once_spawned, daemon=True).start()
    assert launcher.run() == 0
    assert not process_alive(seen["pid"], None)
    assert not launcher.s.state_file.exists()


@windows_only
def test_stop_command_stops_a_running_launcher(tmp_path, monkeypatch):
    port = free_port()
    launcher = _launcher(tmp_path, port, monkeypatch)
    result: dict[str, int] = {}

    def stop_when_ready() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and core.probe_backend(port, timeout_s=0.5) is not Backend.OURS:
            time.sleep(0.2)
        result["stop"] = run.cmd_stop(launcher.s, run.Output(None), timeout_s=30)

    t = threading.Thread(target=stop_when_ready, daemon=True)
    t.start()
    assert launcher.run() == 0
    t.join(timeout=40)
    assert result.get("stop") == 0
    assert core.probe_backend(port, timeout_s=0.5) is Backend.FREE


def test_status_lists_every_component(tmp_path):
    settings = Settings(root=tmp_path, backend_port=free_port(),
                        llm_endpoint="http://192.168.0.2:1")
    rows = {name: text for _level, name, text in run.status_lines(settings)}
    assert set(rows) >= {"backend", "dashboard", "local LLM"}
    assert rows["backend"] == "not running"
    assert "NOT a local address" in rows["local LLM"]


def test_shortcut_command_builds_no_script_from_paths(monkeypatch, tmp_path):
    if not IS_WINDOWS:
        pytest.skip("Windows shortcuts")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, env, **kwargs):
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(run.subprocess, "run", fake_run)
    monkeypatch.setattr(run, "shortcut_paths", lambda: {"start-menu": tmp_path / "x" / "a'b.lnk"})
    settings = Settings(root=tmp_path / "with space & 'quote'")
    assert run.cmd_shortcut(settings, run.Output(None), "start-menu", remove=False) == 0
    cmd, env = calls[0]
    assert "quote" not in " ".join(cmd)  # paths only ever reach PowerShell as env vars
    assert env["FYJ_TARGET"].endswith("FindYourJob.cmd")


def test_launcher_reuses_a_backend_that_won_the_port_race(tmp_path, monkeypatch):
    # the logon task's backend binds the port while ours is still starting; ours exits
    launcher = _launcher(tmp_path, free_port(), monkeypatch)
    monkeypatch.setattr(core, "backend_command",
                        lambda python, p, reload=False: [python, "-c", "raise SystemExit(1)"])
    answers = iter([Backend.FREE])
    monkeypatch.setattr(core, "probe_backend",
                        lambda port, timeout_s=2.0: next(answers, Backend.OURS))
    monkeypatch.setattr(core, "backend_pid", lambda port, timeout_s=2.0: 4)  # not our child
    lines: list[str] = []
    launcher.out = lines.append  # type: ignore[assignment]
    threading.Timer(3.0, lambda: setattr(launcher, "stop_requested", True)).start()
    assert launcher.run() == 0
    assert launcher.children == {}
    assert any("reusing it" in line for line in lines)
    assert not any("exited during startup" in line for line in lines)


def test_port_open_but_silent_gets_a_grace_period(tmp_path, monkeypatch):
    launcher = _launcher(tmp_path, free_port(), monkeypatch)
    answers = iter([Backend.STARTING, Backend.STARTING])
    monkeypatch.setattr(core, "probe_backend",
                        lambda port, timeout_s=2.0: next(answers, Backend.OURS))
    threading.Timer(2.0, lambda: setattr(launcher, "stop_requested", True)).start()
    assert launcher.run() == 0
    assert launcher.children == {}  # reused once it answered


def test_port_that_never_answers_is_refused(tmp_path, monkeypatch):
    launcher = _launcher(tmp_path, free_port(), monkeypatch)
    monkeypatch.setattr(run, "STARTING_GRACE_S", 1.0)
    monkeypatch.setattr(core, "probe_backend", lambda port, timeout_s=2.0: Backend.STARTING)
    assert launcher.run() == 1
