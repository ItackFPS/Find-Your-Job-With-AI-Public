"""Job explanation generator.

Takes a job, user profile, and filter result → structured human-readable reasons.

Design goals
------------
- All reasons are grounded in actual job data and configured profile values.
- Unknown / missing data is NOT treated as a negative signal.
- Rejection reasons are specific and actionable.
- Accepted-job concerns are separate from blocking reasons.
- Cross-language role matching: French and English role names are equivalent.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from src.models.job import Job
from src.models.profile import JobSearchProfile


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ExplanationReason:
    emoji: str
    text: str
    kind: str  # 'positive' | 'concern' | 'rejection'


@dataclass
class ExplanationResult:
    positive_reasons: list[ExplanationReason] = field(default_factory=list)
    concerns: list[ExplanationReason] = field(default_factory=list)
    rejection_reasons: list[ExplanationReason] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Keyword sets for heuristics (no external dependencies)
# ---------------------------------------------------------------------------

_JUNIOR_WORDS = {
    "junior", "entry", "entry-level", "entry level",
    "intern", "internship", "stage", "alternance", "apprenti",
    "graduate", "débutant", "debutant", "trainee",
}

_SENIOR_WORDS = {
    "senior", "lead", "principal", "staff", "director",
    "vp", "vice president", "head of", "chef de", "c-level",
    "cto", "cpo", "ceo", "coo", "manager",
}

_FRENCH_TITLE_WORDS = {
    "développeur", "developpeur", "développeuse", "developpeuse",
    "ingénieur", "ingenieur", "informatique", "logiciel",
    "technicien", "analyste", "concepteur",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    """Lowercase + strip diacritics for accent-insensitive matching."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _contains_any(haystack: str, needles: set[str]) -> str | None:
    """Return the first matching needle found in haystack, or None."""
    for needle in needles:
        if needle in haystack:
            return needle
    return None


def _title_matched_domain(
    title: str,
    domain_keywords: dict[str, Any],
    target_domains: list[str],
) -> str | None:
    """Return the first domain whose title keywords match the job title, else None."""
    title_norm = _norm(title)
    for domain in target_domains:
        kws = domain_keywords.get(domain, {})
        for kw in kws.get("title", []):
            if _norm(kw) in title_norm:
                return domain
    return None


def _detect_posting_language(job: Job) -> str | None:
    """Heuristic: detect whether the job posting is in English or French."""
    if job.source in ("france-travail", "france_travail"):
        return "French"
    title = job.title or ""
    title_norm = _norm(title)
    if any(w in title_norm for w in _FRENCH_TITLE_WORDS):
        return "French"
    # High ASCII ratio → almost certainly English (or at least not French)
    if title and sum(c.isascii() for c in title) / len(title) > 0.88:
        return "English"
    return None


def _matched_skills(job: Job, target_skills: list[str]) -> list[str]:
    """Return target skills that appear in the job title or first 3000 chars of description."""
    text = _norm(f"{job.title or ''} {(job.description or '')[:3000]}")
    return [s for s in target_skills if _norm(s) in text]


def _detect_required_unknown_language(job: Job, accepted: list[str]) -> str | None:
    """Detect if the description explicitly requires a language not in the accepted list."""
    desc = (job.description or "").lower()[:3000]
    candidates = {
        "German": ["german required", "german mandatory", "german fluent",
                   "fließend deutsch", "deutsch erforderlich"],
        "Dutch": ["dutch required", "dutch mandatory", "dutch fluent"],
        "Spanish": ["spanish required", "spanish mandatory"],
        "Italian": ["italian required", "italian mandatory"],
    }
    accepted_lower = {a.lower() for a in accepted}
    for lang, patterns in candidates.items():
        if lang.lower() not in accepted_lower:
            if any(p in desc for p in patterns):
                return lang
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_explanation(
    job: Job,
    profile: JobSearchProfile,
    filter_result: str,
    rejection_detail: str | None = None,
    domain_keywords: dict[str, Any] | None = None,
    score_factors: dict[str, float] | None = None,
) -> ExplanationResult:
    """Generate a structured human-readable explanation for a job decision.

    Args:
        job:              The job being explained.
        profile:          The user's search profile (from config/profile.yaml).
        filter_result:    ``'passed'`` or ``'rejected'``.
        rejection_detail: The filter check that failed (``'location'``,
                          ``'seniority'``, ``'title_keywords'``, …).
                          Required when ``filter_result == 'rejected'``.
        domain_keywords:  Keyword vocabulary loaded from ``config/filters.yaml``.
                          Used to produce domain-specific role-match labels.
        score_factors:    Per-factor scores from the deterministic scorer
                          (optional, reserved for future richer explanations).

    Returns:
        An :class:`ExplanationResult` with positive reasons, concerns, and/or
        rejection reasons.
    """
    if filter_result == "rejected":
        return _explain_rejection(job, profile, rejection_detail)
    return _explain_acceptance(job, profile, domain_keywords or {}, score_factors)


