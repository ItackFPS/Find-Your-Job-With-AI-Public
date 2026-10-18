"""Tests for Phase 5 (2026-09-10) dashboard tier visibility on GET /api/jobs.

Covers:
- min_tier query param filters by the canonical score_to_tier() tier,
  without touching storage or changing any score/tier.
- score_100/tier are exposed on JobOut.score (ScoreBreakdownOut).
- department is exposed on JobOut so the frontend can label Var/PACA.
- Pagination stays correct when min_tier reduces the candidate set.
- Numerical score/ordering (score DESC) is unaffected by min_tier.
- Low-priority jobs remain stored and queryable (min_tier omitted / 'low_priority').
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


@pytest.fixture()
def tmp_db(tmp_path):
    db = str(tmp_path / "test.db")
    s = Storage(db_path=db)
    s.init_db()
    s.close()
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.close()
    return db


@pytest.fixture()
def client(tmp_db):
    app.state.db_path = tmp_db
    app.state.config_dir = str(Path("config"))
    return TestClient(app)


def _make_job(
    db: str,
    *,
    job_id: str | None = None,
    title: str = "Dev",
    location: str = "Toulon",
    department: str | None = "83",
    countries: list[str] | None = None,
    remote: bool = False,
    work_mode: str | None = None,
    source: str = "test",
    status: str = "active",
) -> str:
    jid = job_id or str(uuid.uuid4())
    conn = sqlite3.connect(db)
    conn.execute(
        """INSERT OR REPLACE INTO jobs
           (id, title, location, department, countries, remote, work_mode,
            source, status, discovered_at, canonical_url, apply_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
        (
            jid, title, location, department,
            json.dumps(countries or ["France"]),
            1 if remote else 0,
            work_mode or ("remote" if remote else "on_site"),
            source, status,
            datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return jid


def _make_run(db: str, *, status: str = "completed", started_offset_hours: int = 1) -> str:
    rs = RunStorage(db_path=db)
    rs.init_db()
    run_id = str(uuid.uuid4())
    started_at = (datetime.now(UTC) - timedelta(hours=started_offset_hours)).isoformat()
    conn = rs._get_conn()
    conn.execute(
        "INSERT INTO pipeline_runs (id, started_at, status, stats_json) VALUES (?, ?, ?, '{}')",
        (run_id, started_at, status),
    )
    if status in ("completed", "failed"):
        conn.execute(
            "UPDATE pipeline_runs SET completed_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), run_id),
        )
    conn.commit()
    rs.close()
    return run_id


def _score_job(db: str, run_id: str, job_id: str, final_score: float) -> None:
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.store_job_scores(run_id, [{"job_id": job_id, "final_score": final_score,
                                   "deterministic_score": final_score, "semantic_score": 0.0}])
    rs.close()


# score_100 -> approximate tier under Phase 4 calibration (80/72/65/60):
# 0.85 -> exceptional, 0.75 -> excellent, 0.68 -> strong, 0.62 -> relevant,
# 0.40 -> low_priority.
_SCORES = {
    "exceptional": 0.85, "excellent": 0.75, "strong": 0.68,
    "relevant": 0.62, "low_priority": 0.40,
}


class TestMinTierFiltering:
    def test_no_min_tier_returns_every_tier(self, client, tmp_db):
        run = _make_run(tmp_db)
        for tier, score in _SCORES.items():
            j = _make_job(tmp_db, job_id=f"job-{tier}", department=None, location="Paris")
            _score_job(tmp_db, run, j, score)

        r = client.get("/api/jobs")
        assert r.status_code == 200
        assert len(r.json()) == 5

    def test_min_tier_relevant_excludes_low_priority_only(self, client, tmp_db):
        run = _make_run(tmp_db)
        for tier, score in _SCORES.items():
            j = _make_job(tmp_db, job_id=f"job-{tier}", department=None, location="Paris")
            _score_job(tmp_db, run, j, score)

        r = client.get("/api/jobs", params={"min_tier": "relevant"})
        assert r.status_code == 200
        tiers = {j["score"]["tier"] for j in r.json()}
        assert tiers == {"exceptional", "excellent", "strong", "relevant"}
        assert "low_priority" not in tiers

    def test_min_tier_exceptional_returns_only_exceptional(self, client, tmp_db):
        run = _make_run(tmp_db)
        for tier, score in _SCORES.items():
            j = _make_job(tmp_db, job_id=f"job-{tier}", department=None, location="Paris")
            _score_job(tmp_db, run, j, score)

        r = client.get("/api/jobs", params={"min_tier": "exceptional"})
        assert r.status_code == 200
        jobs = r.json()
        assert len(jobs) == 1
        assert jobs[0]["score"]["tier"] == "exceptional"

    def test_low_priority_jobs_remain_stored_and_queryable(self, client, tmp_db):
        """(8) min_tier is a presentation filter only — the low_priority job
        is never deleted, and is still returned when explicitly requested."""
        run = _make_run(tmp_db)
        j = _make_job(tmp_db, job_id="job-low", department=None, location="Paris")
        _score_job(tmp_db, run, j, _SCORES["low_priority"])

        # Filtered out by the default-equivalent floor...
        r = client.get("/api/jobs", params={"min_tier": "relevant"})
        assert r.json() == []

        # ...but still fully queryable via the "show everything" value.
        r = client.get("/api/jobs", params={"min_tier": "low_priority"})
        assert len(r.json()) == 1
        assert r.json()[0]["id"] == "job-low"

        # ...and via the DB directly (never deleted).
        s = Storage(db_path=tmp_db)
        s.init_db()
        try:
            assert s.get_job_by_id("job-low") is not None
        finally:
            s.close()

    def test_score_ordering_preserved_under_min_tier(self, client, tmp_db):
        """(9) Numerical score DESC ordering is unaffected by min_tier."""
        run = _make_run(tmp_db)
        for tier, score in _SCORES.items():
            j = _make_job(tmp_db, job_id=f"job-{tier}", department=None, location="Paris")
            _score_job(tmp_db, run, j, score)

        r = client.get("/api/jobs", params={"min_tier": "relevant"})
        scores = [j["score"]["final_score"] for j in r.json()]
        assert scores == sorted(scores, reverse=True)

    def test_min_tier_combines_with_pagination(self, client, tmp_db):
        """A page of `limit` results is filled with tier-matching jobs, not
        thinned out after the fact (filtering happens before pagination)."""
        run = _make_run(tmp_db)
        # 3 low_priority jobs interleaved with 3 relevant+ jobs by score.
        scores = [0.90, 0.30, 0.85, 0.20, 0.80, 0.10]
        for i, score in enumerate(scores):
            j = _make_job(tmp_db, job_id=f"job-{i}", department=None, location="Paris")
            _score_job(tmp_db, run, j, score)

        r = client.get("/api/jobs", params={"min_tier": "relevant", "limit": 2})
        jobs = r.json()
        assert len(jobs) == 2
        assert all(j["score"]["tier"] != "low_priority" for j in jobs)

    def test_min_tier_no_completed_run_is_a_noop(self, client, tmp_db):
        """No completed run -> no run-scoped score data to filter by; the
        param is silently ignored (documented behavior), not an error."""
        _make_job(tmp_db, job_id="job-no-run", department=None, location="Paris")
        r = client.get("/api/jobs", params={"min_tier": "exceptional"})
        assert r.status_code == 200
        assert len(r.json()) == 1


class TestScoreBreakdownExposesTierAndScore100:
    def test_score_100_and_tier_present(self, client, tmp_db):
        run = _make_run(tmp_db)
        j = _make_job(tmp_db, job_id="job-1", department=None, location="Paris")
        _score_job(tmp_db, run, j, 0.85)

        r = client.get("/api/jobs")
        body = r.json()[0]
        assert body["score"]["score_100"] == 85
        assert body["score"]["tier"] == "exceptional"
        # And the score itself is untouched by the presence of a tier.
        assert body["score"]["final_score"] == 0.85


class TestDepartmentExposedOnJobOut:
    def test_var_department_resolved_from_canonical_column(self, client, tmp_db):
        run = _make_run(tmp_db)
        j = _make_job(tmp_db, job_id="job-var", department="83", location="Toulon (83)")
        _score_job(tmp_db, run, j, 0.7)

        r = client.get("/api/jobs")
        assert r.json()[0]["department"] == "83"

    def test_department_resolved_from_location_when_column_absent(self, client, tmp_db):
        run = _make_run(tmp_db)
        j = _make_job(tmp_db, job_id="job-loc", department=None, location="Toulon (83)")
        _score_job(tmp_db, run, j, 0.7)

        r = client.get("/api/jobs")
        assert r.json()[0]["department"] == "83"

    def test_department_none_when_unresolvable(self, client, tmp_db):
        run = _make_run(tmp_db)
        j = _make_job(tmp_db, job_id="job-remote", department=None, location="", countries=[], remote=True)
        _score_job(tmp_db, run, j, 0.7)

        r = client.get("/api/jobs")
        assert r.json()[0]["department"] is None
