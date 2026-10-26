"""Hard constraint gate — rejects jobs that are objectively incompatible
with the candidate's profile.

RULE: Semantic similarity MUST NOT override a hard constraint.
A job that violates a hard constraint is rejected before scoring,
regardless of how high its semantic or skill score might be.

Hard constraints (VIOLATED → REJECT):
  1. Education stated as a requirement > candidate max (profile.max_education)
     — MANDATORY, or PREFERRED stating the bar ("Bac+4 ou équivalent"), with
     attributable span evidence. Desirable ("Bac+5 souhaité"), OPTIONAL
     ("non éliminatoire") and UNKNOWN mentions never reject — see
     _check_education and ADR 0004.
  2. Principal skill in title not in profile
  3. Mandatory+principal skill in description
  4. Experience MANDATORY > candidate max — with attributable span evidence

Soft mismatches (PASS, apply score penalty):
  - Education CORE or desirable PREFERRED > candidate max → 20% penalty
    (UNKNOWN and OPTIONAL are exempt — see
    _check_education / gate_jobs: an ambiguous mention with no explicit
    qualifier is not evidence of an actual requirement, so it is not
    penalized at all, not even softly)
  - Mandatory secondary skill in description (not principal) → 40% penalty

UNKNOWN / contradicted evidence (PASS_WITH_UNCERTAINTY):
  - Any constraint dimension where evidence is absent, ambiguous, or contradicted
    by other evidence → passes downstream, uncertainty flag set

Flags (pass, no penalty, surface to user):
  - Ambiguous mandatory tech in description (low centrality signal)

Three-valued evaluation
-----------------------
    SATISFIED  — evidence confirms compatibility
    VIOLATED   — strong attributable evidence proves incompatibility
    UNKNOWN    — evidence absent, ambiguous, or contradicted

Aggregate rule:
    if any VIOLATED (non-contradicted): REJECT
    elif any UNKNOWN or contradiction:  PASS_WITH_UNCERTAINTY
    else:                               PASS
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from typing import Callable

from src.requirements.models import (
    ConstraintState,
    EducationKind,
    EducationRequirement,
    EvidenceSource,
    GateResult,
    HardRejectionReason,
    Importance,
    JobRequirements,
)

logger = logging.getLogger(__name__)

_RULE_VERSION = "hard-filter.v3"

# Score penalty for preferred/unknown/core education exceeding candidate max.
_PREFERRED_EDUCATION_PENALTY = 0.20

# Score penalty for a mandatory-but-secondary skill missing from candidate profile.
# Applied multiplicatively on top of the education penalty.
_MANDATORY_SECONDARY_SKILL_PENALTY = 0.40

# Score penalty for preferred experience year mismatches (PREFERRED > profile max).
# Mandatory excess is a hard reject (see _check_experience); preferred is soft only.
_PREFERRED_EXPERIENCE_PENALTY = 0.20


def _norm(text: str) -> str:
    """Lowercase + strip diacritics."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _states_required_level(edu: EducationRequirement) -> bool:
    """True when the posting makes *edu* a requirement (ADR 0004).

    MANDATORY always does. PREFERRED does only when the level is stated as
    the bar and softened by nothing but an equivalence clause ("Bac+4 ou
    équivalent"); a desirability cue ("Bac+5 souhaité", "un plus") is a wish,
    and an LLM-only PREFERRED reading carries no equivalence evidence.
    OPTIONAL ("non éliminatoire"), CORE and UNKNOWN never do.
    """
    if edu.importance == Importance.MANDATORY:
        return True
    return (
        edu.importance == Importance.PREFERRED
        and edu.is_or_alternative
        and not edu.is_desirable
    )


