"""End-to-end controlled pipeline test.

Validates the complete pipeline (Collection → Normalization → Validation →
Deduplication → Hard Filtering → Scoring → Semantic Matching →
Personalization → SQLite) using a small, deterministic set of synthetic jobs.

No Internet access required. No real collectors are called. The real
data/jobs.db is never touched — an isolated temporary database is used.

Expected pipeline outcome for the 5 synthetic jobs:
  Collected  : 5
  Validated  : 5  (all have required fields)
  Deduplicated: 4  (Job 5 is a cross-source duplicate of Job 1)
  Filtered   : 2  (Job 3 rejected by title_keywords; Job 4 rejected by seniority)
  Scored     : 2
  Semantic   : 2
  Personalized: 2  (no stored feedback → no adjustment applied)
  Stored     : 2
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from tests.fixtures.profiles import TEST_PROFILE_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_jobs() -> list[Job]:
    """Return the 5 synthetic test jobs."""

    # Job 1 — Très pertinent (should pass every stage)
    job1 = Job(
        id="ctrl-j1",
        source="test_source_1",
        source_job_id="TS1-001",
        title="Junior Python Developer",
        company="TechStartup Paris",
        description=(
            "Stage/alternance en développement Python. "
            "Missions : scripts d'automatisation, outils de monitoring réseau, "
            "participation à des projets de cybersécurité. "
            "Débutant accepté, bonne maîtrise de Python requise."
        ),
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="internship",
        published_at=_NOW - timedelta(days=2),
    )

    # Job 2 — Pertinent mais moins parfait (should also pass)
    job2 = Job(
        id="ctrl-j2",
        source="test_source_1",
        source_job_id="TS1-002",
        title="Junior Full Stack Developer",
        company="WebAgency Lyon",
        description=(
            "Poste junior de développeur full stack. "
            "Stack : React, Node.js, JavaScript, quelques notions de Python. "
            "Idéal pour profil récemment diplômé, quelques projets personnels suffisent."
        ),
        location="Lyon, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        published_at=_NOW - timedelta(days=4),
    )

    # Job 3 — Hors profil (wrong domain → rejected by title_keywords filter)
    job3 = Job(
        id="ctrl-j3",
        source="test_source_1",
        source_job_id="TS1-003",
        title="Comptable Junior",
        company="Finance SA",
        description=(
            "Poste de comptable junior dans un cabinet comptable. "
            "Gestion de la TVA, bilans, relations clients PME. "
            "Formation comptabilité exigée."
        ),
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        published_at=_NOW - timedelta(days=1),
    )

    # Job 4 — Rejected by hard constraint (no junior keyword → seniority filter)
    job4 = Job(
        id="ctrl-j4",
        source="test_source_1",
        source_job_id="TS1-004",
        title="Senior Software Engineer",
        company="BigTech France",
        description=(
            "Senior engineering position. 5+ years required. "
            "Deep expertise in distributed systems, cloud architecture, mentoring junior staff."
        ),
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        published_at=_NOW - timedelta(days=3),
    )

    # Job 5 — Mirror posting of Job 1 on a second job board.
    # Must be deduplicated by cross-source content identity.
    # Matches job1 on all four identity fields: company, title, location,
    # employment_type.  Only source and source_job_id differ.
    job5 = Job(
        id="ctrl-j5",
        source="test_source_2",
        source_job_id="TS2-099",
        title="Junior Python Developer",      # identical to job1
        company="TechStartup Paris",          # identical to job1
        description=(
            "Offre miroir publiée sur un second job board. "
            "Même poste que ctrl-j1 — doit être dédupliqué par le pipeline."
        ),
        location="Paris, France",             # identical to job1 (genuine mirror)
        countries=["France"],
        remote=True,
        employment_type="internship",         # identical to job1
        published_at=_NOW - timedelta(days=2),
    )

    return [job1, job2, job3, job4, job5]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db(tmp_path):
    """Isolated SQLite database — never data/jobs.db."""
    return str(tmp_path / "test_pipeline.db")


@pytest.fixture
def synthetic_jobs() -> list[Job]:
    return _make_jobs()


# ---------------------------------------------------------------------------
# Stage-by-stage unit assertions
# ---------------------------------------------------------------------------

class TestValidJobTraversesPipeline:
    """Job 1 should reach every stage and end up in SQLite."""

    def test_job1_passes_validation(self, synthetic_jobs):
        from src.pipeline.validate import validate_jobs

        valid, errors = validate_jobs([synthetic_jobs[0]])
        assert len(valid) == 1
        assert errors == []

    def test_job1_passes_filter(self, synthetic_jobs):
        from src.pipeline.filter import filter_jobs, load_filter_config

        cfg = load_filter_config(TEST_PROFILE_PATH, "config/filters.yaml")
        passed, rejected = filter_jobs([synthetic_jobs[0]], cfg)
        assert synthetic_jobs[0].id in [j.id for j in passed], (
            f"Job 1 should pass the filter but was rejected: {rejected}"
        )

    def test_job1_receives_score(self, synthetic_jobs):
        from src.pipeline.scoring import load_scoring_config, score_jobs

        cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs([synthetic_jobs[0]], cfg)
        assert len(scored) == 1
        _job, score = scored[0]
        assert 0.0 <= score <= 1.0, f"Score out of range: {score}"

    def test_job1_stored_in_sqlite(self, synthetic_jobs, temp_db):
        from src.storage.sqlite import Storage

        storage = Storage(db_path=temp_db)
        storage.init_db()
        storage.upsert_job(synthetic_jobs[0])
        assert storage.count_jobs() == 1
        storage.close()


class TestDuplicateRemoval:
    """Job 5 is a cross-source duplicate of Job 1 and must be removed."""

    def test_cross_source_dedup_removes_job5(self, synthetic_jobs):
        from src.pipeline.deduplicate import deduplicate_jobs

        # Validate first (dedup operates on valid jobs)
        from src.pipeline.validate import validate_jobs

        valid, _ = validate_jobs([synthetic_jobs[0], synthetic_jobs[4]])
        deduped, removed = deduplicate_jobs(valid)

        assert removed == 1, f"Expected 1 duplicate removed, got {removed}"
        assert len(deduped) == 1

    def test_full_dedup_count(self, synthetic_jobs):
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.validate import validate_jobs

        valid, _ = validate_jobs(synthetic_jobs)
        deduped, removed = deduplicate_jobs(valid)

        assert removed == 1, f"Expected exactly 1 duplicate, got {removed}"
        assert len(deduped) == 4


class TestOffProfileFiltering:
    """Job 3 (Comptable Junior) must be rejected by the title_keywords filter."""

    def test_job3_rejected_by_title_keywords(self, synthetic_jobs):
        from src.pipeline.filter import filter_jobs, load_filter_config

        cfg = load_filter_config(TEST_PROFILE_PATH, "config/filters.yaml")
        _, rejected = filter_jobs([synthetic_jobs[2]], cfg)

        assert synthetic_jobs[2].id in rejected, (
            "Job 3 (Comptable Junior) should be rejected but was not"
        )
        assert rejected[synthetic_jobs[2].id] == "title_keywords"


class TestConstrainedJobFiltering:
    """Job 4 (Senior Software Engineer) must be rejected by the seniority filter."""

    def test_job4_rejected_by_seniority(self, synthetic_jobs):
        from src.pipeline.filter import filter_jobs, load_filter_config

        cfg = load_filter_config(TEST_PROFILE_PATH, "config/filters.yaml")
        _, rejected = filter_jobs([synthetic_jobs[3]], cfg)

        assert synthetic_jobs[3].id in rejected, (
            "Job 4 (Senior Software Engineer) should be rejected but was not"
        )
        assert rejected[synthetic_jobs[3].id] == "seniority"


class TestScoring:
    """Jobs 1 and 2 should receive valid scores after passing all filters."""

    def test_scored_jobs_have_valid_range(self, synthetic_jobs):
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs

        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(synthetic_jobs[:4], flt_cfg)  # exclude dup for clarity

        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        assert len(scored) >= 1
        for _job, score in scored:
            assert 0.0 <= score <= 1.0


class TestSemanticMatching:
    """Semantic matching should run on the filtered+scored jobs using all-MiniLM-L6-v2."""

    def test_semantic_matching_runs(self, synthetic_jobs):
        from src.embeddings.embedding import EmbeddingService
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.semantic_matching import (
            apply_semantic_matching,
            build_profile_embedding,
            integrate_semantic_into_ranking,
            load_semantic_config,
        )

        # Filter + score first
        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(synthetic_jobs, flt_cfg)

        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        assert len(scored) > 0, "No jobs survived filtering; cannot test semantic matching"

        # Build semantic components
        sem_cfg = load_semantic_config("config/semantic_config.yaml")
        svc = EmbeddingService(
            model_name=sem_cfg["embedding"]["model_name"],
            cache_max_entries=sem_cfg["embedding"]["cache_max_entries"],
            device=sem_cfg["embedding"].get("device", "auto"),
        )

        profile_info = {
            "seniority": "junior",
            "employment_type": "internship",
            "location": "France",
            "languages": ["French", "English"],
            "skills": ["Python", "automation", "networking", "cybersecurity"],
            "responsibilities": "development, scripting, automation, networking",
        }
        profile_emb = build_profile_embedding(profile_info, svc)
        assert len(profile_emb) > 0

        jobs_only = [job for job, _ in scored]
        semantic_results, _ = apply_semantic_matching(jobs_only, profile_emb, sem_cfg, svc)
        assert len(semantic_results) > 0

        final_ranked = integrate_semantic_into_ranking(scored, semantic_results, sem_cfg)
        assert len(final_ranked) == len(scored)
        for _job, final_score in final_ranked:
            assert isinstance(final_score, float)

    def test_semantic_scores_are_floats(self, synthetic_jobs):
        """Semantic scores must be numeric and non-negative."""
        from src.embeddings.embedding import EmbeddingService
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.semantic_matching import (
            apply_semantic_matching,
            build_profile_embedding,
            load_semantic_config,
        )

        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(synthetic_jobs, flt_cfg)
        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        sem_cfg = load_semantic_config("config/semantic_config.yaml")
        svc = EmbeddingService(
            model_name=sem_cfg["embedding"]["model_name"],
            cache_max_entries=sem_cfg["embedding"]["cache_max_entries"],
            device=sem_cfg["embedding"].get("device", "auto"),
        )

        profile_info = {
            "seniority": "junior",
            "employment_type": "internship",
            "location": "France",
            "languages": ["French", "English"],
            "skills": ["Python"],
            "responsibilities": "development",
        }
        profile_emb = build_profile_embedding(profile_info, svc)
        jobs_only = [job for job, _ in scored]
        semantic_results, _ = apply_semantic_matching(jobs_only, profile_emb, sem_cfg, svc)

        for _job_id, sim_score in semantic_results:
            assert isinstance(sim_score, float)
            assert sim_score >= 0.0


class TestPersonalizationWithNoFeedback:
    """Personalization must not crash when the feedback table is empty."""

    def test_no_crash_with_zero_feedback(self, synthetic_jobs, temp_db):
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import apply_personalization
        from src.personalization.storage import PersonalizationStorage
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs

        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(synthetic_jobs, flt_cfg)

        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        p_storage = PersonalizationStorage(db_path=temp_db)
        p_storage.init_db()

        assert p_storage.count_feedback() == 0

        p_cfg = load_personalization_config("config/personalization.yaml")
        personalized, explanations = apply_personalization(scored, p_storage, p_cfg)

        # Must not raise; output length must match input
        assert len(personalized) == len(scored)
        p_storage.close()

    def test_scores_unchanged_with_no_feedback(self, synthetic_jobs, temp_db):
        """With zero feedback, personalization should not change base scores."""
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import apply_personalization
        from src.personalization.storage import PersonalizationStorage
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs

        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(synthetic_jobs, flt_cfg)
        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        p_storage = PersonalizationStorage(db_path=temp_db)
        p_storage.init_db()
        p_cfg = load_personalization_config("config/personalization.yaml")

        personalized, _ = apply_personalization(scored, p_storage, p_cfg)
        p_storage.close()

        base_scores = {job.id: s for job, s in scored}
        for job, final_score in personalized:
            # min_feedback_count=1 → no adjustment applied with 0 feedback
            assert final_score == pytest.approx(base_scores[job.id], abs=1e-6), (
                f"Score for {job.id} changed unexpectedly: {base_scores[job.id]} → {final_score}"
            )


class TestSQLiteStorage:
    """Final results must be correctly persisted in the isolated test database."""

    def test_correct_jobs_stored(self, synthetic_jobs, temp_db):
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.validate import validate_jobs
        from src.storage.sqlite import Storage

        valid, _ = validate_jobs(synthetic_jobs)
        deduped, _ = deduplicate_jobs(valid)
        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(deduped, flt_cfg)
        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        storage = Storage(db_path=temp_db)
        storage.init_db()
        for job, _ in scored:
            storage.upsert_job(job)

        assert storage.count_jobs() == len(scored)

        # Both Job 1 and Job 2 should be present; Jobs 3, 4, 5 must not be
        stored_ids = {j.id for j in storage.get_jobs(limit=100)}
        assert "ctrl-j1" in stored_ids
        assert "ctrl-j2" in stored_ids
        assert "ctrl-j3" not in stored_ids
        assert "ctrl-j4" not in stored_ids

        storage.close()

    def test_real_db_not_modified(self, synthetic_jobs, temp_db):
        """Running the test pipeline must not write anything to data/jobs.db.

        The real database is never opened (opening it with ``Storage.init_db`` would itself touch
        the user's data): only its size and modification time are compared.
        """
        real_db = Path(__file__).resolve().parents[1] / "data" / "jobs.db"

        def _stat() -> tuple[int, int] | None:
            return (real_db.stat().st_size, real_db.stat().st_mtime_ns) if real_db.exists() else None

        stat_before = _stat()

        # Run the full pipeline against temp_db
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.validate import validate_jobs
        from src.storage.sqlite import Storage

        valid, _ = validate_jobs(synthetic_jobs)
        deduped, _ = deduplicate_jobs(valid)
        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(deduped, flt_cfg)
        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)

        storage = Storage(db_path=temp_db)
        storage.init_db()
        for job, _ in scored:
            storage.upsert_job(job)
        storage.close()

        # Verify real DB unchanged (stat only, never opened)
        assert _stat() == stat_before, "Real DB changed during the test pipeline"


# ---------------------------------------------------------------------------
# Full end-to-end test with detailed stage-by-stage reporting
# ---------------------------------------------------------------------------

class TestFullE2EPipeline:
    """Complete end-to-end run with stage-by-stage assertions and console report."""

    def test_full_pipeline_e2e(self, synthetic_jobs, temp_db, capsys):  # noqa: PLR0914
        from src.embeddings.embedding import EmbeddingService
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import apply_personalization
        from src.personalization.storage import PersonalizationStorage
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.normalize import normalize_jobs
        from src.pipeline.scoring import load_scoring_config, score_jobs
        from src.pipeline.semantic_matching import (
            apply_semantic_matching,
            build_profile_embedding,
            integrate_semantic_into_ranking,
            load_semantic_config,
        )
        from src.pipeline.validate import validate_jobs
        from src.storage.sqlite import Storage

        jobs = synthetic_jobs

        # ── COLLECT (simulated) ──────────────────────────────────────────────
        # Jobs already built as Job objects; normalize_jobs passes them through.
        collected = normalize_jobs("test_controlled", jobs)
        print(f"\nCollected  : {len(collected)}")
        assert len(collected) == 5

        # ── VALIDATE ────────────────────────────────────────────────────────
        valid, val_errors = validate_jobs(collected)
        print(f"Validated  : {len(valid)}  (errors: {len(val_errors)})")
        assert len(valid) == 5, f"Expected 5 valid, got {len(valid)}. Errors: {val_errors}"

        # ── DEDUPLICATE ──────────────────────────────────────────────────────
        deduped, dupes_removed = deduplicate_jobs(valid)
        print(f"Deduped    : {len(deduped)}  (removed: {dupes_removed})")
        assert dupes_removed == 1, f"Expected 1 duplicate removed, got {dupes_removed}"
        assert len(deduped) == 4

        # ── FILTER ───────────────────────────────────────────────────────────
        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, rejected = filter_jobs(deduped, flt_cfg)
        print(f"Filtered   : {len(filtered)}  (rejected: {len(rejected)})")
        for jid, reason in rejected.items():
            print(f"  ✗ {jid}  → {reason}")

        assert len(filtered) == 2, (
            f"Expected 2 jobs after filtering, got {len(filtered)}. "
            f"Rejected: {rejected}"
        )
        assert "ctrl-j3" in rejected and rejected["ctrl-j3"] == "title_keywords"
        assert "ctrl-j4" in rejected and rejected["ctrl-j4"] == "seniority"

        # ── SCORE ────────────────────────────────────────────────────────────
        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs(filtered, sc_cfg)
        print(f"Scored     : {len(scored)}")
        for job, score in scored:
            print(f"  {score:.3f}  {job.title}  —  {job.company}")

        assert len(scored) == 2
        for _job, score in scored:
            assert 0.0 <= score <= 1.0

        # ── SEMANTIC MATCHING ────────────────────────────────────────────────
        sem_cfg = load_semantic_config("config/semantic_config.yaml")
        svc = EmbeddingService(
            model_name=sem_cfg["embedding"]["model_name"],
            cache_max_entries=sem_cfg["embedding"]["cache_max_entries"],
            device=sem_cfg["embedding"].get("device", "auto"),
        )

        profile_info = {
            "seniority": "junior",
            "employment_type": "internship",
            "location": "France",
            "languages": ["French", "English"],
            "skills": ["Python", "automation", "networking", "cybersecurity"],
            "responsibilities": "development, scripting, automation, networking",
        }
        profile_emb = build_profile_embedding(profile_info, svc)

        jobs_only = [job for job, _ in scored]
        semantic_results, below_threshold = apply_semantic_matching(
            jobs_only, profile_emb, sem_cfg, svc
        )
        final_ranked = integrate_semantic_into_ranking(scored, semantic_results, sem_cfg)

        print(f"Semantic   : {len(semantic_results)} matched  ({below_threshold} below threshold)")
        for job, fscore in final_ranked:
            print(f"  {fscore:.3f}  {job.title}  —  {job.company}")

        assert len(final_ranked) == len(scored)

        # ── PERSONALIZATION ──────────────────────────────────────────────────
        p_storage = PersonalizationStorage(db_path=temp_db)
        p_storage.init_db()
        p_cfg = load_personalization_config("config/personalization.yaml")

        personalized, explanations = apply_personalization(final_ranked, p_storage, p_cfg)
        print(f"Personalized: {len(personalized)}  (feedback events: {p_storage.count_feedback()})")
        p_storage.close()

        assert len(personalized) == len(final_ranked)

        # ── STORE ────────────────────────────────────────────────────────────
        storage = Storage(db_path=temp_db)
        storage.init_db()
        for job, _ in personalized:
            storage.upsert_job(job)
        stored = storage.count_jobs()
        print(f"Stored     : {stored}")

        assert stored == 2

        # ── VERIFY CONTENTS ──────────────────────────────────────────────────
        retrieved = storage.get_jobs(limit=10)
        stored_ids = {j.id for j in retrieved}
        assert "ctrl-j1" in stored_ids, "Job 1 missing from DB"
        assert "ctrl-j2" in stored_ids, "Job 2 missing from DB"

        storage.close()

        print("\n✓ Full end-to-end controlled pipeline test passed.")