# ---------------------------------------------------------------------------
# Explanation derived from the decision journal (single canonical decision)
# ---------------------------------------------------------------------------

_FILTER_LABELS = {
    "location": "location", "remote": "remote policy", "work_mode": "work mode", "seniority": "seniority",
    "published_age": "posting age", "salary": "salary", "title_keywords": "role / domain keywords",
    "contract_type": "contract type",
}
_FACTOR_LABELS = {
    "location_fit": "Location fit", "domain_relevance": "Domain relevance", "skills_match": "Skills match",
    "seniority_match": "Seniority match", "experience_fit": "Experience fit",
}


def _evidence_text(e: Any) -> str:
    """One human-readable sentence for an ``EvidenceItem`` (a pure rendering of the record)."""
    rule = e.source.rule_id
    if rule.startswith("score.") and e.weight is not None and e.observed is not None:
        label = _FACTOR_LABELS.get(rule.removeprefix("score."), rule)
        try:
            return f"{label}: {round(float(e.observed) * 100)}% (counts for {round(e.weight * 100)}% of the score)"
        except (ValueError, TypeError):
            return str(e.message)
    text: str = str(e.message)
    if rule.startswith("filter.") and e.state == "satisfied":
        text = f"Passes the {_FILTER_LABELS.get(rule.removeprefix('filter.'), rule)} check"
    if e.span is not None and e.span.text.strip():
        quote = e.span.text.strip()
        if len(quote) > _MAX_EVIDENCE_QUOTE_LEN:
            quote = quote[: _MAX_EVIDENCE_QUOTE_LEN - 1].rstrip() + "…"
        text = f'{text} (posting says: "{quote}")'
    return text


def explain_from_decision(decision: Any) -> ExplanationResult:
    """Render a ``DecisionRecord`` as the legacy explanation shape: no heuristics of its own, no job
    re-analysis — every line comes from the journaled evidence, so the explanation cannot disagree
    with the decision it explains.

    Rejected: every violated evidence item as a rejection reason. Otherwise (accepted / review):
    supporting evidence as positives; against, UNKNOWN and conflicts as concerns. Repetitive
    hard-filter lines are folded into one ("Passes N hard-filter checks: …", "No data for …")."""
    result = ExplanationResult()
    if decision.outcome == "rejected":
        for e in decision.evidence:
            if e.state == "violated":
                result.rejection_reasons.append(ExplanationReason("🔴", _evidence_text(e), "rejection"))
        if not result.rejection_reasons:
            result.rejection_reasons.append(ExplanationReason(
                "🔴", f"Rejected at {decision.deciding_stage}", "rejection"))
        return result
    passed_filters: list[str] = []
    unknown_filters: list[str] = []
    for e in decision.evidence:
        rule = e.source.rule_id
        if rule.startswith("filter.") and e.polarity != "negative":
            label = _FILTER_LABELS.get(rule.removeprefix("filter."), rule)
            (passed_filters if e.polarity == "positive" else unknown_filters).append(label)
        elif e.polarity == "positive":
            result.positive_reasons.append(ExplanationReason("🟢", _evidence_text(e), "positive"))
        elif e.polarity == "negative":
            result.concerns.append(ExplanationReason("🔴", _evidence_text(e), "concern"))
        else:
            result.concerns.append(ExplanationReason("🟡", f"Unknown: {_evidence_text(e)}", "concern"))
    if passed_filters:
        result.positive_reasons.insert(0, ExplanationReason(
            "🟢", f"Passes {len(passed_filters)} hard-filter check(s): {', '.join(passed_filters)}", "positive"))
    if unknown_filters:
        result.concerns.append(ExplanationReason(
            "🟡", f"No data for {', '.join(unknown_filters)} (the hard filter accepts missing data by default)", "concern"))
    for c in decision.conflicts:
        result.concerns.append(ExplanationReason("🟡", f"Conflict: {c.description}", "concern"))
    if decision.outcome == "review":
        result.concerns.insert(0, ExplanationReason("🟡", "This job is flagged for review: the signals disagree", "concern"))
    return result


