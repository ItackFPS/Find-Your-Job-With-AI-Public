"""The main list survives empty, failed and partial runs (issue #253, B11 finding 4).

Each run carries the stored active offers of every enabled source it did not collect
(``src/pipeline/carryover.py``); an offer leaves the list only by an explicit rule. Temporary
databases and fake collectors through the product's run path; no network."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.collectors.base import BaseCollector, CollectorResult


class _Board(BaseCollector):
    """One fake collector per source name: ``plan[name] = (jobs, errors)``."""

    plan: dict[str, tuple[list[dict[str, Any]], list[str]]] = {}

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)
        self._name = str((config or {}).get("name", ""))

    def collect(self) -> CollectorResult:
        jobs, errors = _Board.plan.get(self._name, ([], []))
        return CollectorResult(source_name=self._name, jobs=list(jobs), errors=list(errors))


def _job(source: str, n: int) -> dict[str, Any]:
    key = f"{source}-{n}"
    return {"source_job_id": key, "url": f"https://example.test/{key}", "title": f"Python Developer {key}",
            "company": f"Acme {key}", "location": "Paris", "description": f"Python developer position {key}."}


def _sources_yaml(alpha: bool = True, beta: bool = True) -> str:
    return ("sources:\n"
            f"  - {{name: alpha, type: alpha, active: {str(alpha).lower()}}}\n"
            f"  - {{name: beta, type: beta, active: {str(beta).lower()}}}\n")


@pytest.fixture
def board(tmp_path: Path, monkeypatch):  # noqa: ANN201
    from src.api.runner import PipelineRunner
    from src.collectors import freeze
    from src.collectors.registry import registry

    monkeypatch.setattr(registry, "get_collector_class", lambda _type: _Board)
    monkeypatch.setattr(freeze, "ENFORCED", False)  # only the fake collectors above run
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "profile.yaml").write_text("target_domains: [software_development]\ntarget_skills: [python]\n",
                                      encoding="utf-8")
    (cfg / "filters.yaml").write_text("domain_keywords:\n  software_development:\n    title: [developer]\n",
                                      encoding="utf-8")
    (cfg / "sources.yaml").write_text(_sources_yaml(), encoding="utf-8")
    db = str(tmp_path / "jobs.db")

    def run(alpha: Any = (), beta: Any = (), alpha_errors: Any = (), beta_errors: Any = ()) -> dict[str, Any]:
        from src.storage.runs import RunStorage

        monkeypatch.setattr(_Board, "plan", {"alpha": (list(alpha), list(alpha_errors)),
                                             "beta": (list(beta), list(beta_errors))})
        rs = RunStorage(db_path=db)
        rs.init_db()
        run_id = f"run-{len(rs._get_conn().execute('SELECT id FROM pipeline_runs').fetchall()):02d}"
        rs.create_run(run_id)
        rs.close()
        PipelineRunner(db_path=db, config_dir=str(cfg))._execute(run_id)
        rs = RunStorage(db_path=db)
        try:
            stats = (rs.get_run(run_id) or {}).get("stats") or {}
        finally:
            rs.close()
        return {"id": run_id, **stats}

    run.db = db  # type: ignore[attr-defined]
    run.cfg = cfg  # type: ignore[attr-defined]
    return run


def _listed(db: str) -> dict[str, int]:
    """Offers per source in the visible run (the main list before presentation filters)."""
    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=db)
    try:
        run = rs.get_latest_visible_run()
        ids = rs.get_job_ids_for_run(run["id"]) if run else []
    finally:
        rs.close()
    if not ids:
        return {}
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            f"SELECT source, COUNT(*) FROM jobs WHERE id IN ({','.join('?' * len(ids))}) "
            "AND status != 'expired' GROUP BY source", ids).fetchall()
    finally:
        conn.close()
    return dict(rows)


def _row(db: str, source_job_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        job = conn.execute("SELECT * FROM jobs WHERE source_job_id = ?", (source_job_id,)).fetchone()
        seen = conn.execute("SELECT MAX(last_seen_at) FROM job_provenance WHERE job_id = ?",
                            (job["id"],)).fetchone()[0]
    finally:
        conn.close()
    return {**dict(job), "last_seen": seen}


A = [_job("alpha", 1), _job("alpha", 2)]
B = [_job("beta", 1)]


def test_a_source_that_returns_nothing_keeps_its_offers_and_is_named(board) -> None:  # noqa: ANN001
    first = board(alpha=A, beta=B)
    assert first["outcome"] == "first_run_empty" or first["outcome"] == "successful"
    assert _listed(board.db) == {"alpha": 2, "beta": 1}

    second = board(alpha=[], beta=B)
    assert second["by_source"]["alpha"]["suspect_empty"] is True
    assert second["outcome"] == "partial"
    assert second["incomplete_sources"] == {"alpha": "suspect_empty"}
    assert second["carried_by_source"] == {"alpha": 2}
    assert second["collected"] == 1                       # what the sources returned, carried apart
    assert _listed(board.db) == {"alpha": 2, "beta": 1}   # was {"beta": 1} before issue #253


def test_every_source_empty_keeps_the_list_and_names_the_sources(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    empty = board()
    assert empty["outcome"] == "no_successful_source"
    assert empty["incomplete_sources"] == {"alpha": "suspect_empty", "beta": "suspect_empty"}
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def test_a_failed_source_keeps_its_offers(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    down = board(alpha_errors=["HTTP 503"], beta=B)
    assert down["by_source"]["alpha"]["status"] == "failed"
    assert down["outcome"] == "partial"
    assert down["incomplete_sources"] == {"alpha": "failed"}
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def test_a_partial_source_keeps_the_offers_it_did_not_return(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    part = board(alpha=A[:1], alpha_errors=["page 2: HTTP 500"], beta=B)
    assert part["by_source"]["alpha"]["status"] == "partial"
    assert part["incomplete_sources"] == {"alpha": "partial"}
    assert part["carried_by_source"] == {"alpha": 1}
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def test_a_genuinely_empty_source_is_not_suspect(board) -> None:  # noqa: ANN001
    first = board(alpha=A)                                # beta has never returned anything
    assert "suspect_empty" not in first["by_source"]["beta"]
    assert first["incomplete_sources"] == {}
    again = board(alpha=A)
    assert again["outcome"] == "no_new_offers" and again["incomplete_sources"] == {}
    assert _listed(board.db) == {"alpha": 2}


def test_a_genuinely_empty_first_run_says_so(board) -> None:  # noqa: ANN001
    assert board()["outcome"] == "first_run_empty"
    assert _listed(board.db) == {}


def test_an_interrupted_run_keeps_the_previous_list(board, monkeypatch) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("injected crash")

    monkeypatch.setattr("src.decisions.recorder.DecisionRecorder.finalize", boom)
    crashed = board(alpha=A, beta=B)
    assert crashed["outcome"] == "failed"
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def test_a_carried_offer_is_not_a_sighting(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    before = _row(board.db, "alpha-1")
    board(alpha=[], beta=B)
    after = _row(board.db, "alpha-1")
    assert after["last_seen"] == before["last_seen"]       # provenance not refreshed
    assert after["discovered_at"] == before["discovered_at"]
    assert after["status"] == "active"


def test_a_source_turned_off_is_not_carried(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    (Path(board.cfg) / "sources.yaml").write_text(_sources_yaml(alpha=False), encoding="utf-8")
    off = board(beta=B)
    assert off["carried_by_source"] == {}
    assert "alpha" in off["sources_off"]             # recorded: the list can restore alpha at once
    assert _listed(board.db) == {"beta": 1}
    (Path(board.cfg) / "sources.yaml").write_text(_sources_yaml(alpha=True), encoding="utf-8")
    on = board(alpha=[], beta=B)                           # back on, even before alpha returns anything
    assert on["carried_by_source"] == {"alpha": 2}
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def _age(db: str, source_prefix: str, days: int) -> None:
    conn = sqlite3.connect(db)
    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    ids = [r[0] for r in conn.execute("SELECT id FROM jobs WHERE source_job_id LIKE ?", (f"{source_prefix}%",))]
    for jid in ids:
        conn.execute("UPDATE jobs SET discovered_at = ? WHERE id = ?", (old, jid))
        conn.execute("UPDATE job_provenance SET first_seen_at = ?, last_seen_at = ? WHERE job_id = ?", (old, old, jid))
    conn.commit()
    conn.close()


def test_an_offer_not_collected_for_30_days_expires_as_stale(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    _age(board.db, "alpha", 31)
    run = board(beta=B)
    assert "alpha" not in run["carried_by_source"]
    assert _row(board.db, "alpha-1")["status"] == "expired"
    assert _row(board.db, "alpha-1")["expiry_reason"] == "stale"
    assert _listed(board.db) == {"beta": 1}

    board(alpha=A, beta=B)                                # seen again: active again
    assert _row(board.db, "alpha-1")["status"] == "active"
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def test_an_offer_younger_than_the_cap_stays(board) -> None:  # noqa: ANN001
    board(alpha=A, beta=B)
    _age(board.db, "alpha", 29)
    board(beta=B)
    assert _row(board.db, "alpha-1")["status"] == "active"
    assert _listed(board.db) == {"alpha": 2, "beta": 1}


def test_the_age_cap_is_read_from_retention_yaml(tmp_path: Path) -> None:
    from src.pipeline.orchestrator import expire_after_days_unseen

    assert expire_after_days_unseen(tmp_path) == 30
    (tmp_path / "retention.yaml").write_text("expire_after_days_unseen: 14\n", encoding="utf-8")
    assert expire_after_days_unseen(tmp_path) == 14
    (tmp_path / "retention.yaml").write_text("expire_after_days_unseen: 0\n", encoding="utf-8")
    assert expire_after_days_unseen(tmp_path) == 1
    root = Path(__file__).resolve().parents[1]
    assert expire_after_days_unseen(root / "config") == 30


def test_turning_a_source_back_on_lists_its_offers_before_any_run(board, monkeypatch) -> None:  # noqa: ANN001
    """End to end through the product's run path and the API (#253 review): a run while alpha is
    off leaves alpha's offers unevaluated; turning alpha on lists them at once, off hides them at
    once, and the next run evaluates them again."""
    from fastapi.testclient import TestClient

    from src.api.app import app
    from src.source_enablement import settings_path_for

    for key, value in {"db_path": board.db, "config_dir": str(board.cfg),
                       "settings_path": str(settings_path_for(board.db))}.items():
        monkeypatch.setitem(app.state._state, key, value)
    client = TestClient(app)
    # Découverte off: with three offers its random slots would take some of them out of the list.
    (Path(board.cfg) / "personalization.yaml").write_text(
        "personal_model:\n  exploration:\n    slots: 0\n", encoding="utf-8")

    def listed() -> list[str]:
        return sorted(j["source"] for j in client.get("/api/jobs", params={"limit": 500}).json())

    board(alpha=A, beta=B)
    yaml_path = Path(board.cfg) / "sources.yaml"
    yaml_path.write_text(_sources_yaml(alpha=False), encoding="utf-8")
    assert listed() == ["beta"]                                            # off: hidden at once
    board(beta=B)                                                          # a run while alpha is off
    assert listed() == ["beta"]
    yaml_path.write_text(_sources_yaml(alpha=True), encoding="utf-8")
    assert listed() == ["alpha", "alpha", "beta"]                          # on: back at once, no run
    assert client.get("/api/jobs/count").json()["count"] == 3
    assert client.get("/api/jobs/list-status").json()["restored_by_source"] == {"alpha": 2}
    yaml_path.write_text(_sources_yaml(alpha=False), encoding="utf-8")
    assert listed() == ["beta"]                                            # off again: hidden at once
    yaml_path.write_text(_sources_yaml(alpha=True), encoding="utf-8")
    after = board(alpha=[], beta=B)                                        # next run carries them
    assert after["carried_by_source"] == {"alpha": 2}
    assert listed() == ["alpha", "alpha", "beta"]
    assert client.get("/api/jobs/list-status").json()["restored_by_source"] == {}


def test_a_user_reported_offer_stays_expired_when_collected_again(board) -> None:  # noqa: ANN001
    """The user's "no longer available" wins over a new sighting and over carry-forward; a stale
    offer collected again is active again (the other half is in the stale test above)."""
    from src.storage.sqlite import Storage

    board(alpha=A, beta=B)
    reported = _row(board.db, "alpha-1")["id"]
    s = Storage(db_path=board.db)
    s.init_db()
    assert s.set_reported_expired(reported, True, datetime.now(UTC))
    s.close()

    board(alpha=A, beta=B)                                # alpha-1 genuinely collected again
    row = _row(board.db, "alpha-1")
    assert (row["status"], row["expiry_reason"]) == ("expired", "user_report")
    assert _listed(board.db) == {"alpha": 1, "beta": 1}
    carried = board(alpha=[], beta=B)                     # and never carried
    assert carried["carried_by_source"] == {"alpha": 1}
    assert _row(board.db, "alpha-1")["expiry_reason"] == "user_report"
