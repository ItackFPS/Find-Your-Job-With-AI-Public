"""Tests for GET /api/jobs/digest/weekly — weekly location digest endpoint.

Covers:
- France + Var 83 filtering (country AND department)
- 7-day discovered_at window
- Real stored final_score used (no recalculation)
- Descending score ordering
- Deterministic tiebreak (score DESC, discovered_at DESC)
- Top 10 limit
- Fewer than 10 jobs returns all available
- Zero matching jobs returns empty list
- Non-Var France jobs are excluded
- Non-France jobs are excluded
- Jobs older than the window are excluded
- Score comes from job_scores table, not recalculated
- Existing pipeline webhook endpoint is unaffected
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.models.job import Job
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "test_digest.db")


@pytest.fixture()
def storages(db_path):
    s = Storage(db_path=db_path)
    s.init_db()
    rs = RunStorage(db_path=db_path)
    rs.init_db()
    yield s, rs
    s.close()
    rs.close()


@pytest.fixture()
def client(db_path):
    app = FastAPI()
    app.state.db_path = db_path
    app.state.config_dir = "config"
    from src.api.routes.jobs import router as jobs_router
    app.include_router(jobs_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(UTC)


def _job(
    title: str,
    location: str,
    countries: list[str],
    department: str | None = None,
    discovered_at: datetime | None = None,
    source: str = "test",
) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        title=title,
        company="TestCo",
        location=location,
        countries=countries,
        department=department,
        source=source,
        remote=False,
        discovered_at=discovered_at or _now(),
    )


def _store_jobs_and_scores(
    s: Storage,
    rs: RunStorage,
    jobs: list[Job],
    scores: list[float],
) -> str:
    """Store jobs and a single run with the given final_scores. Returns run_id."""
    s.upsert_jobs_batch(jobs)
    run_id = str(uuid.uuid4())
    rs.create_run(run_id)
    score_rows = [
        {
            "job_id": j.id,
            "deterministic_score": scores[i],
            "semantic_score": scores[i],
            "final_score": scores[i],
            "score_breakdown": {},
        }
        for i, j in enumerate(jobs)
    ]
    rs.store_job_scores(run_id, score_rows)
    rs.complete_run(run_id, {"personalized": len(jobs)})
    return run_id


# ---------------------------------------------------------------------------
# Tests: France + Var 83 filtering
# ---------------------------------------------------------------------------

class TestVarFiltering:
    def test_returns_only_var_jobs(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        var_job = _job("Dev Toulon", "Toulon, France", ["France"])
        paris_job = _job("Dev Paris", "Paris, France", ["France"])
        german_job = _job("Dev Berlin", "Berlin", ["Germany"])

        _store_jobs_and_scores(s, rs, [var_job, paris_job, german_job], [0.80, 0.90, 0.95])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        ids = [j["job_id"] for j in resp.json()]
        assert var_job.id in ids
        assert paris_job.id not in ids
        assert german_job.id not in ids

    def test_department_column_takes_priority(self, client, db_path):
        """Jobs with department='83' are included even without location parsing."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j = _job("Dev Var", "Some location without city hint", ["France"], department="83")
        _store_jobs_and_scores(s, rs, [j], [0.75])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["job_id"] == j.id

    def test_department_83_via_location_extraction(self, client, db_path):
        """Jobs without department column but with Toulon in location are included."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j = _job("Dev Toulon", "Toulon (83), France", ["France"], department=None)
        _store_jobs_and_scores(s, rs, [j], [0.82])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert resp.json()[0]["job_id"] == j.id

    def test_non_var_department_excluded(self, client, db_path):
        """Jobs with department='13' (Bouches-du-Rhône) are not returned."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j_marseille = _job("Dev Marseille", "Marseille, France", ["France"], department="13")
        j_toulon = _job("Dev Toulon", "Toulon, France", ["France"])
        _store_jobs_and_scores(s, rs, [j_marseille, j_toulon], [0.95, 0.70])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        ids = [j["job_id"] for j in resp.json()]
        assert j_marseille.id not in ids
        assert j_toulon.id in ids


# ---------------------------------------------------------------------------
# Tests: 7-day window
# ---------------------------------------------------------------------------