# ---------------------------------------------------------------------------
# Requirements-gate structured evidence (see src.requirements.gate /
# src.requirements.models.HardRejectionReason.to_dict — the JSON format
# stored in RejectionRecord.rejection_detail for the "requirements_gate"
# rejection stage, as opposed to the short plain-string category codes the
# older "filter" stage stores, e.g. "location"/"seniority").
# ---------------------------------------------------------------------------

_MAX_EVIDENCE_QUOTE_LEN = 160


def _try_parse_gate_evidence(rejection_detail: str) -> list[dict[str, Any]] | None:
    """Parse ``rejection_detail`` as the requirements gate's structured JSON
    evidence format, if that's what it is.

    Returns the list of usable reason dicts (each with at least a non-empty
    ``message``), or ``None`` when ``rejection_detail`` is not gate evidence
    at all (a legacy plain-string category, handled by the caller's existing
    branches) or is gate-JSON-shaped but malformed/empty (caller falls back
    to a safe generic message — never re-raises, never returns the raw text).
    """
    stripped = rejection_detail.strip()
    if not stripped.startswith("["):
        return None  # legacy plain-string category (e.g. "location") — not JSON
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None  # malformed JSON — caller uses a safe generic fallback
    if not isinstance(parsed, list):
        return None
    valid_reasons = [
        item for item in parsed
        if isinstance(item, dict) and isinstance(item.get("message"), str) and item["message"].strip()
    ]
    return valid_reasons or None


def _explain_gate_reasons(reasons: list[dict[str, Any]]) -> list[ExplanationReason]:
    """Render the requirements gate's structured evidence as human-readable
    rejection reasons — one per gate finding, in the order the gate produced
    them. ``message`` is already a plain-language sentence (see
    HardRejectionReason construction in src/requirements/gate.py); the
    matched job-posting text is appended as a short quote when present,
    since it's the concrete evidence a user can verify against the posting.
    """
    out: list[ExplanationReason] = []
    for item in reasons:
        text = item["message"].strip()
        span = item.get("span")
        if isinstance(span, dict):
            quote = span.get("text")
            if isinstance(quote, str) and quote.strip():
                quote = quote.strip()
                if len(quote) > _MAX_EVIDENCE_QUOTE_LEN:
                    quote = quote[: _MAX_EVIDENCE_QUOTE_LEN - 1].rstrip() + "…"
                text = f'{text} (posting says: "{quote}")'
        out.append(ExplanationReason(emoji="🔴", text=text, kind="rejection"))
    return out


# ---------------------------------------------------------------------------
# Rejection explanations
# ---------------------------------------------------------------------------

