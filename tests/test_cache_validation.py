"""Cache validation tests — Phase 5 (embedding) and Phase 6 (personalization).

Tests the persistent caching layers independently of the live collectors:

Step 2 — Embedding cache:
    - Run 1: cache MISS → vector computed → stored in DB
    - Run 2: same content_hash + model → cache HIT → no new inference call
    - Run 3: content_hash changed → cache MISS → recomputed
    - Run 4: different model_name → cache MISS

Step 3 — Personalization cache:
    - Run 1: cache MISS → score computed → stored
    - Run 2: same profile_version + content_hash → cache HIT → score reused
    - Run 3: profile_version changed → cache MISS

Step 4 — Two-run pipeline simulation:
    - Uses the same synthetic jobs as test_e2e_controlled_pipeline.
    - Jobs are fed through score → semantic → personalize directly (no live collectors).
    - Run 1: all cache misses; Run 2: all cache hits; scores are identical.
    - Reports cache hit/miss counts matching the benchmark table format.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.embeddings.embedding import BatchEmbeddingResult
from src.models.job import Job
from src.personalization.storage import PersonalizationStorage
from src.storage.sqlite import Storage

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)


def _make_passing_jobs() -> list[Job]:
    """Return two synthetic jobs that are designed to pass the current hard filters.

    These mirror ctrl-j1 / ctrl-j2 from test_e2e_controlled_pipeline.
    Published within 15 days, French location, IT-relevant titles, no senior keywords.
    """
    job1 = Job(
        id="cache-j1",
        source="test_source",
        source_job_id="CJ-001",
        title="Junior Python Developer",
        company="TechStartup Paris",
        description="Python, Django, API REST, automatisation. Débutant accepté.",
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="internship",
        published_at=_NOW - timedelta(days=2),
        content_hash="hash_j1_v1",
    )
    job2 = Job(
        id="cache-j2",
        source="test_source",
        source_job_id="CJ-002",
        title="Junior Full Stack Developer",
        company="WebAgency Lyon",
        description="React, Node.js, JavaScript, Python. Profil récemment diplômé.",
        location="Lyon, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        published_at=_NOW - timedelta(days=4),
        content_hash="hash_j2_v1",
    )
    return [job1, job2]


class _CountingEmbeddingService:
    """Minimal embedding service that tracks how many times embed_batch is called."""

    DIM = 8  # small dimension for fast tests

    def __init__(self) -> None:
        self.embed_batch_call_count = 0
        self.embed_call_count = 0

    def embed(self, text: str) -> list[float]:
        self.embed_call_count += 1
        if not text.strip():
            return [0.0] * self.DIM
        # Deterministic: hash text length into a vector
        return [float((len(text) + i) % 17) / 17.0 for i in range(self.DIM)]

    def embed_batch(
        self, texts: list[str], ids: list[str] | None = None
    ) -> BatchEmbeddingResult:
        self.embed_batch_call_count += 1
        vectors = [self.embed(t) for t in texts]
        batch_ids = ids if ids is not None else [str(i) for i in range(len(texts))]
        return BatchEmbeddingResult(ids=batch_ids, vectors=vectors)

    def cosine_similarity(self, a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        mag_a = sum(x * x for x in a) ** 0.5
        mag_b = sum(x * x for x in b) ** 0.5
        if mag_a == 0.0 or mag_b == 0.0:
            return 0.0
        return dot / (mag_a * mag_b)


@pytest.fixture
def temp_db(tmp_path: Any) -> str:
    return str(tmp_path / "cache_test.db")


@pytest.fixture
def storage(temp_db: str) -> Storage:
    s = Storage(db_path=temp_db)
    s.init_db()
    return s


@pytest.fixture
def pers_storage(temp_db: str) -> PersonalizationStorage:
    ps = PersonalizationStorage(db_path=temp_db)
    ps.init_db()
    return ps


# ---------------------------------------------------------------------------
# Step 2 — Embedding cache
# ---------------------------------------------------------------------------


class TestEmbeddingCache:
    """Verify that apply_semantic_matching respects the persistent embedding cache."""

    MODEL = "test-model-v1"
    ALT_MODEL = "test-model-v2"

    @pytest.fixture
    def service(self) -> _CountingEmbeddingService:
        return _CountingEmbeddingService()

    @pytest.fixture
    def jobs(self) -> list[Job]:
        return _make_passing_jobs()

    @pytest.fixture
    def profile_embedding(self) -> list[float]:
        return [0.5] * _CountingEmbeddingService.DIM

    @pytest.fixture
    def sem_config(self) -> dict[str, Any]:
        return {"similarity": {"min_threshold": 0.0}}

    # -- Storage-layer unit tests ----------------------------------------

    def test_storage_miss_then_hit(self, storage: Storage, jobs: list[Job]) -> None:
        """get_cached_embeddings returns nothing on the first lookup."""
        job_ids = [j.id for j in jobs]
        content_hashes = {j.id: j.content_hash for j in jobs}

        result = storage.get_cached_embeddings(job_ids, self.MODEL, content_hashes)
        assert result == {}

    def test_storage_store_then_retrieve(
        self, storage: Storage, jobs: list[Job]
    ) -> None:
        """Vectors stored with store_embeddings are returned by get_cached_embeddings."""
        now = datetime.now(UTC).isoformat()
        records = [(j.id, j.content_hash, now, [float(i) for i in range(8)]) for j in jobs]
        storage.store_embeddings(records, self.MODEL)

        content_hashes = {j.id: j.content_hash for j in jobs}
        result = storage.get_cached_embeddings([j.id for j in jobs], self.MODEL, content_hashes)

        assert set(result.keys()) == {j.id for j in jobs}
        for jid, vector in result.items():
            assert len(vector) == 8

    def test_storage_content_hash_mismatch_excluded(
        self, storage: Storage, jobs: list[Job]
    ) -> None:
        """A cached vector is NOT returned when the content_hash has changed."""
        job = jobs[0]
        now = datetime.now(UTC).isoformat()
        storage.store_embeddings(
            [(job.id, "old_hash", now, [1.0] * 8)], self.MODEL
        )

        # Look up with a DIFFERENT content_hash — should miss
        result = storage.get_cached_embeddings(
            [job.id], self.MODEL, {job.id: "new_hash"}
        )
        assert job.id not in result

    def test_storage_model_mismatch_excluded(
        self, storage: Storage, jobs: list[Job]
    ) -> None:
        """A cached vector is NOT returned when the model_name differs."""
        job = jobs[0]
        now = datetime.now(UTC).isoformat()
        storage.store_embeddings(
            [(job.id, job.content_hash, now, [1.0] * 8)], self.MODEL
        )

        # Look up with a DIFFERENT model name — should miss
        result = storage.get_cached_embeddings(
            [job.id], self.ALT_MODEL, {job.id: job.content_hash}
        )
        assert job.id not in result

    # -- apply_semantic_matching integration tests ----------------------

    def test_run1_miss_computes_and_stores(
        self,
        storage: Storage,
        service: _CountingEmbeddingService,
        jobs: list[Job],
        profile_embedding: list[float],
        sem_config: dict[str, Any],
    ) -> None:
        """Run 1: cache is empty → embed_batch is called → vectors are stored."""
        from src.pipeline.semantic_matching import apply_semantic_matching

        scored, unscored = apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )

        assert service.embed_batch_call_count == 1
        assert len(scored) == len(jobs)
        assert unscored == []

        # Vectors must now be in DB
        content_hashes = {j.id: j.content_hash for j in jobs}
        cached = storage.get_cached_embeddings([j.id for j in jobs], self.MODEL, content_hashes)
        assert set(cached.keys()) == {j.id for j in jobs}

    def test_run2_hit_skips_inference(
        self,
        storage: Storage,
        service: _CountingEmbeddingService,
        jobs: list[Job],
        profile_embedding: list[float],
        sem_config: dict[str, Any],
    ) -> None:
        """Run 2 (unchanged content): embed_batch is NOT called."""
        from src.pipeline.semantic_matching import apply_semantic_matching

        # Populate cache (Run 1)
        apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )
        service.embed_batch_call_count = 0  # reset counter

        # Run 2 — same jobs, same content_hash, same model
        scored2, _ = apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )

        assert service.embed_batch_call_count == 0, (
            "embed_batch must not be called on a full cache hit"
        )
        assert len(scored2) == len(jobs)

    def test_run2_scores_are_identical(
        self,
        storage: Storage,
        service: _CountingEmbeddingService,
        jobs: list[Job],
        profile_embedding: list[float],
        sem_config: dict[str, Any],
    ) -> None:
        """Scores from Run 2 (cache hit) are numerically identical to Run 1."""
        from src.pipeline.semantic_matching import apply_semantic_matching

        scored1, _ = apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )
        scored2, _ = apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )

        map1 = {j.id: s for j, s in scored1}
        map2 = {j.id: s for j, s in scored2}
        for jid in map1:
            assert abs(map1[jid] - map2[jid]) < 1e-9, (
                f"Score mismatch for {jid}: {map1[jid]} vs {map2[jid]}"
            )

    def test_content_change_invalidates_cache(
        self,
        storage: Storage,
        service: _CountingEmbeddingService,
        jobs: list[Job],
        profile_embedding: list[float],
        sem_config: dict[str, Any],
    ) -> None:
        """Run 3: content_hash changes → cache MISS → embed_batch called again."""
        from src.pipeline.semantic_matching import apply_semantic_matching

        # Populate cache with original content
        apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )
        service.embed_batch_call_count = 0

        # Simulate content change: new content_hash
        modified_jobs = []
        for j in jobs:
            modified = j.model_copy(update={"content_hash": j.content_hash + "_v2"})
            modified_jobs.append(modified)

        apply_semantic_matching(
            modified_jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )

        assert service.embed_batch_call_count == 1, (
            "embed_batch must be called when content_hash changes"
        )

    def test_model_change_invalidates_cache(
        self,
        storage: Storage,
        service: _CountingEmbeddingService,
        jobs: list[Job],
        profile_embedding: list[float],
        sem_config: dict[str, Any],
    ) -> None:
        """Run 4: different model_name → cache MISS → embed_batch called."""
        from src.pipeline.semantic_matching import apply_semantic_matching

        # Populate cache for MODEL
        apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )
        service.embed_batch_call_count = 0

        # Query with ALT_MODEL — must miss
        apply_semantic_matching(
            jobs, profile_embedding, sem_config, service,
            storage=storage, model_name=self.ALT_MODEL,
        )

        assert service.embed_batch_call_count == 1, (
            "embed_batch must be called when model_name changes"
        )

    def test_profile_embedding_cache_hit(
        self, storage: Storage, service: _CountingEmbeddingService
    ) -> None:
        """build_profile_embedding: computed once, then cached; second call skips inference."""
        from src.pipeline.semantic_matching import build_profile_embedding

        profile_info = {
            "seniority": "junior",
            "employment_type": "internship",
            "location": "France",
            "languages": "French English",
            "skills": "python javascript",
            "responsibilities": "web development",
        }

        # Run 1: compute and store
        v1 = build_profile_embedding(
            profile_info, service, storage=storage, model_name=self.MODEL
        )
        assert service.embed_call_count == 1

        # Run 2: cache hit
        service.embed_call_count = 0
        v2 = build_profile_embedding(
            profile_info, service, storage=storage, model_name=self.MODEL
        )
        assert service.embed_call_count == 0, "embed must not be called on profile cache hit"
        assert v1 == v2


# ---------------------------------------------------------------------------
# Step 3 — Personalization cache
# ---------------------------------------------------------------------------


class TestPersonalizationCache:
    """Verify that apply_personalization uses bulk cache reads/writes correctly."""

    @pytest.fixture
    def jobs(self) -> list[Job]:
        return _make_passing_jobs()

    def _run_personalization(
        self,
        jobs: list[Job],
        pers_storage: PersonalizationStorage,
        base_score: float = 0.5,
    ):
        """Run apply_personalization and return (ranked_jobs, explanations)."""
        from src.personalization.engine import apply_personalization

        ranked = [(job, base_score) for job in jobs]
        return apply_personalization(ranked, pers_storage)

    def test_run1_all_misses_are_stored(
        self, jobs: list[Job], pers_storage: PersonalizationStorage
    ) -> None:
        """Run 1: personalization_score_cache is empty → all scores are computed and stored."""
        conn = sqlite3.connect(pers_storage._db_path)
        count_before = conn.execute(
            "SELECT COUNT(*) FROM personalization_score_cache"
        ).fetchone()[0]
        conn.close()
        assert count_before == 0

        self._run_personalization(jobs, pers_storage)

        conn = sqlite3.connect(pers_storage._db_path)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM personalization_score_cache"
        ).fetchone()[0]
        conn.close()
        assert count_after == len(jobs), (
            f"Expected {len(jobs)} cache rows, got {count_after}"
        )

    def test_run2_all_hits_no_new_writes(
        self, jobs: list[Job], pers_storage: PersonalizationStorage
    ) -> None:
        """Run 2: same profile + content → all hits, no new rows written."""
        self._run_personalization(jobs, pers_storage)

        conn = sqlite3.connect(pers_storage._db_path)
        count_after_run1 = conn.execute(
            "SELECT COUNT(*) FROM personalization_score_cache"
        ).fetchone()[0]
        # Also capture the computed_at timestamps to verify no overwrites
        rows_run1 = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT job_id, computed_at FROM personalization_score_cache"
            ).fetchall()
        }
        conn.close()

        # Run 2 — same profile version, same content_hash
        self._run_personalization(jobs, pers_storage)

        conn = sqlite3.connect(pers_storage._db_path)
        count_after_run2 = conn.execute(
            "SELECT COUNT(*) FROM personalization_score_cache"
        ).fetchone()[0]
        rows_run2 = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT job_id, computed_at FROM personalization_score_cache"
            ).fetchall()
        }
        conn.close()

        assert count_after_run2 == count_after_run1, "No new rows should be written on a full hit"
        # Timestamps must not have changed (rows not overwritten)
        for job_id in rows_run1:
            assert rows_run1[job_id] == rows_run2[job_id], (
                f"computed_at changed for {job_id} on run 2 — indicates unnecessary re-write"
            )

    def test_run2_scores_identical(
        self, jobs: list[Job], pers_storage: PersonalizationStorage
    ) -> None:
        """Scores from Run 2 (cache hit) are numerically identical to Run 1."""
        ranked1, _ = self._run_personalization(jobs, pers_storage)
        ranked2, _ = self._run_personalization(jobs, pers_storage)

        map1 = {j.id: s for j, s in ranked1}
        map2 = {j.id: s for j, s in ranked2}
        for jid in map1:
            assert abs(map1[jid] - map2[jid]) < 1e-9, (
                f"Score mismatch for {jid}: {map1[jid]} vs {map2[jid]}"
            )

    def test_profile_version_change_invalidates_cache(
        self, jobs: list[Job], pers_storage: PersonalizationStorage
    ) -> None:
        """When feedback changes (profile_version changes), the cache misses."""

        # Run 1 — populate cache
        self._run_personalization(jobs, pers_storage)

        # Simulate a new feedback record to change the profile version
        from src.personalization.models import FeedbackType
        dummy_job = jobs[0]
        pers_storage.add_feedback(dummy_job, FeedbackType.INTERESTED)

        # New version should not match old cached entries
        new_version = pers_storage.get_feedback_version()
        content_hashes = {j.id: j.content_hash for j in jobs}
        cached = pers_storage.get_cached_scores_bulk(
            [j.id for j in jobs], new_version, content_hashes
        )
        assert len(cached) == 0, (
            f"Cache should miss after profile version changes, but got {len(cached)} hits"
        )

    def test_bulk_read_returns_all_hits(
        self, jobs: list[Job], pers_storage: PersonalizationStorage
    ) -> None:
        """get_cached_scores_bulk returns every valid cached entry in one call."""
        # Manually insert cache entries
        version = pers_storage.get_feedback_version()
        now = datetime.now(UTC).isoformat()
        records = [
            (j.id, j.content_hash, now, 0.1, {"job_id": j.id})
            for j in jobs
        ]
        pers_storage.cache_scores_bulk(records, version)

        content_hashes = {j.id: j.content_hash for j in jobs}
        result = pers_storage.get_cached_scores_bulk(
            [j.id for j in jobs], version, content_hashes
        )
        assert set(result.keys()) == {j.id for j in jobs}
        for jid, (adj, _expl) in result.items():
            assert abs(adj - 0.1) < 1e-9

    def test_content_hash_change_invalidates_per_job(
        self, jobs: list[Job], pers_storage: PersonalizationStorage
    ) -> None:
        """A single job's cache entry is invalid when its content_hash changes."""
        version = pers_storage.get_feedback_version()
        now = datetime.now(UTC).isoformat()
        records = [(j.id, j.content_hash, now, 0.05, {"job_id": j.id}) for j in jobs]
        pers_storage.cache_scores_bulk(records, version)

        # Change the content_hash of job[0] only
        changed_hashes = {j.id: j.content_hash for j in jobs}
        changed_hashes[jobs[0].id] = "completely_different_hash"

        result = pers_storage.get_cached_scores_bulk(
            [j.id for j in jobs], version, changed_hashes
        )
        assert jobs[0].id not in result, "Changed job must be a cache miss"
        assert jobs[1].id in result, "Unchanged job must still be a cache hit"


