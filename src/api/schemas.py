"""Pydantic response schemas for the web API.

These are the serialization contracts between the FastAPI backend and the
React frontend.  They are separate from the pipeline's internal data models
so that API shape changes don't require touching pipeline code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

class LocationPreferenceIn(BaseModel):
    """Mirrors ``src.models.profile.LocationPreference`` exactly."""
    type: Literal["department", "remote"]
    value: str
    status: Literal["preferred", "acceptable", "excluded"] = "preferred"
    label: str | None = None


class HomePlaceIn(BaseModel):
    """Mirrors ``src.models.profile.HomePlace``."""
    name: str
    insee: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    department: str | None = None
    precision: Literal["commune", "department"] = "commune"


class RemotePolicyIn(BaseModel):
    """Mirrors ``src.models.profile.RemotePolicy``."""
    remote_france: bool = True
    remote_eu: bool = False
    remote_worldwide: bool = False
    hybrid: bool = True


class GeographyIn(BaseModel):
    # Distance model (F4.3) — mirrors GeographyPreferences; see src/pipeline/location_distance.py.
    home: HomePlaceIn | None = None
    max_distance_km: int = Field(default=50, ge=5, le=300)
    strict_distance: bool = False
    remote_policy: RemotePolicyIn = RemotePolicyIn()
    excluded_departments: list[str] = []
    preferred_countries: list[str] = []
    allowed_regions: list[str] = []
    # Mirrors GeographyPreferences.location_preferences — ordered
    # department/remote priority list, list position = priority (see
    # src/pipeline/scoring.py::_score_location_fit and
    # src/pipeline/location_match.py::resolve_job_location).
    location_preferences: list[LocationPreferenceIn] = []


class ExperienceIn(BaseModel):
    preferred_levels: list[str] = []
    max_years: int | None = None
    excluded_seniority: list[str] = []


CareerLevelIn = Literal["internship", "alternance", "graduate", "junior", "mid", "senior", "lead", "manager"]


class ExperienceEntryIn(BaseModel):
    """Mirrors ``src.models.profile.ExperienceEntry``."""
    id: str = Field(min_length=1)
    role: str
    employer: str | None = None
    start: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    skills: list[str] = []
    responsibilities: list[str] = []
    relevant: bool = True


class EducationEntryIn(BaseModel):
    """Mirrors ``src.models.profile.EducationEntry`` (``level``: French/EQF 3–8)."""
    id: str = Field(min_length=1)
    qualification: str
    level: Literal[3, 4, 5, 6, 7, 8] | None = None
    field: str | None = None
    institution: str | None = None
    year: int | None = Field(default=None, ge=1950, le=2100)
    in_progress: bool = False
    foreign: bool = False


class CareerTargetsIn(BaseModel):
    """Mirrors ``src.models.profile.CareerTargets``."""
    levels: list[CareerLevelIn] = []
    stretch: bool = False
    years_override: float | None = Field(default=None, ge=0, le=60)


class ProfileReviewIn(BaseModel):
    """Mirrors ``src.models.profile.ProfileReview`` — the one-time review screen."""
    pending: bool = True
    notes: list[str] = []
    migrated_at: str | None = None


class EscoEntryIn(BaseModel):
    """ESCO concept metadata stored alongside a target skill."""
    concept_uri: str | None = None
    label: str
    keyword: str = ""
    custom: bool = False


class MatchingIn(BaseModel):
    """Mirrors ``MatchingPreferences`` — requirements-gate configuration."""
    education_hard_constraint: bool = True
    education_preferred_penalty: float = 0.20
    principal_skill_gate: bool = True
    mandatory_central_skill_gate: bool = True
    llm_extraction_enabled: bool = False


class ProfileIn(BaseModel):
    """Profile update payload — mirrors JobSearchProfile exactly."""
    target_domains: list[str] = []
    focus_domain: bool = True
    target_skills: list[str] = []
    target_skills_esco: list[EscoEntryIn] = []
    geography: GeographyIn = GeographyIn()
    work_modes: list[str] = []
    contract_types: list[str] = []
    experience: ExperienceIn = ExperienceIn()
    languages: list[str] = []
    freshness_days: int = Field(default=30, ge=1)
    max_education: str | None = None
    matching: MatchingIn = MatchingIn()
    # Mirrors JobSearchProfile.looking_for — consumed by Phase 3 semantic
    # matching (folded into the profile embedding text); not read by Phase 2
    # or the deterministic filter/requirements gate.
    looking_for: str | None = None
    # Profile v2 (F5.1) — mirrors JobSearchProfile; not read by the matching yet.
    profile_version: int = Field(default=1, ge=1, le=2)
    experiences: list[ExperienceEntryIn] = []
    education: list[EducationEntryIn] = []
    targets: CareerTargetsIn = CareerTargetsIn()
    review: ProfileReviewIn | None = None


ProfileOut = ProfileIn  # same shape; alias for clarity


class PlaceOut(BaseModel):
    """One commune from the offline autocomplete (GET /api/geo/places)."""
    name: str
    insee: str
    department: str | None = None
    department_name: str | None = None
    lat: float
    lon: float


# ---------------------------------------------------------------------------
# Pipeline runs
# ---------------------------------------------------------------------------

class SourceStats(BaseModel):
    collected: int = 0
    accepted: int = 0
    errors: int = 0
    # completed | partial | failed | skipped | paused (absent on runs recorded before it existed)
    status: str | None = None
    # why a source was skipped (and what to set up), paused, failed or only partly collected
    reason: str | None = None
    # Source plug-in contract (plan F6.1); all absent on older runs.
    # official | public_page | unofficial
    access_class: str | None = None
    # "parser_changed": pages were served (HTTP 200) but no offer could be read from them
    canary: str | None = None
    # failed runs in a row (circuit breaker); paused_until: when a paused source runs again
    consecutive_failures: int | None = None
    paused_until: str | None = None
    request_cap_reached: bool | None = None
    # no HTTP answer at all (DNS, connect, timeout); network_outage: the whole run was
    # unreachable, so no circuit breaker moved
    network_unreachable: bool | None = None
    network_outage: bool | None = None
    # sudden drop of the source's yield: severity (zero | drop), current_yield,
    # baseline_yield, baseline_run_count, is_new (src/pipeline/source_health.py)
    yield_anomaly: dict[str, Any] | None = None


class RunStats(BaseModel):
    collected: int = 0
    validated: int = 0
    deduplicated: int = 0
    filtered: int = 0
    # Two-stage filter breakdown (see src/pipeline/orchestrator.py PipelineStats).
    hard_filter_input: int = 0
    hard_filter_passed: int = 0
    hard_filter_rejected: int = 0
    requirements_gate_input: int = 0
    requirements_gate_passed: int = 0
    requirements_gate_rejected: int = 0
    # Non-null when the gate itself raised and was bypassed (all input jobs
    # passed through untouched) — see PipelineStats.requirements_gate_error.
    requirements_gate_error: str | None = None
    # Lightweight LLM semantic eligibility gate (see src/ai/eligibility.py /
    # PipelineStats.llm_eligibility* in orchestrator.py). Zero when the gate
    # is disabled (default) — same shape as the two gates above.
    llm_eligibility_input: int = 0
    llm_eligibility_passed: int = 0
    llm_eligibility_rejected: int = 0
    llm_eligibility: dict[str, Any] = {}
    validation_rejected: int = 0
    deduplication_removed: int = 0
    filter_rejected: int = 0
    scored: int = 0
    semantically_matched: int = 0
    personalized: int = 0
    duration_seconds: float = 0.0
    by_source: dict[str, SourceStats] = {}
    by_country: dict[str, int] = {}
    by_work_mode: dict[str, int] = {}
    errors: list[str] = []
    profile_hash: str = ""
    model_name: str = ""
    scoring_config_hash: str = ""
    current_stage: str = ""
    # Whether this run was started with Phase 2 (local AI) enabled.
    use_ai: bool = False
    # Phase 2 progress/result summary (see PipelineStats.phase2 in
    # src/pipeline/orchestrator.py) — total/completed/failed candidate
    # counts, updated live while Phase 2 runs. Empty when AI is disabled or
    # Phase 2 hasn't started yet.
    phase2: dict[str, Any] = {}
    # Live item-level progress for whichever stage is currently running:
    # {"stage": str, "processed": int, "total": int}. Covers every stage
    # (collect, validate, deduplicate, filter, requirements_gate, score,
    # semantic, phase2, personalize) uniformly — see PipelineStats.stage_progress
    # in src/pipeline/orchestrator.py. Empty between updates/when idle.
    stage_progress: dict[str, Any] = {}


class PipelineRunOut(BaseModel):
    id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    stats: RunStats = RunStats()
    error: str | None = None


class StartRunIn(BaseModel):
    """Optional request body for POST /api/pipeline/run.

    Omitting the body (or ``use_ai``) preserves the pre-existing default:
    AI disabled.
    """
    use_ai: bool = False


class StartRunOut(BaseModel):
    run_id: str
    status: str = "running"


class PipelineEventOut(BaseModel):
    """SSE progress event payload — see GET /api/pipeline/events.

    Reuses ``RunStats`` (the same shape returned by GET /runs/{id}) rather
    than inventing a second stats representation; this also acts as an
    allow-list so the stream can never leak a field the polling endpoint
    doesn't already expose.
    """
    type: str
    run_id: str
    status: str
    stage: str = ""
    stats: RunStats = RunStats()
    timestamp: str
    # Mirrors PipelineRunOut.error (same field, same trust level — already
    # exposed today via GET /runs/{id}); never a raw traceback.
    error: str | None = None


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class ScoreBreakdownOut(BaseModel):
    deterministic_score: float | None = None
    semantic_score: float | None = None
    final_score: float | None = None
    factors: dict[str, float] = {}
    run_id: str | None = None
    # Phase 5 (2026-09-10): expose the same canonical tier every notification
    # channel already uses (src.pipeline.scoring.score_to_tier), so the
    # dashboard/frontend never needs its own copy of the tier thresholds.
    # score_100 = final_score * 100, rounded.
    score_100: int | None = None
    tier: str | None = None


class JobOut(BaseModel):
    id: str
    source: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    # Phase 5: resolved French department code (canonical `department` column
    # when set, else the same extract_department_code() fallback the
    # location resolver (src/pipeline/location_match.py) also uses) — so the
    # frontend can show department/PACA distinctly without its own copy of
    # that parsing logic.
    department: str | None = None
    countries: list[str] = []
    remote: bool = False
    work_mode: str | None = None
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = "EUR"
    published_at: datetime | None = None
    discovered_at: datetime
    skills: list[str] = []
    canonical_url: str | None = None
    apply_url: str | None = None
    status: str = "active"
    application_status: str | None = None
    score: ScoreBreakdownOut | None = None


class ProvenanceOut(BaseModel):
    """One source an offer was seen on (issue #86)."""

    source: str
    url: str | None = None
    first_seen_at: datetime
    last_seen_at: datetime


class JobDetailOut(JobOut):
    description: str | None = None
    # Every source the offer was seen on, its own source first (issue #86).
    provenance: list[ProvenanceOut] = []


class ExplanationSignal(BaseModel):
    signal_type: str
    value: str
    contribution: float
    feedback_count: int


class ExplanationReason(BaseModel):
    """A single human-readable reason shown in the job explanation UI."""
    emoji: str
    text: str
    kind: str  # 'positive' | 'concern' | 'rejection'


class JobExplanationOut(BaseModel):
    job_id: str
    overall_score: float | None = None          # 0–100, rounded
    score: ScoreBreakdownOut | None = None      # only `factors` is rendered (Match factors); the rest stays internal
    filter_result: str = "passed"               # 'passed' | 'rejected'
    rejection_reason: str | None = None         # raw code; kept for backward compat
    positive_reasons: list[ExplanationReason] = []
    concerns: list[ExplanationReason] = []
    rejection_reasons: list[ExplanationReason] = []
    personalization_signals: list[ExplanationSignal] = []
    personalization_adjustment: float = 0.0
    # 'decision' = rendered from the decision journal (single canonical decision);
    # 'heuristic' = legacy explain.py analysis (job predates the journal / journal off).
    explanation_source: str = "heuristic"
    decision_id: str | None = None


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class DashboardOut(BaseModel):
    latest_run: PipelineRunOut | None = None
    total_jobs_in_db: int = 0
    is_running: bool = False
    current_run_id: str | None = None


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

class CollectorOut(BaseModel):
    name: str
    type: str
    active: bool
    domain: str | None = None
    last_run_stats: SourceStats | None = None
    last_run_at: str | None = None
