"""The lab's AppContainer sandbox (``src/lab/sandbox.py``): what it grants, and — on Windows — that the OS
actually enforces it.

The live test starts a real AppContainer process and tries, from inside, everything the boundary must
stop: writing ``jobs.db``, ``config/`` and the data folder, reading secrets (including one created after
the grant), reaching a loopback port that IS listening, and the internet. ``isolation-check``'s own code
then runs inside the container and must report ``os_enforced`` without any stand-in or monkeypatch."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

from src.lab import __main__ as lab_cli
from src.lab import sandbox

ROOT = Path(__file__).resolve().parents[1]
SID = "S-1-15-2-1-2-3-4-5-6-7"


def _tree(root: Path) -> Path:
    (root / "config").mkdir(parents=True)
    (root / "config" / "filters.yaml").write_text("a: 1\n")
    (root / "config" / "notifications.local.yaml").write_text("name: me\n")
    (root / "data" / "lab").mkdir(parents=True)
    sqlite3.connect(root / "data" / "jobs.db").close()
    (root / ".env").write_text("SECRET=x\n")
    (root / ".env.example").write_text("SECRET=\n")
    (root / "README.md").write_text("readme\n")
    for name in ("__init__.py", "lab/__init__.py", "lab/isolation.py", "lab/sandbox.py"):
        (root / "src" / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / "src" / name, root / "src" / name)
    return root / "data" / "lab"


def _granted(cmds: list[list[str]]) -> dict[str, str]:
    return {Path(c[1]).name or c[1]: c[3].split(":", 1)[1] for c in cmds if c[2] == "/grant"}


def test_the_plan_never_grants_a_secret_and_lets_no_new_file_inherit_at_the_top(tmp_path):
    lab = _tree(tmp_path)
    cmds = sandbox.grant_plan(tmp_path, lab, SID, runtimes=[])
    assert all(c[0] == "icacls" and c[3].startswith(f"*{SID}:") for c in cmds)      # the container's SID only
    assert not any(c[2] == "/deny" for c in cmds)            # Windows ignores denies for AppContainer SIDs
    granted = _granted(cmds)
    assert granted[tmp_path.name] == "(CI)RX"                 # folder grant only: a new .env starts closed
    assert ".env" not in granted and "notifications.local.yaml" not in granted
    assert granted[".env.example"] == "RX" and granted["README.md"] == "RX" and granted["filters.yaml"] == "RX"
    assert granted["src"] == "(OI)(CI)RX" and granted["data"] == "(OI)(CI)RX"
    assert "config" not in granted                            # reached through the root's folder grant only
    assert cmds[-1] == ["icacls", str(lab), "/grant", f"*{SID}:(OI)(CI)M"]          # the one writable place


def test_revoke_removes_the_container_from_everything_grant_touched(tmp_path):
    lab = _tree(tmp_path)
    grant = {c[1] for c in sandbox.grant_plan(tmp_path, lab, SID, runtimes=[tmp_path / "py"])
             if Path(c[1]).exists()}
    revoke = sandbox.revoke_plan(tmp_path, lab, SID, runtimes=[tmp_path / "py"])
    assert {c[1] for c in revoke} == grant and all(c[2:] == ["/remove", f"*{SID}"] for c in revoke)


@pytest.mark.skipif(not sandbox.supported(), reason="Windows AppContainer")
def test_the_grant_plan_changes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(lab_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sandbox, "container_sid", lambda: SID)
    applied: list[object] = []
    monkeypatch.setattr(sandbox, "apply", applied.append)
    (tmp_path / "data").mkdir()
    assert lab_cli.main(["sandbox", "grant"]) == 0
    assert applied == [] and not (tmp_path / "data" / "lab").exists()
    assert "--apply" in capsys.readouterr().err


@pytest.mark.skipif(not sandbox.supported(), reason="Windows AppContainer")
@pytest.mark.parametrize("action", ["grant", "revoke"])
def test_apply_after_the_action_changes_the_acls(monkeypatch, tmp_path, action):
    # regression: an argparse.REMAINDER command swallowed "--apply", so the grant stayed a silent plan
    monkeypatch.setattr(lab_cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sandbox, "container_sid", lambda: SID)
    applied: list[list[list[str]]] = []
    monkeypatch.setattr(sandbox, "apply", applied.append)
    (tmp_path / "data").mkdir()
    assert lab_cli.main(["sandbox", action, "--apply"]) == 0
    assert len(applied) == 1 and applied[0]


@pytest.mark.skipif(not sandbox.supported(), reason="Windows AppContainer")
def test_run_passes_the_command_after_the_separator_untouched(monkeypatch):
    monkeypatch.setattr(sandbox, "container_sid", lambda: SID)
    calls: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(sandbox, "run", lambda argv, cwd, internet=False: calls.append((argv, internet)) or 7)
    assert lab_cli.main(["sandbox", "run", "--internet", "--", "python", "-m", "src.lab", "--lab-db",
                         "data/lab/lab.db", "isolation-check", "--apply"]) == 7
    assert calls == [([sys.executable, "-m", "src.lab", "--lab-db", "data/lab/lab.db", "isolation-check", "--apply"],
                      True)]


def test_the_sandboxed_environment_carries_no_credentials(tmp_path):
    folder = tmp_path / "Packages" / "app.lab" / "AC"
    env = sandbox.sandbox_env(folder, {"PATH": "p", "SystemRoot": r"C:\Windows", "INDEED_API_KEY": "k",
                                       "FRANCE_TRAVAIL_CLIENT_SECRET": "s", "GITHUB_TOKEN": "t"})
    assert env["PATH"] == "p" and env["SYSTEMROOT"] == r"C:\Windows"
    assert not {"INDEED_API_KEY", "FRANCE_TRAVAIL_CLIENT_SECRET", "GITHUB_TOKEN"} & set(env)
    assert env["TEMP"] == str(folder / "Temp") and env["USERPROFILE"] == str(folder)
    assert env["LOCALAPPDATA"] == str(tmp_path)          # Windows derives the container's TEMP from it
    assert env["FYJ_LAB_AGENT"]                  # the human commands refuse to run in there


def test_a_human_command_is_refused_inside_the_container(monkeypatch):
    monkeypatch.setattr(sandbox, "in_app_container", lambda: True)
    assert "AppContainer" in (lab_cli._not_a_present_human() or "")


def test_run_without_a_command_and_off_windows_fails_cleanly(monkeypatch, capsys):
    if sandbox.supported():
        monkeypatch.setattr(sandbox, "container_sid", lambda: SID)
        assert lab_cli.main(["sandbox", "run"]) == 2
        assert "needs a command" in capsys.readouterr().err
    else:
        assert lab_cli.main(["sandbox", "run", "--", "python", "-V"]) == 2
        assert "Windows only" in capsys.readouterr().err


@pytest.mark.skipif(not sandbox.supported(), reason="Windows AppContainer")
def test_the_sandbox_refuses_to_start_from_an_elevated_process(monkeypatch):
    monkeypatch.setattr(sandbox, "elevated", lambda: True)
    with pytest.raises(sandbox.SandboxError, match="elevated"):
        sandbox.run([sys.executable, "-V"], ROOT)


def _accept_until_closed(listener: socket.socket) -> None:
    try:
        while True:
            listener.accept()[0].close()
    except OSError:
        pass


PROBE = r"""
import json, os, socket, sys
root, port = sys.argv[1], int(sys.argv[2])
sys.path.insert(0, root)
from src.lab import sandbox
from src.lab.isolation import check_isolation
out = {"in_container": sandbox.in_app_container()}
def attempt(name, fn):
    try:
        fn()
        out[name] = "allowed"
    except OSError as exc:
        out[name] = type(exc).__name__
