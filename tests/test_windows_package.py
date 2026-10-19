"""The Windows installer (packaging/windows): what it installs, keeps and creates.

The installer itself is built and exercised end to end by packaging/windows/acceptance.ps1
(it needs Inno Setup and several minutes); these tests pin the rules that keep user data
and the launch path safe, so a change to the scripts cannot quietly break them.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

import pytest

from src.launcher import core
from src.launcher.core import Level

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging" / "windows"
ISS = (PKG / "FindYourJob.iss").read_text(encoding="utf-8")
BUILD = (PKG / "build.ps1").read_text(encoding="utf-8")


def _section(name: str) -> list[str]:
    """Non-comment lines of one [Section] of the .iss file."""
    match = re.search(rf"^\[{name}\]\n(.*?)(?=^\[|\Z)", ISS, re.M | re.S)
    assert match, name
    return [ln for ln in match.group(1).splitlines() if ln.strip() and not ln.lstrip().startswith(";")]


def _setup(key: str) -> str:
    for line in _section("Setup"):
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip()
    raise KeyError(key)


# -- installer script ----------------------------------------------------------------------------


def test_installs_per_user_without_admin_rights():
    assert _setup("PrivilegesRequired") == "lowest"
    assert _setup("PrivilegesRequiredOverridesAllowed") == ""
    assert _setup("DefaultDirName").startswith("{localappdata}\\Programs\\")


def test_app_id_is_fixed_so_upgrades_find_the_installation():
    assert _setup("AppId") == "{{6F0D6C2A-5B7E-4C1B-9E55-2D3C4F9A7B10}"


def test_desktop_shortcut_is_offered_and_can_be_declined():
    task = next(ln for ln in _section("Tasks") if ln.startswith('Name: "desktopicon"'))
    assert "unchecked" not in task  # created by default, the user can untick it
    desktop = [ln for ln in _section("Icons") if "{userdesktop}" in ln]
    assert len(desktop) == 1 and "Tasks: desktopicon" in desktop[0]


def test_shortcuts_target_the_installed_launcher_with_its_icon():
    icons = _section("Icons")
    launch = [ln for ln in icons if 'Filename: "{app}\\{#AppExeName}"' in ln]
    assert len(launch) == 3  # Start Menu, Stop, desktop
    for line in launch:
        assert 'WorkingDir: "{app}"' in line and 'IconFilename: "{app}\\FindYourJob.ico"' in line
    assert re.search(r'#define AppExeName "FindYourJob\.cmd"', ISS)
    stop = [ln for ln in launch if "Parameters:" in ln]
    assert len(stop) == 1 and 'Parameters: "stop"' in stop[0]
    assert any(ln.startswith('Name: "{group}\\') and "{app}\\logs" in ln for ln in icons)


def test_launching_after_install_passes_no_arguments():
    # the launcher starts no scan; nothing here may ask it to
    (run,) = _section("Run")
    assert "Parameters" not in run and "skipifsilent" in run


def test_user_data_is_never_installed_overwritten_or_uninstalled():
    files = _section("Files")
    app = next(ln for ln in files if 'DestDir: "{app}";' in ln)
    assert 'Excludes: "\\config\\*"' in app
    config = next(ln for ln in files if 'DestDir: "{app}\\config"' in ln)
    assert "onlyifdoesntexist" in config and "uninsneveruninstall" in config
    for line in files:
        assert not re.search(r"\\data\\|\.env\b|\.local\.|\.db\b", line, re.I)
    dirs = _section("Dirs")
    for name in ("data", "logs"):
        line = next(ln for ln in dirs if f'"{{app}}\\{name}"' in ln)
        assert "uninsneveruninstall" in line
    deleted = " ".join(_section("InstallDelete"))
    for name in ("data", "config", "logs", ".env"):
        assert f"{{app}}\\{name}\"" not in deleted


def test_uninstall_stops_the_app_and_asks_before_deleting_data():
    (run,) = _section("UninstallRun")
    assert '"-m src.launcher stop"' in run and "waituntilterminated" in run
    code = ISS[ISS.index("procedure CurUninstallStepChanged"):]
    assert "UninstallSilent()" in code  # silent uninstall keeps data
    assert "MB_DEFBUTTON2" in code  # the default answer is No
    assert code.index("MsgBox") < code.index("DelTree")


def test_upgrade_stops_a_running_copy_through_its_launcher():
    assert _setup("CloseApplications") == "no"
    assert "function PrepareToInstall" in ISS and "-m src.launcher stop" in ISS


# -- build script ----------------------------------------------------------------------------------


def test_build_packages_only_committed_files():
    assert '"archive", "--format=tar", "-o", $Tar, $Commit' in BUILD
    assert "--untracked-files=no" in BUILD


def test_build_refuses_personal_or_secret_files_in_the_stage():
    block = BUILD[BUILD.index("$Forbidden"):BUILD.index("if ($Forbidden)")]
    for pattern in ('".env"', '"*.local.*"', '".db"', '".log"', '"data\\*"', '"logs\\*"'):
        assert pattern in block


def test_bundled_python_is_isolated_from_the_users_environment():
    assert '"Lib`r`nDLLs`r`n..`r`nimport site"' in BUILD  # ._pth: no PYTHONPATH, no user site
    assert "PYTHONPATH = \"C:\\does-not-exist\"" in BUILD  # and the build proves it


def test_build_ships_what_the_self_check_reads():
    from src.evaluation.__main__ import DEFAULT_SELF_CHECK

    for path in DEFAULT_SELF_CHECK:
        assert path.relative_to(ROOT).parts[:3] == ("tests", "fixtures", "evaluation")
    assert "tests\\fixtures\\evaluation" in BUILD


def test_runtime_requirements_pin_every_declared_dependency():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = project["dependencies"] + project["optional-dependencies"]["linkedin"]

    def norm(name: str) -> str:
        return re.sub(r"[-_.]+", "-", name).lower()

    pins: dict[str, str] = {}
    for line in (PKG / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        assert sep and version.strip(), f"not pinned: {line}"
        pins[norm(name)] = version
    for requirement in declared:
        name = re.match(r"[A-Za-z0-9_.-]+", requirement).group(0)
        assert norm(name) in pins, f"{name} is declared in pyproject.toml but not pinned"
    for tool in ("pip", "wheel", "pytest", "mypy", "ruff"):
        assert tool not in pins, f"{tool} must not be bundled"


def test_api_reports_the_packaged_version():
    from src.api.app import app

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert app.version == project["version"]


def test_icon_matches_its_generator():
    sys.path.insert(0, str(PKG))
    try:
        import make_icon
    finally:
        sys.path.remove(str(PKG))
    assert (PKG / "FindYourJob.ico").read_bytes() == make_icon.ico()


def test_cmd_prefers_the_bundled_runtime():
    text = (ROOT / "FindYourJob.cmd").read_text(encoding="utf-8")
    assert text.index(r"%~dp0runtime\python.exe") < text.index(r"%~dp0.venv\Scripts\python.exe")


# -- launcher in an installed copy ----------------------------------------------------------------


@pytest.fixture()
def installed(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "runtime"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "runtime"))
    monkeypatch.setattr(core, "missing_modules", lambda *a: [])
    return tmp_path


def _checks(root: Path) -> dict[str, core.Check]:
    return {c.name: c for c in core.prerequisite_checks(root)}


def test_installed_copy_needs_no_venv_and_no_node(installed, monkeypatch):
    monkeypatch.setattr(core.shutil, "which", lambda _name: None)
    (installed / "frontend" / "dist").mkdir(parents=True)
    (installed / "frontend" / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    checks = _checks(installed)
    assert core.bundled_runtime(installed)
    assert "venv" not in checks
    assert checks["python"].level is Level.OK and "bundled" in checks["python"].message
    assert checks["dashboard"].level is Level.OK
    assert all(c.level < Level.ERROR for c in checks.values())
    assert str(installed) in checks[".env"].fix


def test_installed_copy_with_missing_files_says_reinstall(installed, monkeypatch):
    monkeypatch.setattr(core, "missing_modules", lambda *a: ["fastapi"])
    checks = _checks(installed)
    for name in ("packages", "dashboard"):
        assert checks[name].level is Level.ERROR
        assert checks[name].fix == core.REINSTALL_FIX
        assert "npm" not in checks[name].fix and ".venv" not in checks[name].fix


def test_a_checkout_is_not_mistaken_for_an_installed_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / ".venv"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "python312"))
    assert not core.bundled_runtime(tmp_path)
    assert "venv" not in _checks(tmp_path)  # inside a venv: no warning either


def test_self_check_shortcut_runs_the_bundled_python_in_a_window_that_stays_open():
    (line,) = [ln for ln in _section("Icons") if "self-check" in ln]
    assert 'Filename: "{cmd}"' in line and "/k " in line
    assert r"{app}\runtime\python.exe" in line and "-m src.evaluation self-check" in line
