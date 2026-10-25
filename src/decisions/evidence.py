"""Builders turning what the stages already computed into typed ``EvidenceItem``s.

Observation only: nothing here influences a decision. Filter verdicts always come from the real
``matches_*`` functions; gate states/reasons come from ``GateResult``.

Labelling policy (conservative — the journal must never claim more than was established):
  * a check that could not reject anything (no constraint configured) produces NO evidence;
  * a configured check on a job with no data for it is UNKNOWN (the filter accepts by default);
  * only a configured check with data present is SATISFIED;
  * a gate dimension is SATISFIED only when the gate evaluated it and applied no penalty.
``observed``/``expected`` are structured metadata (job fields, config slices), never description
text; description-derived quotes (spans, conflict quotes) follow ``store_*_text``.
"""

from __future__ import annotations

from typing import Any

from src.decisions.models import ConflictItem, Dimension, EvidenceItem, SourceRef, SpanRef
from src.pipeline.filter import explain_filter_checks, filter_expected, filter_observed, freshness_reference
from src.pipeline.location_distance import assess
from src.requirements.models import GateResult, HardRejectionReason

FILTER_DIMENSION: dict[str, Dimension] = {
    "location": "location", "distance": "location", "remote_policy": "location", "remote": "work_mode", "work_mode": "work_mode", "seniority": "seniority",
    "published_age": "freshness", "salary": "salary", "title_keywords": "domain", "contract_type": "contract",
}
GATE_DIMENSION: dict[str, Dimension] = {
    "education": "education", "experience": "experience", "principal_skill": "skills",
    "mandatory_skill": "skills", "mandatory_secondary_skill": "skills",
}
SCRUBBED_CONFLICT = "Work-mode metadata contradicts the description (quote withheld: job text not stored)"


def _src(stage: str, rule_id: str, rule_version: str | None = None, evidence: str | None = None) -> SourceRef:
    return SourceRef(stage=stage, rule_id=rule_id, rule_version=rule_version, evidence_source=evidence)


def expected_cache(config: dict[str, Any]) -> dict[str, str | None]:
    """``expected`` strings for every check, computed once per run."""
    return {code: filter_expected(code, config) for code in FILTER_DIMENSION}


# -- hard filter ---------------------------------------------------------------------------------------


def applicability(code: str, job: Any, config: dict[str, Any]) -> str:
    """``not_applicable`` | ``unknown`` | ``satisfied`` for a check that the filter let the job pass.

    Mirrors the inputs each ``matches_*`` reads (structure of the filter config + the job fields);
    it is a labelling aid, never a verdict.
    """
    def has(*keys: str) -> bool:
        part: Any = config
        for k in keys:
            part = part.get(k) if isinstance(part, dict) else None
        return bool(part)

    if code == "location":
        if not has("locations", "include"):
            return "not_applicable"
        return "satisfied" if (job.location or job.countries) else "unknown"
    if code in ("distance", "remote_policy"):
        cfg = config.get("location_distance")
        if not cfg or (code == "distance" and not cfg.get("strict_distance")):
            return "not_applicable"            # no home, or not strict: the check cannot reject
        a = assess(job, cfg)
        if code == "distance":
            return "satisfied" if a.road_km is not None else "unknown"
        if a.remote.kind in ("onsite", "hybrid"):
            return "not_applicable"            # not a remote offer: the remote policy does not apply
        return "unknown" if a.remote.kind == "unknown" else "satisfied"
    if code == "remote":
        return "satisfied" if has("remote", "allowed") else "not_applicable"
    if code == "work_mode":
        allowed = {str(m).lower().replace("-", "_").replace(" ", "_")
                   for m in (config.get("work_modes", {}) or {}).get("allowed", [])}
        if not allowed or "on_site" in allowed or "hybrid" in allowed:
            return "not_applicable"            # accepts every work mode: the check cannot reject
        return "satisfied" if (job.work_mode or job.remote or job.countries) else "unknown"
    if code == "seniority":
        if not (has("seniority", "exclude") or has("seniority", "include")):
            return "not_applicable"
        return "satisfied" if job.title else "unknown"
    if code == "published_age":
        return "satisfied" if freshness_reference(job)[0] is not None else "unknown"   # ADR 0012
    if code == "salary":
        req = config.get("salary_requirements", {}) or {}
        needs_min, needs_max = req.get("min_salary") is not None, req.get("max_salary") is not None
        if not (needs_min or needs_max):
            return "not_applicable"
        ok = (needs_min and job.salary_min is not None) or (needs_max and job.salary_max is not None)
        return "satisfied" if ok else "unknown"
    if code == "title_keywords":
        if not any(has("title_keywords", k) for k in ("include", "exclude", "non_it_functions")):
            return "not_applicable"
        return "satisfied" if job.title else "unknown"
    if code == "contract_type":
        if not has("contract_type", "exclude_title_keywords"):
            return "not_applicable"
        return "satisfied" if job.title else "unknown"
    return "not_applicable"