def _explain_rejection(
    job: Job,
    profile: JobSearchProfile,
    rejection_detail: str | None,
) -> ExplanationResult:
    blocking: list[ExplanationReason] = []
    positive: list[ExplanationReason] = []

    if rejection_detail:
        gate_reasons = _try_parse_gate_evidence(rejection_detail)
        if gate_reasons is not None:
            return ExplanationResult(rejection_reasons=_explain_gate_reasons(gate_reasons))

    if rejection_detail == "location":
        preferred = (
            ", ".join(profile.geography.preferred_countries)
            if profile.geography.preferred_countries
            else "your preferred locations"
        )
        preferred_lower = {c.lower() for c in profile.geography.preferred_countries}
        job_countries = job.countries or []
        job_countries_lower = [c.lower() for c in job_countries]
        job_loc = job.location or ""
        is_confirmed_foreign = bool(job_countries_lower) and not any(
            c in preferred_lower for c in job_countries_lower
        )

        if is_confirmed_foreign and not job.remote:
            # On-site / hybrid outside preferred country.
            display = ", ".join(job_countries) if job_countries else job_loc
            blocking.append(ExplanationReason(
                emoji="🔴",
                text=(
                    f"On-site or hybrid position in {display} — physical presence "
                    f"abroad is required and the role is outside {preferred}"
                ),
                kind="rejection",
            ))
        elif is_confirmed_foreign and job.remote:
            # Remote but scope not confirmed as France-accessible.
            display = ", ".join(job_countries) if job_countries else job_loc
            blocking.append(ExplanationReason(
                emoji="🔴",
                text=(
                    f"Position based in {display} — remote eligibility from "
                    f"{preferred} is not confirmed (no EU-wide or worldwide scope "
                    "was indicated)"
                ),
                kind="rejection",
            ))
        elif job_loc:
            blocking.append(ExplanationReason(
                emoji="🔴",
                text=(
                    f"Location '{job_loc}' does not match your preferred "
                    f"locations ({preferred})"
                ),
                kind="rejection",
            ))
        else:
            blocking.append(ExplanationReason(
                emoji="🔴",
                text=f"Location does not match your preferred locations ({preferred})",
                kind="rejection",
            ))

        # For location rejections, surface what matched positively so the user
        # understands the job was otherwise relevant.
        _append_positive_highlights(job, profile, positive)

    elif rejection_detail == "seniority":
        title_lower = (job.title or "").lower()
        matched_word = _contains_any(title_lower, _SENIOR_WORDS)
        label = matched_word.capitalize() if matched_word else "Senior/executive"
        blocking.append(ExplanationReason(
            emoji="🔴",
            text=f"'{label}' role — excluded by your seniority preferences",
            kind="rejection",
        ))

    elif rejection_detail == "title_keywords":
        domains_display = (
            ", ".join(d.replace("_", " ") for d in profile.target_domains[:3])
            if profile.target_domains
            else "your target domains"
        )
        blocking.append(ExplanationReason(
            emoji="🔴",
            text=f"Role does not match your configured target domains ({domains_display})",
            kind="rejection",
        ))

    elif rejection_detail == "published_age":
        days = profile.freshness_days
        blocking.append(ExplanationReason(
            emoji="🔴",
            text=f"Posted more than {days} days ago — outside your freshness window",
            kind="rejection",
        ))

    elif rejection_detail == "salary":
        blocking.append(ExplanationReason(
            emoji="🔴",
            text="Salary does not meet your configured requirements",
            kind="rejection",
        ))

    elif rejection_detail in ("remote", "work_mode"):
        blocking.append(ExplanationReason(
            emoji="🔴",
            text="Work mode does not match your configured requirements",
            kind="rejection",
        ))

    elif rejection_detail and rejection_detail.strip().startswith(("[", "{")):
        # JSON-shaped (gate evidence) but _try_parse_gate_evidence above
        # couldn't extract any usable reason from it (malformed JSON, or an
        # empty/fieldless list) — never surface the raw JSON text itself.
        blocking.append(ExplanationReason(
            emoji="🔴",
            text="Did not meet one or more of your configured requirements.",
            kind="rejection",
        ))

    else:
        blocking.append(ExplanationReason(
            emoji="🔴",
            text=(
                f"Did not pass filter: {rejection_detail}"
                if rejection_detail
                else "Unable to determine the specific rejection reason"
            ),
            kind="rejection",
        ))

    return ExplanationResult(rejection_reasons=blocking, positive_reasons=positive)


# ---------------------------------------------------------------------------
# Helpers shared between rejection and acceptance explanations
# ---------------------------------------------------------------------------

