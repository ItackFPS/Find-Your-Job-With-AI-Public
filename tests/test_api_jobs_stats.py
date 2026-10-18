"""Tests for GET /api/jobs/stats — real DB-derived stats for the Stats page."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.models.job import Job
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def client(tmp_db: str):
    from src.api.app import app

    s = Storage(db_path=tmp_db)
    s.init_db()

    jobs = [
        Job(id="j1", title="Développeur Python", source="indeed", skills=["python", "docker"]),
        Job(id="j2", title="DevOps Engineer", source="indeed", skills=["python", "aws"]),
        Job(id="j3", title="Ingénieur Sécurité", source="eures", skills=["python"]),
    ]
    for job in jobs:
        s.upsert_job(job)
    s.close()

    rs = RunStorage(db_path=tmp_db)
    rs.init_db()
    rs.create_run("run-1")
    rs.complete_run("run-1", {})
    rs.store_job_scores("run-1", [
        {"job_id": "j1", "final_score": 0.95},  # exceptional
        {"job_id": "j2", "final_score": 0.62},  # relevant (Phase 4: relevant is [60,65))
        {"job_id": "j3", "final_score": 0.30},  # low_priority
    ])
    rs.store_rejections("run-1", [
        {"job_id": "j4", "rejection_stage": "filter", "rejection_detail": "location", "title": "x", "source": "indeed"},
        {"job_id": "j5", "rejection_stage": "filter", "rejection_detail": "location", "title": "y", "source": "indeed"},
        {"job_id": "j6", "rejection_stage": "requirements_gate", "rejection_detail": "education", "title": "z", "source": "eures"},
    ])
    rs.close()

    app.state.db_path = tmp_db
    return TestClient(app)


class TestJobStats:
    def test_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/jobs/stats")
        assert r.status_code == 200

    def test_total_jobs_scoped_to_latest_run(self, client: TestClient) -> None:
        data = client.get("/api/jobs/stats").json()
        assert data["total_jobs"] == 3

    def test_skills_frequency_is_real(self, client: TestClient) -> None:
        data = client.get("/api/jobs/stats").json()
        by_name = {f["name"]: f["count"] for f in data["skills"]}
        assert by_name["python"] == 3

    def test_tools_bucketed_separately_from_skills(self, client: TestClient) -> None:
        data = client.get("/api/jobs/stats").json()
        tool_names = {f["name"] for f in data["tools"]}
        skill_names = {f["name"] for f in data["skills"]}
        assert "docker" in tool_names
        assert "aws" in tool_names
        assert "docker" not in skill_names

    def test_score_tier_distribution(self, client: TestClient) -> None:
        data = client.get("/api/jobs/stats").json()
        tiers = {f["tier"]: f["count"] for f in data["score_tiers"]}
        assert tiers == {"exceptional": 1, "relevant": 1, "low_priority": 1}

    def test_rejection_reasons(self, client: TestClient) -> None:
        data = client.get("/api/jobs/stats").json()
        rejections = {(f["stage"], f["detail"]): f["count"] for f in data["rejections"]}
        assert rejections[("filter", "location")] == 2
        assert rejections[("requirements_gate", "education")] == 1

    def test_requirements_gate_json_detail_normalized_to_type(self, tmp_db: str) -> None:
        """requirements_gate stores a JSON list of structured reasons (see
        rejection_reasons_to_detail in src/requirements/gate.py) — the stats
        endpoint must group by the first reason's `type`, not the raw JSON.
        """
        import json as _json

        from src.api.app import app

        s = Storage(db_path=tmp_db)
        s.init_db()
        s.upsert_job(Job(id="j1", title="Ingénieur Python", source="indeed"))
        s.close()

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        rs.create_run("run-1")
        rs.complete_run("run-1", {})
        rs.store_job_scores("run-1", [{"job_id": "j1", "final_score": 0.5}])
        rs.store_rejections("run-1", [{
            "job_id": "j2",
            "rejection_stage": "requirements_gate",
            "rejection_detail": _json.dumps([{"type": "principal_skill", "requirement": "Java", "candidate_value": "not in profile"}]),
            "title": "z", "source": "indeed",
        }])
        rs.close()

        app.state.db_path = tmp_db
        client = TestClient(app)
        data = client.get("/api/jobs/stats").json()
        rejections = {(f["stage"], f["detail"]): f["count"] for f in data["rejections"]}
        assert rejections[("requirements_gate", "principal_skill")] == 1

    def test_title_categories_bucket_real_titles(self, client: TestClient) -> None:
        data = client.get("/api/jobs/stats").json()
        categories = {f["name"]: f["count"] for f in data["title_categories"]}
        assert categories["Python / Data"] == 1
        assert categories["DevOps / Cloud"] == 1
        assert categories["Cybersecurity"] == 1

    def test_task_descriptions_are_not_presented_as_skills(self, tmp_db: str) -> None:
        """Regression: France Travail's `competences[].libelle` values are
        ROME task/activity descriptions ("Configurer le poste de travail aux
        besoins de l'utilisateur"), not skills — they must never surface in
        the skills/tools facets, generically (not just this one example)."""
        from src.api.app import app

        s = Storage(db_path=tmp_db)
        s.init_db()
        s.upsert_job(Job(
            id="j1", title="Technicien support", source="france-travail",
            skills=[
                "Configurer le poste de travail aux besoins de l'utilisateur",
                "Administrer un système d'informations",
                "Apporter une assistance technique aux équipes",
                "Python",
            ],
        ))
        s.close()

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        rs.create_run("run-1")
        rs.complete_run("run-1", {})
        rs.store_job_scores("run-1", [{"job_id": "j1", "final_score": 0.5}])
        rs.close()

        app.state.db_path = tmp_db
        client = TestClient(app)
        data = client.get("/api/jobs/stats").json()
        all_names = {f["name"] for f in data["skills"]} | {f["name"] for f in data["tools"]}

        assert "Configurer le poste de travail aux besoins de l'utilisateur" not in all_names
        assert "Administrer un système d'informations" not in all_names
        assert "Apporter une assistance technique aux équipes" not in all_names
        assert any(name.lower() == "python" for name in all_names)

    def test_case_variants_of_the_same_technology_are_merged(self, tmp_db: str) -> None:
        """Regression: raw skill values are grouped case-sensitively in SQL,
        so "Docker" and "docker" from different sources used to appear as two
        separate facets instead of one merged count."""
        from src.api.app import app

        s = Storage(db_path=tmp_db)
        s.init_db()
        s.upsert_job(Job(id="j1", title="Dev", source="linkedin", skills=["Docker"]))
        s.upsert_job(Job(id="j2", title="Dev", source="indeed", skills=["docker"]))
        s.upsert_job(Job(id="j3", title="Dev", source="indeed", skills=["docker"]))
        s.close()

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        rs.create_run("run-1")
        rs.complete_run("run-1", {})
        rs.store_job_scores("run-1", [
            {"job_id": "j1", "final_score": 0.5},
            {"job_id": "j2", "final_score": 0.5},
            {"job_id": "j3", "final_score": 0.5},
        ])
        rs.close()

        app.state.db_path = tmp_db
        client = TestClient(app)
        data = client.get("/api/jobs/stats").json()
        docker_facets = [f for f in data["tools"] if f["name"].lower() == "docker"]

        assert len(docker_facets) == 1, f"Expected one merged 'docker' facet, got {docker_facets}"
        assert docker_facets[0]["count"] == 3
        # Majority-vote display casing: 2 lower-case vs 1 title-case.
        assert docker_facets[0]["name"] == "docker"

    def test_score_tier_counts_sum_to_total_jobs(self, client: TestClient) -> None:
        """Every job in the fixture has a non-null final_score, so the tier
        distribution must fully account for total_jobs — no jobs silently
        dropped from the population it's supposed to summarize."""
        data = client.get("/api/jobs/stats").json()
        assert sum(f["count"] for f in data["score_tiers"]) == data["total_jobs"]

    def test_no_completed_run_returns_all_jobs_no_run_scoped_stats(self, tmp_db: str) -> None:
        """Falls back to all jobs in DB (like /countries) when no run has completed yet."""
        from src.api.app import app

        s = Storage(db_path=tmp_db)
        s.init_db()
        s.upsert_job(Job(id="j1", title="Développeur Python", source="indeed", skills=["python"]))
        s.close()
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        rs.close()

        app.state.db_path = tmp_db
        client = TestClient(app)

        data = client.get("/api/jobs/stats").json()
        assert data["total_jobs"] == 1
        assert data["score_tiers"] == []
        assert data["rejections"] == []
