"""Structured event models for the notification system.

Each event is a plain dataclass that serialises to a stable dict suitable
for Discord embeds, n8n webhook payloads, or any future provider.

Version field guarantees: once an event version is published, its fields
are additive-only (new optional fields may appear; existing fields keep
their names and types).  Consumers MUST tolerate unknown fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Sub-structures
# ---------------------------------------------------------------------------

@dataclass
class NotifPipelineStats:
    collected: int = 0
    validated: int = 0
    deduplicated: int = 0
    filtered: int = 0
    scored: int = 0
    semantically_matched: int = 0
    personalized: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "collected": self.collected,
            "validated": self.validated,
            "deduplicated": self.deduplicated,
            "filtered": self.filtered,
            "scored": self.scored,
            "semantically_matched": self.semantically_matched,
            "personalized": self.personalized,
            "duration_seconds": round(self.duration_seconds, 1),
            "errors": self.errors[:5],
        }


@dataclass
class JobSummary:
    job_id: str
    title: str | None
    company: str | None
    location: str | None
    remote: bool
    work_mode: str | None
    skills: list[str]
    final_score: float
    canonical_url: str | None
    apply_url: str | None

    def to_dict(self) -> dict[str, Any]:
        # Deferred: src.pipeline's package __init__ eagerly imports
        # sentence-transformers/torch (see src/linkedin/scanner.py's
        # _load_filters_config docstring for the same concern) — a plain
        # notification-event dataclass must not force that cost onto every
        # caller that just imports this lightweight module.
        from src.pipeline.scoring import score_to_tier

        score_100 = round(self.final_score * 100)
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "remote": self.remote,
            "work_mode": self.work_mode,
            "skills": self.skills[:10],
            "score": round(self.final_score, 4),
            "final_score": round(self.final_score, 4),
            "score_100": score_100,
            "tier": score_to_tier(score_100),
            "canonical_url": self.canonical_url,
            "apply_url": self.apply_url,
        }


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class PipelineCompletedEvent:
    """Emitted when a pipeline run finishes (successfully or with failure).

    Three distinct job collections travel in this event — do not conflate them:

    ``tier_counts``
        Aggregate integer counts over ALL scored jobs in the run (thousands).
        ``tier_counts[tier] == len(digest_jobs[tier])`` always holds.

    ``digest_jobs``
        ALL scored jobs (thousands), classified into the same five tiers,
        with a compact per-job schema (id/title/company/location/url/score/
        score_100/tier — no factors, no embeddings, no raw descriptions).
        This is the complete dataset for building a digest display; consumers
        may show only the top N per tier without losing the underlying counts.

    ``new_high_match_jobs``
        A SEPARATE, capped (<=50) list of jobs newly scored since the previous
        run, sorted by final_score desc. This is the notification-candidate
        subset used to decide which jobs to individually announce — using
        ``digest_jobs`` for that purpose would re-announce the same jobs on
        every run.

    ``all_jobs_count`` is the total number of personalized/scored jobs (kept
    for backward compatibility); ``total_scored_jobs`` is the same figure
    computed directly from ``result.scored_jobs`` and is guaranteed to equal
    ``sum(tier_counts.values())``.
    """

    run_id: str
    status: str                  # 'completed' | 'failed'
    stats: NotifPipelineStats
    new_high_match_jobs: list[JobSummary] = field(default_factory=list)
    error: str | None = None
    all_jobs_count: int = 0
    tier_counts: dict[str, int] = field(default_factory=dict)
    digest_jobs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    total_scored_jobs: int = 0
    event: str = "pipeline.completed"
    version: int = 1
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "version": self.version,
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "status": self.status,
            "error": self.error,
            "all_jobs_count": self.all_jobs_count,
            "total_scored_jobs": self.total_scored_jobs,
            "tier_counts": self.tier_counts,
            "digest_jobs": self.digest_jobs,
            "stats": self.stats.to_dict(),
            "new_high_match_jobs": [j.to_dict() for j in self.new_high_match_jobs],
        }
