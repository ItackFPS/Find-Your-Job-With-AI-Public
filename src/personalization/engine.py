"""Phase 4: deterministic feedback-weighted personalization engine.

Design decisions (mirroring the Phase 3 semantic-matching module):
- No ML model is trained. Preferences are learned with simple, explainable
  weighted averages over past feedback -- deterministic and inspectable.
- Personalization is a bounded *adjustment* added to the existing
  deterministic + semantic score. It can never resurrect a job that failed
  a hard filter, because it only ever operates on the ranked list handed
  to it (which has already been through Phase 1 hard filters).
- Every adjustment ships with a `PersonalizationExplanation` so the final
  ranking stays explainable.
- A preference profile is rebuilt only when the stored feedback actually
  changes (`PersonalizationStorage.get_feedback_version`), and a per-job
  score cache (keyed by that version + the job's own content hash) means
  unchanged jobs are never rescored on repeated runs.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)

from src.models.job import Job
from src.personalization.config import (
    DEFAULT_PERSONALIZATION_CONFIG,
    load_personalization_config,
)
from src.personalization.models import (
    Feedback,
    FeedbackType,
    PersonalizationExplanation,
    SignalMatch,
)
from src.personalization.storage import PersonalizationStorage

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "at", "with",
    "on", "is", "are", "&",
}

_TOKEN_RE = re.compile(r"[a-zA-Z0-9+#.]+")


def _title_keywords(title: str | None) -> list[str]:
    """Tokenize a job title into lowercase keywords, dropping stopwords/noise."""
    if not title:
        return []
    tokens = _TOKEN_RE.findall(title.lower())
    return [t for t in tokens if len(t) > 2 and t not in _STOPWORDS]


def _content_hash_for(job: Job) -> str:
    """Return a stable content hash for a job, falling back if none is stored."""
    if job.content_hash:
        return job.content_hash
    basis = f"{job.title or ''}|{job.description or ''}|{job.company or ''}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


@dataclass
class _SignalStats:
    """Accumulated feedback weight for a single preference signal value."""

    total_weight: float = 0.0
    count: int = 0

    @property
    def average(self) -> float:
        return self.total_weight / self.count if self.count else 0.0


@dataclass
class PreferenceProfile:
    """Aggregated, deterministic preference signals derived from feedback history.

    Each bucket maps a lowercase signal value (company name, skill, title
    keyword) to the average feedback weight observed for it. This is a
    simple weighted-average model, not a trained ML model, by design.
    """

    version: str
    feedback_count: int
    company: dict[str, _SignalStats] = field(default_factory=dict)
    skill: dict[str, _SignalStats] = field(default_factory=dict)
    title_keyword: dict[str, _SignalStats] = field(default_factory=dict)
    remote: _SignalStats = field(default_factory=_SignalStats)
    # job_id -> most recent terminal feedback type, used to optionally
    # exclude already-actioned jobs from being shown again.
    actioned_job_ids: dict[str, FeedbackType] = field(default_factory=dict)


def _accumulate(bucket: dict[str, _SignalStats], key: str, weight: float) -> None:
    stats = bucket.setdefault(key, _SignalStats())
    stats.total_weight += weight
    stats.count += 1


_TERMINAL_FEEDBACK = (FeedbackType.NOT_INTERESTED, FeedbackType.REJECTED, FeedbackType.APPLIED)


def _recency_decay_factor(
    created_at: datetime | None, now: datetime, config: dict[str, Any]
) -> float:
    """Return a deterministic ``[recency_min_factor, 1.0]`` multiplier for
    one feedback event's age, relative to ``now``.

    Phase NEXT / I9: a standard exponential half-life — the multiplier is
    exactly 0.5 at ``recency_half_life_days`` old, 0.25 at twice that, and so
    on, floored at ``recency_min_factor`` so very old feedback never decays
    all the way to zero influence. Deterministic and explainable: the same
    ``(created_at, now)`` pair always yields the same factor, with no
    randomness or hidden state — see config/personalization.yaml for why the
    default constants were chosen.

    Never returns a value that penalizes ambiguous input: a missing
    timestamp, a naive (timezone-less) datetime, a future timestamp (clock
    skew), or any arithmetic failure all fall back to "no decay" (1.0) —
    this only ever discounts feedback with a genuinely known, parseable,
    past timestamp. This intentionally never treats an unknown age as
    evidence the feedback is stale.
    """
    p_cfg = config.get("personalization", DEFAULT_PERSONALIZATION_CONFIG["personalization"])
    half_life_days = float(
        p_cfg.get(
            "recency_half_life_days",
            DEFAULT_PERSONALIZATION_CONFIG["personalization"]["recency_half_life_days"],
        )
    )
    min_factor = float(
        p_cfg.get(
            "recency_min_factor",
            DEFAULT_PERSONALIZATION_CONFIG["personalization"]["recency_min_factor"],
        )
    )
    if half_life_days <= 0 or created_at is None:
        return 1.0

    try:
        reference = created_at if created_at.tzinfo is not None else created_at.replace(tzinfo=UTC)
        age_days = (now - reference).total_seconds() / 86400.0
    except (TypeError, ValueError, OverflowError):
        return 1.0

    if age_days <= 0:
        return 1.0  # same-instant or future (clock skew) -> full weight, never boosted above 1.0

    factor = 0.5 ** (age_days / half_life_days)
    weight: float = max(min_factor, min(1.0, factor))
    return weight


def build_preference_profile(
    feedback_records: list[Feedback],
    config: dict[str, Any],
    version: str,
    now: datetime | None = None,
) -> PreferenceProfile:
    """Build a deterministic preference profile from stored feedback.

    Args:
        feedback_records: All feedback events to learn from (any order).
        config: Personalization configuration (uses ``feedback_weights`` and
            ``personalization.recency_half_life_days``/``recency_min_factor``).
        version: Fingerprint identifying this exact feedback set, used for
            downstream caching.
        now: Reference instant for recency decay. Defaults to the current
            time; callers that need reproducible output across repeated
            calls (tests, or a single pipeline run reusing one instant for
            every decay calculation) should pass an explicit value.
    """
    weights_cfg = config.get("feedback_weights", DEFAULT_PERSONALIZATION_CONFIG["feedback_weights"])
    reference_now = now if now is not None else datetime.now(UTC)
    profile = PreferenceProfile(version=version, feedback_count=len(feedback_records))

    for fb in feedback_records:
        base_weight = float(weights_cfg.get(fb.feedback_type.value, 0.0))
        weight = base_weight * _recency_decay_factor(fb.created_at, reference_now, config)

        if fb.company:
            _accumulate(profile.company, fb.company.strip().lower(), weight)

        for skill in fb.skills:
            if skill:
                _accumulate(profile.skill, skill.strip().lower(), weight)

        for kw in _title_keywords(fb.title):
            _accumulate(profile.title_keyword, kw, weight)

        if fb.remote:
            profile.remote.total_weight += weight
            profile.remote.count += 1

        if fb.feedback_type in _TERMINAL_FEEDBACK:
            profile.actioned_job_ids[fb.job_id] = fb.feedback_type

    return profile


def score_job_personalization(
    job: Job,
    profile: PreferenceProfile,
    config: dict[str, Any],
) -> tuple[float, PersonalizationExplanation]:
    """Compute a bounded personalization adjustment for a single job.

    Returns:
        A ``(clamped_adjustment, explanation)`` tuple. ``clamped_adjustment``
        is always within ``[-max_abs_adjustment, +max_abs_adjustment]`` from
        the configured ``personalization`` section, so it can never dominate
        the deterministic + semantic base score -- and it never causes a job
        to be added to or removed from the ranked list on its own.
    """
    feature_weights = config.get("feature_weights", DEFAULT_PERSONALIZATION_CONFIG["feature_weights"])
    p_cfg = config.get("personalization", DEFAULT_PERSONALIZATION_CONFIG["personalization"])
    min_feedback = int(p_cfg.get("min_feedback_count", 1))
    max_abs = float(p_cfg.get("max_abs_adjustment", 0.3))
    exclude_actioned = bool(p_cfg.get("exclude_actioned_jobs", True))

    if exclude_actioned and job.id in profile.actioned_job_ids:
        reason = f"already marked '{profile.actioned_job_ids[job.id].value}'"
        return 0.0, PersonalizationExplanation(
            job_id=job.id,
            feedback_considered=profile.feedback_count,
            excluded_reason=reason,
        )

    if profile.feedback_count < min_feedback:
        return 0.0, PersonalizationExplanation(
            job_id=job.id,
            feedback_considered=profile.feedback_count,
            cold_start=True,
        )

    matches: list[SignalMatch] = []
    total_contribution = 0.0

    if job.company:
        key = job.company.strip().lower()
        stats = profile.company.get(key)
        if stats and stats.count > 0:
            contribution = stats.average * float(feature_weights.get("company", 1.0))
            total_contribution += contribution
            matches.append(
                SignalMatch(
                    signal_type="company",
                    value=job.company,
                    feedback_count=stats.count,
                    average_weight=stats.average,
                    contribution=contribution,
                )
            )

    skill_contributions: list[float] = []
    for skill in job.skills:
        stats = profile.skill.get(skill.strip().lower())
        if stats and stats.count > 0:
            contribution = stats.average * float(feature_weights.get("skills", 1.0))
            skill_contributions.append(contribution)
            matches.append(
                SignalMatch(
                    signal_type="skill",
                    value=skill,
                    feedback_count=stats.count,
                    average_weight=stats.average,
                    contribution=contribution,
                )
            )
    if skill_contributions:
        total_contribution += sum(skill_contributions) / len(skill_contributions)

    keyword_contributions: list[float] = []
    for kw in _title_keywords(job.title):
        stats = profile.title_keyword.get(kw)
        if stats and stats.count > 0:
            contribution = stats.average * float(feature_weights.get("title_keywords", 1.0))
            keyword_contributions.append(contribution)
            matches.append(
                SignalMatch(
                    signal_type="title_keyword",
                    value=kw,
                    feedback_count=stats.count,
                    average_weight=stats.average,
                    contribution=contribution,
                )
            )
    if keyword_contributions:
        total_contribution += sum(keyword_contributions) / len(keyword_contributions)

    if job.remote and profile.remote.count > 0:
        contribution = profile.remote.average * float(feature_weights.get("remote", 1.0))
        total_contribution += contribution
        matches.append(
            SignalMatch(
                signal_type="remote",
                value="remote",
                feedback_count=profile.remote.count,
                average_weight=profile.remote.average,
                contribution=contribution,
            )
        )

    # Normalize by the *total configured* feature weight (not just the
    # matched subset) so that a single strong signal (e.g. company alone)
    # does not saturate the clamp as easily as several signals combined.
    # This keeps per-signal contributions comparable across jobs that match
    # a different number/mix of dimensions, which matters for explainable,
    # relative ranking between jobs.
    normalizer = sum(float(w) for w in feature_weights.values()) or 1.0
    raw = total_contribution / normalizer

    clamped = max(-max_abs, min(max_abs, raw))

    explanation = PersonalizationExplanation(
        job_id=job.id,
        raw_adjustment=raw,
        clamped_adjustment=clamped,
        matches=matches,
        feedback_considered=profile.feedback_count,
    )
    return clamped, explanation


def apply_personalization(
    ranked_jobs: list[tuple[Job, float]],
    storage: PersonalizationStorage,
    config: dict[str, Any] | None = None,
    timing: "dict | None" = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[tuple[Job, float]], dict[str, PersonalizationExplanation]]:
    """Apply Phase 4 personalization on top of an already hard-filtered ranking.

    ``ranked_jobs`` is expected to already be the output of Phase 1 hard
    filters + Phase 2/3 scoring -- this function only re-weights and possibly
    drops entries already present in that list. It never adds a job that
    is not already present, so hard filters remain authoritative.

    Args:
        ranked_jobs: ``(job, score)`` pairs from the deterministic +
            semantic ranking stage (Phase 1-3 output).
        storage: Feedback/score-cache storage.
        config: Optional pre-loaded personalization config. If omitted,
            loads from ``config/personalization.yaml``.
        progress_callback: Optional ``(processed, total)`` callback invoked
            after each job — purely observational, never affects any
            adjustment or ranking decision.

    Returns:
        ``(final_ranked_jobs, explanations)`` where ``explanations`` maps
        job id to the ``PersonalizationExplanation`` that justifies its
        adjustment (including excluded jobs, for auditability).
    """
    cfg = config if config is not None else load_personalization_config("config/personalization.yaml")

    # Captured once and reused for both the recency-decay reference instant
    # below and the cache-write timestamp further down, so this whole call
    # has exactly one "now" — never two clock reads a few milliseconds apart.
    now_dt = datetime.now(UTC)

    _t0 = time.perf_counter()
    feedback_records = storage.get_all_feedback()
    version = storage.get_feedback_version()
    # Phase NEXT / I9: recency-decayed weights depend on `now_dt`, but the
    # score cache below is keyed only on (job_id, feedback version, content
    # hash) — it does NOT know time has passed. A cached score can therefore
    # be up to a few days stale relative to the true continuously-decaying
    # value. Accepted deliberately: the default 90-day half-life changes
    # slowly enough that this is a negligible drift, not a correctness bug,
    # and a cache keyed on decay-sensitive time would defeat the whole
    # point of caching (every job would miss on every run). If half-life is
    # ever configured much shorter, revisit this.
    profile = build_preference_profile(feedback_records, cfg, version, now=now_dt)
    if timing is not None:
        timing["pers_feedback_load"] = time.perf_counter() - _t0

    job_ids = [job.id for job, _ in ranked_jobs]
    content_hashes = {job.id: _content_hash_for(job) for job, _ in ranked_jobs}

    # Bulk cache lookup — one SELECT instead of N
    _t0 = time.perf_counter()
    cached_bulk = storage.get_cached_scores_bulk(job_ids, version, content_hashes)
    if timing is not None:
        timing["pers_cache_lookup"] = time.perf_counter() - _t0

    cache_hits = len(cached_bulk)
    cache_misses = len(job_ids) - cache_hits
    logger.info(
        "Personalization cache: %d hits, %d misses (version=%s)",
        cache_hits, cache_misses, version[:8],
    )

    explanations: dict[str, PersonalizationExplanation] = {}
    adjusted: list[tuple[Job, float]] = []
    new_cache_records: list[tuple[str, str, str, float, dict]] = []
    now = now_dt.isoformat()

    _t0 = time.perf_counter()
    _pers_total = len(ranked_jobs)
    for _i, (job, base_score) in enumerate(ranked_jobs, start=1):
        content_hash = content_hashes[job.id]

        if job.id in cached_bulk:
            adjustment, explanation_data = cached_bulk[job.id]
            explanation = PersonalizationExplanation(**explanation_data)
        else:
            adjustment, explanation = score_job_personalization(job, profile, cfg)
            new_cache_records.append(
                (job.id, content_hash, now, adjustment, explanation.model_dump())
            )

        explanations[job.id] = explanation
        final_score = min(max(base_score + adjustment, 0.0), 1.0)
        adjusted.append((job, final_score))
        if progress_callback is not None:
            progress_callback(_i, _pers_total)
    if timing is not None:
        timing["pers_compute_loop"] = time.perf_counter() - _t0
        timing["pers_cache_hits"] = cache_hits
        timing["pers_cache_misses"] = cache_misses

    # Bulk write new entries — one transaction instead of N
    _t0 = time.perf_counter()
    if new_cache_records:
        storage.cache_scores_bulk(new_cache_records, version)
    if timing is not None:
        timing["pers_cache_write"] = time.perf_counter() - _t0

    p_cfg = cfg.get("personalization", DEFAULT_PERSONALIZATION_CONFIG["personalization"])
    if bool(p_cfg.get("exclude_actioned_jobs", True)):
        adjusted = [
            (job, score) for job, score in adjusted if explanations[job.id].excluded_reason is None
        ]

    adjusted.sort(key=lambda x: x[1], reverse=True)
    return adjusted, explanations
