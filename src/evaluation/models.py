"""Versioned data contracts of the evaluation harness (all JSON-serialisable)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.evaluation import SCHEMA_VERSION

Verdict = Literal["accept", "reject"]
Expected = Literal["accept", "reject", "borderline"]
# human: a person decided without an assistant proposal for that job on record (independent).
# human_assisted: a person decided while an assistant proposal was on record (confirmed or changed);
# a valid label, but NOT independent evidence about the assistant or a strategy tuned with it.
# derived: computed from the feedback journal.
LabelSource = Literal["human", "human_assisted", "derived"]
DatasetKind = Literal["synthetic", "real"]
Stage = Literal["filter", "requirements_gate", "passed"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetManifest(_Strict):
    schema_version: int = SCHEMA_VERSION
    dataset_id: str
    version: str
    kind: DatasetKind
    created_at: str
    # Reference instant used to freeze time-dependent production checks
    # (the published-age filter) so a replay is reproducible.
    as_of: str
    job_count: int
    label_scheme_version: int = 1
    jobs_sha256: str
    labels_sha256: str
    profile_sha256: str | None = None
    notes: str = ""


class LabelRecord(_Strict):
    job_id: str
    expected: Expected
    category: str | None = None
    reason: str = ""
    label_source: LabelSource
    labeler: str
    labeled_at: str


class LLMSpec(_Strict):
    endpoint: str
    model_name: str
    timeout_seconds: int = 30
    max_tokens: int = 512
    # Free-form, user supplied (quantisation, server, VRAM...) — not detectable.
    model_meta: dict[str, str] = Field(default_factory=dict)


class StrategyRef(_Strict):
    name: str = "current"
    config_dir: str = "config"
    # Overrides the dataset's pinned profile (default: dataset profile, else
    # <config_dir>/profile.yaml).
    profile_path: str | None = None
    llm: LLMSpec | None = None
    # ESCO database enabling the production synonym lookup in scoring. None (default) =
    # disabled, so results do not depend on a developer's local data/esco.db.
    esco_db: str | None = None


class Decision(_Strict):
    job_id: str
    verdict: Verdict
    stage: Stage
    reasons: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    extraction_method: str | None = None
    score: float | None = None
    factors: dict[str, float] | None = None
    # Disagreement-engine assessment (rules-only signals; set when the run was started with --assess).
    assessment: dict[str, Any] | None = None


class RunResult(_Strict):
    schema_version: int = SCHEMA_VERSION
    run_id: str
    created_at: str
    dataset: dict[str, Any]
    strategy: dict[str, Any]
    code: dict[str, Any]
    environment: dict[str, Any]
    decisions: list[Decision]
    metrics: dict[str, Any]
    timings: dict[str, Any]
    llm: dict[str, Any]
    warnings: list[str] = Field(default_factory=list)


class GoldenDecisions(_Strict):
    """Change detector: decisions only — no timestamps, environment or timings."""

    schema_version: int = SCHEMA_VERSION
    dataset: dict[str, Any]
    strategy_fingerprint: str
    decisions: list[Decision]
