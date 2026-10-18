"""Phase 3: Local Semantic Matching.

Adds semantic similarity scoring on top of the existing Phase 1 pipeline.
Uses sentence-transformers for local embeddings (no cloud APIs).

Key design decisions:
- Hard filters remain authoritative: semantic matching NEVER makes a rejected job pass.
- Semantic score is additive to the deterministic score in the final ranking.
- Embeddings are cached to avoid unnecessary recomputation.
- Missing/empty data is handled gracefully (returns 0 similarity).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import yaml

from src.embeddings.embedding import (
    EmbeddingService,
)
from src.models.job import Job

if TYPE_CHECKING:
    from src.storage.sqlite import Storage

_PROFILE_CACHE_KEY = "__profile__"

logger = logging.getLogger(__name__)


def load_semantic_config(config_path: str) -> dict[str, Any]:
    """Load semantic matching configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        logger.warning("Semantic config not found at %s, using defaults", config_path)
        return {
            "embedding": {"model_name": "paraphrase-multilingual-MiniLM-L12-v2", "cache_max_entries": 10_000, "device": "auto"},
            "similarity": {"weight": 0.2, "min_threshold": 0.3, "require_hard_filters_first": True},
        }

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise TypeError(f"Expected a mapping in {config_path}")

    return data


def build_profile_embedding(
    profile_info: dict[str, Any],
    embedding_service: EmbeddingService,
    template: str = " {seniority} {employment_type} {location} {languages} {skills} {responsibilities}",
    storage: "Storage | None" = None,
    model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
) -> list[float]:
    """Build an embedding for a candidate's profile.

    When *storage* is provided the vector is cached in the job_embeddings table
    keyed by ``(__profile__, model_name)`` with the profile's content_hash.
    On subsequent calls with an unchanged profile and the same model the cached
    vector is returned without running a model forward pass.

    Args:
        profile_info: Dictionary with seniority, employment_type, location, languages, skills, responsibilities.
        embedding_service: The embedding service to use.
        template: Template string with placeholders to fill from profile_info.
        storage: Optional persistent storage for caching the profile vector.
        model_name: Model identifier used as part of the cache key.

    Returns:
        A list of floats representing the profile embedding vector.
    """
    profile_hash = hashlib.sha256(
        json.dumps(profile_info, sort_keys=True).encode()
    ).hexdigest()[:32]

    if storage is not None:
        cached = storage.get_cached_embeddings(
            [_PROFILE_CACHE_KEY], model_name, {_PROFILE_CACHE_KEY: profile_hash}
        )
        if _PROFILE_CACHE_KEY in cached:
            logger.info("Profile embedding: cache hit")
            return cached[_PROFILE_CACHE_KEY]

    try:
        text = template.format(**profile_info)
    except KeyError as exc:
        logger.warning("Missing profile field %s, using empty profile", exc)
        return embedding_service.embed("")

    if not text.strip():
        logger.warning("Built profile text is empty, returning zero vector")
        return embedding_service.embed("")

    vector = embedding_service.embed(text)

    if storage is not None:
        now = datetime.now(UTC).isoformat()
        storage.store_embeddings(
            [(_PROFILE_CACHE_KEY, profile_hash, now, vector)], model_name
        )
        logger.info("Profile embedding: computed and cached")

    return vector