class HardConstraintGate:
    """Applies hard constraint checks to a job before it reaches scoring.

    Args:
        user_max_education_level:  Bac+N integer from the profile (e.g. 2).
        education_hard_constraint: Whether a MANDATORY education requirement
                                   above the candidate's max causes a hard
                                   reject (default True). When False, the
                                   mismatch is treated like a PREFERRED/UNKNOWN
                                   mismatch — it passes with the existing soft
                                   education score penalty instead of being
                                   rejected.
        principal_skill_gate:      Whether to reject on title-based principal
                                   skill mismatches (default True).
        mandatory_skill_gate:      Whether to reject on mandatory+central skill
                                   mismatches extracted by LLM (default True).
        profile_version_hash:      SHA-256 prefix of the profile config used;
                                   stored in every GateResult for audit trail.
    """

    def __init__(
        self,
        user_max_education_level: int | None,
        user_max_years_experience: int | None = None,
        education_hard_constraint: bool = True,
        principal_skill_gate: bool = True,
        mandatory_skill_gate: bool = True,
        profile_version_hash: str = "",
        career_level: dict | None = None,
    ):
        self._user_max_edu = user_max_education_level
        self._user_max_years_exp = user_max_years_experience
        self._education_hard_constraint = education_hard_constraint
        self._principal_gate = principal_skill_gate
        self._mandatory_gate = mandatory_skill_gate
        self._profile_version_hash = profile_version_hash
        # Career-stage check (F5.2, src/requirements/career_level.py): the
        # ``career_level`` slice of build_filter_config; None for a v1 profile.
        self._career = career_level

    def check(
        self,
        requirements: JobRequirements,
        job: object | None = None,
    ) -> GateResult:
        """Check one job's requirements against the user's hard constraints.

        Three-valued evaluation per constraint dimension:
            VIOLATED (non-contradicted) → contributes to rejection
            UNKNOWN / contradicted      → contributes to uncertainty flag
            SATISFIED                   → no action

        Returns GateResult(passes=True) when all constraints are satisfied.
        Returns GateResult(passes=False, reasons=[...]) with structured rejection
        reasons when at least one constraint is VIOLATED.
        Returns GateResult(passes=True, uncertainty=True) when evidence is
        ambiguous or contradictory (PASS_WITH_UNCERTAINTY).
        Soft mismatches (secondary mandatory skills) go into score_penalties.
        Ambiguous signals go into flags.
        """
        reasons: list[HardRejectionReason] = []
        score_penalties: list[HardRejectionReason] = []
        flags: list[str] = []
        contradictions: list[str] = []
        has_unknown = False
        skills_unknown = False      # observability only (GateResult.states)

        # ── 1. Education hard constraint ──────────────────────────────────
        edu_reasons, edu_state, edu_flag = self._check_education(requirements)
        reasons.extend(edu_reasons)
        if edu_flag:
            flags.append(edu_flag)
        if edu_state == ConstraintState.UNKNOWN:
            has_unknown = True

        # ── 2. Experience hard constraint ─────────────────────────────────
        exp_reasons, exp_state = self._check_experience(requirements)
        reasons.extend(exp_reasons)
        if exp_state == ConstraintState.UNKNOWN:
            has_unknown = True

        # ── 3. Principal skill gate (title-based) ─────────────────────────
        if self._principal_gate:
            skill_reasons, or_alt_flags = self._check_principal_skills(requirements)
            reasons.extend(skill_reasons)
            flags.extend(or_alt_flags)

        # ── 4. Description-based skill gate ───────────────────────────────
        # is_principal=True  + MANDATORY → hard reject
        # is_principal=False + MANDATORY → score penalty
        # UNKNOWN + raw_text             → flag only
        if self._mandatory_gate:
            hard, soft, skill_flags = self._check_description_skills(requirements)
            reasons.extend(hard)
            score_penalties.extend(soft)
            flags.extend(skill_flags)
            skills_unknown = bool(skill_flags)

        # ── 5. Career stage (profile v2 targets, F5.2) ────────────────────
        # Needs the job (title, description, contract); a title-only conflict is
        # UNKNOWN (review), never a rejection.
        career_state: ConstraintState | None = None
        if self._career is not None and self._career.get("max_rank") is not None and job is not None:
            career_reasons, career_state, career_message = self._check_career(requirements, job)
            reasons.extend(career_reasons)
            if career_state == ConstraintState.UNKNOWN:
                has_unknown = True
                flags.append(f"Career level: {career_message}")

        skills_state = (
            ConstraintState.VIOLATED if any(r.type in ("principal_skill", "mandatory_skill") for r in reasons)
            else ConstraintState.UNKNOWN if skills_unknown or not (self._principal_gate or self._mandatory_gate)
            else ConstraintState.SATISFIED     # both skill gates disabled → not evaluated → UNKNOWN
        )

        # Aggregate: REJECT only when violations exist AND no unresolved contradiction.
        # PASS_WITH_UNCERTAINTY when violations are contradicted or UNKNOWN evidence present.
        passes = len(reasons) == 0
        uncertainty = has_unknown or bool(contradictions)

        return GateResult(
            passes=passes,
            uncertainty=uncertainty,
            reasons=reasons,
            requirements=requirements,
            score_penalties=score_penalties,
            flags=flags,
            contradictions=contradictions,
            profile_version_hash=self._profile_version_hash,
            rule_version=_RULE_VERSION,
            states={"education": edu_state, "experience": exp_state, "skills": skills_state,
                    **({"seniority": career_state} if career_state is not None else {})},
        )

    def _check_career(
        self, requirements: JobRequirements, job: object
    ) -> tuple[list[HardRejectionReason], ConstraintState, str]:
        from src.requirements.career_level import CAREER_RULE_VERSION, assess_career_level, job_level_signals

        cfg = self._career or {}
        signals = job_level_signals(
            getattr(job, "title", None), getattr(job, "description", None),
            getattr(job, "employment_type", None), requirements.experience, cfg.get("vocabulary") or {},
        )
        assessment = assess_career_level(signals, cfg.get("max_rank"))
        if assessment.state != ConstraintState.VIOLATED:
            return [], assessment.state, assessment.message
        return [HardRejectionReason(
            type="career_level",
            requirement="; ".join(s.evidence for s in assessment.conflicts),
            candidate_value=f"targets {', '.join(cfg.get('levels') or [])}",
            severity="hard",
            constraint_state=ConstraintState.VIOLATED,
            rule_version=CAREER_RULE_VERSION,
            message=assessment.message,
        )], assessment.state, assessment.message

    def _check_education(
        self, requirements: JobRequirements
    ) -> tuple[list[HardRejectionReason], ConstraintState, str | None]:
        """Return (rejection_reasons, constraint_state, flag) for education.

        Three-valued logic:
            edu absent                   → UNKNOWN (no data; never reject)
            edu is LLM_UNCORROBORATED    → UNKNOWN + flag (no textual evidence
                                            for the claim at all; never reject,
                                            never penalize — see module docstring
                                            in extractor.py)
            edu.level ≤ user max         → SATISFIED
            edu.level > user max
              + MANDATORY, or PREFERRED stating the bar (_states_required_level)
                + education_hard_constraint → VIOLATED (hard reject)
                + hard constraint disabled  → SATISFIED from gate perspective
                                              (soft penalty handled by gate_jobs,
                                              same as CORE)
              + CORE                          → SATISFIED from gate perspective
                                               (soft penalty handled by gate_jobs)
              + UNKNOWN                       → UNKNOWN (pass with uncertainty;
                                               2026-09-11: NO penalty either —
                                               see gate_jobs; an ambiguous
                                               mention with no explicit
                                               qualifier is not evidence of a
                                               real requirement, nor of its
                                               absence: "Profil recherché :
                                               Un Master …" is recorded as
                                               uncertain, never rejected)

        Why some PREFERRED readings reject (not "mandatory only"): the
        profile's `max_education` is a ceiling on *requirements*. "Bac+4 ou
        équivalent" is PREFERRED-classified by extract_education() because of
        the "ou équivalent" phrasing, but it states the bar and must reject.
        A desirability cue ("Bac+5 souhaité", "apprécié", "un plus") is a
        wish, not a requirement: soft penalty only (ADR 0004, amended
        2026-10 after the bm46 unsafe rejection). The one exception is `is_experience_alternative` (e.g.
        "Bac+5 ou expérience équivalente") — a genuinely different, explicit
        substitute path (professional experience instead of the degree), not
        just softer wording of the same requirement — which is exempt from
        the hard ceiling entirely, same as before this change.
        UNKNOWN is deliberately excluded from the hard reject AND (as of
        2026-09-11) from the soft penalty: it is reserved for genuinely
        ambiguous mentions with no explicit qualifier — including the "vous
        préparez un Bac+N" / "en cours de préparation" student-targeting
        phrasing (see extractor tests), which describes a candidate
        currently PURSUING a diploma, not one already holding it — so it
        must not be treated as an explicit requirement, not even a soft one.
        CORE is excluded from the hard reject the same way (LLM-only value,
        never produced by the heuristic path) but still receives the soft
        penalty via gate_jobs, since it represents an actual (if
        LLM-sourced) requirement claim rather than a genuine absence of one.
        OPTIONAL importance marks an explicitly non-eliminatory mention
        ("Bac+5 apprécié, non éliminatoire", "not required"): never a reject,
        never a penalty.
        """
        edu = requirements.education
        if edu is None:
            # No education requirement stated — constraint is absent, not ambiguous.
            return [], ConstraintState.SATISFIED, None

        if edu.source == EvidenceSource.LLM_UNCORROBORATED:
            # The LLM claimed an education requirement but no matching text
            # exists in the job posting. Per the evidence-authority rule, an
            # unsupported LLM inference can never independently produce a
            # VIOLATED state (nor a soft penalty, see gate_jobs) — it is
            # evidence-free and must default to UNKNOWN.
            flag = (
                f"LLM claimed education requirement '{edu.level_label}' "
                f"({edu.importance.value}) with no supporting text in the job "
                f"posting — discarded as unsupported, not used for rejection."
            )
            return [], ConstraintState.UNKNOWN, flag

        if self._user_max_edu is None:
            # Cannot evaluate without a profile maximum — treat as UNKNOWN (don't reject).
            return [], ConstraintState.UNKNOWN, None

        # Issue #40: profile.max_education is the highest level HELD. An
        # enrolment requirement asks where the candidate studies now, and
        # preparing a diploma is not holding it (Job 39).
        if edu.kind == EducationKind.ENROLLED:
            return self._check_enrolment(edu)
        if edu.kind == EducationKind.PREPARING and edu.importance != Importance.MANDATORY:
            return [], ConstraintState.SATISFIED, None

        if edu.level_bac_plus <= self._user_max_edu:
            return [], ConstraintState.SATISFIED, None

        candidate_label = f"bac+{self._user_max_edu}"
        required_label = edu.level_label or f"bac+{edu.level_bac_plus}"
        # The message names why the level counts as required: a PREFERRED
        # reading only rejects as a stated bar ("Bac+5 ou équivalent"), and
        # calling that "preferred" would contradict the decision it explains.
        stated_as = (
            "mandatory" if edu.importance == Importance.MANDATORY
            else "stated level, equivalence accepted"
        )

        if (
            _states_required_level(edu)
            and self._education_hard_constraint
            and not edu.is_experience_alternative
        ):
            reason = HardRejectionReason(
                type="education",
                requirement=required_label,
                candidate_value=candidate_label,
                severity="hard",
                constraint_state=ConstraintState.VIOLATED,
                rule_version=_RULE_VERSION,
                message=(
                    f"Job requires {required_label} ({stated_as}); "
                    f"candidate maximum is {candidate_label}."
                ),
                span=edu.span,
                evidence_source=edu.source,
            )
            return [reason], ConstraintState.VIOLATED, None

        # No qualifier at all: whether the level is required is not known.
        # Recorded as such (PASS_WITH_UNCERTAINTY), never a reject or penalty.
        if edu.importance == Importance.UNKNOWN:
            return [], ConstraintState.UNKNOWN, None

        # Desirable PREFERRED, OPTIONAL, CORE, or a requirement with the hard
        # constraint disabled → at most a soft penalty (gate_jobs).
        return [], ConstraintState.SATISFIED, None

    def _check_enrolment(
        self, edu: EducationRequirement
    ) -> tuple[list[HardRejectionReason], ConstraintState, str | None]:
        """ENROLLED requirement: the candidate must currently be in year
        ``enrolment_level_min`` (a range starts at its lowest level).

        Being in year E requires having completed E-1, so only a candidate
        whose held level makes that impossible (max_education < E-1) is
        rejected. An unknown stage ("Étudiant(e) en école d'ingénieur") is
        never a reason to reject. No soft penalty either way.
        """
        assert self._user_max_edu is not None  # checked by the caller
        required = edu.enrolment_level_min
        if required is None:
            return [], ConstraintState.UNKNOWN, None
        if self._user_max_edu >= required - 1 or not self._education_hard_constraint:
            return [], ConstraintState.SATISFIED, None
        candidate_label = f"bac+{self._user_max_edu}"
        reason = HardRejectionReason(
            type="education",
            requirement=f"enrolled in bac+{required}",
            candidate_value=candidate_label,
            severity="hard",
            constraint_state=ConstraintState.VIOLATED,
            rule_version=_RULE_VERSION,
            message=(
                f"Job targets students currently in bac+{required} ({edu.level_label}); "
                f"candidate holds {candidate_label}, so cannot be enrolled at that level yet."
            ),
            span=edu.span,
            evidence_source=edu.source,
        )
        return [reason], ConstraintState.VIOLATED, None

    def _check_experience(
        self, requirements: JobRequirements
    ) -> tuple[list[HardRejectionReason], ConstraintState]:
        """Return (rejection_reasons, constraint_state) for experience.

        Three-valued logic:
            exp absent                   → SATISFIED (no requirement stated)
            user max absent              → UNKNOWN (can't evaluate; never reject)
            exp.min_years ≤ user max     → SATISFIED
            exp.min_years > user max
              + MANDATORY importance     → VIOLATED (hard reject)
              + PREFERRED                → SATISFIED from gate perspective
                                           (soft penalty handled by gate_jobs)
        """
        exp = requirements.experience
        if exp is None:
            return [], ConstraintState.SATISFIED

        if self._user_max_years_exp is None:
            return [], ConstraintState.UNKNOWN

        if exp.min_years <= self._user_max_years_exp:
            return [], ConstraintState.SATISFIED

        if exp.importance == Importance.MANDATORY:
            reason = HardRejectionReason(
                type="experience",
                requirement=f"{exp.min_years}+ years",
                candidate_value=f"max {self._user_max_years_exp} years",
                severity="hard",
                constraint_state=ConstraintState.VIOLATED,
                rule_version=_RULE_VERSION,
                message=(
                    f"Job requires at least {exp.min_years} years experience (mandatory); "
                    f"candidate maximum is {self._user_max_years_exp} years."
                ),
                span=exp.span,
                evidence_source=exp.source,
            )
            return [reason], ConstraintState.VIOLATED

        # PREFERRED excess → soft penalty only (handled by gate_jobs).
        return [], ConstraintState.SATISFIED

    def _check_principal_skills(
        self, requirements: JobRequirements
    ) -> tuple[list[HardRejectionReason], list[str]]:
        """Return (rejection_reasons, flags) for principal skill checks.

        ``requirements.principal_skill_mismatches`` only ever contains
        heuristic findings (always attributable to the title) and
        LLM-corroborated ones (already confirmed to appear in the actual job
        text by RequirementExtractor's corroboration step) — so every entry
        here is safe to hard-reject on. Uncorroborated LLM claims are routed
        separately and only ever become flags, never rejection reasons.
        """
        reasons: list[HardRejectionReason] = []
        for tech in requirements.principal_skill_mismatches:
            reasons.append(HardRejectionReason(
                type="principal_skill",
                requirement=tech,
                candidate_value="not in profile",
                severity="hard",
                constraint_state=ConstraintState.VIOLATED,
                rule_version=_RULE_VERSION,
                message=(
                    f"Job title centers on '{tech}' which is not in the candidate's profile."
                ),
            ))
        flags = [
            f"OR-alternative tech in title (not rejected): {tech}"
            for tech in getattr(requirements, "title_or_alternatives", [])
        ]
        flags.extend(
            f"LLM claimed principal technology '{tech}' with no supporting text "
            "in the job posting — discarded as unsupported, not used for rejection."
            for tech in getattr(requirements, "principal_skill_uncorroborated", [])
        )
        return reasons, flags

    def _check_description_skills(
        self, requirements: JobRequirements
    ) -> tuple[list[HardRejectionReason], list[HardRejectionReason], list[str]]:
        """Classify missing skills from description into hard/soft/flag buckets.

        Returns:
            (hard_reasons, soft_penalties, flags)
            hard_reasons   — is_principal=True, MANDATORY → reject
            soft_penalties — is_principal=False, MANDATORY → score penalty
            flags          — importance=UNKNOWN with a raw_text snippet, or an
                             LLM_UNCORROBORATED mandatory claim → surface only
        """
        hard: list[HardRejectionReason] = []
        soft: list[HardRejectionReason] = []
        flags: list[str] = []

        for skill_req in requirements.skills:
            if skill_req.source == EvidenceSource.LLM_UNCORROBORATED:
                # No textual evidence for this claim — never reject, never
                # penalize. Surface it for audit only.
                flags.append(
                    f"LLM claimed skill '{skill_req.skill}' ({skill_req.importance.value}) "
                    "with no supporting text in the job posting — discarded as unsupported."
                )
                continue
            if skill_req.importance == Importance.MANDATORY:
                if skill_req.is_principal:
                    hard.append(HardRejectionReason(
                        type="mandatory_skill",
                        requirement=skill_req.skill,
                        candidate_value="not in profile",
                        severity="hard",
                        constraint_state=ConstraintState.VIOLATED,
                        rule_version=_RULE_VERSION,
                        span=skill_req.span,
                        evidence_source=skill_req.source,
                        message=(
                            f"Job centers on '{skill_req.skill}' (mandatory, principal) — "
                            "not in candidate profile."
                        ),
                    ))
                else:
                    soft.append(HardRejectionReason(
                        type="mandatory_secondary_skill",
                        requirement=skill_req.skill,
                        candidate_value="not in profile",
                        severity="soft",
                        constraint_state=ConstraintState.VIOLATED,
                        rule_version=_RULE_VERSION,
                        span=skill_req.span,
                        evidence_source=skill_req.source,
                        message=(
                            f"Job requires '{skill_req.skill}' (mandatory secondary) — "
                            "not in candidate profile."
                        ),
                    ))
            elif skill_req.importance == Importance.UNKNOWN and skill_req.raw_text:
                flags.append(f"Missing mandatory?: {skill_req.skill}")

        return hard, soft, flags


