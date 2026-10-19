"""Data models for structured job requirement extraction.

Each extracted requirement carries an importance level so that the pipeline
can distinguish hard constraints (mandatory) from soft preferences (preferred).

Three-valued evaluation
-----------------------
Every hard constraint evaluates to one of three states:

    SATISFIED  — evidence confirms compatibility
    VIOLATED   — high-quality, attributable evidence proves incompatibility
    UNKNOWN    — information is absent, ambiguous, or contradictory

A job is rejected ONLY when a constraint is VIOLATED and the evidence is
non-contradicted.  UNKNOWN is always a pass.  Contradictory evidence yields
PASS_WITH_UNCERTAINTY (operationally a pass), never a rejection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Importance(str, Enum):
    """How important a requirement is to the role."""

    MANDATORY = "mandatory"   # explicitly required — hard constraint
    CORE = "core"             # strongly implied but no explicit qualifier
    PREFERRED = "preferred"   # explicitly optional / nice-to-have
    OPTIONAL = "optional"     # incidental mention, low weight
    UNKNOWN = "unknown"       # could not determine importance


class EvidenceSource(str, Enum):
    """Where a requirement's data came from, and how much it can be trusted.

    HEURISTIC          — regex/vocabulary extraction directly against the job
                          text. Always attributable (carries a real span).
    LLM_CORROBORATED    — produced or refined by the LLM, but the underlying
                          claim was independently confirmed to appear in the
                          actual job title/description (a real span was found).
                          This is "LLM interpretation of evidence that exists".
    LLM_UNCORROBORATED  — produced by the LLM with NO matching text found in
                          the job posting. Retained for observability/audit
                          only; per the evidence-authority rule, it can never
                          independently cause a VIOLATED (hard reject) or a
                          score penalty — see HardConstraintGate.
    """

    HEURISTIC = "heuristic"
    LLM_CORROBORATED = "llm_corroborated"
    LLM_UNCORROBORATED = "llm_uncorroborated"


class ConstraintState(str, Enum):
    """Three-valued result of evaluating one hard constraint against a job.

    SATISFIED  — evidence confirms the constraint is met.
    VIOLATED   — strong, attributable evidence proves the constraint is broken.
    UNKNOWN    — evidence is absent, ambiguous, or contradictory.

    Only VIOLATED (with non-contradicted evidence) can cause rejection.
    UNKNOWN always passes; contradictory VIOLATED → UNKNOWN.
    """

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


@dataclass
class EvidenceSpan:
    """Provenance record linking a decision to a specific passage in the source.

    Attributes:
        text:   The original text snippet that produced the claim.
        start:  Byte/char offset of the first character in the source string.
        end:    Byte/char offset one past the last character.
        field:  Which source field the span is from ("description", "title",
                "metadata", "location", …).
        extractor_id: Identifier of the rule/model that produced this span
                      (e.g. "education.bac5.fr.v1", "experience.min.en.v2").
    """

    text: str
    start: int
    end: int
    field: str = "description"
    extractor_id: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "field": self.field,
            "extractor_id": self.extractor_id,
        }


class EducationKind(str, Enum):
    """What an education mention asks of the candidate (issue #40).

    Compared against ``profile.max_education``, the highest level the
    candidate already HOLDS:

    HELD       — the degree must already be obtained ("Titulaire d'un Bac+5",
                 "Bac+5 requis"). Today's behavior; see ADR 0004.
    ENROLLED   — the candidate must currently be studying at a level/stage
                 ("Étudiant(e) en Bac+3 à Bac+4", "en dernière année d'école
                 d'ingénieur"). Not proof of holding that level.
    PREPARING  — the candidate prepares a diploma, typically through the
                 alternance itself ("Vous préparez un Bac+5", Job 39).
    """

    HELD = "held"
    ENROLLED = "enrolled"
    PREPARING = "preparing"


@dataclass
class EducationRequirement:
    """A normalized education level requirement extracted from a job description.

    ``level_bac_plus`` uses the French Bac+N convention as a comparable integer:
        0 = Bac (lycée), 2 = BTS/DUT, 3 = Licence/Bachelor, 4 = Master 1,
        5 = Master 2 / Diplôme d'ingénieur, 8 = Doctorat/PhD.

    ``is_or_alternative`` is True when the requirement is phrased as an OR
    alternative (e.g. "Bac+5 ou expérience équivalente" or "Bac+4 ou
    équivalent").  The candidate may satisfy either the education branch OR
    the alternative condition, so the gate must never apply a score penalty
    for the education mismatch alone. It does NOT exempt the requirement from
    the hard education ceiling (see HardConstraintGate._check_education) —
    a generic "ou équivalent" still names an explicit level and is rejected
    like any other MANDATORY/PREFERRED excess.

    ``is_experience_alternative`` is a narrower subset of the above: True only
    when the alternative is *specifically* professional experience
    ("ou/sinon expérience équivalente" — never a generic "ou équivalent").
    This is a genuinely different, well-defined substitute path (not just
    softer wording of the same degree requirement), so it DOES exempt the
    requirement from the hard education ceiling — the candidate can qualify
    without the degree at all.

    ``is_desirable`` is True when a desirability cue ("souhaité", "apprécié",
    "un plus", "preferred", ...) qualifies the mention: the level is wished
    for, not required, so it is never a hard ceiling (ADR 0004). A PREFERRED
    mention softened *only* by an equivalence clause ("Bac+4 ou équivalent")
    keeps ``is_desirable`` False: it states the bar and accepts equivalent
    diplomas.
    """

    level_bac_plus: int          # 0, 2, 3, 4, 5, 8
    level_label: str             # human-readable, e.g. "Bac+5", "Master 2"
    importance: Importance = Importance.UNKNOWN
    raw_text: str = ""           # original snippet for debugging
    span: EvidenceSpan | None = None  # provenance (char offsets + extractor id)
    is_or_alternative: bool = False   # True → OR structure; skip score penalty
    is_experience_alternative: bool = False  # True → experience substitutes; exempt from hard ceiling
    is_desirable: bool = False        # True → desirability wording; never a hard ceiling
    source: EvidenceSource = EvidenceSource.HEURISTIC
    kind: EducationKind = EducationKind.HELD
    # ENROLLED only: lowest Bac+N year the candidate must currently be in
    # (ranges use their lowest level); None when the stage is not stated.
    enrolment_level_min: int | None = None


@dataclass
class ExperienceRequirement:
    """A minimum years-of-experience requirement extracted from a job description.

    Deterministic-only by design: the LLM extraction prompt does not request
    experience data, and this class never carries a non-HEURISTIC source. The
    regex vocabulary in ``experience.py`` already covers explicit mandatory/
    preferred phrasing in FR/EN with good precision; adding LLM enrichment
    here would introduce the same corroboration-safety surface as education/
    skills for no demonstrated benefit. Revisit only if benchmark evidence
    (see the LLM upgrade benchmark) shows a material recall gap.
    """

    min_years: int
    importance: Importance = Importance.UNKNOWN
    raw_text: str = ""
    span: EvidenceSpan | None = None
    source: EvidenceSource = EvidenceSource.HEURISTIC


@dataclass
class SkillRequirement:
    """A single skill/technology extracted from a job description."""

    skill: str                   # normalized name, e.g. "Rust", "Python"
    importance: Importance = Importance.UNKNOWN
    is_principal: bool = False   # True when detected as the role's core competency
    raw_text: str = ""
    span: EvidenceSpan | None = None
    source: EvidenceSource = EvidenceSource.HEURISTIC


@dataclass
class JobRequirements:
    """All structured requirements extracted from a single job.

    ``extraction_method`` records which signal produced the data:
        "heuristic"  — fast regex/vocabulary analysis (always available)
        "llm"        — local LLM structured extraction
        "combined"   — heuristic baseline enriched by LLM
    """

    education: EducationRequirement | None = None
    experience: ExperienceRequirement | None = None
    skills: list[SkillRequirement] = field(default_factory=list)
    # Technologies detected as the principal/defining competency from the job title.
    # These are already normalized and checked against the user's profile.
    principal_skill_mismatches: list[str] = field(default_factory=list)
    # Technologies that appear in the title via "X ou/or Y" with a known user skill.
    # These are genuine OR alternatives — don't hard-reject, surface as a flag instead.
    title_or_alternatives: list[str] = field(default_factory=list)
    # LLM-claimed principal technologies with NO corroborating text found in the
    # job title/description. Never used for rejection — surfaced as flags only.
    principal_skill_uncorroborated: list[str] = field(default_factory=list)
    occupation_title: str | None = None
    extraction_method: str = "heuristic"


@dataclass
class HardRejectionReason:
    """Machine-readable reason why a job failed the hard constraint gate.

    Stored as a JSON-serialisable dict (via ``to_dict()``) in
    ``RejectionRecord.rejection_detail`` so the UI and tests can inspect it.
    """

    type: str             # "education" | "principal_skill" | "mandatory_skill"
    requirement: str      # what the job requires
    candidate_value: str  # what the candidate has (or "unknown")
    severity: str = "hard"
    message: str = ""
    constraint_state: ConstraintState = ConstraintState.VIOLATED
    span: EvidenceSpan | None = None
    rule_version: str = "hard-filter.v3"
    # Provenance of the evidence that produced this rejection — always HEURISTIC
    # or LLM_CORROBORATED in practice, since LLM_UNCORROBORATED evidence is
    # blocked from reaching a rejection reason by HardConstraintGate.
    evidence_source: EvidenceSource = EvidenceSource.HEURISTIC

    def to_dict(self) -> dict:
        d: dict = {
            "type": self.type,
            "requirement": self.requirement,
            "candidate_value": self.candidate_value,
            "severity": self.severity,
            "message": self.message,
            "constraint_state": self.constraint_state.value,
            "rule_version": self.rule_version,
            "evidence_source": self.evidence_source.value,
        }
        if self.span is not None:
            d["span"] = self.span.to_dict()
        return d


@dataclass
class GateResult:
    """Result of the hard constraint gate for one job.

    Three-valued summary
    --------------------
    passes=True,  uncertainty=False  →  PASS          (all constraints satisfied)
    passes=True,  uncertainty=True   →  PASS_WITH_UNCERTAINTY  (ambiguous/contradicted)
    passes=False, uncertainty=False  →  REJECT         (strong, attributable violation)

    ``passes`` is kept for backward compatibility with existing callers.
    """

    passes: bool
    uncertainty: bool = False          # True when any constraint is UNKNOWN or contradicted
    reasons: list[HardRejectionReason] = field(default_factory=list)
    requirements: JobRequirements | None = None
    # Mandatory skills that are secondary (not principal) — apply score penalty, do not reject.
    score_penalties: list[HardRejectionReason] = field(default_factory=list)
    # Ambiguous mandatory tech — job passes without penalty, surfaced as a flag.
    flags: list[str] = field(default_factory=list)
    # Evidence that conflicts within or across fields (e.g. metadata remote=True
    # but description says "4 days on site"). Tracked for audit; never cause rejection alone.
    contradictions: list[str] = field(default_factory=list)
    # SHA-256 prefix of the profile config used for this evaluation (for audit trail).
    profile_version_hash: str = ""
    # Rule-set version applied (incremented when gate logic changes).
    rule_version: str = "hard-filter.v3"
    # Three-valued state the gate computed per dimension ("education", "experience", "skills").
    # Observability only (decision journal): never read by any gate/scoring decision.
    states: dict[str, ConstraintState] = field(default_factory=dict)

    # Phase LATER / I14: typed gate -> scoring handoff. Previously smuggled
    # through job.raw_payload with underscore-prefixed keys
    # (_education_penalty, _skill_penalty, _experience_penalty,
    # _extracted_min_experience_years), which permanently polluted the
    # persisted raw_payload DB column with gate-computed values that have
    # nothing to do with the collector's original source JSON. gate_jobs()
    # now sets these directly on the GateResult it already returns per job;
    # score_jobs_with_breakdown reads them from there instead.
    education_penalty: float = 0.0
    skill_penalty: float = 0.0
    experience_penalty: float = 0.0
    min_experience_years: int | None = None

    @property
    def pass_with_uncertainty(self) -> bool:
        """True when the job passes but evidence is ambiguous or contradictory."""
        return self.passes and self.uncertainty