def apply_semantic_matching(
    jobs: list[Job],
    profile_embedding: list[float],
    config: dict[str, Any],
    embedding_service: EmbeddingService,
    enriched_texts: dict[str, str] | None = None,
    storage: "Storage | None" = None,
    model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
    timing: "dict | None" = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[tuple[Job, float]], list[tuple[Job, float]]]:
    """Apply semantic matching to a list of jobs using batch embedding.

    Collects all job texts first, loads any cached embeddings from persistent
    storage (keyed by job_id + model_name + content_hash), embeds only the
    uncached/changed jobs in one batch call, stores the new vectors back, then
    computes cosine similarities for all jobs.

    Args:
        jobs: Jobs to match (must already pass hard filters).
        profile_embedding: Pre-computed embedding for the candidate profile.
        config: Semantic matching configuration.
        embedding_service: The embedding service to use.
        enriched_texts: Optional dict mapping job_id -> enriched text produced
            by Phase 2 (AI analysis). When present for a job, the enriched text
            is used for embedding instead of the raw title+description, improving
            semantic scoring for jobs whose structure was extracted by the LLM.
        storage: Optional persistent storage for embedding caching.  When
            provided, vectors are loaded from and written to the job_embeddings
            table so they survive across pipeline runs.
        model_name: Name of the embedding model; used as part of the cache key.
        progress_callback: Optional ``(processed, total)`` callback — purely
            observational, never affects any score. The batch embedding call
            itself is a single opaque model inference (no per-item hook
            available there), so this fires around it as one stage-level
            checkpoint, then per-item during the cosine-similarity pass that
            follows, which dominates wall-clock time far less but does have
            a natural per-job loop.

    Returns:
        Tuple of (semantically_scored_jobs, unscored_jobs).
        - semantically_scored_jobs: List of (job, semantic_score) tuples.
        - unscored_jobs: Jobs that were skipped due to missing data.
    """
    if not jobs:
        return [], []

    # ── Build job texts ────────────────────────────────────────────────────────
    texts_by_id: dict[str, str] = {}
    empty_jobs: list[Job] = []

    for job in jobs:
        if enriched_texts and job.id in enriched_texts:
            job_text = enriched_texts[job.id]
        else:
            parts = []
            if job.title:
                t = job.title.lower().strip()
                parts.append(t)
                parts.append(t)  # Title repeated to weight it more than the description.
            if job.skills:
                parts.append(" ".join(job.skills).lower())
            if job.description:
                # Limit description to 500 chars to reduce the influence of generic
                # portal wrapper text (e.g. Talents Handicap boilerplate) that can
                # distort the semantic embedding for jobs with uninformative descriptions.
                parts.append(job.description[:500].lower().strip())
            job_text = " ".join(parts)

        if not job_text.strip():
            logger.debug("Job %s has no text content, skipping semantic scoring", job.id)
            empty_jobs.append(job)
        else:
            texts_by_id[job.id] = job_text

    scored: list[tuple[Job, float]] = []
    unscored: list[tuple[Job, float]] = [(job, 0.0) for job in empty_jobs]

    if not texts_by_id:
        return scored, unscored

    job_by_id = {job.id: job for job in jobs}
    content_hashes = {jid: job_by_id[jid].content_hash for jid in texts_by_id}

    # ── Load cached embeddings from persistent storage ─────────────────────────
    cached_vectors: dict[str, list[float]] = {}
    _t0 = time.perf_counter()
    if storage is not None:
        cached_vectors = storage.get_cached_embeddings(
            list(texts_by_id.keys()), model_name, content_hashes
        )
    if timing is not None:
        timing["embed_cache_lookup"] = time.perf_counter() - _t0

    cache_hits = len(cached_vectors)
    cache_misses = len(texts_by_id) - cache_hits
    logger.info(
        "Embedding cache: %d hits, %d misses (model=%s)",
        cache_hits, cache_misses, model_name,
    )

    # ── Batch-encode only jobs without a valid cached vector ──────────────────
    new_vectors: dict[str, list[float]] = {}
    uncached_ids = [jid for jid in texts_by_id if jid not in cached_vectors]

    _t0 = time.perf_counter()
    if uncached_ids:
        uncached_texts = [texts_by_id[jid] for jid in uncached_ids]
        batch_result = embedding_service.embed_batch(uncached_texts, ids=uncached_ids)
        for jid, vector in zip(batch_result.ids, batch_result.vectors):
            new_vectors[jid] = vector
    if timing is not None:
        timing["embed_batch_compute"] = time.perf_counter() - _t0

        # ── Persist newly computed embeddings ──────────────────────────────────
    _t0 = time.perf_counter()
    if storage is not None and new_vectors:
        now = datetime.now(UTC).isoformat()
        records = [
            (jid, content_hashes[jid], now, new_vectors[jid])
            for jid in new_vectors
        ]
        storage.store_embeddings(records, model_name)
    if timing is not None:
        timing["embed_cache_write"] = time.perf_counter() - _t0

    # ── Merge cached and newly computed vectors, then score ───────────────────
    all_vectors = {**cached_vectors, **new_vectors}
    min_threshold = config.get("similarity", {}).get("min_threshold", 0.3)
    _sem_total = len(texts_by_id)

    if progress_callback is not None:
        # Embedding computation itself is one opaque batch call (no per-item
        # hook available inside it) — report it as a single stage-level
        # checkpoint once it finishes, before the per-item cosine pass below.
        progress_callback(len(all_vectors), _sem_total)

    _t0 = time.perf_counter()
    for i, (jid, vector) in enumerate(all_vectors.items(), start=1):
        job = job_by_id[jid]
        sem_score = embedding_service.cosine_similarity(vector, profile_embedding)
        if sem_score < min_threshold:
            logger.debug(
                "Job %s semantic score %.3f below threshold %.3f",
                jid, sem_score, min_threshold,
            )
        scored.append((job, sem_score))
        if progress_callback is not None:
            progress_callback(i, _sem_total)
    if timing is not None:
        timing["cosine_similarity"] = time.perf_counter() - _t0
        timing["embed_cache_hits"] = cache_hits
        timing["embed_cache_misses"] = cache_misses

    return scored, unscored


