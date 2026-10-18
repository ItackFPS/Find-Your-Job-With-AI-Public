"""Tests for pipeline run API endpoints."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def client(tmp_db: str):
    from src.api.app import app
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    # Initialise DB tables in the temp path
    s = Storage(db_path=tmp_db)
    s.init_db()
    s.close()
    rs = RunStorage(db_path=tmp_db)
    rs.init_db()
    rs.close()

    # Override app state to use isolated DB
    app.state.db_path = tmp_db
    app.state.runner = PipelineRunner(db_path=tmp_db)

    return TestClient(app)


class TestStartRun:
    def test_returns_202(self, client: TestClient) -> None:
        with patch("src.api.runner.run_pipeline") as mock_run:
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            mock_run.return_value = PipelineResult(
                run_id=str(uuid.uuid4()),
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                status="completed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
            )
            r = client.post("/api/pipeline/run")
            # Wait for the daemon thread to finish while the mock is still active.
            # Without this, the thread may call the real run_pipeline after the patch
            # expires, making real HTTP requests that leak into subsequent tests.
            runner = client.app.state.runner
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)
        assert r.status_code == 202

    def test_returns_run_id(self, client: TestClient) -> None:
        with patch("src.api.runner.run_pipeline") as mock_run:
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            from datetime import datetime, timezone
            mock_run.return_value = PipelineResult(
                run_id=str(uuid.uuid4()),
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="completed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
            )
            r = client.post("/api/pipeline/run")
            # Wait for the daemon thread to finish while the mock is still active.
            runner = client.app.state.runner
            deadline = time.time() + 5.0
            while runner.is_running() and time.time() < deadline:
                time.sleep(0.01)
        data = r.json()
        assert "run_id" in data
        assert len(data["run_id"]) == 36  # UUID format

    def test_duplicate_run_returns_409(self, client: TestClient) -> None:
        """A second concurrent run request must return 409."""
        # Force is_running to return True
        client.app.state.runner._current_run_id = str(uuid.uuid4())
        try:
            r = client.post("/api/pipeline/run")
            assert r.status_code == 409
        finally:
            client.app.state.runner._current_run_id = None


class TestStartRunAIFlag:
    """Issue 1 — the API must be able to request an AI-enabled run, default
    to AI disabled when omitted, and propagate the flag through
    PipelineRunner to the actual run_pipeline() call (not just accept it and
    silently drop it)."""

    def _wait_for_run(self, client: TestClient) -> None:
        runner = client.app.state.runner
        deadline = time.time() + 5.0
        while runner.is_running() and time.time() < deadline:
            time.sleep(0.01)

    def test_no_body_defaults_to_ai_disabled(self, client: TestClient) -> None:
        """POST with no body must preserve the pre-existing default: AI off."""
        captured: dict = {}

        def fake_run_pipeline(*args, **kwargs):
            from datetime import datetime, timezone
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            captured.update(kwargs)
            return PipelineResult(
                run_id=str(uuid.uuid4()),
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="completed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
            )

        with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
            r = client.post("/api/pipeline/run")
            self._wait_for_run(client)

        assert r.status_code == 202
        assert captured.get("use_ai") is False

    def test_empty_json_body_defaults_to_ai_disabled(self, client: TestClient) -> None:
        """POST with an explicit empty {} body must also default to AI off."""
        captured: dict = {}

        def fake_run_pipeline(*args, **kwargs):
            from datetime import datetime, timezone
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            captured.update(kwargs)
            return PipelineResult(
                run_id=str(uuid.uuid4()),
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="completed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
            )

        with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
            r = client.post("/api/pipeline/run", json={})
            self._wait_for_run(client)

        assert r.status_code == 202
        assert captured.get("use_ai") is False

    def test_use_ai_true_propagates_to_run_pipeline(self, client: TestClient) -> None:
        """POST {"use_ai": true} must reach run_pipeline(use_ai=True)."""
        captured: dict = {}

        def fake_run_pipeline(*args, **kwargs):
            from datetime import datetime, timezone
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            captured.update(kwargs)
            return PipelineResult(
                run_id=str(uuid.uuid4()),
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="completed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
            )

        with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
            r = client.post("/api/pipeline/run", json={"use_ai": True})
            self._wait_for_run(client)

        assert r.status_code == 202
        assert captured.get("use_ai") is True

    def test_use_ai_recorded_in_run_stats_immediately(self, client: TestClient, tmp_db: str) -> None:
        """The AI flag must be visible in the run's stats as soon as the run
        starts — not only once the pipeline itself gets around to setting
        it — so the dashboard can show it from the first poll."""
        def fake_run_pipeline(*args, **kwargs):
            from datetime import datetime, timezone
            from src.pipeline.orchestrator import PipelineResult, PipelineStats
            # Simulate the run taking a moment, so the test can observe the
            # pre-population before the real pipeline stats overwrite it.
            time.sleep(0.05)
            return PipelineResult(
                run_id=str(uuid.uuid4()),
                started_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                status="completed",
                stats=PipelineStats(),
                scored_jobs=[],
                rejection_records=[],
            )

        with patch("src.api.runner.run_pipeline", side_effect=fake_run_pipeline):
            r = client.post("/api/pipeline/run", json={"use_ai": True})
            run_id = r.json()["run_id"]

            from src.storage.runs import RunStorage
            rs = RunStorage(db_path=tmp_db)
            rs.init_db()
            row = rs.get_run(run_id)
            rs.close()
            self._wait_for_run(client)

        assert row is not None
        assert row["stats"].get("use_ai") is True

    def test_get_run_exposes_use_ai_in_api_response(self, client: TestClient, tmp_db: str) -> None:
        """RunStats.use_ai must actually reach the API response — the same
        'computed internally but dropped by the field whitelist' bug class
        as the earlier hard_filter/requirements_gate breakdown regression."""
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"collected": 1, "use_ai": True})
        rs.close()

        data = client.get(f"/api/pipeline/runs/{run_id}").json()
        assert data["stats"]["use_ai"] is True


class TestGetRun:
    def _create_completed_run(self, db_path: str) -> str:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"collected": 10, "filtered": 5})
        rs.close()
        return run_id

    def test_get_run_returns_200(self, client: TestClient, tmp_db: str) -> None:
        run_id = self._create_completed_run(tmp_db)
        r = client.get(f"/api/pipeline/runs/{run_id}")
        assert r.status_code == 200

    def test_get_run_contains_stats(self, client: TestClient, tmp_db: str) -> None:
        run_id = self._create_completed_run(tmp_db)
        data = client.get(f"/api/pipeline/runs/{run_id}").json()
        assert data["stats"]["collected"] == 10
        assert data["stats"]["filtered"] == 5

    def test_get_nonexistent_run_returns_404(self, client: TestClient) -> None:
        r = client.get("/api/pipeline/runs/does-not-exist")
        assert r.status_code == 404

    def test_get_run_contains_hard_filter_and_requirements_gate_breakdown(
        self, client: TestClient, tmp_db: str
    ) -> None:
        """Regression: the two-stage filter breakdown added to PipelineStats
        must actually reach the API response, not just the internal
        dataclass. `_row_to_run_out` (src/api/routes/pipeline.py) previously
        whitelisted fields one by one and silently dropped any field not
        explicitly listed there AND on the `RunStats` schema — exactly the
        same "field exists in the model but not the API contract" bug class
        as the earlier profile.max_education issue, just one layer over."""
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {
            "collected": 100,
            "validated": 100,
            "deduplicated": 90,
            "hard_filter_input": 90,
            "hard_filter_passed": 60,
            "hard_filter_rejected": 30,
            "requirements_gate_input": 60,
            "requirements_gate_passed": 45,
            "requirements_gate_rejected": 15,
            "filtered": 45,
            "scored": 45,
        })
        rs.close()

        data = client.get(f"/api/pipeline/runs/{run_id}").json()
        stats = data["stats"]
        assert stats["hard_filter_input"] == 90
        assert stats["hard_filter_passed"] == 60
        assert stats["hard_filter_rejected"] == 30
        assert stats["requirements_gate_input"] == 60
        assert stats["requirements_gate_passed"] == 45
        assert stats["requirements_gate_rejected"] == 15

    def test_get_run_contains_llm_eligibility_and_requirements_gate_error(
        self, client: TestClient, tmp_db: str
    ) -> None:
        """Regression: `requirements_gate_error` and the llm_eligibility_*
        breakdown were added to PipelineStats/RunStats but `_build_run_stats`
        never forwarded them — the exact same "whitelist forgot the new
        field" bug class as test_get_run_contains_hard_filter_and_
        requirements_gate_breakdown above, recurring on a different field
        set. Both GET /runs/{id} and the SSE stream share `_build_run_stats`,
        so this one test covers both surfaces."""
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {
            "collected": 100,
            "validated": 100,
            "deduplicated": 90,
            "filtered": 40,
            "scored": 40,
            "requirements_gate_error": "RequirementExtractor raised: boom",
            "llm_eligibility_input": 60,
            "llm_eligibility_passed": 40,
            "llm_eligibility_rejected": 20,
            "llm_eligibility": {"successful": 55, "failed": 5},
        })
        rs.close()

        data = client.get(f"/api/pipeline/runs/{run_id}").json()
        stats = data["stats"]
        assert stats["requirements_gate_error"] == "RequirementExtractor raised: boom"
        assert stats["llm_eligibility_input"] == 60
        assert stats["llm_eligibility_passed"] == 40
        assert stats["llm_eligibility_rejected"] == 20
        assert stats["llm_eligibility"] == {"successful": 55, "failed": 5}


class TestListRuns:
    def test_empty_list(self, client: TestClient) -> None:
        r = client.get("/api/pipeline/runs")
        assert r.status_code == 200
        assert r.json() == []

    def test_lists_runs(self, client: TestClient, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        for _ in range(3):
            rid = str(uuid.uuid4())
            rs.create_run(rid)
            rs.complete_run(rid, {})
        rs.close()

        r = client.get("/api/pipeline/runs")
        assert r.status_code == 200
        assert len(r.json()) == 3


class TestOrchestratorSharedWithCLI:
    """Verify that the API runner and CLI both use the same orchestrator."""

    def test_runner_imports_orchestrator(self) -> None:
        import inspect
        from src.api import runner
        source = inspect.getsource(runner)
        assert "from src.pipeline.orchestrator import run_pipeline" in source

    def test_main_imports_orchestrator(self) -> None:
        import inspect
        from src import main
        source = inspect.getsource(main)
        assert "from src.pipeline.orchestrator import run_pipeline" in source
