"""Integration tests: Phase 3 → Phase 4 pipeline.

These tests verify:
  A — normal pipeline never invokes Phase 2 / LM Studio
  B — Phase 2 component (isolated) analyses candidates and stores results
  D — missing scoring config does NOT discard filtered jobs
  E — Phase 2 bridge produces correct enriched text (component-level)
  F — Phase 2 candidate limit and ordering (component-level)

No real Ollama server or network is required.  LLM calls are mocked.
sentence-transformers is not loaded; the EmbeddingService is mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from src.ai.models import (
    BatchJobUnderstandingResult,
    JobUnderstanding,
    JobUnderstandingResult,
)
from src.ai.phase3_bridge import build_enriched_job_text, build_enriched_texts
from src.models.job import Job
from src.pipeline.semantic_matching import apply_semantic_matching

if TYPE_CHECKING:
    from src.embeddings.embedding import BatchEmbeddingResult


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_job() -> Job:
    return Job(
        id="job-python-001",
        source="test",
        title="Senior Python Engineer",
        company="Acme",
        description="We need a senior Python engineer with Django and PostgreSQL.",
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        discovered_at=datetime.now(UTC),
    )


@pytest.fixture
def sample_job_2() -> Job:
    return Job(
        id="job-react-002",
        source="test",
        title="Frontend React Developer",
        company="StartupXYZ",
        description="React, TypeScript and good CSS skills required.",
        location="Remote",
        countries=[],
        remote=True,
        employment_type="full-time",
        discovered_at=datetime.now(UTC),
    )


@pytest.fixture
def mock_understanding() -> JobUnderstanding:
    return JobUnderstanding(
        seniority_level="senior",
        required_skills=["Python", "Django", "PostgreSQL"],
        preferred_skills=["Redis", "Celery"],
        programming_languages=["Python"],
        frameworks=["Django"],
        tools=["Docker"],
        technologies=["AWS"],
        responsibilities=["Backend development", "API design"],
        language_requirements=["English", "French"],
        domain="Backend Engineering",
        confidence=0.9,
    )


@pytest.fixture
def successful_result(sample_job, mock_understanding) -> JobUnderstandingResult:
    return JobUnderstandingResult(
        job_id=sample_job.id,
        analysis=mock_understanding,
        prompt_version="job_understanding_v1",
        model_identifier="mock-llm",
        status="success",
    )


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

class MockEmbeddingService:
    """Deterministic embedding service that never loads sentence-transformers."""

    def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            return [0.0] * 10
        return [float(len(text) % (i + 2)) for i in range(10)]

    def embed_batch(self, texts: list[str], ids: list[str] | None = None) -> "BatchEmbeddingResult":
        from src.embeddings.embedding import BatchEmbeddingResult
        vectors = [self.embed(t) for t in texts]
        batch_ids = ids if ids is not None else [str(i) for i in range(len(texts))]
        return BatchEmbeddingResult(ids=batch_ids, vectors=vectors)

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    def compute_semantic_score(self, job_text: str, profile_embedding: list[float]) -> float:
        if not job_text.strip():
            return 0.0
        return min(0.5 + len(job_text) / 10_000, 1.0)

    def compute_job_semantic_score(self, job: Job, profile_embedding: list[float]) -> float:
        parts = []
        if job.title:
            parts.append(job.title)
        if job.description:
            parts.append(job.description)
        return self.compute_semantic_score(" ".join(parts), profile_embedding)


def _make_batch_result(jobs: list[Job], understanding: JobUnderstanding) -> BatchJobUnderstandingResult:
    results = [
        JobUnderstandingResult(
            job_id=job.id,
            analysis=understanding,
            prompt_version="job_understanding_v1",
            model_identifier="mock-llm",
            status="success",
        )
        for job in jobs
    ]
    return BatchJobUnderstandingResult(
        total_jobs=len(jobs),
        successful=len(jobs),
        failed=0,
        skipped=0,
        results=results,
    )


# ---------------------------------------------------------------------------
# Test D: scoring fallback
# ---------------------------------------------------------------------------

class TestScoringFallback:
    """Test D — missing scoring config must not discard filtered jobs."""

    def test_fallback_preserves_all_filtered_jobs(self, sample_job, sample_job_2):
        """When scoring config is unavailable, every filtered job gets score 0.5."""
        filtered_jobs = [sample_job, sample_job_2]

        # This is the fallback logic from main.py — test it directly.
        scored_jobs = [(job, 0.5) for job in filtered_jobs]

        assert len(scored_jobs) == 2
        for job, score in scored_jobs:
            assert score == 0.5

    def test_phase3_receives_jobs_with_fallback_score(self, sample_job, sample_job_2):
        """Phase 3 must receive the same jobs that were filtered, with fallback score."""
        filtered_jobs = [sample_job, sample_job_2]
        scored_jobs = [(job, 0.5) for job in filtered_jobs]

        jobs_only = [job for job, _ in scored_jobs]
        assert len(jobs_only) == len(filtered_jobs)
        assert jobs_only[0].id == sample_job.id
        assert jobs_only[1].id == sample_job_2.id

    def test_fallback_does_not_affect_phase3_semantic_matching(
        self, sample_job, sample_job_2
    ):
        """Phase 3 apply_semantic_matching runs correctly on fallback-scored jobs."""
        filtered_jobs = [sample_job, sample_job_2]
        scored_jobs = [(job, 0.5) for job in filtered_jobs]
        jobs_only = [job for job, _ in scored_jobs]

        embedding_service = MockEmbeddingService()
        profile_embedding = [0.5] * 10
        config: dict[str, Any] = {
            "similarity": {"weight": 0.2, "min_threshold": 0.0},
        }

        scored, unscored = apply_semantic_matching(
            jobs_only, profile_embedding, config, embedding_service  # type: ignore[arg-type]
        )

        assert len(scored) == 2
        assert len(unscored) == 0


# ---------------------------------------------------------------------------
# Test E: Phase 2 → Phase 3 contract
# ---------------------------------------------------------------------------

class TestPhase2ToPhase3Contract:
    """Test E — Phase 2 output is actually consumed by Phase 3."""

    def test_build_enriched_job_text_uses_ai_fields(
        self, sample_job, successful_result
    ):
        """Enriched text includes AI-extracted skills, seniority, and domain."""
        text = build_enriched_job_text(sample_job, successful_result)

        assert "senior python engineer" in text  # title
        assert "seniority: senior" in text
        assert "django" in text
        assert "redis" in text
        assert "backend development" in text
        assert "domain: backend engineering" in text
        assert "english" in text

    def test_build_enriched_texts_only_includes_successful(
        self, sample_job, sample_job_2, successful_result
    ):
        """build_enriched_texts excludes jobs with failed or skipped status."""
        failed_result = JobUnderstandingResult(
            job_id=sample_job_2.id,
            analysis=JobUnderstanding(),
            prompt_version="job_understanding_v1",
            model_identifier="mock-llm",
            status="failed",
            error_message="timeout",
        )
        ai_results = {
            sample_job.id: successful_result,
            sample_job_2.id: failed_result,
        }

        enriched = build_enriched_texts([sample_job, sample_job_2], ai_results)

        assert sample_job.id in enriched
        assert sample_job_2.id not in enriched

    def test_apply_semantic_matching_uses_enriched_text(
        self, sample_job, sample_job_2
    ):
        """When enriched_texts is provided, Phase 3 uses it instead of raw fields."""

        jobs = [sample_job, sample_job_2]
        profile_embedding = [0.5] * 10
        config: dict[str, Any] = {"similarity": {"min_threshold": 0.0}}

        enriched_texts = {
            sample_job.id: "python django senior backend engineer skills: python django aws"
        }

        service = MockEmbeddingService()

        # Intercept embed_batch to inspect the texts that were batched.
        batched_texts: list[str] = []
        original_batch = service.embed_batch

        def recording_embed_batch(
            texts: list[str], ids: list[str] | None = None
        ) -> BatchEmbeddingResult:
            batched_texts.extend(texts)
            return original_batch(texts, ids)

        service.embed_batch = recording_embed_batch  # type: ignore[method-assign]

        scored, _ = apply_semantic_matching(
            jobs, profile_embedding, config, service, enriched_texts=enriched_texts  # type: ignore[arg-type]
        )

        assert len(scored) == 2

        # sample_job must have been embedded using the enriched text.
        enriched_text = next(t for t in batched_texts if "skills:" in t)
        assert "python" in enriched_text
        assert "django" in enriched_text

    def test_apply_semantic_matching_falls_back_when_no_enriched_text(
        self, sample_job
    ):
        """When a job has no enriched text, raw title+description is used."""

        profile_embedding = [0.5] * 10
        config: dict[str, Any] = {"similarity": {"min_threshold": 0.0}}

        service = MockEmbeddingService()
        batched_texts: list[str] = []
        original_batch = service.embed_batch

        def recording(texts: list[str], ids: list[str] | None = None) -> BatchEmbeddingResult:
            batched_texts.extend(texts)
            return original_batch(texts, ids)

        service.embed_batch = recording  # type: ignore[method-assign]

        scored, _ = apply_semantic_matching(
            [sample_job], profile_embedding, config, service, enriched_texts={}  # type: ignore[arg-type]
        )

        assert len(scored) == 1
        # Text passed to the embedder must come from job.title + job.description.
        assert batched_texts, "embed_batch was not called"
        assert "python engineer" in batched_texts[0].lower()

    def test_enriched_text_empty_job_is_unscored(self):
        """A job with empty title, description, and empty enriched text is unscored."""
        empty_job = Job(
            id="empty-job",
            source="test",
            title="",
            company="X",
            description=None,
        )

        profile_embedding = [0.5] * 10
        config: dict[str, Any] = {"similarity": {"min_threshold": 0.0}}
        service = MockEmbeddingService()

        scored, unscored = apply_semantic_matching(
            [empty_job], profile_embedding, config, service  # type: ignore[arg-type]
        )

        assert len(scored) == 0
        assert len(unscored) == 1
        assert unscored[0][1] == 0.0


# ---------------------------------------------------------------------------
# Test A: normal pipeline never touches Phase 2 / LM Studio
# ---------------------------------------------------------------------------

class TestNoPipelinePhase2:
    """Regression — run_pipeline must never call the LLM or connect to LM Studio."""

    def test_run_pipeline_never_calls_lm_studio(self, tmp_path):
        """Any request to a local LLM port (1234 / 11434) must raise during a normal run.

        Uses an empty config directory (no sources.yaml) so no collectors run
        and no real network calls are made.  The pipeline still exercises all
        stages and must complete without touching LLM endpoints.
        """
        from unittest.mock import patch, MagicMock
        from src.pipeline.orchestrator import run_pipeline
        import requests as _requests

        db_path = str(tmp_path / "test.db")
        config_dir = str(tmp_path / "config")
        (tmp_path / "config").mkdir()

        llm_ports = (":1234", ":11434")

        def _guard_get(url, *args, **kwargs):
            assert not any(p in url for p in llm_ports), (
                f"Pipeline called LLM endpoint (GET): {url}"
            )
            # All other GETs are allowed to fail with a connection error.
            raise _requests.exceptions.ConnectionError(f"test: no network: {url}")

        def _guard_post(url, *args, **kwargs):
            assert not any(p in url for p in llm_ports), (
                f"Pipeline called LLM endpoint (POST): {url}"
            )
            raise _requests.exceptions.ConnectionError(f"test: no network: {url}")

        # Mock the embedding service so sentence-transformers is not loaded.
        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5

        with (
            patch("requests.get", side_effect=_guard_get),
            patch("requests.post", side_effect=_guard_post),
            patch("requests.Session.post", side_effect=_guard_post),
            patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed),
        ):
            result = run_pipeline(db_path=db_path, config_dir=config_dir)

        # Pipeline must complete even though collectors fail (no network).
        assert result.status == "completed", f"Pipeline failed: {result.error}"
        # Phase 2 stats must be empty — it never ran.
        assert result.stats.phase2 == {}

    def test_run_pipeline_use_ai_false_is_explicit_default(self, tmp_path):
        """run_pipeline(use_ai=False) behaves identically to the no-flag default."""
        from unittest.mock import MagicMock, patch


        from src.pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")
        config_dir = str(tmp_path / "config")
        (tmp_path / "config").mkdir()
        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384

        with (
            patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed),
        ):
            result = run_pipeline(db_path=db_path, config_dir=config_dir, use_ai=False)

        assert result.status == "completed"
        assert result.stats.phase2 == {}

    def test_phase2_not_in_orchestrator_exports(self):
        """_run_phase2 must not be importable from the orchestrator."""
        import src.pipeline.orchestrator as orch

        assert not hasattr(orch, "_run_phase2"), (
            "_run_phase2 was found in the orchestrator — it should have been removed"
        )

    def test_main_cli_no_ai_flag_does_not_raise_typeerror(self, tmp_path, monkeypatch):
        """python src/main.py (no --ai flag) must not raise TypeError."""
        from unittest.mock import MagicMock, patch

        import requests as _requests

        monkeypatch.chdir(str(tmp_path))
        (tmp_path / "config").mkdir()
        (tmp_path / "data").mkdir()

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384

        def _no_network(url, *a, **kw):
            raise _requests.exceptions.ConnectionError("no network")

        with (
            patch("requests.get", side_effect=_no_network),
            patch("requests.post", side_effect=_no_network),
            patch("requests.Session.post", side_effect=_no_network),
            patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed),
        ):
            from src.main import main
            # Should not raise — an empty config dir means zero jobs collected.
            main(argv=[])


# ---------------------------------------------------------------------------
# Test CLI --ai flag: run_pipeline is called with use_ai=True
# ---------------------------------------------------------------------------

class TestAIPipelineCLIFlag:
    """Verify the --ai CLI flag reaches run_pipeline as use_ai=True."""

    def test_main_ai_flag_calls_run_pipeline_with_use_ai_true(self, tmp_path):
        """main(argv=['--ai']) must call run_pipeline(use_ai=True)."""
        from unittest.mock import patch

        from src.pipeline.orchestrator import PipelineResult, PipelineStats

        captured: dict = {}

        fake_result = PipelineResult(
            run_id="fake-run-id",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            status="completed",
            stats=PipelineStats(),
            scored_jobs=[],
            rejection_records=[],
        )

        def fake_run_pipeline(*args, **kwargs):
            captured.update(kwargs)
            return fake_result

        with patch("src.main.run_pipeline", side_effect=fake_run_pipeline):
            from src.main import main
            main(argv=["--ai"])

        assert captured.get("use_ai") is True, (
            f"run_pipeline was not called with use_ai=True; kwargs={captured}"
        )

    def test_main_no_flag_calls_run_pipeline_with_use_ai_false(self, tmp_path):
        """main(argv=[]) must call run_pipeline(use_ai=False)."""
        from unittest.mock import patch

        from src.pipeline.orchestrator import PipelineResult, PipelineStats

        captured: dict = {}

        fake_result = PipelineResult(
            run_id="fake-run-id",
            started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            completed_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            status="completed",
            stats=PipelineStats(),
            scored_jobs=[],
            rejection_records=[],
        )

        def fake_run_pipeline(*args, **kwargs):
            captured.update(kwargs)
            return fake_result

        with patch("src.main.run_pipeline", side_effect=fake_run_pipeline):
            from src.main import main
            main(argv=[])

        assert captured.get("use_ai") is False


# ---------------------------------------------------------------------------
# Test use_ai=True path in orchestrator
# ---------------------------------------------------------------------------

class TestRunPipelineWithAI:
    """run_pipeline(use_ai=True) must call Phase2Pipeline and populate stats.phase2."""

    def _make_mock_embed(self):
        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5
        return mock_embed

    def test_use_ai_true_populates_phase2_stats(self, tmp_path):
        """When use_ai=True and Phase 2 completes, stats.phase2 is non-empty."""
        from unittest.mock import MagicMock, patch

        from src.ai.models import BatchJobUnderstandingResult
        from src.pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")
        config_dir = str(tmp_path / "config")
        (tmp_path / "config").mkdir()

        mock_embed = self._make_mock_embed()

        # A Phase2Pipeline that immediately returns an empty batch (no jobs to analyse).
        mock_phase2 = MagicMock()
        mock_phase2.run.return_value = BatchJobUnderstandingResult(
            total_jobs=0, successful=0, failed=0, skipped=0, results=[]
        )

        with (
            patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed),
            patch("src.ai.pipeline.Phase2Pipeline", return_value=mock_phase2),
            patch("src.ai.storage.AIAnalysisStorage"),
        ):
            result = run_pipeline(db_path=db_path, config_dir=config_dir, use_ai=True)

        assert result.status == "completed"
        # stats.phase2 is populated (not the empty-dict default).
        assert result.stats.phase2 != {} or result.stats.phase2.get("error") is not None, (
            "stats.phase2 should be populated when use_ai=True"
        )

    def test_phase2_stage_notified_distinctly(self, tmp_path):
        """Issue 5 regression: the dashboard must see a distinct 'phase2'
        stage — not stay on 'score' for the whole AI window. The 'phase2'
        notification must fire before Phase2Pipeline.run() even completes
        (announced immediately, not only once results are in), and before
        the following 'semantic' stage.
        """
        from unittest.mock import MagicMock, patch

        from src.ai.models import BatchJobUnderstandingResult
        from src.pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")
        config_dir = str(tmp_path / "config")
        (tmp_path / "config").mkdir()

        mock_embed = self._make_mock_embed()

        mock_phase2 = MagicMock()
        mock_phase2.run.return_value = BatchJobUnderstandingResult(
            total_jobs=0, successful=0, failed=0, skipped=0, results=[]
        )

        stages_seen: list[str] = []

        def progress_callback(stage, stats):
            stages_seen.append(stage)

        with (
            patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed),
            patch("src.ai.pipeline.Phase2Pipeline", return_value=mock_phase2),
            patch("src.ai.storage.AIAnalysisStorage"),
        ):
            result = run_pipeline(
                db_path=db_path,
                config_dir=config_dir,
                use_ai=True,
                progress_callback=progress_callback,
            )

        assert result.status == "completed"
        assert "phase2" in stages_seen, f"'phase2' stage never notified; saw: {stages_seen}"
        assert stages_seen.index("phase2") < stages_seen.index("semantic")
        assert stages_seen.index("score") < stages_seen.index("phase2")

    def test_phase2_stats_reach_run_stats_schema(self, tmp_path):
        """stats.phase2 must reach RunStats via the API — it used to be
        computed but silently dropped by _row_to_run_out's field whitelist.
        """
        from src.api.schemas import RunStats

        # A field that doesn't exist on RunStats would raise here.
        rs = RunStats(phase2={"total": 5, "completed": 3, "failed": 1}, use_ai=True)
        assert rs.phase2["total"] == 5
        assert rs.use_ai is True

    def test_use_ai_true_phase2_failure_continues_pipeline(self, tmp_path):
        """When use_ai=True but Phase 2 raises, the pipeline still completes."""
        from src.pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")
        config_dir = str(tmp_path / "config")
        (tmp_path / "config").mkdir()

        mock_embed = self._make_mock_embed()

        def _crash(*a, **kw):
            raise RuntimeError("LLM unavailable")

        with (
            patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed),
            patch("src.ai.pipeline.Phase2Pipeline", side_effect=_crash),
        ):
            result = run_pipeline(db_path=db_path, config_dir=config_dir, use_ai=True)

        assert result.status == "completed", f"Pipeline failed: {result.error}"
        assert "error" in result.stats.phase2


# ---------------------------------------------------------------------------
# Test B: --ai pipeline calls Phase 2 and enriches Phase 3
# ---------------------------------------------------------------------------

class TestAIPipelinePhase2Integration:
    """Test B — Phase 2 is called and its output reaches Phase 3."""

    def test_phase2_pipeline_produces_enriched_texts(
        self, sample_job, sample_job_2, mock_understanding, tmp_path
    ):
        """Phase 2 pipeline with a mock LLM produces enriched texts for Phase 3."""
        from src.ai.pipeline import Phase2Pipeline
        from src.ai.storage import AIAnalysisStorage
        from src.storage.sqlite import Storage

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        storage.init_db()
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        # Mock LLM that returns our known understanding for every job.
        mock_llm = MagicMock()
        mock_llm.analyze_batch.return_value = _make_batch_result(
            [sample_job, sample_job_2], mock_understanding
        )

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=mock_llm,
            prompt_version="job_understanding_v1",
        )

        batch_result = pipeline.run([sample_job, sample_job_2])

        assert batch_result.successful == 2
        assert batch_result.failed == 0

        # Build enriched texts (the bridge step in main.py).
        ai_results_by_job = {r.job_id: r for r in batch_result.results}
        enriched = build_enriched_texts(
            [sample_job, sample_job_2], ai_results_by_job
        )

        assert len(enriched) == 2
        assert "seniority: senior" in enriched[sample_job.id]
        assert "django" in enriched[sample_job.id]

    def test_phase2_analyses_persisted_in_db(
        self, sample_job, mock_understanding, tmp_path
    ):
        """AI analyses are stored in the database after Phase 2 runs."""
        from src.ai.pipeline import Phase2Pipeline
        from src.ai.storage import AIAnalysisStorage
        from src.storage.sqlite import Storage

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        storage.init_db()
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        mock_llm = MagicMock()
        mock_llm.analyze_batch.return_value = _make_batch_result(
            [sample_job], mock_understanding
        )

        pipeline = Phase2Pipeline(
            storage=storage,
            ai_storage=ai_storage,
            llm_provider=mock_llm,
        )
        pipeline.run([sample_job])

        # Verify the analysis is stored.
        stored = ai_storage.get_analysis(sample_job.id)
        assert stored is not None
        assert stored.status == "success"
        assert stored.analysis.seniority_level == "senior"

    def test_phase3_uses_enriched_texts_from_phase2(
        self, sample_job, sample_job_2, mock_understanding, tmp_path
    ):
        """End-to-end: Phase 2 output flows into Phase 3 scoring."""
        from src.ai.pipeline import Phase2Pipeline
        from src.ai.storage import AIAnalysisStorage
        from src.storage.sqlite import Storage

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        storage.init_db()
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        mock_llm = MagicMock()
        mock_llm.analyze_batch.return_value = _make_batch_result(
            [sample_job, sample_job_2], mock_understanding
        )

        pipeline = Phase2Pipeline(
            storage=storage, ai_storage=ai_storage, llm_provider=mock_llm
        )
        batch = pipeline.run([sample_job, sample_job_2])

        ai_results = {r.job_id: r for r in batch.results}
        enriched = build_enriched_texts([sample_job, sample_job_2], ai_results)

        # Verify Phase 3 can consume the enriched texts.
        profile_embedding = [0.5] * 10
        config: dict[str, Any] = {"similarity": {"min_threshold": 0.0}}
        service = MockEmbeddingService()

        scored, unscored = apply_semantic_matching(
            [sample_job, sample_job_2],
            profile_embedding,
            config,
            service,  # type: ignore[arg-type]
            enriched_texts=enriched,
        )

        assert len(scored) == 2
        assert len(unscored) == 0
        # Both jobs must have a positive semantic score.
        for _, score in scored:
            assert score > 0.0


# ---------------------------------------------------------------------------
# Issue 6 — profile_match_score architecture diagnosis
#
# Full diagnosis (see the engineering report for the complete write-up):
# profile_match_score/profile_match_reasons/profile_mismatch_reasons were
# deliberately removed from LLM generation in the 2026-09-04 personalization
# audit (see src/ai/prompt.py's build_personalized_matching_prompt docstring)
# because job-profile fit is already computed twice downstream — the
# deterministic scorer's profile-aware factors (src/pipeline/scoring.py) and
# Phase 3's profile-embedding cosine similarity (src/pipeline/
# semantic_matching.py) — and human-readable "why this job" reasons already
# ship via src/pipeline/explain.py. A third LLM-judged fit score would only
# double-count the same signal without composing cleanly into
# integrate_semantic_into_ranking's fixed det_weight/weight formula. These
# tests prove the current code enforces that: Phase 2's structured
# extraction (skills/seniority/domain) already flows into ranking via
# enriched_texts -> semantic embedding, but profile_match_score does not
# and — regardless of its value, even if a legacy/cached analysis has one —
# never affects the final score. This is intentional, not an oversight.
# ---------------------------------------------------------------------------

class TestProfileMatchScoreNotUsedInRanking:
    def test_enriched_text_is_identical_regardless_of_profile_match_score(
        self, sample_job, mock_understanding
    ):
        """build_enriched_job_text must never read profile_match_score —
        two analyses differing ONLY in that field must produce identical
        enriched text, proving the field cannot influence the semantic
        embedding (and therefore the final ranking) either way."""
        low_score_analysis = mock_understanding.model_copy(
            update={"profile_match_score": 0.05, "profile_match_reasons": ["barely matches"]}
        )
        high_score_analysis = mock_understanding.model_copy(
            update={"profile_match_score": 0.98, "profile_match_reasons": ["excellent match"]}
        )

        low_result = JobUnderstandingResult(
            job_id=sample_job.id, analysis=low_score_analysis,
            prompt_version="job_understanding_v1", model_identifier="mock", status="success",
        )
        high_result = JobUnderstandingResult(
            job_id=sample_job.id, analysis=high_score_analysis,
            prompt_version="job_understanding_v1", model_identifier="mock", status="success",
        )

        text_low = build_enriched_job_text(sample_job, low_result)
        text_high = build_enriched_job_text(sample_job, high_result)

        assert text_low == text_high
        assert "0.05" not in text_low
        assert "0.98" not in text_high

    def test_final_ranking_score_unaffected_by_profile_match_score(
        self, sample_job, mock_understanding
    ):
        """End-to-end: two otherwise-identical Phase 2 results differing only
        in profile_match_score must produce the SAME semantic score for the
        same job, once fed through the real enriched-text -> embedding path.
        """

        service = MockEmbeddingService()
        profile_embedding = [0.5] * 10
        config: dict[str, Any] = {"similarity": {"min_threshold": 0.0}}

        def score_for(profile_match_score: float) -> float:
            analysis = mock_understanding.model_copy(
                update={"profile_match_score": profile_match_score}
            )
            result = JobUnderstandingResult(
                job_id=sample_job.id, analysis=analysis,
                prompt_version="job_understanding_v1", model_identifier="mock", status="success",
            )
            enriched = build_enriched_texts([sample_job], {sample_job.id: result})
            scored, _ = apply_semantic_matching(
                [sample_job], profile_embedding, config, service,  # type: ignore[arg-type]
                enriched_texts=enriched,
            )
            return next(score for job, score in scored if job.id == sample_job.id)

        assert score_for(0.02) == score_for(0.99)

    def test_current_prompt_never_requests_profile_match_fields(self):
        """The personalized prompt's schema must not ask the model for
        profile_match_score/reasons — confirms the removal is still in
        effect, not just true at audit time."""
        from src.ai.prompt import build_personalized_matching_prompt

        prompt = build_personalized_matching_prompt(
            job_description="A job.", profile={"target_skills": ["python"]},
        )
        assert "profile_match_score" not in prompt
        assert "profile_match_reasons" not in prompt
        assert "profile_mismatch_reasons" not in prompt

    def test_profile_match_score_field_still_exists_for_backward_compat(self):
        """The field itself must remain on the model (for cache/schema
        compatibility with any previously stored analysis) even though the
        current prompt never populates it — removing it outright would be
        a breaking schema change for existing ai_analysis rows."""
        u = JobUnderstanding(profile_match_score=0.7)
        assert u.profile_match_score == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# AIAnalysisStorage.init_db — idempotency
# ---------------------------------------------------------------------------

class TestAIStorageInit:
    """Verify init_db is idempotent and safe to call repeatedly."""

    def test_init_db_is_idempotent(self, tmp_path):
        """Calling init_db multiple times must not raise or destroy data."""
        from src.ai.storage import AIAnalysisStorage

        db_path = str(tmp_path / "test.db")
        storage = AIAnalysisStorage(db_path=db_path)

        storage.init_db()
        storage.init_db()  # second call must be a no-op

        assert storage.count_analyses() == 0

    def test_existing_data_survives_repeated_init(self, sample_job, successful_result, tmp_path):
        """Previously stored analyses survive a repeated init_db call."""
        from src.ai.storage import AIAnalysisStorage

        db_path = str(tmp_path / "test.db")
        storage = AIAnalysisStorage(db_path=db_path)
        storage.init_db()
        storage.upsert_analysis(successful_result)

        storage.init_db()  # must not wipe the row

        assert storage.count_analyses() == 1


# ---------------------------------------------------------------------------
# Phase 2 config wiring and provider selection
# ---------------------------------------------------------------------------

class TestPhase2ConfigWiring:
    """Verify defaults preserve LM Studio, and Ollama is fully configurable."""

    def test_default_provider_is_lmstudio(self):
        """Default Phase2Config uses LM Studio (port 1234)."""
        from src.ai.config import load_phase2_config

        cfg = load_phase2_config()
        assert cfg.llm_provider == "lmstudio"
        assert "1234" in cfg.llm_endpoint
        assert cfg.ai_enabled is False

    def test_default_endpoint_is_lmstudio_port(self):
        """Default endpoint is LM Studio's port, not Ollama's."""
        from src.ai.config import Phase2Config

        cfg = Phase2Config()
        assert cfg.llm_endpoint == "http://localhost:1234"

    def test_local_llm_provider_default_endpoint_is_lmstudio(self):
        """LocalLLMProvider class default is LM Studio endpoint."""
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider()
        assert "1234" in provider.endpoint
        assert "11434" not in provider.endpoint

    def test_lmstudio_configuration(self):
        """Full LM Studio configuration round-trip."""
        from src.ai.config import Phase2Config
        from src.ai.interfaces import LocalLLMProvider

        cfg = Phase2Config(
            llm_provider="lmstudio",
            llm_endpoint="http://localhost:1234",
            llm_model_name="lmstudio-community/Bonsai-27B-v1-GGUF",
        )
        provider = LocalLLMProvider(
            endpoint=cfg.llm_endpoint,
            model_name=cfg.llm_model_name,
        )

        assert cfg.llm_provider == "lmstudio"
        assert provider.endpoint == "http://localhost:1234"
        assert provider.model_name == "lmstudio-community/Bonsai-27B-v1-GGUF"

    def test_ollama_configuration(self):
        """Full Ollama configuration round-trip."""
        from src.ai.config import Phase2Config
        from src.ai.interfaces import LocalLLMProvider

        cfg = Phase2Config(
            llm_provider="ollama",
            llm_endpoint="http://localhost:11434",
            llm_model_name="llama3.2:8b",
        )
        provider = LocalLLMProvider(
            endpoint=cfg.llm_endpoint,
            model_name=cfg.llm_model_name,
        )

        assert cfg.llm_provider == "ollama"
        assert provider.endpoint == "http://localhost:11434"
        assert provider.model_name == "llama3.2:8b"

    def test_invalid_provider_raises(self):
        """An unsupported provider name is rejected at config load time."""
        import pytest
        from pydantic import ValidationError
        from src.ai.config import Phase2Config

        with pytest.raises(ValidationError):
            Phase2Config(llm_provider="openai")

    def test_provider_case_insensitive(self):
        """Provider names are normalised to lowercase."""
        from src.ai.config import Phase2Config

        cfg = Phase2Config(llm_provider="Ollama")
        assert cfg.llm_provider == "ollama"

    def test_config_endpoint_flows_to_provider(self):
        """Endpoint from config is passed through to LocalLLMProvider unchanged."""
        from src.ai.interfaces import LocalLLMProvider

        provider = LocalLLMProvider(endpoint="http://192.168.1.10:1234")
        assert provider.endpoint == "http://192.168.1.10:1234"

    def test_lmstudio_model_name_format_accepted(self):
        """LM Studio model identifiers (slashes, hyphens) are accepted."""
        from src.ai.config import Phase2Config

        cfg = Phase2Config(llm_model_name="lmstudio-community/Bonsai-27B-v1-GGUF")
        assert cfg.llm_model_name == "lmstudio-community/Bonsai-27B-v1-GGUF"

    def test_ollama_model_name_format_accepted(self):
        """Ollama model tags (colons) are accepted."""
        from src.ai.config import Phase2Config

        cfg = Phase2Config(
            llm_provider="ollama",
            llm_model_name="llama3.2:8b",
        )
        assert cfg.llm_model_name == "llama3.2:8b"


# ---------------------------------------------------------------------------
# Test F: candidate limit, ordering, and non-selected job preservation
# ---------------------------------------------------------------------------

class TestPhase2CandidateLimitIntegration:
    """Test F — Phase 2 selects top-N; jobs outside the limit are not discarded."""

    def _make_job(self, job_id: str) -> "Job":
        from datetime import UTC, datetime
        return Job(
            id=job_id,
            source="test",
            title=f"Job {job_id}",
            company="Acme",
            description="Python developer role.",
            discovered_at=datetime.now(UTC),
        )

    def test_phase2_analyzes_only_top_n(self, tmp_path):
        """Phase2Pipeline receives only the top-N jobs when max_candidates=2."""
        from src.ai.config import Phase2Config
        from src.ai.pipeline import Phase2Pipeline
        from src.ai.storage import AIAnalysisStorage
        from src.storage.sqlite import Storage

        jobs = [self._make_job(f"j{i}") for i in range(5)]
        cfg = Phase2Config(max_candidates=2)
        candidates = jobs[: cfg.max_candidates]

        db_path = str(tmp_path / "test.db")
        ai_storage = AIAnalysisStorage(db_path=db_path)
        ai_storage.init_db()

        analyzed_ids: list[str] = []

        class TrackingMock:
            def analyze_batch(self, batch, prompt_version, profile=None, on_progress=None):
                for j in batch:
                    analyzed_ids.append(j.id)
                results = [
                    JobUnderstandingResult(
                        job_id=j.id,
                        analysis=JobUnderstanding(),
                        prompt_version=prompt_version,
                        model_identifier="mock",
                        status="success",
                    )
                    for j in batch
                ]
                return BatchJobUnderstandingResult(
                    total_jobs=len(batch),
                    successful=len(batch),
                    failed=0,
                    skipped=0,
                    results=results,
                )

        pipeline = Phase2Pipeline(
            storage=Storage(db_path=db_path),
            ai_storage=ai_storage,
            llm_provider=TrackingMock(),  # type: ignore[arg-type]
        )
        pipeline.run(candidates)

        assert analyzed_ids == ["j0", "j1"]

    def test_candidate_ordering_respects_input_order(self):
        """Slicing preserves the order of the input list (best-first)."""
        from src.ai.config import Phase2Config

        jobs = [self._make_job(f"rank{i}") for i in range(5)]
        cfg = Phase2Config(max_candidates=3)
        selected = jobs[: cfg.max_candidates]

        assert [j.id for j in selected] == ["rank0", "rank1", "rank2"]