j = lambda *p: os.path.join(root, *p)
attempt("read_code", lambda: open(j("config", "filters.yaml")).read())
attempt("read_jobs_db", lambda: open(j("data", "jobs.db"), "rb").read())
attempt("write_jobs_db", lambda: open(j("data", "jobs.db"), "r+b").close())
attempt("plant_in_data", lambda: open(j("data", "jobs.db-wal"), "wb").close())
attempt("write_config", lambda: open(j("config", "filters.yaml"), "a").close())
attempt("plant_module", lambda: open(j("src", "evil.py"), "w").close())
attempt("delete_jobs_db", lambda: os.remove(j("data", "jobs.db")))
attempt("read_env", lambda: open(j(".env")).read())
attempt("read_local", lambda: open(j("config", "notifications.local.yaml")).read())
attempt("read_env_created_later", lambda: open(j(".env.later")).read())
attempt("write_lab", lambda: open(j("data", "lab", "lab.db"), "wb").close())
import tempfile
attempt("temp_file", lambda: os.close(tempfile.mkstemp()[0]))
attempt("loopback", lambda: socket.create_connection(("127.0.0.1", port), timeout=3).close())
attempt("internet", lambda: socket.create_connection(("1.1.1.1", 443), timeout=3).close())
out["check"] = check_isolation(root, j("data", "jobs.db"), j("config"), lab_dir=j("data", "lab"),
                               api_ports=(port,), relay_ports=())