def filter_failure_items(
    job: Any, config: dict[str, Any], code: str, *, all_failing: bool,
    expected: dict[str, str | None] | None = None,
) -> list[EvidenceItem]:
    """Evidence for a filter rejection: the deciding check (``code``, from ``filter_jobs``) and,
    optionally, every other check the job also fails."""
    exp = expected if expected is not None else expected_cache(config)
    items = [EvidenceItem(
        dimension=FILTER_DIMENSION.get(code, "other"), polarity="negative", state="violated",
        source=_src("filter", f"filter.{code}", evidence="rule"),
        message=f"Rejected by hard filter: {code}", observed=filter_observed(code, job, config),
        expected=exp.get(code))]
    if all_failing:
        for o in explain_filter_checks(job, config):
            if not o.passed and o.code != code:
                items.append(EvidenceItem(
                    dimension=FILTER_DIMENSION.get(o.code, "other"), polarity="negative", state="violated",
                    source=_src("filter", f"filter.{o.code}", evidence="rule"),
                    message=f"Also fails hard filter: {o.code} (not the deciding check)",
                    observed=o.observed, expected=o.expected))
    return items


def filter_pass_items(
    job: Any, config: dict[str, Any], expected: dict[str, str | None] | None = None
) -> list[EvidenceItem]:
    """Evidence for a job that PASSED the filter (every check passed, by construction — nothing is
    re-run). Per the labelling policy: nothing for checks that could not reject, UNKNOWN when the
    job had no data for a configured check, SATISFIED otherwise."""
    exp = expected if expected is not None else expected_cache(config)
    items: list[EvidenceItem] = []
    for code, dimension in FILTER_DIMENSION.items():
        verdict = applicability(code, job, config)
        if verdict == "not_applicable":
            continue
        known = verdict == "satisfied"
        items.append(EvidenceItem(
            dimension=dimension, polarity="positive" if known else "unknown",
            state="satisfied" if known else "unknown", source=_src("filter", f"filter.{code}", evidence="rule"),
            message=(f"Passes hard filter: {code}" if known else
                     f"No data for {code}; hard filter accepts by default (unknown is not a rejection)"),
            observed=filter_observed(code, job, config), expected=exp.get(code)))
    return items


# -- requirements gate ---------------------------------------------------------------------------------


def reason_item(reason: HardRejectionReason, *, soft: bool) -> EvidenceItem:
    span = None
    if reason.span is not None:
        span = SpanRef(field=reason.span.field, start=reason.span.start, end=reason.span.end,
                       text=reason.span.text, extractor_id=reason.span.extractor_id)
    state = reason.constraint_state.value
    return EvidenceItem(
        dimension=GATE_DIMENSION.get(reason.type, "other"),
        polarity="negative" if state == "violated" else "unknown", state=state,  # type: ignore[arg-type]
        source=_src("requirements_gate", f"gate.{reason.type}" + (".soft" if soft else ""),
                    reason.rule_version, reason.evidence_source.value),
        message=reason.message, observed=reason.requirement, expected=reason.candidate_value, span=span)


def _soft_penalty_item(dimension: Dimension, fraction: float, version: str) -> EvidenceItem:
    return EvidenceItem(
        dimension=dimension, polarity="negative", state="violated", weight=fraction,
        source=_src("requirements_gate", f"gate.{dimension}.soft", version, "heuristic"),
        message=f"{dimension.capitalize()} above the candidate's level: soft score penalty of {round(fraction * 100)}%")


