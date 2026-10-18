"""Versioned contracts of the decision journal.

``DecisionRecord`` answers, for one job in one run: what was decided, at which stage,
under which strategy, with which score, on what evidence, what was UNKNOWN, what
contradicted, and how the job was exposed (rank/tier). ``EvidenceItem`` is the
smallest structure that carries the existing three-valued gate evidence
(``ConstraintState``, ``EvidenceSpan``, ``EvidenceSource``) for every pipeline stage.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.decisions import DECISION_SCHEMA_VERSION

Outcome = Literal["accepted", "rejected", "review"]
Dimension = Literal[
    "location", "domain", "skills", "seniority", "experience", "education",
    "work_mode", "contract", "salary", "freshness", "language", "data_quality", "other",
]
Polarity = Literal["positive", "negative", "unknown"]
State = Literal["satisfied", "violated", "unknown"]
StageName = Literal[
    "validate", "deduplicate", "filter", "requirements_gate", "llm_eligibility", "score", "ranking",
]
StageResult = Literal["pass", "reject", "uncertain", "skipped", "error"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpanRef(_Model):
    field: str = "description"
    start: int
    end: int
    text: str
    extractor_id: str = ""


class SourceRef(_Model):
    """Who produced an evidence item."""

    stage: str
    rule_id: str
    rule_version: str | None = None
    # heuristic | llm_corroborated | llm_uncorroborated | metadata | rule (non-text evidence)
    evidence_source: str | None = None


class EvidenceItem(_Model):
    """One piece of evidence. ``observed`` = what the job says/shows; ``expected`` = what the
    profile offers or requires for comparison. ``span`` is set when the evidence quotes the
    job text. ``state``/``polarity``: VIOLATED → negative, SATISFIED → positive, UNKNOWN →
    unknown (never treated as a rejection)."""

    dimension: Dimension
    polarity: Polarity
    state: State
    source: SourceRef
    message: str = ""
    observed: str | None = None
    expected: str | None = None
    weight: float | None = None
    span: SpanRef | None = None


class ConflictItem(_Model):
    dimensions: list[Dimension] = Field(default_factory=list)
    description: str


class StageTraceEntry(_Model):
    stage: StageName
    result: StageResult
    detail: str | None = None


class Exposure(_Model):
    """How the job was surfaced after ranking (feedback must be read with this: only exposed
    jobs can receive feedback)."""

    rank: int | None = None
    final_score: float | None = None
    deterministic_score: float | None = None
    semantic_score: float | None = None
    personalization_adjustment: float | None = None
    tier: str | None = None
    notified_channel: str | None = None


class JobMeta(_Model):
    title: str | None = None
    company: str | None = None
    source: str | None = None
    location: str | None = None
    content_hash: str | None = None


class DecisionRecord(_Model):
    schema_version: int = DECISION_SCHEMA_VERSION
    decision_id: str
    run_id: str
    job_id: str
    decided_at: str
    outcome: Outcome
    deciding_stage: StageName
    strategy_version: str
    # Deterministic score of an accepted job (0-1); None for rejected jobs.
    fit_score: float | None = None
    # Reserved for P2 (disagreement engine); never a verbalised LLM confidence.
    decision_confidence: float | None = None
    confidence_calibrated: bool = False
    evidence: list[EvidenceItem] = Field(default_factory=list)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)
    stage_trace: list[StageTraceEntry] = Field(default_factory=list)
    exposure: Exposure | None = None
    snapshot_hash: str | None = None
    job: JobMeta = Field(default_factory=JobMeta)

    @property
    def unknowns(self) -> list[EvidenceItem]:
        """Everything the decision could not establish (UNKNOWN evidence)."""
        return [e for e in self.evidence if e.state == "unknown"]