class TestDateWindow:
    def test_includes_jobs_within_7_days(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        recent = _job("Recent", "Toulon, France", ["France"], discovered_at=_now() - timedelta(days=3))
        _store_jobs_and_scores(s, rs, [recent], [0.80])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    def test_excludes_jobs_older_than_window(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        old = _job("Old", "Toulon, France", ["France"], discovered_at=_now() - timedelta(days=10))
        _store_jobs_and_scores(s, rs, [old], [0.90])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

    def test_boundary_job_included(self, client, db_path):
        """A job discovered exactly `days` ago (minus 1 minute) should be included."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        boundary = _job("Boundary", "Toulon, France", ["France"],
                        discovered_at=_now() - timedelta(days=7) + timedelta(minutes=1))
        _store_jobs_and_scores(s, rs, [boundary], [0.75])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# Tests: Score ordering
# ---------------------------------------------------------------------------

class TestScoreOrdering:
    def test_sorted_by_score_descending(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        jobs = [
            _job(f"Job {s_val}", "Toulon, France", ["France"])
            for s_val in [0.60, 0.90, 0.75, 0.85]
        ]
        _store_jobs_and_scores(s, rs, jobs, [0.60, 0.90, 0.75, 0.85])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        scores = [j["score_100"] for j in data]
        assert scores == sorted(scores, reverse=True)

    def test_deterministic_tiebreak_by_freshness(self, client, db_path):
        """When two jobs share the same score, the more recently discovered one ranks higher."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        older = _job("Older same score", "Toulon, France", ["France"],
                     discovered_at=_now() - timedelta(days=2))
        newer = _job("Newer same score", "Toulon, France", ["France"],
                     discovered_at=_now() - timedelta(days=1))
        _store_jobs_and_scores(s, rs, [older, newer], [0.80, 0.80])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["job_id"] == newer.id
        assert data[1]["job_id"] == older.id

    def test_rank_field_is_sequential(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        jobs = [_job(f"Job {i}", "Toulon, France", ["France"]) for i in range(5)]
        _store_jobs_and_scores(s, rs, jobs, [0.5 + i * 0.05 for i in range(5)])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        ranks = [j["rank"] for j in resp.json()]
        assert ranks == list(range(1, len(ranks) + 1))


# ---------------------------------------------------------------------------
# Tests: Top 10 limit
# ---------------------------------------------------------------------------

class TestTopLimit:
    def test_returns_at_most_10_by_default(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        jobs = [_job(f"Job {i}", "Toulon, France", ["France"]) for i in range(15)]
        _store_jobs_and_scores(s, rs, jobs, [0.5 + i * 0.02 for i in range(15)])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 10

    def test_returns_fewer_than_limit_when_not_enough(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        jobs = [_job(f"Job {i}", "Toulon, France", ["France"]) for i in range(4)]
        _store_jobs_and_scores(s, rs, jobs, [0.70, 0.75, 0.80, 0.85])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert len(resp.json()) == 4

    def test_zero_matching_jobs_returns_empty_list(self, client, db_path):
        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_custom_limit_respected(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        jobs = [_job(f"Job {i}", "Toulon, France", ["France"]) for i in range(10)]
        _store_jobs_and_scores(s, rs, jobs, [0.5 + i * 0.02 for i in range(10)])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=3")
        assert resp.status_code == 200
        assert len(resp.json()) == 3


# ---------------------------------------------------------------------------
# Tests: Score is real stored value (not recalculated)
# ---------------------------------------------------------------------------

class TestScoreAuthority:
    def test_score_100_matches_stored_final_score(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j = _job("Scored job", "Toulon, France", ["France"])
        _store_jobs_and_scores(s, rs, [j], [0.876])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["final_score"] == pytest.approx(0.876, abs=0.0001)
        assert data[0]["score_100"] == 88  # round(0.876 * 100) = 88

    def test_job_with_no_score_gets_zero(self, client, db_path):
        """A job with no score record in job_scores gets final_score=0.0."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j = _job("Unscored", "Toulon, France", ["France"])
        s.upsert_jobs_batch([j])
        # No score record stored.
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["final_score"] == 0.0
        assert data[0]["score_100"] == 0

    def test_top_job_has_highest_score(self, client, db_path):
        """Rank 1 job has the highest stored final_score."""
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j_low = _job("Low scorer", "Toulon, France", ["France"])
        j_high = _job("High scorer", "Toulon, France", ["France"])
        _store_jobs_and_scores(s, rs, [j_low, j_high], [0.50, 0.92])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["job_id"] == j_high.id
        assert data[0]["rank"] == 1
        assert data[0]["score_100"] == 92


# ---------------------------------------------------------------------------
# Tests: Response field presence
# ---------------------------------------------------------------------------

class TestResponseFields:
    def test_required_fields_present(self, client, db_path):
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()

        j = _job("Fields test", "Toulon, France", ["France"])
        j.apply_url = "https://example.com/apply"
        j.canonical_url = "https://example.com/job/1"
        j.skills = ["Python", "SQL"]
        _store_jobs_and_scores(s, rs, [j], [0.80])
        s.close()
        rs.close()

        resp = client.get("/api/jobs/digest/weekly?country=France&department=83&days=7&limit=10")
        assert resp.status_code == 200
        item = resp.json()[0]
        assert "rank" in item
        assert "job_id" in item
        assert "title" in item
        assert "company" in item
        assert "location" in item
        assert "remote" in item
        assert "work_mode" in item
        assert "skills" in item
        assert "source" in item
        assert "final_score" in item
        assert "score_100" in item
        assert "canonical_url" in item
        assert "apply_url" in item
        assert "discovered_at" in item


# ---------------------------------------------------------------------------
# Tests: Existing webhook endpoint unaffected
# ---------------------------------------------------------------------------

class TestExistingEndpointsUnaffected:
    def test_list_jobs_endpoint_still_works(self, client, db_path):
        """GET /api/jobs returns 200 (existing endpoint unaffected)."""
        resp = client.get("/api/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_digest_route_does_not_shadow_job_id_route(self, client, db_path):
        """The /digest/weekly literal path does not get matched by /{job_id}."""
        # If routing were wrong, this would try to look up job_id="digest"
        # and return 404, not the digest response.
        resp = client.get("/api/jobs/digest/weekly")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