def gate_items(res: GateResult) -> list[EvidenceItem]:
    """All gate evidence for a job: violations, soft penalties, flags, and the per-dimension states
    the gate computed. A dimension is only SATISFIED/positive when the gate evaluated it and
    applied no penalty to it."""
    items = [reason_item(r, soft=False) for r in res.reasons]
    items += [reason_item(r, soft=True) for r in res.score_penalties]
    items += [EvidenceItem(
        dimension="skills" if "skill" in flag.lower() or "mandatory" in flag.lower() else "other",
        polarity="unknown", state="unknown", source=_src("requirements_gate", "gate.flag", res.rule_version),
        message=flag) for flag in res.flags]
    penalised: dict[str, float] = {"education": res.education_penalty, "experience": res.experience_penalty}
    for dim in ("education", "experience"):
        if penalised[dim] > 0:
            items.append(_soft_penalty_item(dim, penalised[dim], res.rule_version))  # type: ignore[arg-type]
    reasoned = {GATE_DIMENSION.get(r.type) for r in res.reasons}
    req = res.requirements
    for dim_key in ("education", "experience", "skills"):
        state = res.states.get(dim_key)
        if state is None or dim_key in reasoned or state.value == "violated":
            continue
        if penalised.get(dim_key, 0) > 0 or (dim_key == "skills" and res.score_penalties):
            continue                                # a penalty was applied: never claim "satisfied"
        observed = None
        span = None
        if dim_key == "education" and req is not None and req.education is not None:
            e = req.education
            observed, span = f"{e.level_label} ({e.importance.value}, {e.kind.value})", e.span
        elif dim_key == "experience" and req is not None and req.experience is not None:
            x = req.experience
            observed, span = f"{x.min_years} years ({x.importance.value})", x.span
        absent = observed is None and dim_key != "skills"
        items.append(EvidenceItem(
            dimension=dim_key,  # type: ignore[arg-type]
            polarity="positive" if state.value == "satisfied" else "unknown", state=state.value,  # type: ignore[arg-type]
            source=_src("requirements_gate", f"gate.{dim_key}", res.rule_version, "heuristic"),
            message=(f"No {dim_key} requirement stated" if absent
                     else f"{dim_key.capitalize()} constraint {state.value}"
                     + (" (not evaluated or inconclusive)" if state.value == "unknown" else "")),
            observed=observed,
            span=SpanRef(field=span.field, start=span.start, end=span.end, text=span.text,
                         extractor_id=span.extractor_id) if span is not None else None))
    return items


# -- scoring ---------------------------------------------------------------------------------------------

FACTOR_DIMENSION: dict[str, Dimension] = {
    "location_fit": "location", "domain_relevance": "domain", "skills_match": "skills",
    "seniority_match": "seniority", "experience_fit": "experience",
}
# Presentation bands for a 0-1 factor value (NOT decision thresholds): the scorer treats mid values
# as neutral/uncertain (e.g. 0.5 for an unknown experience), so only clearly high/low values are
# labelled positive/negative and the middle band is UNKNOWN.
POSITIVE_FROM = 0.7
NEGATIVE_UP_TO = 0.25


def score_items(expl: Any) -> list[EvidenceItem]:
    """Evidence for an accepted job's deterministic score: one item per factor (value + weight share),
    the dealbreaker gates that fired (and whether they changed anything), the top-location bonus, and
    the penalties applied. ``weight`` is only ever a factor's share of the composite; other numbers
    (bonus, cap, multiplier, penalty) live in ``observed`` and the message."""
    items: list[EvidenceItem] = []
    total = sum(expl.weights.values()) or 1.0
    loc = expl.location_match
    for factor, value in expl.factors.items():
        dimension = FACTOR_DIMENSION.get(factor, "other")
        if value >= POSITIVE_FROM:
            polarity, state = "positive", "satisfied"
        elif value <= NEGATIVE_UP_TO:
            polarity, state = "negative", "violated"
        else:
            polarity, state = "unknown", "unknown"
        extra = ""
        if factor == "location_fit" and loc:
            extra = f"; matches {loc['type']} preference '{loc['value']}' ({loc['status']}, priority {loc['priority']})"
        share = expl.weights.get(factor, 0.0) / total
        items.append(EvidenceItem(
            dimension=dimension, polarity=polarity, state=state,  # type: ignore[arg-type]
            source=_src("score", f"score.{factor}", evidence="rule"),
            message=f"{factor} = {value:.2f} (weight share {share:.2f}){extra}",
            observed=f"{value:.3f}", weight=round(share, 4)))
    detail = getattr(expl, "location_detail", None)
    if detail:
        items.extend(location_detail_items(detail))
    if expl.top_location_bonus:
        label = "Close to home" if detail else "Top preferred location"
        items.append(EvidenceItem(
            dimension="location", polarity="positive", state="satisfied",
            source=_src("score", "score.top_location_bonus", evidence="rule"),
            message=f"{label}: +{expl.top_location_bonus:.2f} added before the gates",
            observed=f"+{expl.top_location_bonus:.2f}"))
    if expl.domain_gate_fired:
        effect = (f"composite capped at {expl.domain_cap}" if expl.domain_cap_applied
                  else f"composite already within the {expl.domain_cap} cap (no change)")
        items.append(EvidenceItem(
            dimension="domain", polarity="negative", state="violated",
            source=_src("score", "score.gate.domain", evidence="rule"),
            message=f"Dealbreaker gate: confirmed wrong domain; {effect}", observed=f"cap={expl.domain_cap}"))
    if expl.seniority_gate_fired:
        items.append(EvidenceItem(
            dimension="seniority", polarity="negative", state="violated",
            source=_src("score", "score.gate.seniority", evidence="rule"),
            message=f"Dealbreaker gate: confirmed seniority mismatch; composite multiplied by {expl.seniority_multiplier}",
            observed=f"x{expl.seniority_multiplier}"))
    dim_of = {"education": "education", "skill": "skills", "experience": "experience", "language": "language"}
    for name, fraction in expl.penalties.items():
        if fraction > 0:
            items.append(EvidenceItem(
                dimension=dim_of[name], polarity="negative", state="violated",  # type: ignore[arg-type]
                source=_src("score", f"score.penalty.{name}", evidence="rule"),
                message=f"{name.capitalize()} penalty: score multiplied by {1 - fraction:.2f}",
                observed=f"x{1 - fraction:.2f}"))
    return items


