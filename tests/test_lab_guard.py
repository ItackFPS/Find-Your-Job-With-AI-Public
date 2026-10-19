"""The lab write guard is enforced by the process, not by convention (ADR 0011).

Each scenario runs in a child interpreter: an audit hook cannot be removed, so installing it in the
test process would guard the whole test session. Everything happens in a throwaway project root.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = textwrap.dedent(r'''
    import json, os, shutil, socket, sqlite3, subprocess, sys
    from pathlib import Path
    from src.lab.guard import GuardViolation, install_write_guard, lab_writable_roots

    root = Path(sys.argv[1]); outside = Path(sys.argv[2])
    install_write_guard(root, lab_writable_roots(root / "data" / "lab.db", root / "data" / "lab" / "strategies"))
    results = {}

    def attempt(name, fn):
        try:
            fn()
            results[name] = "ok"
        except GuardViolation:
            results[name] = "denied"
        except OSError as exc:                 # e.g. connection refused: the guard let it through
            results[name] = "ok"

    attempt("write_config", lambda: (root / "config" / "filters.yaml").write_text("x"))
    attempt("append_jobs_db", lambda: open(root / "data" / "jobs.db", "ab").close())
    attempt("os_open_jobs_db", lambda: os.close(os.open(root / "data" / "jobs.db", os.O_RDWR)))
    attempt("sqlite_rw_jobs_db", lambda: sqlite3.connect(str(root / "data" / "jobs.db")).close())
    attempt("sqlite_rw_jobs_db_path", lambda: sqlite3.connect(root / "data" / "jobs.db").close())
    attempt("sqlite_rw_jobs_db_bytes", lambda: sqlite3.connect(os.fsencode(root / "data" / "jobs.db")).close())
    attempt("sqlite_uri_rw_jobs_db", lambda: sqlite3.connect(
        f"file:{(root / 'data' / 'jobs.db').as_posix()}?mode=rw", uri=True).close())
    attempt("public_udp", lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("192.0.2.1", 53)))
    attempt("sqlite_ro_jobs_db", lambda: sqlite3.connect(
        f"file:{(root / 'data' / 'jobs.db').as_posix()}?mode=ro", uri=True).execute("SELECT 1").fetchone())
    attempt("read_config", lambda: (root / "config" / "filters.yaml").read_text())
    attempt("replace_into_config", lambda: os.replace(root / "data" / "lab" / "strategies" / "f",
                                                      root / "config" / "filters.yaml"))
    attempt("remove_config", lambda: os.remove(root / "config" / "filters.yaml"))
    attempt("write_src", lambda: (root / "src" / "evil.py").write_text("x"))
    attempt("lab_db_rw", lambda: sqlite3.connect(str(root / "data" / "lab.db")).execute(
        "CREATE TABLE IF NOT EXISTS t (x)").connection.commit())
    attempt("bundle_store_write", lambda: (root / "data" / "lab" / "strategies" / "g").write_text("x"))
    attempt("outside_root_write", lambda: (outside / "tmp.txt").write_text("x"))
    attempt("subprocess", lambda: subprocess.run([sys.executable, "-c", "pass"]))
    attempt("os_system", lambda: os.system("echo hi"))
    attempt("public_network", lambda: socket.create_connection(("192.0.2.1", 443), timeout=0.2))
    attempt("public_dns", lambda: socket.getaddrinfo("example.com", 443))
    attempt("loopback_network", lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.2))
    attempt("mkdir_existing_data", lambda: (root / "data").mkdir(parents=True, exist_ok=True))
    from src.lab.promotion import deactivate     # what an agent would need to change production's strategy
    attempt("promote_from_the_lab", lambda: deactivate(root / "data" / "jobs.db", approved_by="agent", reason="x"))
    from src.lab.promotion import _append        # the lowest-level writer of the pointer, called directly
    attempt("append_pointer_directly", lambda: _append(root / "data" / "jobs.db",
                                                       ("p", "t", "h", "b", "agent", "c", "e", "r", "approve")))
    from src.lab.__main__ import main            # the human CLI route, from inside the guarded process
    results["deactivate_cli_rc"] = main(["--jobs-db", str(root / "data" / "jobs.db"), "--lab-db",
                                         str(root / "data" / "lab.db"), "deactivate", "--by", "agent",
                                         "--reason", "x"])

    # SQLite writes that happen inside SQLite, where no Python audit event fires
    def attach_and_write(conn_target, attach_target, uri=False):
        c = sqlite3.connect(conn_target, uri=uri)
        c.execute(f"ATTACH DATABASE '{attach_target}' AS p")
        c.execute("INSERT INTO p.jobs VALUES ('pwned')")
        c.commit()
    jobs = root / "data" / "jobs.db"
    sqlite_refused = (GuardViolation, sqlite3.DatabaseError)
    def attempt_sql(name, fn):
        try:
            fn()
            results[name] = "ok"
        except sqlite_refused:
            results[name] = "denied"
    attempt_sql("attach_from_memory", lambda: attach_and_write(":memory:", str(jobs)))
    attempt_sql("attach_from_lab_db", lambda: attach_and_write(str(root / "data" / "lab.db"), str(jobs)))
    attempt_sql("attach_rw_uri_from_ro", lambda: attach_and_write(
        f"file:{jobs.as_posix()}?mode=ro", f"file:{jobs.as_posix()}?mode=rw", uri=True))
    attempt_sql("vacuum_into_config", lambda: sqlite3.connect(":memory:").execute(
        f"VACUUM INTO '{(root / 'config' / 'copy.db').as_posix()}'"))
    attempt("load_extension", lambda: sqlite3.connect(":memory:").enable_load_extension(True))
    attempt("unrestricted_connection", lambda: sqlite3.Connection(":memory:"))
    # other spellings and names of protected files
    attempt("hard_link_to_jobs_db", lambda: os.link(jobs, outside / "alias.db"))
    attempt("upper_case_alias", lambda: open(str(jobs).upper(), "ab").close())
    attempt("dotdot_alias", lambda: open(root / "data" / ".." / "data" / "jobs.db", "ab").close())
    attempt("device_prefix_alias", lambda: sqlite3.connect(("\\\\?\\" if os.name == "nt" else "") + str(jobs)).close())
    attempt("public_gethostbyname", lambda: socket.gethostbyname("example.com"))
    if os.name == "nt":
        import _winapi
        attempt("winapi_write_handle", lambda: _winapi.CloseHandle(_winapi.CreateFile(
            str(root / "config" / "filters.yaml"), 0x40000000, 0, 0, 3, 0x80, 0)))
        attempt("winapi_create_process", lambda: _winapi.CreateProcess(
            None, "cmd /c exit", None, None, False, 0, None, None, None))
        attempt("winapi_junction_into_config", lambda: _winapi.CreateJunction(str(root / "config"), str(outside / "j")))
    with sqlite3.connect(f"file:{jobs.as_posix()}?mode=ro", uri=True) as c:   # reads still work
        results["jobs_rows"] = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    results["config_files"] = sorted(p.name for p in (root / "config").iterdir())
    print(json.dumps(results))
''')


@pytest.fixture()
def guarded(tmp_path):
    root = tmp_path / "proj"
    (root / "config").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "data" / "lab" / "strategies").mkdir(parents=True)
    (root / "config" / "filters.yaml").write_text("name: x\n", encoding="utf-8")
    (root / "data" / "lab" / "strategies" / "f").write_text("y", encoding="utf-8")
    with sqlite3.connect(root / "data" / "jobs.db") as conn:
        conn.execute("CREATE TABLE jobs (id TEXT)")
    outside = tmp_path / "outside"
    outside.mkdir()
    before = {p: p.read_bytes() for p in (root / "config" / "filters.yaml", root / "data" / "jobs.db")}
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(root), str(outside)], cwd=ROOT,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1]), root, before


def test_production_data_and_config_cannot_be_written(guarded):
    results, root, before = guarded
    for name in ("write_config", "append_jobs_db", "os_open_jobs_db", "sqlite_rw_jobs_db",
                 "sqlite_rw_jobs_db_path", "sqlite_rw_jobs_db_bytes", "sqlite_uri_rw_jobs_db",
                 "replace_into_config", "remove_config", "write_src", "promote_from_the_lab"):
        assert results[name] == "denied", name
    for p, data in before.items():
        assert p.read_bytes() == data                # nothing reached the disk
    assert not (root / "src" / "evil.py").exists()


_BYPASSES = ("append_pointer_directly", "attach_from_memory", "attach_from_lab_db", "attach_rw_uri_from_ro",
             "vacuum_into_config", "load_extension", "unrestricted_connection", "hard_link_to_jobs_db",
             "dotdot_alias", "device_prefix_alias", "public_gethostbyname")
# Windows only: a case-insensitive filesystem (elsewhere an upper-cased path is another, absent file) and _winapi
_WINDOWS_BYPASSES = ("upper_case_alias", "winapi_write_handle", "winapi_create_process",
                     "winapi_junction_into_config")


def test_bypass_attempts_are_refused_and_change_nothing(guarded):
    results, root, before = guarded
    names = _BYPASSES + (_WINDOWS_BYPASSES if sys.platform == "win32" else ())
    assert {n: results[n] for n in names} == dict.fromkeys(names, "denied")
    assert results["deactivate_cli_rc"] != 0                 # the human CLI cannot promote from a guarded process
    assert results["jobs_rows"] == 0 and results["config_files"] == ["filters.yaml"]
    for p, data in before.items():
        assert p.read_bytes() == data


def test_reads_and_the_labs_own_store_still_work(guarded):
    results, root, _ = guarded
    for name in ("sqlite_ro_jobs_db", "read_config", "lab_db_rw", "bundle_store_write", "outside_root_write",
                 "mkdir_existing_data"):
        assert results[name] == "ok", name
    assert (root / "data" / "lab.db").is_file()


def test_no_process_and_no_network_beyond_loopback(guarded):
    results, _, _ = guarded
    assert results["subprocess"] == results["os_system"] == "denied"
    assert results["public_network"] == results["public_dns"] == results["public_udp"] == "denied"
    assert results["loopback_network"] == "ok"            # a local LLM server stays reachable


def test_the_guard_cannot_be_reinstalled_with_wider_roots(tmp_path):
    script = textwrap.dedent('''
        import sys
        from src.lab.guard import GuardViolation, install_write_guard
        install_write_guard(sys.argv[1], [sys.argv[1] + "/a"])
        install_write_guard(sys.argv[1], [sys.argv[1] + "/a"])          # same roots: idempotent
        try:
            install_write_guard(sys.argv[1], [sys.argv[1]])
            print("widened")
        except GuardViolation:
            print("refused")
    ''')
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "refused", out.stderr


def test_a_fresh_install_can_create_the_labs_own_store(tmp_path):
    # Fresh install: neither data/lab nor the default store data/lab/strategies exists. Creating the store
    # (parents=True) first creates data/lab, which is no writable root itself; the guard prepares the
    # missing parents of its validated roots before it is installed, and still refuses everything else.
    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from src.lab.guard import GuardViolation, install_write_guard
        root = Path(sys.argv[1])
        store = root / "data" / "lab" / "strategies"
        install_write_guard(root, [root / "data" / "lab.db", store])
        (store / "bundle-1").mkdir(parents=True)
        try:
            (root / "data" / "lab" / "other").mkdir()
            print("widened")
        except GuardViolation:
            print("store ok, rest refused")
    ''')
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "store ok, rest refused", out.stderr


@pytest.mark.parametrize("writable", ["data/jobs.db", "data/jobs.db-wal", "data", "config", "config/x",
                                      "src", ".", "elsewhere/prod.db"])
def test_production_paths_can_never_be_made_writable(tmp_path, writable):
    # e.g. `--lab-db data/jobs.db` or `--store config` would otherwise whitelist production itself
    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from src.lab.guard import GuardViolation, guard_installed, install_write_guard
        root = Path(sys.argv[1])
        try:
            install_write_guard(root, [root / sys.argv[2]], jobs_db=root / "elsewhere" / "prod.db")
            print("installed")
        except GuardViolation:
            print("refused", guard_installed())
    ''')
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path), writable], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip() == "refused False", out.stderr
    assert not (tmp_path / "elsewhere").exists() and not (tmp_path / "config").exists()   # a refusal creates nothing


def test_the_lab_cli_refuses_a_lab_db_that_is_the_production_db(tmp_path):
    script = textwrap.dedent('''
        import sys
        from src.lab.__main__ import main
        from src.lab.guard import guard_installed
        print(main(["--jobs-db", sys.argv[1], "--lab-db", sys.argv[1], "hypothesis", "x"]), guard_installed())
    ''')
    prod = tmp_path / "prod.db"
    out = subprocess.run([sys.executable, "-c", script, str(prod)], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "2 False", out.stderr
    assert "may not be writable" in out.stderr
    assert not prod.exists()


def test_the_lab_cli_runs_guarded_except_for_human_promotion(tmp_path):
    script = textwrap.dedent('''
        import sys
        from src.lab.__main__ import main
        from src.lab.guard import guard_installed
        rc = main(["--lab-db", sys.argv[1], "hypothesis", "raise the skills weight"])
        print(rc, guard_installed())
    ''')
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path / "lab.db")], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "0 True", out.stderr
    script_promo = script.replace('"hypothesis", "raise the skills weight"',
                                  '"--jobs-db", sys.argv[1] + ".none", "rollback", "p", "--by", "a", "--reason", "r"')
    out = subprocess.run([sys.executable, "-c", script_promo, str(tmp_path / "lab.db")], cwd=ROOT,
                         capture_output=True, text=True, timeout=60)
    # promotion runs unguarded (it must write jobs.db) and, with no human typing, refuses before writing
    assert out.stdout.strip().splitlines()[-1] == "2 False", out.stderr
    assert "refusing rollback" in out.stderr


def test_the_lab_cli_guard_allows_no_network_not_even_loopback(tmp_path):
    """Loopback would reach the application's own API (pipeline runs, profile edits): the lab CLI allows none."""
    script = textwrap.dedent('''
        import socket, sys
        from pathlib import Path
        from src.lab.guard import GuardViolation, install_write_guard
        root = Path(sys.argv[1])
        install_write_guard(root, [root / "lab"], loopback_ports=())
        out = []
        for port in (8000, 1234):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                out.append("ok")
            except GuardViolation:
                out.append("denied")
            except OSError:
                out.append("ok")                       # refused by the OS: the guard let it through
        print(" ".join(out))
    ''')
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path)], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.strip() == "denied denied", out.stderr
    narrowed = script.replace("loopback_ports=()", "loopback_ports=(1234,)")
    out = subprocess.run([sys.executable, "-c", narrowed, str(tmp_path)], cwd=ROOT, capture_output=True, text=True,
                         timeout=60)
    assert out.stdout.strip() == "denied ok", out.stderr


