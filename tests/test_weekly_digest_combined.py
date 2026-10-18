"""Tests for GET /api/jobs/digest/weekly-combined.

Validates:
- Run selection: latest completed run, not running/failed
- Race condition: in-progress run is ignored
- Var 83 filtering: France + dept 83, uses existing logic
- Remote filtering: j.remote == True only, not location heuristic
- Ranking: final_score DESC within each section, limit enforced
- No completed run: HTTP 503 with diagnostic detail
- Score scoping: scores from the selected run, not other runs
- Existing /digest/weekly endpoint unaffected (regression)
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    conn.row_factory = sqlite3.Row
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


# ---------------------------------------------------------------------------
# 1. No completed run → 503
# ---------------------------------------------------------------------------

class TestNoCompletedRun:
    def test_503_when_no_run(self, client):
        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 503
        body = r.json()
        assert body["detail"]["error"] == "no_completed_run"

    def test_503_when_only_running_run(self, client, tmp_db):
        _make_run(tmp_db, status="running")
        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 503

    def test_503_when_only_failed_run(self, client, tmp_db):
        _make_run(tmp_db, status="failed")
        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# 2. Latest completed run is selected
# ---------------------------------------------------------------------------

class TestRunSelection:
    def test_uses_latest_completed_not_running(self, client, tmp_db):
        """A currently-running run must not affect which run is selected."""
        old_run = _make_run(tmp_db, status="completed", started_offset_hours=5)
        _make_run(tmp_db, status="running",   started_offset_hours=1)

        j = _make_job(tmp_db, department="83")
        _score_job(tmp_db, old_run, j, 0.9)

        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == old_run  # selected the completed run

    def test_uses_latest_not_oldest_completed(self, client, tmp_db):
        old_run = _make_run(tmp_db, status="completed", started_offset_hours=10)
        new_run = _make_run(tmp_db, status="completed", started_offset_hours=2)

        j_old = _make_job(tmp_db, title="OldJob", department="83")
        j_new = _make_job(tmp_db, title="NewJob", department="83")
        _score_job(tmp_db, old_run, j_old, 0.9)
        _score_job(tmp_db, new_run, j_new, 0.9)

        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 200
        data = r.json()
        assert data["run_id"] == new_run
        titles = [j["title"] for j in data["top_jobs"]]
        assert "NewJob" in titles
        assert "OldJob" not in titles

    def test_ignores_failed_run(self, client, tmp_db):
        good_run   = _make_run(tmp_db, status="completed", started_offset_hours=5)
        _make_run(tmp_db, status="failed",    started_offset_hours=1)

        j = _make_job(tmp_db, department="83")
        _score_job(tmp_db, good_run, j, 0.8)

        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 200
        assert r.json()["run_id"] == good_run


# ---------------------------------------------------------------------------
# 3. Ranking and rank field (global top_jobs — see also TestTopJobsGlobalRanking)
# ---------------------------------------------------------------------------

class TestRankingAndLimit:
    def test_rank_field_is_1_based(self, client, tmp_db):
        run = _make_run(tmp_db, status="completed")
        for i in range(3):
            j = _make_job(tmp_db, department="83")
            _score_job(tmp_db, run, j, 0.8 - i * 0.1)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        ranks = [x["rank"] for x in data["top_jobs"]]
        assert ranks == [1, 2, 3]


# ---------------------------------------------------------------------------
# 6. Score scoping — run-specific scores, no cross-run leakage
# ---------------------------------------------------------------------------

class TestScoreScoping:
    def test_scores_from_selected_run_not_other_runs(self, client, tmp_db):
        """
        Job scored 0.9 in old run, 0.65 in new run (both above the "relevant"
        floor, so the job still qualifies for top_jobs either way — this
        isolates score SCOPING from the floor).
        Digest should use the new (selected) run score = 65, not 90.
        """
        old_run = _make_run(tmp_db, status="completed", started_offset_hours=10)
        new_run = _make_run(tmp_db, status="completed", started_offset_hours=2)

        j = _make_job(tmp_db, department="83")
        _score_job(tmp_db, old_run, j, 0.9)
        _score_job(tmp_db, new_run, j, 0.65)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        assert data["run_id"] == new_run
        match = next((x for x in data["top_jobs"] if x["job_id"] == j), None)
        assert match is not None
        assert match["score_100"] == 65  # new run score, not 90

    def test_running_run_partial_scores_not_used(self, client, tmp_db):
        """
        A completed run has job scored 0.7.
        A newer running run has the same job scored 0.95 (partial).
        Digest must use 0.7 (completed run), not 0.95 (running run).
        """
        good_run    = _make_run(tmp_db, status="completed", started_offset_hours=5)
        running_run = _make_run(tmp_db, status="running",   started_offset_hours=1)

        j = _make_job(tmp_db, department="83")
        _score_job(tmp_db, good_run,    j, 0.7)
        _score_job(tmp_db, running_run, j, 0.95)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        assert data["run_id"] == good_run
        match = next((x for x in data["top_jobs"] if x["job_id"] == j), None)
        assert match is not None
        assert match["score_100"] == 70  # completed run score, not 95


# ---------------------------------------------------------------------------
# 7. Response metadata
# ---------------------------------------------------------------------------

class TestResponseMetadata:
    def test_run_metadata_fields_present(self, client, tmp_db):
        _make_run(tmp_db, status="completed")
        r = client.get("/api/jobs/digest/weekly-combined")
        assert r.status_code == 200
        data = r.json()
        assert "run_id" in data
        assert "run_completed_at" in data
        assert "run_age_hours" in data
        assert "top_jobs" in data
        # Location Priority feature (2026-09-10): the old two-section
        # Var83/remote response shape (var83_count, remote_count, var83,
        # remote) was removed once n8n was cut over to top_jobs.
        assert "var83_count" not in data
        assert "remote_count" not in data
        assert "var83" not in data
        assert "remote" not in data

    def test_top_jobs_count_matches_actual_qualifying_results(self, client, tmp_db):
        run = _make_run(tmp_db, status="completed")
        for _ in range(3):
            j = _make_job(tmp_db, department="83")
            _score_job(tmp_db, run, j, 0.8)
        for _ in range(2):
            j = _make_job(tmp_db, remote=True, department=None, location="Paris")
            _score_job(tmp_db, run, j, 0.8)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        assert len(data["top_jobs"]) == 5


# ---------------------------------------------------------------------------
# 8. Regression — old /digest/weekly endpoint still works
# ---------------------------------------------------------------------------

class TestRegression:
    def test_old_weekly_endpoint_unaffected(self, client, tmp_db):
        """The existing /digest/weekly endpoint must keep working."""
        r = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=5")
        # Either 200 (empty list) or any non-500 response is acceptable.
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert isinstance(r.json(), list)

    def test_combined_and_weekly_coexist(self, client, tmp_db):
        """Both routes must be reachable without shadowing each other."""
        r1 = client.get("/api/jobs/digest/weekly")
        r2 = client.get("/api/jobs/digest/weekly-combined")
        assert r1.status_code != 404
        assert r2.status_code != 404


# ---------------------------------------------------------------------------
# 9. Phase 5 (2026-09-10): canonical tier exposed on digest entries
# ---------------------------------------------------------------------------

class TestDigestExposesCanonicalTier:
    """The weekly email (config/n8n_weekly_digest.json) now reads `tier`
    directly instead of keeping its own copy of the score thresholds."""

    def test_tier_present_and_matches_score_to_tier(self, client, tmp_db):
        from src.pipeline.scoring import score_to_tier

        run = _make_run(tmp_db)
        j1 = _make_job(tmp_db, department="83")
        _score_job(tmp_db, run, j1, 0.85)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        top_job = next(x for x in data["top_jobs"] if x["job_id"] == j1)
        assert top_job["tier"] == score_to_tier(top_job["score_100"]) == "exceptional"

    def test_weekly_endpoint_also_exposes_tier(self, client, tmp_db):
        from src.pipeline.scoring import score_to_tier

        run = _make_run(tmp_db)
        j = _make_job(tmp_db, department="83")
        _score_job(tmp_db, run, j, 0.68)

        data = client.get(
            "/api/jobs/digest/weekly", params={"country": "France", "department": "83"}
        ).json()
        assert data[0]["tier"] == score_to_tier(data[0]["score_100"]) == "strong"


# ---------------------------------------------------------------------------
# 10. Location Priority feature (2026-09-10): top_jobs is a single GLOBAL
# ranking, deliberately independent of profile.geography.location_preferences
# — a conscious contrast with Discord's per-location top-K routing.
# ---------------------------------------------------------------------------

class TestTopJobsGlobalRanking:
    def test_top_jobs_not_filtered_by_department(self, client, tmp_db):
        """A job outside Var/83 (and outside any particular department) still
        appears in top_jobs — this field has no department filter at all."""
        run = _make_run(tmp_db)
        var_job = _make_job(tmp_db, department="83")
        paris_job = _make_job(tmp_db, department="75", location="Paris")
        _score_job(tmp_db, run, var_job, 0.70)
        _score_job(tmp_db, run, paris_job, 0.90)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert var_job in top_ids
        assert paris_job in top_ids

    def test_top_jobs_ranked_by_score_descending(self, client, tmp_db):
        run = _make_run(tmp_db)
        low = _make_job(tmp_db, department="75", location="Paris")
        high = _make_job(tmp_db, department="69", location="Lyon")
        _score_job(tmp_db, run, low, 0.62)
        _score_job(tmp_db, run, high, 0.88)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        top_ids = [j["job_id"] for j in data["top_jobs"]]
        assert top_ids.index(high) < top_ids.index(low)

    def test_top_jobs_excludes_low_priority_jobs(self, client, tmp_db):
        """Same sanity floor Discord uses ("relevant", >=60) — not a
        volume control, just keeps obviously poor matches out."""
        run = _make_run(tmp_db)
        good = _make_job(tmp_db, department="69", location="Lyon")
        bad = _make_job(tmp_db, department="75", location="Paris")
        _score_job(tmp_db, run, good, 0.65)
        _score_job(tmp_db, run, bad, 0.30)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert good in top_ids
        assert bad not in top_ids

    def test_top_jobs_never_padded_below_the_floor(self, client, tmp_db):
        """Fewer than top_limit qualifying jobs -> fewer results, never
        backfilled with sub-floor jobs to reach the requested count."""
        run = _make_run(tmp_db)
        good = _make_job(tmp_db, department="69", location="Lyon")
        _score_job(tmp_db, run, good, 0.65)

        data = client.get("/api/jobs/digest/weekly-combined", params={"top_limit": 50}).json()
        assert len(data["top_jobs"]) == 1

    def test_top_limit_query_param_caps_results(self, client, tmp_db):
        run = _make_run(tmp_db)
        for i in range(5):
            j = _make_job(tmp_db, department="69", location="Lyon", job_id=f"job-{i}")
            _score_job(tmp_db, run, j, 0.70 + i / 100)

        data = client.get("/api/jobs/digest/weekly-combined", params={"top_limit": 2}).json()
        assert len(data["top_jobs"]) == 2
        # highest two scores
        assert {j["job_id"] for j in data["top_jobs"]} == {"job-3", "job-4"}

    def test_no_scored_jobs_top_jobs_is_empty_list(self, client, tmp_db):
        _make_run(tmp_db)
        data = client.get("/api/jobs/digest/weekly-combined").json()
        assert data["top_jobs"] == []

    def test_a_job_matching_no_preferred_department_still_appears_in_top_jobs(self, client, tmp_db):
        """The whole point of top_jobs: it does not consult
        location_preferences at all, so a great job with no configured
        location match is never hidden from the weekly recap the way it
        would be from Discord."""
        run = _make_run(tmp_db)
        unmatched = _make_job(tmp_db, department="21", location="Dijon")
        _score_job(tmp_db, run, unmatched, 0.95)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        assert unmatched in {j["job_id"] for j in data["top_jobs"]}


# ---------------------------------------------------------------------------
# Optional department/remote narrowing (2026-09-12) — restores "department 83
# Top 10" + "remote Top 10" as two calls to this SAME endpoint, instead of a
# separate endpoint or a second ranking implementation. Omitting both params
# must remain byte-for-byte the existing global-ranking behavior covered by
# TestTopJobsGlobalRanking above — these tests only cover the NEW opt-in path.
# ---------------------------------------------------------------------------

class TestOptionalDepartmentRemoteNarrowing:
    def test_no_params_still_returns_the_global_unfiltered_ranking(self, client, tmp_db):
        """Regression guard: adding the new params must not change the
        default (no params) behavior at all."""
        run = _make_run(tmp_db)
        var_job = _make_job(tmp_db, department="83")
        paris_job = _make_job(tmp_db, department="75", location="Paris")
        _score_job(tmp_db, run, var_job, 0.70)
        _score_job(tmp_db, run, paris_job, 0.90)

        data = client.get("/api/jobs/digest/weekly-combined").json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert var_job in top_ids
        assert paris_job in top_ids

    def test_department_param_narrows_to_that_department_only(self, client, tmp_db):
        run = _make_run(tmp_db)
        var_job = _make_job(tmp_db, department="83")
        paris_job = _make_job(tmp_db, department="75", location="Paris")
        _score_job(tmp_db, run, var_job, 0.70)
        _score_job(tmp_db, run, paris_job, 0.90)

        data = client.get("/api/jobs/digest/weekly-combined", params={"department": "83"}).json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert top_ids == {var_job}

    def test_department_param_uses_the_same_fallback_pattern_as_weekly_endpoint(self, client, tmp_db):
        """A job with no explicit department column but a location string
        that resolves to the requested department (extract_department_code)
        must still match — same fallback GET /api/jobs/digest/weekly uses."""
        run = _make_run(tmp_db)
        job = _make_job(tmp_db, department=None, location="Toulon (83)")
        _score_job(tmp_db, run, job, 0.90)

        data = client.get("/api/jobs/digest/weekly-combined", params={"department": "83"}).json()
        assert job in {j["job_id"] for j in data["top_jobs"]}

    def test_remote_true_narrows_to_remote_only(self, client, tmp_db):
        run = _make_run(tmp_db)
        remote_job = _make_job(tmp_db, department="83", remote=True)
        onsite_job = _make_job(tmp_db, department="83", remote=False)
        _score_job(tmp_db, run, remote_job, 0.70)
        _score_job(tmp_db, run, onsite_job, 0.90)

        data = client.get("/api/jobs/digest/weekly-combined", params={"remote": "true"}).json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert top_ids == {remote_job}

    def test_remote_false_narrows_to_non_remote_only(self, client, tmp_db):
        run = _make_run(tmp_db)
        remote_job = _make_job(tmp_db, department="83", remote=True)
        onsite_job = _make_job(tmp_db, department="83", remote=False)
        _score_job(tmp_db, run, remote_job, 0.70)
        _score_job(tmp_db, run, onsite_job, 0.90)

        data = client.get("/api/jobs/digest/weekly-combined", params={"remote": "false"}).json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert top_ids == {onsite_job}

    def test_department_and_remote_together_are_both_applied(self, client, tmp_db):
        run = _make_run(tmp_db)
        matches_both = _make_job(tmp_db, department="83", remote=True)
        dept_only = _make_job(tmp_db, department="83", remote=False)
        remote_only = _make_job(tmp_db, department="75", remote=True, location="Paris")
        for j in (matches_both, dept_only, remote_only):
            _score_job(tmp_db, run, j, 0.80)

        data = client.get(
            "/api/jobs/digest/weekly-combined", params={"department": "83", "remote": "true"}
        ).json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert top_ids == {matches_both}

    def test_tier_floor_still_applies_when_narrowed(self, client, tmp_db):
        """The relevant-tier sanity floor is applied BEFORE narrowing, not
        replaced by it — a low-priority job in the requested department must
        still be excluded."""
        run = _make_run(tmp_db)
        good = _make_job(tmp_db, department="83")
        bad = _make_job(tmp_db, department="83")
        _score_job(tmp_db, run, good, 0.65)
        _score_job(tmp_db, run, bad, 0.30)

        data = client.get("/api/jobs/digest/weekly-combined", params={"department": "83"}).json()
        top_ids = {j["job_id"] for j in data["top_jobs"]}
        assert good in top_ids
        assert bad not in top_ids

    def test_department_narrowing_respects_top_limit(self, client, tmp_db):
        run = _make_run(tmp_db)
        for i in range(5):
            j = _make_job(tmp_db, department="83", job_id=f"var-{i}")
            _score_job(tmp_db, run, j, 0.70 + i / 100)

        data = client.get(
            "/api/jobs/digest/weekly-combined", params={"department": "83", "top_limit": 2}
        ).json()
        assert len(data["top_jobs"]) == 2
        assert {j["job_id"] for j in data["top_jobs"]} == {"var-3", "var-4"}

    def test_no_matching_department_returns_empty_not_503(self, client, tmp_db):
        run = _make_run(tmp_db)
        job = _make_job(tmp_db, department="75", location="Paris")
        _score_job(tmp_db, run, job, 0.90)

        resp = client.get("/api/jobs/digest/weekly-combined", params={"department": "83"})
        assert resp.status_code == 200
        assert resp.json()["top_jobs"] == []