with open(j("data", "lab", "probe.json"), "w") as f:
    json.dump(out, f)
"""


@pytest.mark.skipif(not sandbox.supported(), reason="Windows AppContainer")
def test_the_os_enforces_the_boundary_from_inside_a_real_appcontainer(tmp_path):
    root = tmp_path / "app"
    lab = _tree(root)
    (root / "probe.py").write_text(PROBE)
    name = f"FindYourJobWithAI.LabTest.{uuid.uuid4().hex[:8]}"
    sid = sandbox.container_sid(name)
    runtime = [Path(sys.base_prefix)]
    listener = socket.create_server(("127.0.0.1", 0))         # a port that IS listening: blocked, not idle
    port = listener.getsockname()[1]
    threading.Thread(target=_accept_until_closed, args=(listener,), daemon=True).start()
    try:
        sandbox.apply(sandbox.grant_plan(root, lab, sid, runtimes=runtime))
        (root / ".env.later").write_text("LATE=x\n")          # a secret that appears after the grant
        python = str(Path(sys.base_prefix) / "python.exe")
        code = sandbox.run([python, "-I", "-B", str(root / "probe.py"), str(root), str(port)], root, name=name)
        assert code == 0
        out = json.loads((lab / "probe.json").read_text())
        assert out["in_container"] is True
        assert out["read_code"] == out["read_jobs_db"] == out["write_lab"] == out["temp_file"] == "allowed", out
        for denied in ("write_jobs_db", "plant_in_data", "write_config", "plant_module", "delete_jobs_db",
                       "read_env", "read_local", "read_env_created_later", "internet"):
            assert out[denied] == "PermissionError", (denied, out[denied])
        assert out["loopback"] != "allowed"
        check = out["check"]
        assert check["os_enforced"] is True, check
        assert check["network"] == {str(port): "blocked"}
        assert check["writable"] == [] and check["readable_secrets"] == []
        assert (root / "data" / "jobs.db").exists() and not (root / "src" / "evil.py").exists()
    finally:
        listener.close()
        sandbox.apply(sandbox.revoke_plan(root, lab, sid, runtimes=runtime))
        sandbox.delete_profile(name)
    acl = subprocess.run(["icacls", str(Path(sys.base_prefix))], capture_output=True, text=True,
                         errors="replace").stdout
    assert sid not in acl                                      # nothing left behind on the interpreter
