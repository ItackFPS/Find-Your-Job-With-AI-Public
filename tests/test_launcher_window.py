"""The app's own window process (src/launcher/window.py; update plan F2.2, owner decision K4).

pywebview is never imported here: the window factory and the fallback are injected, and the
backend is a tiny HTTP server answering /api/health like the real one.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.launcher import core, window
from src.launcher.winproc import IS_WINDOWS, StopEvent

windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows named events")


class _Health:
    def __init__(self, token: str | None) -> None:
        body = json.dumps({"status": "ok", "app": core.APP_ID,
                           **({"instance_token": token} if token else {})}).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200 if self.path == "/api/health" else 404)
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
def backend():
    servers: list[_Health] = []

    def make(token: str | None) -> _Health:
        servers.append(_Health(token))
        return servers[-1]

    yield make
    for s in servers:
        s.close()


def _main(tmp_path: Path, port: int, token: str, *, available: bool = True):
    shown: list[str] = []
    fallback: list[str] = []
    alerts: list[str] = []
    code = window.window_main(tmp_path, port, token, show_window=lambda url, root, alive: shown.append(url),
                              fallback=fallback.append, available=lambda: available, alert=alerts.append)
    return code, shown, fallback, alerts


def test_the_window_shows_the_dashboard_of_its_own_instance(tmp_path, backend):
    b = backend("tok")
    code, shown, fallback, alerts = _main(tmp_path, b.port, "tok")
    assert (code, shown, fallback, alerts) == (0, [f"http://127.0.0.1:{b.port}"], [], [])


def test_the_window_never_shows_a_foreign_or_stale_process(tmp_path, backend):
    impostor = backend("another-token")
    code, shown, fallback, alerts = _main(tmp_path, impostor.port, "tok")
    assert code == 3 and shown == [] and fallback == []
    assert "not this app's running instance" in alerts[0]
    log = (tmp_path / "logs" / "launcher" / "window.log").read_text(encoding="utf-8")
    assert "tok" not in log.replace("another-token", "")       # the token is never written down


def test_the_window_watches_its_instance(backend):
    b = backend("tok")
    alive = window._instance_alive(b.port, "tok", misses_allowed=1)
    assert alive() and alive()
    b.close()                                          # the app quit
    assert alive()                                     # one miss is tolerated
    assert not alive()


def test_without_webview2_the_edge_app_window_is_the_fallback(tmp_path, backend):
    b = backend("tok")
    code, shown, fallback, _alerts = _main(tmp_path, b.port, "tok", available=False)
    assert code == 0 and shown == [] and fallback == [f"http://127.0.0.1:{b.port}"]


@windows_only
def test_one_window_per_installation(tmp_path, backend, monkeypatch):
    b = backend("tok")
    monkeypatch.setattr(window, "focus_existing", lambda: True)
    held = StopEvent(window.window_event_name(tmp_path))        # a window is already open
    assert held.create()
    try:
        code, shown, _f, _a = _main(tmp_path, b.port, "tok")
    finally:
        held.close()
    assert code == 1 and shown == []


def test_show_uses_webview2_with_the_app_title_and_icon(tmp_path):
    calls: dict[str, object] = {}
    native = SimpleNamespace(destroy=lambda: calls.update(destroyed=True))

    def start(func, **k):  # noqa: ANN001
        calls.update(start=k)
        func()                                      # pywebview runs it once the window exists

    native.events = SimpleNamespace(before_show=_Handlers())
    fake = SimpleNamespace(create_window=lambda *a, **k: calls.update(window=(a, k)) or native, start=start)
    answers = iter([True, True, False])               # the instance stops answering (Quit, a crash)
    window.show("http://127.0.0.1:5", tmp_path, alive=lambda: next(answers), webview=fake, poll_s=0.01)
    assert calls["destroyed"] is True                 # the window does not outlive the app
    (title, url), kwargs = calls["window"]  # type: ignore[misc]
    assert (title, url) == (core.APP_TITLE, "http://127.0.0.1:5")
    started = calls["start"]
    assert started["gui"] == "edgechromium" and str(started["icon"]).endswith("FindYourJob.ico")  # type: ignore[index]
    assert started["private_mode"] is False                                                    # type: ignore[index]


def test_closing_the_window_ends_its_watcher_at_once(tmp_path):
    import threading
    import time

    threads: list[threading.Thread] = []

    def start(func, **_k):  # noqa: ANN001
        threads.append(threading.Thread(target=func))  # as pywebview does: a non-daemon thread
        threads[0].start()
        time.sleep(0.05)                                # ...then the user closes the window

    fake = SimpleNamespace(create_window=lambda *a, **k: SimpleNamespace(
        destroy=lambda: None, events=SimpleNamespace(before_show=_Handlers())), start=start)
    window.show("http://127.0.0.1:5", tmp_path, alive=lambda: True, webview=fake, poll_s=30)
    threads[0].join(2)
    assert not threads[0].is_alive()                    # the process is not held open for 30 s


def test_the_window_process_gets_the_token_by_environment_not_argv(tmp_path, monkeypatch):
    spawned: list[tuple[list[str], dict[str, str]]] = []

    class FakeJob:
        def spawn(self, cmd, *, cwd, env, log_path):  # noqa: ANN001
            spawned.append((cmd, env))

    monkeypatch.setattr(window, "focus_existing", lambda: False)
    window.open_app_window(tmp_path, 51234, "secret-token", lambda _l: None, job=FakeJob())  # type: ignore[arg-type]
    cmd, env = spawned[0]
    assert cmd[-4:] == ["src.launcher", "window", "--port", "51234"] and "secret-token" not in cmd
    assert env[core.INSTANCE_TOKEN_ENV] == "secret-token"
    if IS_WINDOWS and Path(sys.executable).with_name("pythonw.exe").is_file():
        assert cmd[0].lower().endswith("pythonw.exe")                  # no console window


def test_window_cli_reads_the_token_from_the_environment(monkeypatch, tmp_path):
    from src.launcher import __main__ as cli

    seen: dict[str, object] = {}
    monkeypatch.setattr(core, "project_root", lambda: tmp_path)
    monkeypatch.setattr(cli.os, "chdir", lambda _p: None)
    monkeypatch.setenv(core.INSTANCE_TOKEN_ENV, "tok")
    monkeypatch.setattr(window, "window_main", lambda root, port, token: seen.update(port=port, token=token) or 0)
    assert cli.main(["window", "--port", "51234"]) == 0
    assert seen == {"port": 51234, "token": "tok"}
    assert core.INSTANCE_TOKEN_ENV not in __import__("os").environ   # not inherited by WebView2


@pytest.mark.skipif(sys.platform != "win32", reason="pywebview's Windows backend")
def test_pywebview_and_its_windows_backend_import():
    import clr_loader  # noqa: F401
    import webview  # noqa: F401
    import webview.platforms.winforms  # noqa: F401  # loads pythonnet + WebView2 assemblies


class _Handlers(list):
    """Stands in for a pywebview event / a .NET event: ``+=`` subscribes."""

    def __iadd__(self, handler):  # noqa: ANN001, ANN204
        self.append(handler)
        return self


@pytest.mark.parametrize("url, allowed", [
    ("http://127.0.0.1:5123/jobs?x=1", True),
    ("about:blank", True),
    ("http://127.0.0.1:5124/", False),                 # another local service
    ("https://127.0.0.1:5123/", False),
    ("http://localhost:5123/", False),
    ("https://www.hellowork.com/fr-fr/emplois/1.html", False),
    ("file:///C:/Windows/win.ini", False),
])
def test_only_the_backend_origin_loads_in_the_window(url, allowed):
    assert window.navigation_allowed(url, "http://127.0.0.1:5123") is allowed


def test_external_links_open_in_the_default_browser_not_in_the_window():
    opened: list[str] = []
    webview2 = SimpleNamespace(NavigationStarting=_Handlers())
    app_window = SimpleNamespace(events=SimpleNamespace(before_show=_Handlers()),
                                 native=SimpleNamespace(webview=webview2))
    window.guard_navigation(app_window, "http://127.0.0.1:5123", opener=opened.append)
    app_window.events.before_show[0]()                 # the form exists: the guard attaches
    navigate = webview2.NavigationStarting[0]

    def go(url: str) -> bool:
        args = SimpleNamespace(Uri=url, Cancel=False)
        navigate(None, args)
        return args.Cancel

    assert go("http://127.0.0.1:5123/jobs") is False
    assert go("https://www.linkedin.com/jobs/view/1") is True
    assert go("mailto:hr@example.com") is True
    assert opened == ["https://www.linkedin.com/jobs/view/1"]      # mailto is refused, not opened
