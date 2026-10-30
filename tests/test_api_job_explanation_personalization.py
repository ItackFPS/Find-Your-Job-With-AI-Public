"""Phase NEXT / I8 regression tests: GET /api/jobs/{id}/explanation must expose
the REAL personalization signals already computed by
``src.personalization.engine`` instead of the hardcoded
``personalization_signals=[]`` / ``personalization_adjustment=0.0`` it
returned before this change.

These tests hit the route through the FastAPI TestClient (not just the
engine directly) so a regression in the wiring itself would be caught, while
still using isolated tmp_path databases/config dirs (per CLAUDE.md: tests
must never touch the real data/ or config/ directories).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models.job import Job
from src.personalization.models import FeedbackType
from src.personalization.storage import PersonalizationStorage
from src.storage.sqlite import Storage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def client(tmp_db: str, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from src.api.app import app

    s = Storage(db_path=tmp_db)
    s.init_db()
    s.close()

    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=tmp_db)
    rs.init_db()
    rs.close()

    monkeypatch.setitem(app.state._state, "db_path", tmp_db)
    monkeypatch.setitem(app.state._state, "config_dir", str(tmp_config_dir))

    return TestClient(app)


def _make_job(company: str = "Acme Corp", skills: list[str] | None = None) -> Job:
    return Job(
        source="greenhouse",
        source_job_id="123",
        title="Backend Developer",
        company=company,
        description="A great backend role.",
        skills=skills or ["python"],
        remote=False,
        status="active",
    )


def _seed_job(db_path: str, job: Job) -> None:
    s = Storage(db_path=db_path)
    s.init_db()
    s.upsert_job(job)
    s.close()


class TestNoFeedbackYet:
    def test_returns_empty_signals_and_zero_adjustment(self, client: TestClient, tmp_db: str) -> None:
        job = _make_job()
        _seed_job(tmp_db, job)

        resp = client.get(f"/api/jobs/{job.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["personalization_signals"] == []
        assert data["personalization_adjustment"] == 0.0


class TestMatchingFeedbackProducesRealSignal:
    def test_company_signal_reflects_recorded_feedback(self, client: TestClient, tmp_db: str) -> None:
        # Record positive feedback on a *different* job at the same company,
        # then ask for the explanation of a second job at that company.
        liked_job = _make_job(company="Acme Corp", skills=["python"])
        target_job = _make_job(company="Acme Corp", skills=["python"])
        _seed_job(tmp_db, liked_job)
        _seed_job(tmp_db, target_job)

        pers = PersonalizationStorage(db_path=tmp_db)
        pers.init_db()
        pers.add_feedback(liked_job, FeedbackType.INTERESTED)
        pers.close()

        resp = client.get(f"/api/jobs/{target_job.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()

        signal_types = {s["signal_type"] for s in data["personalization_signals"]}
        assert "company" in signal_types
        assert "skill" in signal_types
        assert data["personalization_adjustment"] > 0.0

    def test_adjustment_matches_direct_engine_computation(self, client: TestClient, tmp_db: str) -> None:
        """The API must never diverge from (or fabricate beyond) what the
        real personalization engine computes for the same inputs."""
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import build_preference_profile, score_job_personalization

        liked_job = _make_job(company="Acme Corp", skills=["python"])
        target_job = _make_job(company="Acme Corp", skills=["python"])
        _seed_job(tmp_db, liked_job)
        _seed_job(tmp_db, target_job)

        pers = PersonalizationStorage(db_path=tmp_db)
        pers.init_db()
        pers.add_feedback(liked_job, FeedbackType.INTERESTED)

        cfg = load_personalization_config("config/does-not-exist.yaml")  # -> defaults
        feedback_records = pers.get_all_feedback()
        version = pers.get_feedback_version()
        profile = build_preference_profile(feedback_records, cfg, version)
        expected_adjustment, expected_expl = score_job_personalization(target_job, profile, cfg)
        pers.close()

        resp = client.get(f"/api/jobs/{target_job.id}/explanation")
        data = resp.json()

        assert data["personalization_adjustment"] == pytest.approx(expected_adjustment)
        assert len(data["personalization_signals"]) == len(expected_expl.matches)


class TestExcludedActionedJob:
    def test_own_terminal_feedback_yields_no_signals(self, client: TestClient, tmp_db: str) -> None:
        """A job the user already marked not_interested is excluded by the
        engine (via ``excluded_reason``) — the API must reflect that as no
        signals / zero adjustment, never leak the exclusion reason as a
        fabricated 'signal'."""
        job = _make_job()
        _seed_job(tmp_db, job)

        pers = PersonalizationStorage(db_path=tmp_db)
        pers.init_db()
        pers.add_feedback(job, FeedbackType.NOT_INTERESTED)
        pers.close()

        resp = client.get(f"/api/jobs/{job.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["personalization_signals"] == []
        assert data["personalization_adjustment"] == 0.0


class TestMalformedPersonalizationConfigDoesNotCrash:
    def test_non_mapping_yaml_falls_back_to_empty(
        self, client: TestClient, tmp_db: str, tmp_config_dir: Path
    ) -> None:
        job = _make_job()
        _seed_job(tmp_db, job)

        pers = PersonalizationStorage(db_path=tmp_db)
        pers.init_db()
        pers.add_feedback(job, FeedbackType.INTERESTED)
        pers.close()

        (tmp_config_dir / "personalization.yaml").write_text("- not\n- a\n- mapping\n", encoding="utf-8")

        resp = client.get(f"/api/jobs/{job.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["personalization_signals"] == []
        assert data["personalization_adjustment"] == 0.0


class TestExistingExplanationBehaviorUnaffected:
    def test_response_still_includes_positive_reasons_shape(self, client: TestClient, tmp_db: str) -> None:
        """Guard against the personalization wiring having broken the
        pre-existing explanation fields."""
        job = _make_job()
        _seed_job(tmp_db, job)

        resp = client.get(f"/api/jobs/{job.id}/explanation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == job.id
        assert "positive_reasons" in data
        assert "concerns" in data
        assert "rejection_reasons" in data
        assert data["filter_result"] == "passed"