def _append_positive_highlights(
    job: Job,
    profile: JobSearchProfile,
    reasons: list[ExplanationReason],
) -> None:
    """Append brief positive signals to a reasons list (used in rejection context).

    Surfaces role match, seniority, language, and skill signals so the user
    can see the job was otherwise relevant despite the blocking reason.
    """
    title_norm = _norm(job.title or "")

    # Seniority / level
    matched_junior = _contains_any(title_norm, _JUNIOR_WORDS)
    if matched_junior:
        reasons.append(ExplanationReason(
            emoji="🟢",
            text=f"{matched_junior.replace('-', ' ').capitalize()} level — matches your experience target",
            kind="positive",
        ))

    # Language
    detected_lang = _detect_posting_language(job)
    if detected_lang and detected_lang in profile.languages:
        reasons.append(ExplanationReason(
            emoji="🟢",
            text=f"{detected_lang} posting — one of your accepted languages",
            kind="positive",
        ))

    # Skills
    matched = _matched_skills(job, profile.target_skills)
    if matched:
        skills_display = ", ".join(matched[:3])
        reasons.append(ExplanationReason(
            emoji="🟢",
            text=f"Mentions relevant skills: {skills_display}",
            kind="positive",
        ))


# ---------------------------------------------------------------------------
# Acceptance explanations
# ---------------------------------------------------------------------------

