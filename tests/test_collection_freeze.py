"""Temporary collection freeze (owner decision D7, ADR 0032): no job source is contacted until its
collector is validated AND the user turns it back on; an update never reactivates one."""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.collectors import freeze
from src.scheduler.service import PIPELINE_SOURCE_ID, DispatchResult, SchedulerService
from src.scheduler.storage import SchedulerStorage

pytestmark = pytest.mark.collection_freeze

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)

SOURCES = """sources:
  - name: eures
    type: eures
    active: true
    config: {}
  - name: france-travail
    type: france_travail
    active: true
    config: {}
"""


def _config(tmp: Path) -> Path:
    from tests.fixtures.profiles import TEST_PROFILE_PATH

    cfg = tmp / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    (cfg / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    return cfg


# -- state -------------------------------------------------------------------------------------------

def test_missing_state_means_everything_is_frozen(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    cfg = _config(tmp_path)
    for t in ("eures", "france_travail", "linkedin", "hellowork", "indeed"):
        assert freeze.collector_allowed(settings, t) is False
        assert freeze.collector_allowed(None, t) is False
    assert freeze.pipeline_allowed(settings, cfg) is False
    assert freeze.status(settings, cfg)["frozen"] is True


def test_no_collector_is_validated_today() -> None:
    assert freeze.VALIDATED_COLLECTORS == {}


def test_an_unvalidated_collector_cannot_be_reactivated(tmp_path: Path) -> None:
    with pytest.raises(freeze.CollectorNotValidated):
        freeze.reactivate(tmp_path / "settings.json", "eures")
    assert not (tmp_path / "settings.json").exists()


def test_a_validated_collector_comes_back_only_after_the_users_reactivation(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    cfg = _config(tmp_path)
    # An update ships eures as validated: still frozen, nobody turned it on.
    monkeypatch.setitem(freeze.VALIDATED_COLLECTORS, "eures", "docs/validation/eures.md")
    assert freeze.collector_allowed(settings, "eures") is False
    assert freeze.pipeline_allowed(settings, cfg) is False
    freeze.reactivate(settings, "eures", now=NOW)
    assert freeze.collector_allowed(settings, "eures") is True
    assert freeze.collector_allowed(settings, "france_travail") is False
    assert freeze.pipeline_allowed(settings, cfg) is True
    freeze.deactivate(settings, "eures")
    assert freeze.collector_allowed(settings, "eures") is False


def test_record_freeze_writes_once_and_never_clears_a_reactivation(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"notifications": {"strong_matches": False}}), encoding="utf-8")
    assert freeze.record_freeze(settings, now=NOW) is True
    monkeypatch.setitem(freeze.VALIDATED_COLLECTORS, "eures", "doc")
    freeze.reactivate(settings, "eures", now=NOW)
    assert freeze.record_freeze(settings) is False           # e.g. the next start after an update
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["collection_freeze"]["since"] == NOW.isoformat(timespec="seconds")
    assert "eures" in data["collection_freeze"]["reactivated"]
    assert data["notifications"] == {"strong_matches": False}  # other settings kept


def test_unreadable_state_stays_frozen(tmp_path: Path, monkeypatch) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("{not json", encoding="utf-8")
    monkeypatch.setitem(freeze.VALIDATED_COLLECTORS, "eures", "doc")
    assert freeze.collector_allowed(settings, "eures") is False
    settings.write_text(json.dumps({"collection_freeze": {"reactivated": {"eures": 1}}}), encoding="utf-8")
    assert freeze.collector_allowed(settings, "eures") is False   # not a timestamp: not a reactivation


# -- preflight and pipeline --------------------------------------------------------------------------

def test_preflight_skips_frozen_sources_and_never_authenticates(tmp_path: Path) -> None:
    from src.pipeline import preflight

    cfg = _config(tmp_path)
    with patch.object(preflight, "_check_france_travail", side_effect=AssertionError("contacted the API")):
        result = preflight.run_preflight(str(cfg), settings_path=tmp_path / "settings.json")
    assert result.ready is True
    assert result.france_travail.status == "SKIPPED"
    assert result.skip_reasons["eures"] == freeze.FROZEN_REASON
    assert result.skip_reasons["france_travail"] == freeze.FROZEN_REASON


def test_a_pipeline_run_never_builds_a_frozen_collector(tmp_path: Path) -> None:
    from src.pipeline.orchestrator import run_pipeline
    from tests.test_decision_journal_orchestrator import _FakeEmbeddingService

    cfg = _config(tmp_path)

    def _refuse(_type):
        raise AssertionError("a frozen collector was looked up")

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService), \
         patch("src.collectors.registry.registry.get_collector_class", _refuse):
        result = run_pipeline(db_path=str(tmp_path / "jobs.db"), config_dir=str(cfg))
    assert result.status == "completed", result.error
    for name in ("eures", "france-travail"):
        row = result.stats.by_source[name]
        assert row["status"] == "skipped" and row["reason"] == freeze.FROZEN_REASON
    assert result.stats.collected == 0