def location_detail_items(detail: dict[str, Any]) -> list[EvidenceItem]:
    """Distance and remote-eligibility evidence (distance model, ``location_distance.describe``).
    Score-stage evidence: "beyond the radius" lowers the score, it never rejects (the only
    distance exclusion is the filter's strict check). An unknown distance stays UNKNOWN."""
    version = detail.get("rule_version")
    place = detail.get("place")
    where = "unknown place" if place is None else f"{place['name']} ({place['precision']})"
    km, radius = detail.get("road_km"), detail.get("max_distance_km")
    expected = f"home {detail.get('home')}, max {radius} km"
    if km is None:
        dist = EvidenceItem(
            dimension="location", polarity="unknown", state="unknown",
            source=_src("score", "score.location.distance", version, "metadata"),
            message=f"Distance unknown ({where}): never a reason to exclude", observed=where, expected=expected)
    else:
        inside = bool(detail.get("within_radius"))
        dist = EvidenceItem(
            dimension="location", polarity="positive" if inside else "negative",
            state="satisfied" if inside else "violated",
            source=_src("score", "score.location.distance", version, "metadata"),
            message=(f"{where}: ~{km} km by road from home, "
                     + ("within" if inside else "beyond") + f" the {radius} km maximum"),
            observed=f"{where}, ~{km} km", expected=expected)
    kind, accepted = detail.get("remote_kind"), bool(detail.get("remote_accepted"))
    area = f" ({detail['remote_area']})" if detail.get("remote_area") else ""
    if accepted:
        polarity, state, msg = "positive", "satisfied", f"Remote offer of an accepted kind: {kind}{area}"
    elif kind == "unknown":
        polarity, state, msg = "unknown", "unknown", "Remote eligibility unknown: never a preferred match"
    elif kind in ("onsite", "hybrid"):
        polarity, state, msg = "unknown", "unknown", f"Work mode {kind}: judged on distance"
    else:
        polarity, state, msg = "negative", "violated", f"Remote kind not accepted by the profile: {kind}{area}"
    remote = EvidenceItem(
        dimension="location", polarity=polarity, state=state,  # type: ignore[arg-type]
        source=_src("score", "score.location.remote_eligibility", version,
                    "heuristic" if detail.get("remote_strength") == "explicit" else "metadata"),
        message=msg, observed=f"{kind}{area} [{detail.get('remote_strength')}: {detail.get('remote_evidence')}]")
    return [dist, remote]


def hard_reject_sources(evidence: list[EvidenceItem], stage: str) -> tuple[str, ...]:
    """``evidence_source`` of the HARD violated items of ``stage`` (soft penalties never count): what a
    rejection actually rests on. One definition for the production journal and the offline evaluation."""
    return tuple(
        str(e.source.evidence_source) for e in evidence
        if e.state == "violated" and e.source.stage == stage and not e.source.rule_id.endswith(".soft")
        and e.source.evidence_source
    )


# -- cross-field conflicts -------------------------------------------------------------------------------


def work_mode_conflict(job: Any, *, keep_quote: bool) -> ConflictItem | None:
    """Metadata-vs-description work-mode contradiction (the extractor exists but is not wired into
    the gate; recording it is observation only). ``Job.remote`` is False when unknown, so only a
    positive ``remote`` or an explicit ``work_mode == 'on_site'`` counts as a metadata claim. The
    description quote is dropped unless job text is stored."""
    from src.requirements.work_mode_evidence import detect_work_mode_contradiction

    text = job.description or ""
    if not text:
        return None
    claim: bool | None = True if job.remote else (False if job.work_mode == "on_site" else None)
    if claim is None:
        return None
    description = detect_work_mode_contradiction(claim, text)
    if not description:
        return None
    return ConflictItem(dimensions=["work_mode"], description=description if keep_quote else SCRUBBED_CONFLICT)
