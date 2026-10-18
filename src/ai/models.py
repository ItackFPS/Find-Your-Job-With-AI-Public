"""Pydantic models for Phase 2: Local AI Job Understanding."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class JobUnderstanding(BaseModel):
    """Structured analysis of a job description by the local LLM.

    All fields are nullable to preserve uncertainty — if the model cannot
    determine a value, it should return ``None`` rather than inventing one.
    """

    # Seniority level extracted from the job description
    seniority_level: str | None = None

    # Core required skills (must-haves)
    required_skills: list[str] = Field(default_factory=list)

    # Preferred / nice-to-have skills
    preferred_skills: list[str] = Field(default_factory=list)

    # Programming languages mentioned
    programming_languages: list[str] = Field(default_factory=list)

    # Frameworks and libraries
    frameworks: list[str] = Field(default_factory=list)

    # Tools and technologies
    tools: list[str] = Field(default_factory=list)

    # Technologies (broad category, may overlap with frameworks/tools)
    technologies: list[str] = Field(default_factory=list)

    # Education requirements
    education_requirements: str | None = None

    # Experience range in years
    experience_years_min: float | None = None
    experience_years_max: float | None = None

    # Employment type (full-time, part-time, contract, etc.)
    employment_type: str | None = None

    # Remote work type
    remote_type: str | None = None

    # Location requirements
    location_requirements: str | None = None

    # Language requirements (e.g. "English", "French")
    language_requirements: list[str] | None = Field(default_factory=list)

    # Key responsibilities / duties
    responsibilities: list[str] = Field(default_factory=list)

    # Top-level key requirements
    key_requirements: list[str] = Field(default_factory=list)

    # Nice-to-have requirements
    nice_to_have: list[str] = Field(default_factory=list)

    # Domain / industry
    domain: str | None = None

    # Confidence score (0.0 – 1.0) reflecting how confident the model is
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # Personalised match fields (populated when profile is provided — v2 prompt)
    profile_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    profile_match_reasons: list[str] = Field(default_factory=list)
    profile_mismatch_reasons: list[str] = Field(default_factory=list)


class JobUnderstandingResult(BaseModel):
    """Wraps a single job's understanding result with metadata."""

    job_id: str
    analysis: JobUnderstanding
    prompt_version: str = "job_understanding_v1"
    model_identifier: str | None = None
    status: str = "success"  # success | failed | skipped
    error_message: str | None = None
    # Root cause when status == "failed" — kept separate from `status` so
    # every existing `status == "success"`/`"failed"`/`"skipped"` check
    # (phase3_bridge.build_enriched_texts, Phase2Pipeline/orchestrator
    # aggregate counts) keeps working unchanged. None for success/skipped.
    # One of: "malformed_json", "truncated", "empty_response", "timeout",
    # "connection_error", "http_error", "validation_error".
    failure_reason: str | None = None
    # True when this result came back only after a bounded retry (see
    # LocalLLMProvider._RETRY_MAX_TOKENS_MULTIPLIER) — kept even for a
    # successful result so metrics can report how often retries were needed.
    retried: bool = False


class BatchJobUnderstandingResult(BaseModel):
    """Aggregated result for a batch of jobs."""

    total_jobs: int
    successful: int
    failed: int
    skipped: int
    cached: int = 0
    time_seconds: float = 0.0
    # How many results required the bounded truncation retry (see
    # LocalLLMProvider.analyze_job) — a subset of `successful` + `failed`.
    retried: int = 0
    # Breakdown of `failed` results by root cause (failure_reason value ->
    # count) — lets callers see *why* the LLM stage failed, not just that it
    # did. Empty when there were no failures.
    failure_reasons: dict[str, int] = Field(default_factory=dict)
    results: list[JobUnderstandingResult]


class EligibilityDecision(BaseModel):
    """Output of the lightweight LLM semantic eligibility gate.

    Deliberately three-valued, not boolean: UNCERTAIN is a first-class outcome
    (not an error), used whenever the model isn't confident either way. This
    is separate evidence from the deterministic ``JobRequirements``/
    ``GateResult`` (src/requirements/models.py) — it is never merged into
    them; the orchestrator alone decides how much weight a REJECT carries
    (see the confidence-threshold consumption rule in src/ai/eligibility.py).
    """

    decision: Literal["PASS", "REJECT", "UNCERTAIN"] = "UNCERTAIN"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""

    @field_validator("decision", mode="before")
    @classmethod
    def _normalize_decision(cls, v: object) -> object:
        # The model sometimes returns "pass"/"reject" in lowercase despite the
        # schema — normalize rather than fail validation over casing.
        if isinstance(v, str):
            return v.strip().upper()
        return v


class EligibilityResult(BaseModel):
    """Wraps a single job's eligibility decision with metadata.

    Mirrors ``JobUnderstandingResult``'s shape so it can share
    ``AIAnalysisStorage`` (job_id + prompt_version + content_hash +
    profile_version cache key) without a second storage mechanism.
    """

    job_id: str
    decision: EligibilityDecision
    prompt_version: str = "eligibility_gate_v1"
    model_identifier: str | None = None
    status: str = "success"  # success | failed | skipped
    error_message: str | None = None


class BatchEligibilityResult(BaseModel):
    """Aggregated result for a batch of eligibility evaluations."""

    total_jobs: int
    successful: int
    failed: int
    passed: int = 0
    rejected: int = 0
    uncertain: int = 0
    # A REJECT that reached reject_confidence but was downgraded (job kept)
    # because either (a) no corroborating evidence (education/experience/
    # seniority/level signal) backed it at all, or (b) the evidence it cited
    # exists but the deterministic extractor already classified it as
    # non-mandatory (PREFERRED/OPTIONAL/UNKNOWN/OR-alternative) — see
    # EligibilityGate.run()'s corroboration and precedence checks.
    unsupported_reject: int = 0
    cached: int = 0
    time_seconds: float = 0.0
    results: list[EligibilityResult]
