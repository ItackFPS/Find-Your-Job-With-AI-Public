"""M1: a v1 ``config/`` folder becomes a working v2 one, and no user edit is lost.

The fixtures in tests/fixtures/config_v1 are byte-for-byte what v1.0.0 installed. Temporary
folders only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from src.upgrade import config_migration as cm
from src.upgrade.config_migration import migrate_config

ROOT = Path(__file__).parent.parent
V1 = Path(__file__).parent / "fixtures" / "config_v1"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
VERSION = "2.0.0"


@pytest.fixture
def install(tmp_path: Path) -> Path:
    """An upgraded install before its first start: v1 config, v2 defaults, an empty data folder."""
    shutil.copytree(ROOT / "config", tmp_path / "config_defaults")
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    for name in ("schedules.yaml", "sources.yaml", "notifications.yaml"):
        shutil.copy(V1 / name, tmp_path / "config" / name)
    return tmp_path


def run(install: Path) -> cm.ConfigMigrationReport | None:
    return migrate_config(install / "config", install / "data", VERSION, NOW)


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _ids(install: Path, name: str, key: str, field: str) -> list[str]:
    return [e[field] for e in _load(install / "config" / name)[key]]


def test_v1_fixtures_are_the_released_files():
    for path in V1.iterdir():
        assert hashlib.sha256(path.read_bytes()).hexdigest() in cm.KNOWN_RELEASED_DEFAULTS[path.name]


def test_an_unedited_v1_config_becomes_the_v2_defaults(install: Path):
    report = run(install)
    assert report is not None and not report.failed
    for name in ("schedules.yaml", "sources.yaml"):
        assert (install / "config" / name).read_bytes() == (ROOT / "config" / name).read_bytes()
    assert "pipeline_daily" in _ids(install, "schedules.yaml", "schedules", "id")
    assert "france-travail-web" in _ids(install, "sources.yaml", "sources", "name")


def test_replaced_files_are_backed_up_first(install: Path):
    report = run(install)
    backup = Path(report.backup_dir)
    assert backup.parent == install / "data" / "backups"
    assert (backup / "schedules.yaml").read_bytes() == (V1 / "schedules.yaml").read_bytes()


def test_a_retired_unedited_default_is_archived(install: Path):
    report = run(install)
    assert "notifications.yaml" in report.archived
    assert not (install / "config" / "notifications.yaml").exists()
    assert (Path(report.backup_dir) / "notifications.yaml").exists()


def test_a_retired_edited_file_is_left_alone(install: Path):
    path = install / "config" / "notifications.yaml"
    path.write_text("mine: true\n", encoding="utf-8")
    report = run(install)
    assert path.read_text(encoding="utf-8") == "mine: true\n"
    assert "notifications.yaml" not in report.archived


def test_crlf_copies_of_a_released_file_still_count_as_unedited(install: Path):
    path = install / "config" / "schedules.yaml"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert "schedules.yaml" in run(install).replaced


def test_an_edited_file_keeps_every_edit_and_gains_the_missing_entry(install: Path):
    path = install / "config" / "schedules.yaml"
    edited = path.read_text(encoding="utf-8").replace("retry_delay_seconds: 300", "retry_delay_seconds: 301  # mine")
    path.write_text(edited, encoding="utf-8")
    before = _load(path)

    report = run(install)

    assert report.entries_added == ["schedules.yaml:pipeline_daily"]
    text = path.read_text(encoding="utf-8")
    after = _load(path)
    assert after["schedules"][0]["id"] == "pipeline_daily"
    assert after["schedules"][0]["startup_policy"] == "when_profile_saved"
    assert after["schedules"][1:] == before["schedules"]
    for line in edited.splitlines():
        assert line in text  # comments and values, line by line
    assert (Path(report.backup_dir) / "schedules.yaml").read_text(encoding="utf-8") == edited


def test_an_edited_sources_file_gains_france_travail_web(install: Path):
    path = install / "config" / "sources.yaml"
    edited = path.read_text(encoding="utf-8").replace("count: 200", "count: 150")
    path.write_text(edited, encoding="utf-8")
    report = run(install)
    assert report.entries_added == ["sources.yaml:france-travail-web"]
    after = _load(path)
    new = after["sources"][0]
    assert new["name"] == "france-travail-web" and new["active"] is True
    assert new["config"] == {"max_pages": 60, "rate_limit_delay": 1.0}
    assert after["sources"][1:] == yaml.safe_load(edited)["sources"]


def test_crlf_edited_file_stays_crlf(install: Path):
    path = install / "config" / "schedules.yaml"
    edited = path.read_bytes().replace(b"retry_max: 0", b"retry_max: 2").replace(b"\n", b"\r\n")
    path.write_bytes(edited)
    run(install)
    data = path.read_bytes()
    assert b"pipeline_daily" in data and data.count(b"\n") == data.count(b"\r\n")


def test_an_edited_file_that_already_has_the_entry_is_not_rewritten(install: Path):
    path = install / "config" / "schedules.yaml"
    text = (ROOT / "config" / "schedules.yaml").read_text(encoding="utf-8") + "# mine\n"
    path.write_text(text, encoding="utf-8")
    report = run(install)
    assert path.read_text(encoding="utf-8") == text and report.entries_added == []


@pytest.mark.parametrize("content", ["schedules: []\n", "schedules: [unclosed\n", "- a\n- b\n", "other: 1\n"])
def test_a_file_the_merge_cannot_handle_is_kept_unchanged(install: Path, content: str):
    path = install / "config" / "schedules.yaml"
    path.write_text(content, encoding="utf-8")
    report = run(install)
    assert path.read_text(encoding="utf-8") == content
    assert "schedules.yaml" in report.kept and not report.failed


def test_a_missing_default_is_restored(install: Path):
    (install / "config" / "schedules.yaml").unlink()
    run(install)
    assert (install / "config" / "schedules.yaml").exists()


def test_runs_once_per_version_so_a_deliberate_removal_stays(install: Path):
    run(install)
    path = install / "config" / "schedules.yaml"
    mine = "schedules: []\n"
    path.write_text(mine, encoding="utf-8")
    assert run(install) is None
    assert path.read_text(encoding="utf-8") == mine


def test_the_next_version_runs_again(install: Path):
    run(install)
    assert migrate_config(install / "config", install / "data", "2.1.0", NOW) is not None


def test_two_runs_with_a_lost_marker_are_idempotent(install: Path):
    run(install)
    first = {p.name: p.read_bytes() for p in (install / "config").iterdir()}
    (install / "data" / cm.STATE_FILENAME).unlink()
    report = run(install)
    assert report.replaced == [] and report.entries_added == []
    assert {p.name: p.read_bytes() for p in (install / "config").iterdir()} == first


def test_no_defaults_folder_means_a_checkout_nothing_is_touched(tmp_path: Path):
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(V1 / "schedules.yaml", config / "schedules.yaml")
    assert migrate_config(config, tmp_path / "data", VERSION, NOW) is None
    assert (config / "schedules.yaml").read_bytes() == (V1 / "schedules.yaml").read_bytes()


def test_a_failed_backup_leaves_the_file_and_retries_next_start(install: Path, monkeypatch):
    def full(self, path):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(cm._Backup, "keep", full)
    report = run(install)
    assert "schedules.yaml" in report.failed
    assert (install / "config" / "schedules.yaml").read_bytes() == (V1 / "schedules.yaml").read_bytes()
    assert not (install / "data" / cm.STATE_FILENAME).exists()

    monkeypatch.undo()
    report = run(install)
    assert "schedules.yaml" in report.replaced


def test_an_interrupted_replace_keeps_the_old_file_and_completes_next_start(install: Path, monkeypatch):
    real = os.replace

    def crash(src, dst):
        if str(dst).endswith("schedules.yaml"):
            raise OSError(5, "interrupted")
        return real(src, dst)

    monkeypatch.setattr(cm.os, "replace", crash)
    report = run(install)
    assert "schedules.yaml" in report.failed
    assert (install / "config" / "schedules.yaml").read_bytes() == (V1 / "schedules.yaml").read_bytes()
    assert not list((install / "config").glob("*.upgrade-tmp"))

    monkeypatch.undo()
    assert not run(install).failed
    assert (install / "config" / "schedules.yaml").read_bytes() == (ROOT / "config" / "schedules.yaml").read_bytes()


def test_the_report_is_recorded_for_later_display(install: Path):
    run(install)
    state = json.loads((install / "data" / cm.STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["config_migrated_for"] == VERSION
    assert "schedules.yaml" in state["last_config_migration"]["replaced"]


# ---- the whole start: lifespan, on a real v1 configuration -----------------------------------


@pytest.fixture
def started(install: Path, isolated_app, monkeypatch) -> Path:
    from fastapi.testclient import TestClient

    from src.api.runner import PipelineRunner
    from tests.test_storage_migrations import _old_jobs_db

    db = install / "data" / "jobs.db"
    _old_jobs_db(db)
    state = isolated_app.state._state
    monkeypatch.setitem(state, "db_path", str(db))
    monkeypatch.setitem(state, "config_dir", str(install / "config"))
    monkeypatch.setitem(state, "settings_path", str(install / "data" / "settings.json"))
    monkeypatch.setitem(state, "runner", PipelineRunner(
        db_path=str(db), config_dir=str(install / "config"), progress_bus=isolated_app.state.progress_bus))
    with TestClient(isolated_app) as client:
        assert client.get("/api/profile").status_code == 200
    return install


def test_a_start_on_a_v1_install_upgrades_config_and_backs_up_the_database(started: Path):
    assert "pipeline_daily" in _ids(started, "schedules.yaml", "schedules", "id")
    assert "france-travail-web" in _ids(started, "sources.yaml", "sources", "name")
    copies = list((started / "data" / "backups").glob("jobs-upgrade-*.db"))
    assert len(copies) == 1
    conn = sqlite3.connect(str(copies[0]))
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (2,)  # as it was before migrating
    assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    conn.close()


def test_a_corrupt_default_never_stops_the_start(install: Path):
    edited = install / "config" / "schedules.yaml"
    edited.write_text(edited.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    (install / "config_defaults" / "schedules.yaml").write_bytes(b"schedules: [\n- id: pipeline_daily\n")
    report = run(install)
    assert report is not None and "schedules.yaml" in report.failed + report.kept
    assert edited.read_text(encoding="utf-8").endswith("# mine\n")
