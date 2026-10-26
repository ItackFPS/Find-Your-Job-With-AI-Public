"""Job search profile — the user's preferences that drive the pipeline."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.models.levels import CareerLevel, EducationLevel

# Phase NEXT / I7: reject unknown keys instead of silently dropping them.
# profile.yaml is hand-edited by users (and round-tripped by the Profile API
# — see src/api/routes/profile.py::update_profile), so a typo'd key
# (e.g. "target_skil" instead of "target_skills") previously vanished with
# no warning at all under pydantic's default extra="ignore". Verified safe
# against the real config/profile.yaml (every top-level and nested key
# already matches a declared field exactly) before enabling this.
_STRICT_PROFILE_CONFIG = ConfigDict(extra="forbid")


class LocationPreference(BaseModel):
    """One entry in the candidate's ordered geographic priority list.

    ``type="department"`` matches a job's French INSEE department code
    (``value`` e.g. "83", "06", "69"). ``type="remote"`` matches any fully
    remote job resolvable to France (``value`` is just a display id, e.g.
    "remote_france" — there is currently only one remote preference kind).

    List position (its index within
    ``GeographyPreferences.location_preferences``) IS the priority — priority
    0 is the candidate's strongest preference. There is deliberately no
    separate numeric priority field: reordering the list in the UI *is*
    reprioritizing, and a redundant integer could desync from the array
    order. See src/pipeline/location_match.py::resolve_job_location, the
    single place that interprets this list against a job.

    ``status="excluded"`` is an explicit, opt-in rejection of that one
    location — it never makes location a hard filter elsewhere (a job with
    no location preference at all is still fully scored/rankable), and it
    vetoes only that specific entry: if the job also matches a different
    non-excluded preference (at any priority), that match still wins.
    """

    model_config = _STRICT_PROFILE_CONFIG

    type: Literal["department", "remote"]
    value: str
    status: Literal["preferred", "acceptable", "excluded"] = "preferred"
    label: str | None = None


class HomePlace(BaseModel):
    """Where the candidate lives: a resolved French place (update plan §C2).

    Normally a commune picked from the offline autocomplete (``precision="commune"``,
    with its INSEE code). A profile migrated from v1 département priorities gets the
    population-weighted centre of its top département instead (``precision="department"``,
    no INSEE code) until the user picks a town. See src/geo/profile_migration.py.
    """

    model_config = _STRICT_PROFILE_CONFIG

    name: str
    insee: str | None = None
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    department: str | None = None
    precision: Literal["commune", "department"] = "commune"


class RemotePolicy(BaseModel):
    """Which remote offers the candidate accepts (update plan §C2, src/geo/remote.py kinds).

    An accepted remote kind scores as a good location match wherever the offer is
    based. A kind left unticked is never rejected for it: it scores like any other
    offer away from home. ``hybrid``: hybrid offers beyond ``max_distance_km`` stay
    acceptable even with ``strict_distance`` (the strict exclusion targets on-site
    work only). An offer whose remote area is ``unknown`` is never a preferred match.
    """

    model_config = _STRICT_PROFILE_CONFIG

    remote_france: bool = True
    remote_eu: bool = False
    remote_worldwide: bool = False
    hybrid: bool = True


class GeographyPreferences(BaseModel):
    model_config = _STRICT_PROFILE_CONFIG

    # Distance model (F4.3). Active when ``home`` is set: location scoring then decays
    # with the distance from home (src/pipeline/location_distance.py) and
    # ``location_preferences`` below is no longer read for scoring (kept, read-only,
    # for v1 compatibility until the profile v2 migration removes it).
    home: HomePlace | None = None
    # Same bounds as the Profile page slider (validated on both sides).
    max_distance_km: int = Field(default=50, ge=5, le=300)
    # The ONLY hard location exclusion: on-site, beyond max_distance_km, and this ticked.
    strict_distance: bool = False
    remote_policy: RemotePolicy = RemotePolicy()
    # v1 ``status: excluded`` départements carried over by the migration, with their v1
    # meaning: no location credit there (never a rejection), and not read by France Travail.
    excluded_departments: list[str] = []
    preferred_countries: list[str] = []
    allowed_regions: list[str] = []
    # v1 (ADR 0005). Ordered list of department/remote preferences — the source of
    # truth for location_fit scoring's priority tiers when no ``home`` is set (see
    # src/pipeline/scoring.py::_score_location_fit, fed via
    # src/pipeline/location_match.py). A ranking preference only: a
    # job outside this list is never rejected for it, and remains fully
    # eligible/rankable on every other factor. Empty by default — no
    # location is preferred until the user configures one on the Profile
    # page.
    location_preferences: list[LocationPreference] = []


class ExperiencePreferences(BaseModel):
    model_config = _STRICT_PROFILE_CONFIG

    preferred_levels: list[str] = []
    max_years: int | None = None
    excluded_seniority: list[str] = []


# Profile v2 (update plan §C3, F5.1). The profile stores *facts* (experiences, education)
# and *targets* (career levels). Until the matching reads them (F5.2/F5.3), the v1
# ``experience`` and ``max_education`` fields above stay the ones the pipeline uses.
PROFILE_SCHEMA_VERSION = 2

_YEAR_MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


class ExperienceEntry(BaseModel):
    """One job, internship or apprenticeship the candidate held."""

    model_config = _STRICT_PROFILE_CONFIG

    id: str = Field(min_length=1)
    role: str
    employer: str | None = None
    start: str = Field(pattern=_YEAR_MONTH)          # "YYYY-MM"
    end: str | None = Field(default=None, pattern=_YEAR_MONTH)  # None = ongoing
    skills: list[str] = []
    responsibilities: list[str] = []
    # Counts towards the computed years of experience (unticked for unrelated jobs).
    relevant: bool = True


class EducationEntry(BaseModel):
    """One qualification, held or in progress. ``level`` is the French/EQF level 3–8."""

    model_config = _STRICT_PROFILE_CONFIG

    id: str = Field(min_length=1)
    qualification: str
    level: EducationLevel | None = None
    field: str | None = None
    institution: str | None = None
    year: int | None = Field(default=None, ge=1950, le=2100)
    in_progress: bool = False
    # A foreign degree: the user picked ``level`` themselves.
    foreign: bool = False


class CareerTargets(BaseModel):
    """The career levels the candidate wants (multi-select) and their experience years."""

    model_config = _STRICT_PROFILE_CONFIG

    levels: list[CareerLevel] = []
    # Also show roles one level above the highest target.
    stretch: bool = False
    # Replaces the years computed from ``experiences`` when set.
    years_override: float | None = Field(default=None, ge=0, le=60)


class ProfileReview(BaseModel):
    """The one-time "we updated your profile, please check" screen (F5.1)."""

    model_config = _STRICT_PROFILE_CONFIG

    pending: bool = True
    notes: list[str] = []
    migrated_at: str | None = None


def _months(year_month: str) -> int:
    y, m = year_month.split("-")
    return int(y) * 12 + int(m) - 1


def compute_experience_years(entries: list[ExperienceEntry], today: date | None = None) -> float:
    """Years covered by the relevant experiences, overlaps counted once, to 0.1 year.

    A month range is inclusive (``2023-01``→``2023-12`` is one year); an ongoing entry runs
    to the current month; an entry ending before it starts counts nothing.
    """
    now = today or date.today()
    current = now.year * 12 + now.month - 1
    spans = sorted(
        (_months(e.start), min(_months(e.end), current) if e.end else current)
        for e in entries if e.relevant
    )
    total = 0
    cur_start: int | None = None
    cur_end = -1
    for start, end in spans:
        if end < start:
            continue
        if cur_start is None or start > cur_end + 1:
            if cur_start is not None:
                total += cur_end - cur_start + 1
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_start is not None:
        total += cur_end - cur_start + 1
    return round(total / 12, 1)


class EscoEntry(BaseModel):
    """An ESCO concept selected by the user for their profile.

    Stores provenance metadata alongside the plain keyword so the pipeline
    can continue using simple strings while the UI tracks canonical ESCO IDs.

    Fields:
        concept_uri:  ESCO concept URI, or a ``local:`` URI for bundled seed
                      concepts that are not yet confirmed against the live taxonomy.
                      ``None`` for fully custom (user-typed) entries.
        label:        Full canonical English label (e.g. ``"Python (programming language)"``).
        keyword:      Simplified keyword used for pipeline matching.
                      Typically the label stripped of parenthetical annotations
                      (e.g. ``"Python"``).
        custom:       ``True`` when the user typed a value not found in ESCO.
    """

    model_config = _STRICT_PROFILE_CONFIG

    concept_uri: str | None = None
    label: str
    keyword: str = ""
    custom: bool = False

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        # Auto-derive keyword from label when not explicitly provided.
        if not self.keyword:
            label = self.label
            if "(" in label:
                label = label[: label.index("(")].strip()
            object.__setattr__(self, "keyword", label)


class MatchingPreferences(BaseModel):
    """Configurable parameters for the requirement matching engine.

    These control which checks cause hard rejection vs. score penalties.
    All settings have safe defaults — the system is usable without this
    section in profile.yaml.
    """

    model_config = _STRICT_PROFILE_CONFIG

    # Reject when a mandatory education level exceeds the candidate's max
    education_hard_constraint: bool = True
    # Score penalty (0.0–1.0) applied when a PREFERRED education exceeds max
    education_preferred_penalty: float = 0.20
    # Reject when the job title centers on a technology not in the profile
    principal_skill_gate: bool = True
    # Reject when a skill is mandatory+principal, detected heuristically from
    # description phrasing (centrality signals) — does not require the LLM.
    mandatory_central_skill_gate: bool = True
    # Enable LLM-based requirement extraction (requires LM Studio/Ollama)
    # Configure the LLM endpoint via REQGATE_LLM_ENDPOINT env var.
    llm_extraction_enabled: bool = False
    # Enable the lightweight LLM semantic eligibility gate (src/ai/eligibility.py).
    # Requires --ai in addition to this flag. See that module's docstring.
    llm_eligibility_enabled: bool = False
    # Minimum confidence (0.0-1.0) an LLM REJECT decision must reach to actually
    # remove a job from the eligibility gate; UNCERTAIN and low-confidence
    # REJECT both pass through unchanged.
    llm_eligibility_reject_confidence: float = 0.75


class JobSearchProfile(BaseModel):
    """User-facing job search preferences.

    Edit ``config/profile.yaml`` to change these.  A future website will
    write that file automatically so the pipeline can be reconfigured
    without touching Python code.

    Serialised as plain YAML/JSON, making it trivially consumable by a
    web frontend or REST API.
    """

    model_config = _STRICT_PROFILE_CONFIG

    target_domains: list[str] = []
    # When True (default), a non-empty target_domains is the sole gate for
    # the hard filter — a job must match one of these domains regardless of
    # target_skills. When False, target_skills also gate acceptance (a job
    # matching a skill passes even if it's outside the chosen domains) —
    # useful for casting a wider net around a skill that spans domains.
    # See build_filter_config() in src/pipeline/filter.py.
    focus_domain: bool = True
    target_skills: list[str] = []
    # ESCO metadata for target_skills — optional, not used by the pipeline.
    # When present, each entry corresponds (by position) to a value in
    # target_skills and carries the canonical ESCO concept URI and label.
    # Absent on profiles saved before ESCO integration; treated as empty list.
    target_skills_esco: list[EscoEntry] = []
    geography: GeographyPreferences = GeographyPreferences()
    work_modes: list[str] = []
    contract_types: list[str] = []
    experience: ExperiencePreferences = ExperiencePreferences()
    languages: list[str] = []
    freshness_days: int = Field(default=30, ge=1)
    # Candidate's maximum education level in Bac+N notation (e.g. "bac+2").
    # Used by the hard constraint gate to reject jobs requiring a higher level.
    # None = no education constraint.
    max_education: str | None = None
    # Matching engine configuration — controls which checks are hard gates
    # vs. soft penalties.
    matching: MatchingPreferences = MatchingPreferences()
    # Free-text description of the kind of role the candidate wants, in their
    # own words. Folded into `responsibilities` by to_semantic_info() below,
    # so it IS read today — by the Phase 3 semantic-matching embedding
    # (src/pipeline/semantic_matching.py). Not read by Phase 2 (LLM
    # enrichment) or the deterministic hard filter/requirements gate.
    looking_for: str | None = None
    # Profile v2 (F5.1). A file without ``profile_version`` is a v1 profile; the
    # migration (src/models/profile_v2_migration.py) fills the fields below.
    profile_version: int = Field(default=1, ge=1, le=PROFILE_SCHEMA_VERSION)
    experiences: list[ExperienceEntry] = []
    education: list[EducationEntry] = []
    targets: CareerTargets = CareerTargets()
    review: ProfileReview | None = None

    def experience_years(self, today: date | None = None) -> float | None:
        """The user's override, else the years computed from experiences, else None."""
        if self.targets.years_override is not None:
            return self.targets.years_override
        if not self.experiences:
            return None
        return compute_experience_years(self.experiences, today)

    def highest_education_level(self) -> int | None:
        """Highest French/EQF level among held (not in-progress) qualifications."""
        levels = [e.level for e in self.education if e.level is not None and not e.in_progress]
        return max(levels) if levels else None

    def to_semantic_info(self) -> dict:
        """Build the ``profile_info`` dict expected by ``build_profile_embedding``.

        The Phase 3 semantic matching embeds this into a vector that is then
        compared against job embeddings.  Values are derived from the profile
        so the semantic index always reflects the user's current preferences.

        This runs on every pipeline run whenever a profile is loaded — it is
        NOT gated behind the "Enable AI" (Phase 2 LLM) flag, which controls a
        separate, optional enrichment stage.
        """
        responsibilities = (
            ", ".join(self.target_domains)
            if self.target_domains
            else "software development"
        )
        if self.looking_for:
            # Free-text description of the desired role — folded into the
            # same field the profile embedding already builds from, so it
            # participates in Phase 3's semantic similarity comparison
            # against each job's own embedding.
            responsibilities = f"{responsibilities}. {self.looking_for}"

        return {
            "seniority": (
                ", ".join(self.experience.preferred_levels)
                if self.experience.preferred_levels
                else "entry-level"
            ),
            "employment_type": (
                ", ".join(self.contract_types) if self.contract_types else "any"
            ),
            "location": (
                ", ".join(self.geography.preferred_countries)
                if self.geography.preferred_countries
                else "Europe"
            ),
            "languages": ", ".join(self.languages) if self.languages else "English",
            # Joined into a string like every other field here — left as a
            # raw list, str.format() renders it as Python-list syntax
            # ("['python', 'sql']") in the embedding text, which the
            # sentence-transformer model was never trained on. Mirrors the
            # same fix already applied to `languages` (see
            # TestProfileLanguagesString in tests/test_ranking_correctness.py).
            "skills": (
                ", ".join(self.target_skills + self.target_domains)
                if (self.target_skills or self.target_domains)
                else "general"
            ),
            "responsibilities": responsibilities,
        }