def _explain_acceptance(
    job: Job,
    profile: JobSearchProfile,
    domain_keywords: dict[str, Any],
    score_factors: dict[str, float] | None,
) -> ExplanationResult:
    positive: list[ExplanationReason] = []
    concerns: list[ExplanationReason] = []

    # ---- Role / domain match ------------------------------------------------
    matched_domain = _title_matched_domain(
        job.title or "", domain_keywords, profile.target_domains
    )
    if matched_domain:
        domain_display = matched_domain.replace("_", " ").title()
        positive.append(ExplanationReason(
            emoji="🟢",
            text=f"{job.title or 'Role'} — matches your target domain: {domain_display}",
            kind="positive",
        ))
    elif job.title:
        # Matched via description fallback — still a positive signal.
        positive.append(ExplanationReason(
            emoji="🟢",
            text=f"{job.title} — passed IT relevance check",
            kind="positive",
        ))

    # ---- Seniority ----------------------------------------------------------
    title_lower = _norm(job.title or "")
    matched_junior = _contains_any(title_lower, _JUNIOR_WORDS)
    matched_senior = _contains_any(title_lower, _SENIOR_WORDS)

    if matched_junior:
        level_display = matched_junior.replace("-", " ").capitalize()
        positive.append(ExplanationReason(
            emoji="🟢",
            text=f"{level_display} role — matches your experience level",
            kind="positive",
        ))
    elif not matched_senior and job.title:
        # Check if the description has an explicit experience-year requirement.
        try:
            from src.requirements.experience import extract_experience
            from src.requirements.models import Importance as _Importance
            _exp = extract_experience(job.description or "")
            _max_years = getattr(getattr(profile, "experience", None), "max_years", None)
        except Exception:
            _exp = None
            _max_years = None

        if (
            _exp is not None
            and _exp.importance == _Importance.MANDATORY
            and _max_years is not None
            and _exp.min_years > _max_years
        ):
            concerns.append(ExplanationReason(
                emoji="🔴",
                text=(
                    f"Requires {_exp.min_years}+ years of experience "
                    f"(explicitly stated in description) — above your {_max_years}-year maximum"
                ),
                kind="concern",
            ))
        elif _exp is not None and _exp.importance == _Importance.MANDATORY:
            concerns.append(ExplanationReason(
                emoji="🟡",
                text=f"Requires {_exp.min_years}+ years of experience (explicitly stated in description) — verify fit",
                kind="concern",
            ))
        else:
            concerns.append(ExplanationReason(
                emoji="🟡",
                text="Experience level not explicitly stated in the title — verify requirements",
                kind="concern",
            ))

    # ---- Remote / work mode -------------------------------------------------
    if job.remote:
        if "remote" in profile.work_modes:
            positive.append(ExplanationReason(
                emoji="🟢",
                text="Fully remote — matches your remote work preference",
                kind="positive",
            ))
        else:
            positive.append(ExplanationReason(
                emoji="🟢",
                text="Remote work available",
                kind="positive",
            ))
    else:
        # Non-remote job: only raise concerns when the job is NOT in a preferred country.
        # French (or otherwise preferred-country) on-site and hybrid jobs are geographically
        # valid — they must not generate a misleading "verify remote" warning.
        preferred_lower = {c.lower() for c in profile.geography.preferred_countries}
        job_in_preferred = any(c.lower() in preferred_lower for c in (job.countries or []))

        desc_snippet = _norm((job.description or "")[:800])
        if job_in_preferred:
            # On-site or hybrid in France (or other preferred country) — no remote concern.
            if "hybrid" in desc_snippet or "hybride" in desc_snippet:
                # Surface hybrid as informational (not a concern for preferred-country jobs).
                positive.append(ExplanationReason(
                    emoji="🟢",
                    text="Hybrid role — on-site days are local (preferred country)",
                    kind="positive",
                ))
        else:
            # Job is outside preferred countries — remote flexibility is relevant to check.
            if "hybrid" in desc_snippet or "hybride" in desc_snippet:
                concerns.append(ExplanationReason(
                    emoji="🟡",
                    text="Hybrid role — verify office location and remote flexibility",
                    kind="concern",
                ))
            elif "remote" in profile.work_modes:
                concerns.append(ExplanationReason(
                    emoji="🟡",
                    text="Not marked as fully remote — verify whether remote work is available",
                    kind="concern",
                ))

    # ---- Location -----------------------------------------------------------
    job_countries = job.countries or []
    preferred_lower = {c.lower() for c in profile.geography.preferred_countries}
    job_location_lower = (job.location or "").lower()

    if job_countries:
        matched_preferred = any(c.lower() in preferred_lower for c in job_countries)
        display_countries = ", ".join(job_countries)
        if matched_preferred:
            positive.append(ExplanationReason(
                emoji="🟢",
                text=f"Located in {display_countries} — matches your preferred location",
                kind="positive",
            ))
        elif job.remote:
            # Remote job outside preferred country that passed the filter —
            # it must have EU/worldwide scope in the location string.
            _EU_WORLDWIDE = {"europe", "eu", "emea", "european",
                             "worldwide", "global", "anywhere", "international"}
            scope_found = next(
                (kw for kw in _EU_WORLDWIDE if kw in job_location_lower), None
            )
            if scope_found:
                positive.append(ExplanationReason(
                    emoji="🟢",
                    text=(
                        f"Remote with {scope_found.upper()} scope — "
                        "eligible to work from France"
                    ),
                    kind="positive",
                ))
            concerns.append(ExplanationReason(
                emoji="🟡",
                text=(
                    f"{display_countries}-based company — confirm employment "
                    "contract and tax arrangements for remote workers from France"
                ),
                kind="concern",
            ))
        else:
            concerns.append(ExplanationReason(
                emoji="🟡",
                text=(
                    f"{display_countries}-based company — verify work authorization "
                    "and location requirements"
                ),
                kind="concern",
            ))

    # ---- Language -----------------------------------------------------------
    detected_lang = _detect_posting_language(job)
    if detected_lang and detected_lang in profile.languages:
        positive.append(ExplanationReason(
            emoji="🟢",
            text=f"{detected_lang} — one of your accepted languages",
            kind="positive",
        ))

    required_unknown_lang = _detect_required_unknown_language(job, profile.languages)
    if required_unknown_lang:
        concerns.append(ExplanationReason(
            emoji="🟡",
            text=f"May require {required_unknown_lang} — not in your accepted languages",
            kind="concern",
        ))

    # ---- Skills -------------------------------------------------------------
    matched = _matched_skills(job, profile.target_skills)
    if matched:
        skills_display = ", ".join(matched[:3])
        positive.append(ExplanationReason(
            emoji="🟢",
            text=f"Mentions relevant skills: {skills_display}",
            kind="positive",
        ))

    # ---- Contract type ------------------------------------------------------
    emp = (job.employment_type or "").lower()
    if emp:
        profile_contract_lower = [ct.lower() for ct in profile.contract_types]
        # Map generic employment_type back to profile contract labels
        _EMP_MAP = {
            "full-time": ["cdi", "cdd", "freelance"],
            "contract": ["cdd", "freelance"],
            "internship": ["internship", "alternance", "stage"],
        }
        matched_contracts = _EMP_MAP.get(emp, [])
        if any(ct in profile_contract_lower for ct in matched_contracts):
            positive.append(ExplanationReason(
                emoji="🟢",
                text=f"{job.employment_type} — compatible contract type",
                kind="positive",
            ))

    # ---- De-duplicate and return --------------------------------------------
    return ExplanationResult(positive_reasons=positive, concerns=concerns)