def normalize_semantic_score(raw_sem: float, config: dict[str, Any]) -> float:
    """Map a raw cosine-similarity score onto a fixed, cross-run-stable [0, 1] scale.

    Deliberately NOT per-run min-max/percentile normalization: that would let a
    weak collection run (few/mediocre jobs) turn its best job into an
    artificially "excellent" (~1.0) semantic score just by being the best of a
    bad batch. Instead this uses fixed reference anchors
    (``similarity.reference_floor`` / ``similarity.reference_ceiling``)
    calibrated once from a large historical sample and stored in config — see
    the comments in config/semantic_config.yaml for the audit data behind the
    default values. The anchors do not move with the current run's data, so
    a given raw similarity value maps to the same normalized value every run.

    When no anchors are configured (``reference_ceiling`` absent or not above
    ``reference_floor``), this is the identity function — returns ``raw_sem``
    unchanged. This keeps the function a no-op for callers/tests that only
    exercise the plain weighted-blend behavior.
    """
    floor = config.get("similarity", {}).get("reference_floor")
    ceiling = config.get("similarity", {}).get("reference_ceiling")
    if floor is None or ceiling is None:
        return raw_sem
    floor = float(floor)
    ceiling = float(ceiling)
    if ceiling <= floor:
        return raw_sem
    return min(max((raw_sem - floor) / (ceiling - floor), 0.0), 1.0)


_normalize_semantic_score = normalize_semantic_score   # former private name, kept for compatibility