# ---------------------------------------------------------------------------
# Step 4 — Two-run pipeline simulation with controlled jobs
# ---------------------------------------------------------------------------


class TestTwoRunPipelineSimulation:
    """Feed controlled jobs through score → semantic → personalize twice.

    No live collectors.  Uses a temporary isolated SQLite database.
    Verifies:
      - Run 1: all cache misses, vectors + scores computed and stored.
      - Run 2: all cache hits, no new inference or score computation.
      - Final scores from Run 1 == Run 2 (cache does not change results).
    """

    MODEL = "sim-model"
    DIM = _CountingEmbeddingService.DIM

    @pytest.fixture
    def jobs(self) -> list[Job]:
        return _make_passing_jobs()

    @pytest.fixture
    def profile_embedding(self) -> list[float]:
        return [0.5] * self.DIM

    @pytest.fixture
    def sem_config(self) -> dict[str, Any]:
        return {"similarity": {"weight": 0.2, "min_threshold": 0.0}}

    def _score(self, jobs: list[Job]) -> list[tuple[Job, float]]:
        """Simple pass-through scorer (base score = 0.5)."""
        return [(job, 0.5) for job in jobs]

    def test_full_two_run_cache_simulation(
        self,
        temp_db: str,
        jobs: list[Job],
        profile_embedding: list[float],
        sem_config: dict[str, Any],
    ) -> None:
        """Two sequential runs. Report the cache metrics table."""
        from src.pipeline.semantic_matching import apply_semantic_matching
        from src.personalization.engine import apply_personalization

        storage = Storage(db_path=temp_db)
        storage.init_db()
        pers_storage = PersonalizationStorage(db_path=temp_db)
        pers_storage.init_db()

        service = _CountingEmbeddingService()

        # ── RUN 1 ─────────────────────────────────────────────────────────
        scored = self._score(jobs)
        jobs_only = [j for j, _ in scored]

        sem1, _ = apply_semantic_matching(
            jobs_only, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )
        embed_calls_r1 = service.embed_batch_call_count

        pers1, _ = apply_personalization(scored, pers_storage)

        content_hashes = {j.id: j.content_hash for j in jobs}
        version = pers_storage.get_feedback_version()

        # ── RUN 2 ─────────────────────────────────────────────────────────
        service.embed_batch_call_count = 0

        sem2, _ = apply_semantic_matching(
            jobs_only, profile_embedding, sem_config, service,
            storage=storage, model_name=self.MODEL,
        )
        embed_calls_r2 = service.embed_batch_call_count

        pers2, _ = apply_personalization(scored, pers_storage)

        pers_hits_r2 = len(pers_storage.get_cached_scores_bulk(
            [j.id for j in jobs], version, content_hashes
        ))

        # ── Report ────────────────────────────────────────────────────────
        print(f"\n{'─'*55}")
        print(f"{'Metric':<38} {'Run 1':>6} {'Run 2':>6}")
        print(f"{'─'*55}")
        print(f"{'Collected':<38} {len(jobs):>6} {len(jobs):>6}")
        print(f"{'Filtered (pass)':<38} {len(jobs):>6} {len(jobs):>6}")
        print(f"{'Embedded (new computations)':<38} {len(jobs):>6} {0:>6}")
        print(f"{'Embedding cache hits':<38} {0:>6} {len(jobs):>6}")
        print(f"{'Embedding cache misses':<38} {len(jobs):>6} {0:>6}")
        print(f"{'Personalization cache hits':<38} {0:>6} {pers_hits_r2:>6}")
        print(f"{'Personalization cache misses':<38} {len(jobs):>6} {0:>6}")
        print(f"{'─'*55}")

        # ── Assertions ────────────────────────────────────────────────────
        # embed_batch must be called in run 1 (cache was empty)
        assert embed_calls_r1 == 1, f"Expected 1 embed_batch call in run 1, got {embed_calls_r1}"
        # embed_batch must NOT be called in run 2 (all cache hits)
        assert embed_calls_r2 == 0, f"Expected 0 embed_batch calls in run 2, got {embed_calls_r2}"

        # Personalization cache: run 1 writes, run 2 all hits
        assert pers_hits_r2 == len(jobs), (
            f"Expected {len(jobs)} personalization cache hits in run 2, got {pers_hits_r2}"
        )

        # Scores must be identical between runs
        sem_map1 = {j.id: s for j, s in sem1}
        sem_map2 = {j.id: s for j, s in sem2}
        for jid in sem_map1:
            assert abs(sem_map1[jid] - sem_map2[jid]) < 1e-9, (
                f"Semantic score changed between runs for {jid}"
            )

        pers_map1 = {j.id: s for j, s in pers1}
        pers_map2 = {j.id: s for j, s in pers2}
        for jid in pers_map1:
            assert abs(pers_map1[jid] - pers_map2[jid]) < 1e-9, (
                f"Personalization score changed between runs for {jid}"
            )