def _hash_profile(profile_data: str) -> str:
    """Return a 12-char SHA-256 prefix of profile data for audit trail."""
    return hashlib.sha256(profile_data.encode()).hexdigest()[:12]


def gate_jobs(
    jobs: "list",                 # list[Job]
    requirements_map: dict,       # job.id → JobRequirements
    gate: HardConstraintGate,
    education_preferred_penalty: float = _PREFERRED_EDUCATION_PENALTY,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple["list", dict[str, "GateResult"]]:
    """Apply hard constraints to all jobs.

    Args:
        progress_callback: Optional ``(processed, total)`` callback invoked
            after each job — purely observational, never affects which jobs
            pass or the reasons recorded.

    Returns:
        (passing_jobs, gate_results_map) where:
            passing_jobs     — jobs that passed all hard constraints
            gate_results_map — job.id → GateResult for ALL jobs (pass and reject).
                For a passing job, this also carries any soft penalty
                (education/skill/experience) and the raw extracted
                min-experience-years the scorer needs — see GateResult's own
                docstring (Phase LATER / I14: no longer injected into
                job.raw_payload).
    """
    passing: list = []
    results: dict[str, GateResult] = {}
    total = len(jobs)

    for i, job in enumerate(jobs, start=1):
        requirements = requirements_map.get(job.id, JobRequirements())
        result = gate.check(requirements, job)
        results[job.id] = result

        if result.passes:
            # Apply soft education penalty for education mismatches that didn't
            # hard-reject. CORE always lands here (never hard-rejected by
            # _check_education — it's an LLM-only value, treated cautiously).
            # Desirable PREFERRED ("Bac+5 souhaité") always lands here.
            # MANDATORY and PREFERRED stating the bar land here only when
            # education_hard_constraint is disabled on the gate — otherwise
            # _check_education would have rejected them outright (ADR 0004).
            # OPTIONAL ("non éliminatoire") is never penalised.
            #
            # UNKNOWN is deliberately EXCLUDED here (2026-09-11 audit fix):
            # it is reserved for genuinely ambiguous mentions with no explicit
            # qualifier (e.g. "Formation Bac+5 en informatique" with no
            # "requis"/"souhaité"/etc.) — the absence of a requirement signal
            # is not itself evidence of a real mismatch, so it must not be
            # softly penalized either. A live audit (2026-09-11) found this
            # was firing on 58% of all accepted jobs that scored below the
            # "relevant" tier, most of which had no other sign of being a
            # poor match — this single condition was responsible for the
            # single largest share of that.
            #
            # OR-alternatives ("Bac+5 ou expérience équivalente") are exempt:
            # the candidate may satisfy the alternative condition, so no penalty.
            # (MANDATORY requirements are never tagged is_or_alternative — see
            # education.py's `_extract_importance` — so this exemption only
            # actually applies to the PREFERRED case, as before.)
            edu = requirements.education
            gate_user_max = gate._user_max_edu
            if (
                edu is not None
                and edu.source != EvidenceSource.LLM_UNCORROBORATED
                and gate_user_max is not None
                and edu.kind == EducationKind.HELD
                and edu.level_bac_plus > gate_user_max
                and edu.importance in (
                    Importance.PREFERRED, Importance.CORE, Importance.MANDATORY
                )
                and not edu.is_or_alternative
                and education_preferred_penalty > 0
            ):
                result.education_penalty = education_preferred_penalty

            # Apply skill penalty for mandatory secondary mismatches.
            if result.score_penalties:
                result.skill_penalty = _MANDATORY_SECONDARY_SKILL_PENALTY

            # Apply soft penalty for PREFERRED experience exceeding candidate max.
            # MANDATORY excess is a hard reject from gate.check(); it never reaches here.
            exp = requirements.experience
            gate_max_years = gate._user_max_years_exp
            if (
                exp is not None
                and gate_max_years is not None
                and exp.min_years > gate_max_years
                and exp.importance == Importance.PREFERRED
            ):
                result.experience_penalty = _PREFERRED_EXPERIENCE_PENALTY

            # Surface the raw extracted years-of-experience requirement (regardless
            # of importance/penalty status) so the scorer's experience-fit curve
            # can read it, via the SAME GateResult already returned per job —
            # see GateResult's own docstring (Phase LATER / I14) for why this
            # replaced the former job.raw_payload side-channel.
            if exp is not None:
                result.min_experience_years = exp.min_years

            passing.append(job)

        if progress_callback is not None:
            progress_callback(i, total)

    logger.info(
        "Requirements gate: %d passed, %d rejected",
        len(passing),
        len(jobs) - len(passing),
    )

    # Observability: when LLM enrichment is enabled, make its impact visible
    # without logging job content. See extractor.py's per-claim logger.info
    # calls for corroborated/discarded detail at DEBUG-adjacent granularity.
    combined = sum(1 for r in requirements_map.values() if r.extraction_method == "combined")
    if combined:
        discarded_flags = sum(
            1 for res in results.values() for f in res.flags if "discarded as unsupported" in f
        )
        logger.info(
            "Requirements gate LLM: %d/%d jobs had LLM-enriched requirements "
            "(extraction_method=combined); %d unsupported LLM claim(s) discarded "
            "across all jobs (never used for rejection/penalty).",
            combined, len(jobs), discarded_flags,
        )

    return passing, results


def rejection_reasons_to_detail(reasons: list[HardRejectionReason]) -> str:
    """Serialize a list of rejection reasons to a JSON string for storage."""
    return json.dumps([r.to_dict() for r in reasons])