def integrate_semantic_into_ranking(
    jobs_with_det_score: list[tuple[Job, float]],
    semantic_results: list[tuple[Job, float]],
    config: dict[str, Any],
) -> list[tuple[Job, float]]:
    """Integrate semantic scores into the deterministic ranking.

    Final score = det_score * (1 - weight) + sem_norm_score * weight

    ``sem_norm_score`` is the raw cosine-similarity score passed through
    ``normalize_semantic_score`` — a fixed-anchor rescaling, not a per-run
    one, so the semantic contribution means the same thing across pipeline
    runs regardless of how strong or weak the rest of that run's job pool is.

    This preserves Phase 1 deterministic scoring and Phase 2 AI analysis,
    adding Phase 3 semantic matching as an additional, intentionally minor
    (secondary/tie-breaking) factor — see ``similarity.weight`` in
    config/semantic_config.yaml.

    Args:
        jobs_with_det_score: Jobs with their deterministic scores from Phase 1/2.
        semantic_results: Jobs with their semantic scores.
        config: Semantic matching configuration (must contain similarity.weight).

    Returns:
        List of (job, final_score) tuples sorted by score descending.

    Raises:
        ValueError: If similarity.weight is outside [0.0, 1.0].
    """
    weight = float(config.get("similarity", {}).get("weight", 0.2))
    if not (0.0 <= weight <= 1.0):
        raise ValueError(
            f"similarity.weight must be in [0.0, 1.0], got {weight}"
        )
    det_weight = 1.0 - weight

    # Enforce min_threshold: semantic scores below this value contribute 0.0
    # to the final ranking.  A score below the threshold means the job has
    # insufficient semantic overlap with the profile to benefit from it.
    # This prevents generic IT vocabulary from artificially boosting unrelated
    # jobs via the semantic channel.
    # Default matches this module's own canonical default (line ~45) and the
    # other min_threshold read above — previously 0.0 here, which would have
    # silently disabled threshold enforcement if the config key were ever
    # omitted, while the sibling read stayed at 0.3.
    min_threshold = float(config.get("similarity", {}).get("min_threshold", 0.3))

    # Build lookup for semantic scores
    sem_scores: dict[str, float] = {job.id: score for job, score in semantic_results}

    # Compute final combined scores
    final_scored: list[tuple[Job, float]] = []
    for job, det_score in jobs_with_det_score:
        raw_sem = sem_scores.get(job.id, 0.0)
        # Apply threshold: a job below the semantic minimum gets 0 semantic contribution.
        sem_score = raw_sem if raw_sem >= min_threshold else 0.0
        if raw_sem < min_threshold and raw_sem > 0:
            logger.debug(
                "Job %s: semantic score %.3f below threshold %.3f → zeroed for ranking",
                job.id, raw_sem, min_threshold,
            )
        sem_norm = normalize_semantic_score(sem_score, config)
        final_score = det_weight * det_score + weight * sem_norm
        final_scored.append((job, min(max(final_score, 0.0), 1.0)))

    # Sort by final score descending
    final_scored.sort(key=lambda x: x[1], reverse=True)

    return final_scored


class SemanticMatchResult:
    """Container for semantic matching results.

    Attributes:
        jobs_with_sem_scores: Jobs with their computed semantic similarity scores.
        jobs_unscored: Jobs that were skipped due to missing data (score = 0.0).
        total_jobs: Total number of input jobs.
        matched_count: Number of jobs with non-zero semantic scores.
    """

    def __init__(
        self,
        jobs_with_sem_scores: list[tuple[Job, float]],
        jobs_unscored: list[tuple[Job, float]],
    ) -> None:
        self.jobs_with_sem_scores = jobs_with_sem_scores
        self.jobs_unscored = jobs_unscored
        self.total_jobs = len(jobs_with_sem_scores) + len(jobs_unscored)
        self.matched_count = sum(1 for _, s in jobs_with_sem_scores if s > 0.0)

    def summary(self) -> str:
        """Return a human-readable summary of the semantic matching results."""
        scores = [s for _, s in self.jobs_with_sem_scores]
        matched_count = sum(1 for s in scores if s > 0.0)
        return (
            f"Semantic Matching Results:\n"
            f"  Total jobs: {self.total_jobs}\n"
            f"  Matched (score > 0): {matched_count}\n"
            f"  Unscored (missing data): {len(self.jobs_unscored)}\n"
            f"  Min score: {min(scores) if scores else 0:.3f}\n"
            f"  Max score: {max(scores) if scores else 0:.3f}"
        )