def test_after_a_lab_cli_command_the_process_cannot_reach_its_own_api(tmp_path):
    script = textwrap.dedent('''
        import socket, sys
        from src.lab.__main__ import main
        from src.lab.guard import GuardViolation
        rc = main(["--lab-db", sys.argv[1], "hypothesis", "raise the skills weight"])
        try:
            socket.create_connection(("127.0.0.1", 8000), timeout=0.2).close()
            print(rc, "ok")
        except GuardViolation:
            print(rc, "denied")
        except OSError:
            print(rc, "ok")
    ''')
    out = subprocess.run([sys.executable, "-c", script, str(tmp_path / "lab.db")], cwd=ROOT, capture_output=True,
                         text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "0 denied", out.stderr


@pytest.mark.parametrize(("raw", "expected"), [
    (r"\\?\D:\proj\data\jobs.db", r"D:\proj\data\jobs.db"),
    (r"\\.\D:\proj\x", r"D:\proj\x"),
    ("//?/D:/proj/x", "D:/proj/x"),
    (r"\\?\UNC\host\share\x", r"\\host\share\x"),
    (r"\\.\pipe\name", r"\\.\pipe\name"),             # a device, not a file of the project
    (r"\\.\PhysicalDrive0", r"\\.\PhysicalDrive0"),
    ("D:/plain/path", "D:/plain/path"),
    ("relative/path", "relative/path"),
])
def test_device_prefixes_are_stripped_only_from_drive_and_unc_paths(raw, expected):
    from src.lab.guard import _strip_device_prefix

    assert _strip_device_prefix(raw) == expected
