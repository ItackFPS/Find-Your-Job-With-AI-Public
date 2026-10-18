"""The ``active_strategy`` pointer is honoured by production, safely (ADR 0011).

Production runs an approved bundle only when the latest pointer is effective, the bundle is intact and it
still reproduces from the current ``config/``; anything else runs on ``config/`` exactly as before.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from src.decisions.active_strategy import resolve_active_strategy
from src.decisions.strategy import build_strategy_manifest
from src.evaluation.dataset import load_dataset
from src.lab.bundle import create_bundle
from src.lab.promotion import PromotionError, _append, deactivate, rollback
from tests.fixtures.profiles import TEST_PROFILE_PATH
from tests.test_decision_journal_orchestrator import _FakeEmbeddingService

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
EXCLUDE_SYMFONY = {"filters.yaml": {"excluded_title_keywords": {"lab_test": ["symfony"]}}}


@pytest.fixture
def env(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    db = tmp_path / "jobs.db"
    sqlite3.connect(db).close()
    return cfg, db, tmp_path / "store"


def _point(db: Path, digest: str, path: Path, pid: str = "ptr-1", at: str = "2026-10-14T00:00:00+00:00") -> None:
    _append(db, (pid, at, digest, str(path), "tester", "cand-1", "exp-1", "test", "approve"))


def test_no_pointer_runs_on_config(env):
    cfg, db, _ = env
    active = resolve_active_strategy(db, cfg)
    assert active.status == "default" and active.path("filters.yaml") == cfg / "filters.yaml"
    assert resolve_active_strategy(db.parent / "missing.db", cfg).status == "default"


def test_an_effective_intact_current_bundle_is_run(env):
    cfg, db, store = env
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    active = resolve_active_strategy(db, cfg)
    assert active.status == "active", active.reason
    assert active.path("filters.yaml") == path / "filters.yaml"
    assert active.path("semantic_config.yaml") == cfg / "semantic_config.yaml"   # not part of a bundle


def test_a_pointer_recorded_before_pipeline_support_stays_inert(env):
    cfg, db, store = env
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    with sqlite3.connect(db) as conn:      # the table as the lab wrote it before pointers were honoured
        conn.execute("""CREATE TABLE active_strategy (pointer_id TEXT PRIMARY KEY, set_at TEXT NOT NULL,
            bundle_hash TEXT NOT NULL, bundle_path TEXT NOT NULL, approved_by TEXT NOT NULL, candidate_id TEXT,
            experiment_id TEXT, reason TEXT NOT NULL, action TEXT NOT NULL)""")
        conn.execute("INSERT INTO active_strategy VALUES ('old', '2026-10-01T00:00:00+00:00', ?, ?, 'a', 'c', 'e', "
                     "'r', 'approve')", (digest, str(path)))
    assert resolve_active_strategy(db, cfg).status == "inert"
    _point(db, digest, path, pid="new")    # a new approval migrates the table; the old row keeps flag 0
    assert resolve_active_strategy(db, cfg).status == "active"
    with sqlite3.connect(db) as conn:
        assert dict(conn.execute("SELECT pointer_id, pipeline_effective FROM active_strategy").fetchall()) == {
            "old": 0, "new": 1}


def test_a_tampered_or_missing_bundle_falls_back_to_config(env):
    cfg, db, store = env
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    (path / "filters.yaml").chmod(0o644)
    (path / "filters.yaml").write_text("tampered: true\n", encoding="utf-8")
    active = resolve_active_strategy(db, cfg)
    assert active.status == "invalid" and active.path("filters.yaml") == cfg / "filters.yaml"
    _point(db, "0" * 64, store / "nowhere", pid="ptr-2", at="2026-10-15T00:00:00+00:00")
    assert resolve_active_strategy(db, cfg).status == "invalid"


@pytest.mark.parametrize("columns, row", [
    ("pointer_id, set_at, bundle_hash, bundle_path, approved_by, action, pipeline_effective",
     ("p", "2026", "h", None, "x", "approve", 1)),                         # nullable column, NULL path
    ("pointer_id, set_at, bundle_hash, bundle_path, approved_by, action, pipeline_effective",
     ("p", "2026", None, "/b", "x", "approve", 1)),
    ("pointer_id, set_at, action, pipeline_effective", ("p", "2026", "approve", 1)),   # no bundle columns
])
def test_a_malformed_pointer_row_falls_back_to_config_instead_of_crashing(env, columns, row):
    cfg, db, _ = env
    with sqlite3.connect(db) as conn:
        conn.execute(f"CREATE TABLE active_strategy ({columns})")
        conn.execute(f"INSERT INTO active_strategy VALUES ({', '.join('?' * len(row))})", row)
    active = resolve_active_strategy(db, cfg)
    assert active.status == "invalid" and active.path("filters.yaml") == cfg / "filters.yaml"


def test_a_bundle_built_on_an_older_config_is_stale_and_cannot_be_approved_again(env):
    cfg, db, store = env
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    with open(cfg / "scoring.yaml", "a", encoding="utf-8") as f:
        f.write("\nlab_test_marker: 1\n")                       # the user edits config/ after the experiment
    active = resolve_active_strategy(db, cfg)
    assert active.status == "stale" and active.path("scoring.yaml") == cfg / "scoring.yaml"
    with pytest.raises(PromotionError, match="stale"):
        rollback(db, "ptr-1", approved_by="a", reason="r", config_dir=cfg)


def test_deactivate_returns_to_config_and_rollback_restores(env):
    cfg, db, store = env
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    deactivate(db, approved_by="a", reason="back to config")
    assert resolve_active_strategy(db, cfg).status == "deactivated"
    rollback(db, "ptr-1", approved_by="a", reason="again", config_dir=cfg)
    assert resolve_active_strategy(db, cfg).status == "active"


def test_a_bundle_built_on_the_active_bundle_records_overrides_relative_to_config(env):
    cfg, db, store = env
    d1, p1 = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    d2, p2 = create_bundle(p1, {"scoring.yaml": {"lab_test_marker": 2}}, store)
    assert json.loads((p2 / "overrides.json").read_text(encoding="utf-8")) == {
        **EXCLUDE_SYMFONY, "scoring.yaml": {"lab_test_marker": 2}}
    _point(db, d2, p2)
    assert resolve_active_strategy(db, cfg).status == "active"


def test_the_strategy_manifest_hashes_the_bundle_only_when_it_runs(env):
    cfg, db, store = env
    plain = build_strategy_manifest(cfg, cfg / "profile.yaml")
    assert build_strategy_manifest(cfg, cfg / "profile.yaml", active=resolve_active_strategy(db, cfg)) == plain
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    m = build_strategy_manifest(cfg, cfg / "profile.yaml", active=resolve_active_strategy(db, cfg))
    assert m["strategy_version"] != plain["strategy_version"]
    assert m["extra"]["active_strategy"] == {"pointer_id": "ptr-1", "bundle_hash": digest}


def _accepted(cfg: Path, db: Path, with_stats: bool = False):
    from src.pipeline.orchestrator import run_pipeline

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg), prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert result.status == "completed", result.error
    accepted = {r.job_title or "" for r in result.scored_jobs}
    if with_stats:   # to_dict() is what src/api/runner.py persists as pipeline_runs.stats_json
        return accepted, result.stats.active_strategy, result.stats.to_dict()
    return accepted, result.stats.active_strategy


def test_the_pipeline_runs_the_approved_bundle_and_falls_back_when_it_goes_stale(env):
    cfg, db, store = env
    baseline, info = _accepted(cfg, db)
    assert info["status"] == "default"
    assert any("Symfony" in t for t in baseline)                  # precondition: config/ accepts them
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path, at="2099-01-01T00:00:00+00:00")
    with_bundle, info = _accepted(cfg, db)
    assert info["status"] == "active" and info["bundle_hash"] == digest
    assert not any("Symfony" in t for t in with_bundle)
    assert with_bundle == {t for t in baseline if "Symfony" not in t}
    with open(cfg / "scoring.yaml", "a", encoding="utf-8") as f:
        f.write("\nlab_test_marker: 1\n")
    after_edit, info = _accepted(cfg, db)
    assert info["status"] == "stale" and any("Symfony" in t for t in after_edit)


# ---------------------------------------------------------------------------
# Every consumer of strategy-controlled files describes the same strategy
# ---------------------------------------------------------------------------

def _scanner(cfg: Path, db: Path | None):
    import src.pipeline.filter  # noqa: F401  (the scanner only filters once the pipeline module is loaded)
    from src.linkedin.scanner import LinkedInConfig, LinkedInScanner
    from src.linkedin.staging import LinkedInStagingStorage

    staging = LinkedInStagingStorage(db_path=str(cfg.parent / "staging.db"))
    return LinkedInScanner(LinkedInConfig(enabled=True), staging, config_dir=str(cfg),
                           db_path=None if db is None else str(db))


def test_strategy_of_run_reads_what_the_run_recorded(env):
    from src.decisions.active_strategy import strategy_of_run

    cfg, db, store = env
    assert strategy_of_run(None, cfg).path("filters.yaml") == cfg / "filters.yaml"
    assert strategy_of_run({"status": "stale", "reason": "r"}, cfg).path("filters.yaml") == cfg / "filters.yaml"
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    recorded = resolve_active_strategy(db, cfg).to_dict()
    with open(cfg / "scoring.yaml", "a", encoding="utf-8") as f:
        f.write("\nlab_test_marker: 1\n")       # config/ edited after the run: the run still used the bundle
    assert resolve_active_strategy(db, cfg).status == "stale"
    assert strategy_of_run(recorded, cfg).path("filters.yaml") == path / "filters.yaml"
    (path / "filters.yaml").chmod(0o644)
    (path / "filters.yaml").write_text("tampered: true\n", encoding="utf-8")
    gone = strategy_of_run(recorded, cfg)
    assert gone.status == "invalid" and gone.path("filters.yaml") == cfg / "filters.yaml"
    forged = {**recorded, "files": {"filters.yaml": str(cfg / "filters.yaml"), "scoring.yaml": str(path / "scoring.yaml")}}
    assert strategy_of_run(forged, cfg).status == "invalid"


def test_the_linkedin_prefilter_follows_the_active_strategy_and_falls_back(env):
    cfg, db, store = env
    assert "symfony" not in _scanner(cfg, db)._load_filters_config()["title_keywords"]["exclude"]
    digest, path = create_bundle(cfg, EXCLUDE_SYMFONY, store)
    _point(db, digest, path)
    assert "symfony" in _scanner(cfg, db)._load_filters_config()["title_keywords"]["exclude"]
    assert "symfony" not in _scanner(cfg, None)._load_filters_config()["title_keywords"]["exclude"]  # no db: config/
    with open(cfg / "scoring.yaml", "a", encoding="utf-8") as f:
        f.write("\nlab_test_marker: 1\n")       # stale → back to config/, exactly like the pipeline
    assert "symfony" not in _scanner(cfg, db)._load_filters_config()["title_keywords"]["exclude"]


def test_job_explanations_use_the_vocabulary_of_the_run_that_decided(env):
    from src.api.routes.jobs import _load_domain_keywords_safe

    cfg, db, store = env
    marker = {"filters.yaml": {"domain_keywords": {"lab_test_domain": ["zzlabword"]}}}
    digest, path = create_bundle(cfg, marker, store)
    _point(db, digest, path)
    run = {"stats": {"active_strategy": resolve_active_strategy(db, cfg).to_dict()}}
    assert _load_domain_keywords_safe(cfg, run).get("lab_test_domain") == ["zzlabword"]
    assert "lab_test_domain" not in _load_domain_keywords_safe(cfg, {"stats": {}})   # a config/ run
    assert "lab_test_domain" not in _load_domain_keywords_safe(cfg, None)            # no run yet
    deactivate(db, approved_by="a", reason="back to config")   # what is active NOW does not rewrite the past
    assert _load_domain_keywords_safe(cfg, run).get("lab_test_domain") == ["zzlabword"]


def test_pipeline_journal_prefilter_and_explanations_agree_on_the_strategy(env):
    """One approved bundle, followed through every consumer: run stats, journal manifest, LinkedIn pre-filter,
    explanation vocabulary and the Lab API all name the same bundle; after deactivation all name config/."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.routes.jobs import _load_domain_keywords_safe
    from src.api.routes.lab import router as lab_router
    from src.decisions.active_strategy import strategy_of_run

    cfg, db, store = env
    overrides = {"filters.yaml": {**EXCLUDE_SYMFONY["filters.yaml"],
                                  "domain_keywords": {"lab_test_domain": ["zzlabword"]}}}
    digest, path = create_bundle(cfg, overrides, store)
    _point(db, digest, path, at="2000-01-01T00:00:00+00:00")   # older than the deactivation below
    accepted, info, stats_dict = _accepted(cfg, db, with_stats=True)
    assert info["status"] == "active" and info["bundle_hash"] == digest
    assert not any("Symfony" in t for t in accepted)                       # filtering used the bundle
    run = {"stats": json.loads(json.dumps(stats_dict))}                     # as the runner persists it
    assert run["stats"]["active_strategy"]["bundle_hash"] == digest        # run statistics
    with sqlite3.connect(db) as conn:                                      # decision journal
        manifests = [json.loads(r[0]) for r in conn.execute("SELECT manifest_json FROM strategy_versions")]
    assert any(m.get("extra", {}).get("active_strategy", {}).get("bundle_hash") == digest for m in manifests)
    assert strategy_of_run(run["stats"]["active_strategy"], cfg).path("filters.yaml") == path / "filters.yaml"
    assert _load_domain_keywords_safe(cfg, run).get("lab_test_domain") == ["zzlabword"]   # explanations
    assert "symfony" in _scanner(cfg, db)._load_filters_config()["title_keywords"]["exclude"]  # pre-filter
    app = FastAPI()
    app.include_router(lab_router)
    app.state.db_path, app.state.config_dir = str(db), str(cfg)
    client = TestClient(app)
    lab_view = client.get("/api/lab/active-strategy").json()["pipeline"]   # Lab UI
    assert (lab_view["status"], lab_view["bundle_hash"]) == ("active", digest)

    deactivate(db, approved_by="a", reason="back to config")
    accepted, info, stats_dict = _accepted(cfg, db, with_stats=True)
    assert info["status"] == "deactivated" and any("Symfony" in t for t in accepted)
    assert "symfony" not in _scanner(cfg, db)._load_filters_config()["title_keywords"]["exclude"]
    run = {"stats": json.loads(json.dumps(stats_dict))}
    assert "lab_test_domain" not in _load_domain_keywords_safe(cfg, run)
    assert client.get("/api/lab/active-strategy").json()["pipeline"]["status"] == "deactivated"