# -- run, scan and schedule entry points -------------------------------------------------------------

def test_the_runner_refuses_to_start_a_run(tmp_path: Path) -> None:
    from src.api.runner import PipelineRunner

    runner = PipelineRunner(db_path=str(tmp_path / "jobs.db"), config_dir=str(_config(tmp_path)))
    with pytest.raises(freeze.CollectionFrozen):
        runner.start_run()
    assert runner.is_running() is False


class _Handler:
    def __init__(self) -> None:
        self.dispatch_calls = 0

    def is_running(self) -> bool:
        return False

    def dispatch(self) -> DispatchResult:
        self.dispatch_calls += 1
        return DispatchResult(run_ref=str(uuid.uuid4()), completed=True)


def _scheduler(tmp_path: Path, handlers: dict, frozen) -> SchedulerService:
    shutil.copy(ROOT / "config" / "schedules.yaml", tmp_path / "schedules.yaml")
    (tmp_path / "profile.local.yaml").write_text("target_titles: []\n", encoding="utf-8")
    svc = SchedulerService(db_path=str(tmp_path / "scheduler.db"), config_dir=str(tmp_path), handlers=handlers,
                           clock=lambda: NOW, opted_in=lambda _s: True, frozen=frozen)
    storage = SchedulerStorage(str(tmp_path / "scheduler.db"), clock=lambda: NOW)
    storage.init_db()
    storage.seed_from_yaml(tmp_path / "schedules.yaml")
    storage.close()
    return svc


def test_the_scheduler_dispatches_nothing_frozen_and_records_no_execution(tmp_path: Path) -> None:
    handlers = {PIPELINE_SOURCE_ID: _Handler(), "linkedin": _Handler(), "hellowork": _Handler()}
    svc = _scheduler(tmp_path, handlers, frozen=lambda _s: True)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert all(h.dispatch_calls == 0 for h in handlers.values())
    storage = SchedulerStorage(str(tmp_path / "scheduler.db"))
    try:
        assert storage.list_executions(limit=50) == []
    finally:
        storage.close()
    with pytest.raises(freeze.CollectionFrozen):
        svc.trigger_manual("linkedin_daily")


def test_an_unreadable_freeze_state_keeps_the_scheduler_from_dispatching(tmp_path: Path) -> None:
    handlers = {PIPELINE_SOURCE_ID: _Handler()}

    def broken(_s):
        raise OSError("disk")

    svc = _scheduler(tmp_path, handlers, frozen=broken)
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert handlers[PIPELINE_SOURCE_ID].dispatch_calls == 0


def test_api_refuses_runs_and_scans_and_reports_the_freeze(isolated_app, tmp_path: Path) -> None:
    cfg = Path(isolated_app.state.config_dir)
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sources.yaml").write_text(SOURCES, encoding="utf-8")
    with TestClient(isolated_app) as client:
        r = client.post("/api/pipeline/run")
        assert r.status_code == 409 and r.json()["detail"] == freeze.FROZEN_REASON
        for path in ("/api/linkedin/scan", "/api/hellowork/scan", "/api/linkedin/resume", "/api/hellowork/resume"):
            r = client.post(path)
            assert r.status_code == 409, path
            assert r.json()["detail"] == freeze.FROZEN_REASON, path
        status = client.get("/api/collection/status").json()
        assert status["frozen"] is True and status["since"]
        assert {c["type"] for c in status["collectors"]} >= {"eures", "france_travail", "linkedin", "hellowork"}
        assert all(not c["allowed"] for c in status["collectors"])
        r = client.post("/api/collection/eures/reactivate")
        assert r.status_code == 409
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored["collection_freeze"]["since"]               # recorded at start
