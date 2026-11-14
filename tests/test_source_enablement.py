"""One answer to "is this source on" for every consumer (Phase B B4-A, finding P-001; owner decision D1).

While a user-switchable source is off its stored offers are hidden: not re-injected, not scored, not
listed. Nothing is deleted; saved and tracked offers stay listed; turning the source on again brings
the offers back. Temporary databases, config directories and settings files only.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.staged_sources import HelloWorkStagedSourceAdapter, LinkedInStagedSourceAdapter
from src.app_settings import set_unofficial_source
from src.source_enablement import (
    source_configured,
    source_enabled,
    switched_off_sources,
)
from tests.test_linkedin_persistence import _insert_job, _make_job_row

SOURCES_YAML = """sources:
  - name: eures
    type: eures
    active: true
  - name: bundesagentur
    type: bundesagentur
    active: false
  - name: indeed
    type: indeed
    active: {indeed}
  - name: hellowork
    type: hellowork
    active: false
"""


def _config(tmp: Path, *, linkedin: bool = True, hellowork_listed: bool = True, indeed: bool = True,
            cfg: Path | None = None) -> Path:
    cfg = cfg or tmp / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    text = SOURCES_YAML.format(indeed=str(indeed).lower())
    if not hellowork_listed:
        text = text.split("  - name: hellowork")[0]
    (cfg / "sources.yaml").write_text(text, encoding="utf-8")
    (cfg / "linkedin.yaml").write_text(f"enabled: {str(linkedin).lower()}\n", encoding="utf-8")
    return cfg


def _choose(settings: Path, **choices: bool) -> None:
    for source, enabled in choices.items():
        set_unofficial_source(settings, source, enabled)


# -- the rule ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("configured,chosen", list(itertools.product([True, False], [True, False, None])))
def test_a_switchable_source_is_on_only_when_configured_and_chosen(tmp_path: Path, configured, chosen) -> None:
    cfg = _config(tmp_path, linkedin=configured, hellowork_listed=configured, indeed=configured)
    settings = tmp_path / "settings.json"
    if chosen is not None:
        _choose(settings, linkedin=chosen, hellowork=chosen, indeed=chosen)
    for source in ("linkedin", "hellowork", "indeed"):
        assert source_configured(source, cfg) is configured, source
        assert source_enabled(source, cfg, settings) is (configured and chosen is True), source
    expected_off = set() if (configured and chosen is True) else {"linkedin", "hellowork", "indeed"}
    assert switched_off_sources(cfg, settings) == expected_off


def test_official_sources_follow_sources_yaml_and_ignore_the_choice(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert source_enabled("eures", cfg, None) is True
    assert source_enabled("bundesagentur", cfg, None) is False
    assert source_enabled("not-listed", cfg, None) is False


def test_hellowork_is_configured_by_its_listing_not_its_active_flag(tmp_path: Path) -> None:
    # Its sources.yaml entry stays active: false on purpose (its own scanner collects it).
    cfg = _config(tmp_path)
    assert source_configured("hellowork", cfg) is True


def test_missing_or_broken_config_reads_as_off(tmp_path: Path) -> None:
    empty = tmp_path / "none"
    empty.mkdir()
    assert source_enabled("linkedin", empty, None) is False
    assert source_enabled("eures", empty, None) is False
    (empty / "linkedin.yaml").write_text("enabled: [unclosed\n", encoding="utf-8")
    assert source_enabled("linkedin", empty, None) is False


def test_the_legacy_indeed_key_still_counts_as_a_choice(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    monkeypatch.setenv("INDEED_API_KEY", "x")
    assert source_enabled("indeed", cfg, tmp_path / "settings.json") is True


# -- every consumer agrees ---------------------------------------------------------------------------

def _store(tmp_path: Path) -> str:
    from src.storage.sqlite import Storage

    db = str(tmp_path / "jobs.db")
    s = Storage(db_path=db)
    s.init_db()
    s.close()
    conn = sqlite3.connect(db)
    for i, source in enumerate(("linkedin", "linkedin", "hellowork", "hellowork", "eures")):
        _insert_job(conn, _make_job_row(f"{source}-{i}", f"{i:08d}", source=source))
    conn.close()
    return db


def _adapter_jobs(adapter_cls, db: str, cfg: Path) -> list[str]:
    adapter = adapter_cls(db_path=db, config_dir=str(cfg))
    try:
        return sorted(j.id for j in adapter.snapshot_for_run("run-x"))
    finally:
        adapter.close()


@pytest.mark.parametrize("configured,chosen", list(itertools.product([True, False], [True, False])))
def test_staged_adapters_preflight_and_rule_agree(tmp_path: Path, configured, chosen) -> None:
    from src.pipeline.preflight import _indeed_not_chosen

    db = _store(tmp_path)
    cfg = _config(tmp_path, linkedin=configured, hellowork_listed=configured, indeed=configured)
    settings = tmp_path / "settings.json"          # next to jobs.db: what the runner reads
    _choose(settings, linkedin=chosen, hellowork=chosen, indeed=chosen)
    on = configured and chosen
    assert bool(_adapter_jobs(LinkedInStagedSourceAdapter, db, cfg)) is on
    assert bool(_adapter_jobs(HelloWorkStagedSourceAdapter, db, cfg)) is on
    assert source_enabled("indeed", cfg, settings) is on
    # The preflight skips an Indeed source only when it is configured and off.
    assert _indeed_not_chosen(str(cfg), settings) is (configured and not chosen)


def test_settings_overview_reports_the_rule(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from src.api.app import app

    db = _store(tmp_path)
    cfg = _config(tmp_path, linkedin=False)          # LinkedIn chosen but not configured
    settings = tmp_path / "settings.json"
    _choose(settings, linkedin=True, hellowork=False, indeed=True)
    for key, value in {"db_path": db, "config_dir": str(cfg), "settings_path": str(settings),
                       "linkedin_config": SimpleNamespace(enabled=False), "hellowork_scanner": object()}.items():
        monkeypatch.setitem(app.state._state, key, value)
    rows = {r["name"]: r for r in TestClient(app).get("/api/sources/overview").json()["sources"]}
    for name in ("linkedin", "hellowork", "indeed"):
        assert rows[name]["collected"] is source_enabled(name, cfg, settings), name
    assert rows["linkedin"]["choice"]["enabled"] is True      # the switch shows the user's choice
    assert rows["linkedin"]["collected"] is False


def test_scheduler_reads_the_same_rule(isolated_app, tmp_path: Path) -> None:
    cfg = Path(isolated_app.state.config_dir)
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "linkedin.yaml").write_text("enabled: false\n", encoding="utf-8")
    _choose(tmp_path / "settings.json", linkedin=True)
    with TestClient(isolated_app):
        scheduler = isolated_app.state.scheduler
        assert scheduler._opted_in("linkedin") is False      # chosen, but the config turns it off
        (cfg / "linkedin.yaml").write_text("enabled: true\n", encoding="utf-8")
        assert scheduler._opted_in("linkedin") is True


# -- D1: hide while off, on -> off -> on ------------------------------------------------------------

def _counts(db: str) -> dict[str, int]:
    conn = sqlite3.connect(db)
    try:
        return dict(conn.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source").fetchall())
    finally:
        conn.close()


@pytest.fixture
def listing(isolated_app, tmp_path: Path):
    from src.storage.sqlite import Storage

    db = isolated_app.state.db_path
    Storage(db_path=db).init_db()
    conn = sqlite3.connect(db)
    for i, source in enumerate(("linkedin", "linkedin", "hellowork", "hellowork", "eures")):
        _insert_job(conn, _make_job_row(f"{source}-{i}", f"{i:08d}", source=source))
    conn.execute("INSERT INTO job_fts (id, title, description, company, skills) "
                 "SELECT id, title, '', company, '' FROM jobs")          # what Storage.save keeps in sync
    conn.commit()
    conn.close()
    _config(tmp_path, cfg=Path(isolated_app.state.config_dir))
    return TestClient(isolated_app), db, Path(isolated_app.state.settings_path)


def _listed(client: TestClient, **params) -> set[str]:
    r = client.get("/api/jobs", params={"limit": 500, **params})
    assert r.status_code == 200
    return {j["id"] for j in r.json()}


def test_on_off_on_hides_and_restores_stored_offers_and_deletes_nothing(listing) -> None:
    client, db, settings = listing
    before = _counts(db)
    _choose(settings, linkedin=True, hellowork=True)
    assert _listed(client) == {"linkedin-0", "linkedin-1", "hellowork-2", "hellowork-3", "eures-4"}

    _choose(settings, hellowork=False)
    assert _listed(client) == {"linkedin-0", "linkedin-1", "eures-4"}
    assert _listed(client, q="DevOps") == {"linkedin-0", "linkedin-1", "eures-4"}   # search too
    assert _counts(db) == before                                                     # nothing deleted

    _choose(settings, hellowork=True)
    assert _listed(client) == {"linkedin-0", "linkedin-1", "hellowork-2", "hellowork-3", "eures-4"}
    assert _counts(db) == before


def test_saved_and_tracked_offers_of_a_switched_off_source_stay_listed(listing) -> None:
    from src.storage.sqlite import Storage

    client, db, settings = listing
    _choose(settings, linkedin=False, hellowork=False)
    assert client.post("/api/jobs/linkedin-0/feedback", json={"type": "saved"}).status_code == 201
    s = Storage(db_path=db)
    s.update_application_status("hellowork-2", "applied")
    s.close()
    assert _listed(client) == {"linkedin-0", "hellowork-2", "eures-4"}
    assert "linkedin-0" in _listed(client, saved_only=True)
    # A single offer stays reachable by its id whatever the switch says.
    assert client.get("/api/jobs/linkedin-1").status_code == 200


def test_a_switched_off_source_is_not_reinjected_into_a_run(tmp_path: Path) -> None:
    db = _store(tmp_path)
    cfg = _config(tmp_path)
    settings = tmp_path / "settings.json"
    _choose(settings, linkedin=True, hellowork=True)
    assert _adapter_jobs(HelloWorkStagedSourceAdapter, db, cfg) == ["hellowork-2", "hellowork-3"]
    _choose(settings, hellowork=False)
    assert _adapter_jobs(HelloWorkStagedSourceAdapter, db, cfg) == []
    assert _adapter_jobs(LinkedInStagedSourceAdapter, db, cfg) == ["linkedin-0", "linkedin-1"]
    _choose(settings, hellowork=True)
    assert _adapter_jobs(HelloWorkStagedSourceAdapter, db, cfg) == ["hellowork-2", "hellowork-3"]
    assert json.loads(settings.read_text(encoding="utf-8"))["unofficial_sources"]["hellowork"] is True
