"""The notification-area app: autostart value, run notifications, tray actions,
backend restart (update plan F2). No real tray icon or registry is touched."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.launcher import core, run
from src.launcher.autostart import Autostart, command
from src.launcher.core import Backend
from src.launcher.tray import HINT_MARKER, RunWatcher, TrayApp
from tests.test_launcher import _launcher, free_port, windows_only

# -- autostart ------------------------------------------------------------------------------


class _Registry:
    def __init__(self, value: str | None = None) -> None:
        self.value = value

    def autostart(self, root: Path) -> Autostart:
        def write(v: str) -> None:
            self.value = v

        def delete() -> None:
            self.value = None

        return Autostart(root, read=lambda: self.value, write=write, delete=delete)


def test_autostart_command_starts_the_tray_without_a_window(tmp_path: Path) -> None:
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "pythonw.exe").write_bytes(b"")
    assert command(tmp_path) == f'"{tmp_path / "runtime" / "pythonw.exe"}" -m src.launcher tray --window none'


def test_autostart_on_off_round_trip(tmp_path: Path) -> None:
    reg = _Registry()
    auto = reg.autostart(tmp_path)
    assert not auto.enabled()
    auto.enable()
    assert auto.enabled() and reg.value == command(tmp_path)
    auto.disable()
    assert not auto.enabled() and reg.value is None


def test_autostart_never_removes_another_installations_value(tmp_path: Path) -> None:
    reg = _Registry('"C:\\Other\\runtime\\pythonw.exe" -m src.launcher tray --window none')
    auto = reg.autostart(tmp_path)
    assert not auto.enabled()
    auto.disable()
    assert reg.value is not None  # left alone


def test_autostart_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    from src.launcher.__main__ import cmd_autostart

    reg = _Registry()
    auto = reg.autostart(tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    assert cmd_autostart(auto, "on", run.Output(None)) == 0
    assert "Start with Windows: on" in capsys.readouterr().out
    assert cmd_autostart(auto, "off", run.Output(None)) == 0 and reg.value is None


# -- run notifications -------------------------------------------------------------------------


def _runs(*runs: dict) -> tuple[int, bytes]:
    return 200, json.dumps(list(runs)).encode()


def test_run_watcher_announces_only_runs_finishing_after_start() -> None:
    responses = [
        _runs({"id": "old", "status": "completed", "stats": {"scored": 9}}),
        _runs({"id": "old", "status": "completed", "stats": {"scored": 9}}),
        _runs({"id": "new", "status": "running", "stats": {}}),
        _runs({"id": "new", "status": "completed", "stats": {"scored": 12}}),
        _runs({"id": "new", "status": "completed", "stats": {"scored": 12}}),
        _runs({"id": "next", "status": "failed"}),
        None,
        (200, b"not json"),
    ]
    watcher = RunWatcher("http://127.0.0.1:1", fetch=lambda _url: responses.pop(0))
    messages = [watcher.poll() for _ in range(8)]
    assert messages[:3] == [None, None, None]  # the run that existed before start is not news
    assert messages[3] == "Job search finished: 12 offer(s) matched your profile."
    assert messages[4] is None  # announced once
    assert messages[5] and "failed" in messages[5]
    assert messages[6:] == [None, None]


def test_run_watcher_first_run_ever_is_announced() -> None:
    responses = [(200, b"[]"), _runs({"id": "r1", "status": "completed", "stats": {"scored": 3}})]
    watcher = RunWatcher("http://127.0.0.1:1", fetch=lambda _url: responses.pop(0))
    assert watcher.poll() is None
    assert watcher.poll() == "Job search finished: 3 offer(s) matched your profile."


# -- tray actions -------------------------------------------------------------------------------


class _FakeIcon:
    def __init__(self, name, image, title, menu) -> None:  # noqa: ANN001
        self.menu, self.notes, self.visible, self.stopped = menu, [], False, threading.Event()

    def notify(self, message: str, title: str) -> None:
        self.notes.append(message)

    def run(self, setup) -> None:  # noqa: ANN001
        setup(self)
        self.stopped.wait(30)

    def stop(self) -> None:
        self.stopped.set()


def _fake_pystray() -> SimpleNamespace:
    class MenuItem:
        def __init__(self, text, action, **kw) -> None:  # noqa: ANN001
            self.text, self.action, self.kw = text, action, kw

    class Menu:
        SEPARATOR = None

        def __init__(self, *items) -> None:  # noqa: ANN002
            self.items = [i for i in items if i is not None]

    return SimpleNamespace(Icon=_FakeIcon, Menu=Menu, MenuItem=MenuItem)


def _item(app: TrayApp, text: str):  # noqa: ANN202
    return next(i for i in app.icon.menu.items if i.text == text)


class _FakeLauncher:
    def __init__(self, tmp_path: Path) -> None:
        self.s = SimpleNamespace(root=tmp_path, logs_dir=tmp_path / "logs", backend_url="http://127.0.0.1:1")
        self.out = run.Output(None)
        self.graceful_stop_s = 1
        self.stop = threading.Event()
        self.notify = None

    def request_stop(self) -> None:
        self.stop.set()

    def run(self, on_ready) -> int:  # noqa: ANN001
        on_ready()
        self.stop.wait(30)
        return 0


def test_tray_quit_stops_the_launcher_and_the_icon(tmp_path: Path) -> None:
    launcher = _FakeLauncher(tmp_path)
    reg = _Registry()
    app = TrayApp(launcher, reg.autostart(tmp_path), pystray=_fake_pystray(),  # type: ignore[arg-type]
                  image_loader=lambda _p: object(),
                  watcher=RunWatcher("x", fetch=lambda _u: None))
    t = threading.Thread(target=lambda: setattr(app, "code", app.run()))
    t.start()
    deadline = time.monotonic() + 10
    while app.icon is None or not app.icon.notes:
        assert time.monotonic() < deadline, "the first-run hint was never shown"
        time.sleep(0.05)
    assert "keeps running" in app.icon.notes[0] and (launcher.s.logs_dir / HINT_MARKER).exists()

    toggle = _item(app, "Start with Windows")
    assert toggle.kw["checked"](toggle) is False
    toggle.action()
    assert toggle.kw["checked"](toggle) is True and reg.value

    _item(app, "Quit").action()
    t.join(15)
    assert not t.is_alive() and launcher.stop.is_set() and app.code == 0  # type: ignore[attr-defined]


def test_first_run_hint_is_shown_once(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / HINT_MARKER).touch()
    app = TrayApp(_FakeLauncher(tmp_path), _Registry().autostart(tmp_path), pystray=_fake_pystray(),  # type: ignore[arg-type]
                  image_loader=lambda _p: object(), watcher=RunWatcher("x", fetch=lambda _u: None))
    app.icon = _FakeIcon("", None, "", None)
    app._on_ready()
    app._done.set()
    assert app.icon.notes == []


# -- backend restart (real processes, as in test_launcher) ----------------------------------------------


@windows_only  # job objects, like the other real-process launcher tests
def test_tray_launcher_restarts_a_crashed_backend_once(tmp_path, monkeypatch):
    port = free_port()
    launcher = _launcher(tmp_path, port, monkeypatch)
    launcher.backend_restarts = 1
    launcher.handle_signals = False
    notes: list[str] = []
    launcher.notify = notes.append
    result: dict[str, int] = {}
    t = threading.Thread(target=lambda: result.setdefault("code", launcher.run()))
    t.start()
    try:
        deadline = time.monotonic() + 60
        while "backend" not in launcher.children or core.probe_backend(port, 0.5) is not Backend.OURS:
            assert time.monotonic() < deadline, "backend never started"
            time.sleep(0.2)
        first = launcher.children["backend"]
        first.kill()  # a crash
        deadline = time.monotonic() + 60
        while True:
            current = launcher.children.get("backend")
            if current is not None and current is not first and \
                    core.probe_backend(port, 0.5) is Backend.OURS:
                break
            assert time.monotonic() < deadline, "backend was not restarted"
            time.sleep(0.2)
        while not notes:  # the notification follows the readiness check
            assert time.monotonic() < deadline, "no notification after the restart"
            time.sleep(0.1)
        assert "restarted" in notes[0]
    finally:
        launcher.request_stop()
        t.join(60)
    assert result.get("code") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="the real pystray backend needs Windows")
def test_pystray_and_pillow_import_on_windows() -> None:
    import pystray  # noqa: F401
    from PIL import Image

    icon = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "FindYourJob.ico"
    assert Image.open(icon).size[0] > 0


# -- graceful stop without a console ------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="named events are Windows-only")
def test_backend_stops_gracefully_when_the_launcher_sets_its_event(monkeypatch) -> None:
    import signal

    from src.api import stop_signal
    from src.launcher.winproc import STOP_EVENT_ENV, StopEvent

    name = f"FindYourJobWithAI.test-stop.{time.monotonic_ns()}"
    monkeypatch.setenv(STOP_EVENT_ENV, name)
    raised = threading.Event()
    monkeypatch.setattr(stop_signal.signal, "raise_signal",
                        lambda sig: raised.set() if sig == signal.SIGBREAK else None)
    event = stop_signal.start_stop_signal_watcher()
    assert event is not None
    try:
        assert StopEvent(name).signal_existing()
        assert raised.wait(10), "SIGBREAK was not raised"
    finally:
        event.close()


def test_no_stop_event_without_the_launcher(monkeypatch) -> None:
    from src.api import stop_signal
    from src.launcher.winproc import STOP_EVENT_ENV

    monkeypatch.delenv(STOP_EVENT_ENV, raising=False)
    assert stop_signal.start_stop_signal_watcher() is None
