"""Lightweight LLM semantic eligibility gate.

Runs after the deterministic requirements gate (``src/requirements/gate.py``),
on jobs that already passed it, to catch *implicit* education/seniority
mismatches the deterministic gate structurally cannot see — e.g. a job titled
"Ingenieur logiciel" that never states "Bac+5 requis" but conventionally
implies it, or a "Senior Software Engineer" title with no explicit years-of-
experience requirement to extract.

This is a separate, additive signal — never merged into ``JobRequirements``/
``GateResult`` (see ``src/requirements/gate.py``'s evidence-authority
discipline, which this module follows in spirit): the deterministic gate
config and its evidence model are never touched. The orchestrator alone
decides how much weight a REJECT decision carries, via a confidence
threshold (see ``EligibilityGate.run``).

Conservative by design (mirrors the lesson from the prior
``RequirementExtractor`` LLM-enrichment benchmark, see that module's
docstring): UNCERTAIN and low-confidence REJECT both pass through. Only a
high-confidence REJECT removes a job. An LLM call failure also passes the job
through (fail-open) rather than risk losing a viable job to a transient error.
No keyword blacklist decides anything — the prompt (``src/ai/prompt.py``)
reasons freely over compact, evidence-based context; this module only
assembles that context and applies the confidence threshold.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.ai.models import BatchEligibilityResult, EligibilityDecision, EligibilityResult
from src.ai.prompt import ELIGIBILITY_PROMPT_VERSION, build_eligibility_prompt
from src.pipeline.scoring import SENIORITY_FR_SYNONYMS, find_seniority_indicators
from src.requirements.models import EducationKind, Importance

if TYPE_CHECKING:
    from src.ai.storage import AIAnalysisStorage
    from src.models.job import Job
    from src.requirements.models import JobRequirements

logger = logging.getLogger(__name__)

# How much of the description to scan for seniority/context words, and how
# much to fall back to verbatim when no structured evidence exists at all.
# Kept well under the model's context window (8192 tokens on this project's
# LM Studio setup) since only ONE job's worth of evidence is ever assembled.
_SCAN_CHARS = 2000
_FALLBACK_DESCRIPTION_CHARS = 800


def _assemble_evidence(job: "Job", requirements: "JobRequirements | None") -> tuple[str, bool]:
    """Build compact, evidence-based context for the eligibility prompt.

    Prefers real deterministic evidence (already extracted by the requirements
    gate moments earlier — no re-extraction here) over a raw description
    slice: education/experience requirement spans, seniority-indicator words
    found in the title/description, and internship/alternance/junior context
    words (labeled with the level they suggest). Only when a job carries none
    of that does this fall back to a capped description excerpt, so the model
    still has something to reason over.

    Returns ``(evidence_text, has_structured_evidence)``. The second value is
    True only when a real education/experience/seniority/level signal was
    found — never for the generic-description or "no evidence" fallback — and
    is used by the caller to refuse to act on a REJECT that has no textual
    anchor at all (see EligibilityGate.run's corroboration requirement).
    """
    lines: list[str] = []

    if requirements is not None:
        edu = requirements.education
        if edu is not None and edu.raw_text:
            lines.append(
                f'- education requirement text: "{edu.raw_text}" '
                f"(importance: {edu.importance.value}, kind: {edu.kind.value})"
            )
        exp = requirements.experience
        if exp is not None and exp.raw_text:
            lines.append(
                f'- experience requirement text: "{exp.raw_text}" '
                f"(importance: {exp.importance.value})"
            )

    combined_text = f"{job.title or ''} {(job.description or '')[:_SCAN_CHARS]}"
    combined_lower = combined_text.lower()

    seniority_hits = sorted(set(find_seniority_indicators(combined_text)))
    if seniority_hits:
        lines.append(f"- seniority/level words found in title or description: {', '.join(seniority_hits)}")

    level_hits: list[str] = []
    for level, synonyms in SENIORITY_FR_SYNONYMS.items():
        for syn in synonyms:
            if syn and syn in combined_lower:
                level_hits.append(f"{syn} (suggests {level.replace('_', ' ')} level)")
    if level_hits:
        lines.append(f"- internship/junior context words found: {', '.join(sorted(set(level_hits)))}")

    has_structured_evidence = bool(lines)

    if not lines:
        desc = (job.description or "")[:_FALLBACK_DESCRIPTION_CHARS].strip()
        if desc:
            lines.append(f"- description excerpt (no structured evidence was extracted): {desc}")
        else:
            lines.append("- no additional evidence available")

    return "\n".join(lines), has_structured_evidence


# Reason-string markers used to decide WHICH deterministic requirement field
# a REJECT decision is actually about, before checking whether that field's
# own modality can support a hard rejection. The eligibility prompt's system
# instructions restrict this gate to "education requirement and seniority/
# experience level" only (see _ELIGIBILITY_SYSTEM_PROMPT) — nothing else — so
# these two categories cover every REJECT this gate can ever produce.
_EDUCATION_REASON_MARKERS = ("educat", "bac_plus", "bacplus", "degree", "diploma", "dipl")
_EXPERIENCE_REASON_MARKERS = ("experien", "years", "year_")


def _deterministic_downgrade_reason(
    requirements: "JobRequirements | None", reason: str
) -> str | None:
    """Return a reason to downgrade a REJECT, or None if it may stand.

    ``EligibilityGate`` only ever runs on jobs that already PASSED
    ``HardConstraintGate`` (see the module docstring) — so by construction,
    any education/experience requirement recorded here that would be a
    genuine MANDATORY-and-exceeding violation was already handled, and would
    have removed the job before it ever reached this gate. A requirement
    recorded as PREFERRED, OPTIONAL, UNKNOWN, CORE, or an OR-alternative is
    exactly the class of evidence ``HardConstraintGate`` itself treats as
    non-disqualifying (soft penalty at most, never a hard reject) — the
    eligibility gate's free-form reasoning must not contradict that
    already-established, authoritative modality. See the 2026-09-05 real-job
    benchmark: the eligibility gate rejected several jobs at 0.9+ confidence
    purely because a "Bac+5 ou équivalent"/"Bac+5 souhaité" mention exists in
    the text, even though ``RequirementExtractor`` had already correctly
    classified it as PREFERRED/OR-alternative and ``HardConstraintGate`` had
    already correctly let the job through on that basis.

    Only checks the field the REJECT reason is actually about (via
    ``_EDUCATION_REASON_MARKERS``/``_EXPERIENCE_REASON_MARKERS``) — a REJECT
    for an unrelated, genuinely evidenced reason (e.g. an explicit title
    seniority word) is not touched just because the job also happens to
    carry an unrelated soft education mention.
    """
    if requirements is None:
        return None
    reason_norm = (reason or "").lower()

    edu = requirements.education
    if edu is not None and any(m in reason_norm for m in _EDUCATION_REASON_MARKERS):
        if edu.kind != EducationKind.HELD:
            return (
                f"education requirement '{edu.level_label}' concerns a degree being "
                f"{edu.kind.value}, not one already held — not an independent held-degree requirement"
            )
        if edu.is_or_alternative:
            return (
                f"education requirement '{edu.level_label}' is an OR-alternative "
                "(e.g. 'Bac+5 ou équivalent') — not an independent mandatory requirement"
            )
        if edu.importance != Importance.MANDATORY:
            return (
                f"education requirement '{edu.level_label}' is classified "
                f"{edu.importance.value} (not mandatory) by the deterministic extractor"
            )

    exp = requirements.experience
    if exp is not None and any(m in reason_norm for m in _EXPERIENCE_REASON_MARKERS):
        if exp.importance != Importance.MANDATORY:
            return (
                f"experience requirement ({exp.min_years}+ years) is classified "
                f"{exp.importance.value} (not mandatory) by the deterministic extractor"
            )

    return None


def _parse_decision(raw_response: str) -> EligibilityDecision:
    """Parse the model's response into an EligibilityDecision.

    Never raises. Any parse failure (empty response, malformed JSON, a
    reasoning model leaking non-JSON text) yields the model's default —
    decision="UNCERTAIN", confidence=0.0 — which passes through downstream,
    never causing a rejection on a parsing failure.
    """
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned[:4].lower() == "json":
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()

    # Defensive: take just the JSON object if the model wrapped it in any
    # extra text (e.g. a leaked reasoning fragment despite /no_think).
    if "{" in cleaned and "}" in cleaned:
        cleaned = cleaned[cleaned.index("{") : cleaned.rindex("}") + 1]

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Eligibility gate: malformed LLM response: %s", raw_response[:200])
        return EligibilityDecision()

    try:
        return EligibilityDecision(**data)
    except (ValueError, TypeError) as exc:
        logger.warning("Eligibility gate: invalid decision shape (%s): %s", exc, raw_response[:200])
        return EligibilityDecision()


@dataclass
class EligibilityStageResult:
    """Outcome of running the LLM eligibility gate as a pipeline stage.

    Mirrors ``src.pipeline.preflight.PreflightResult``'s role: a single typed
    result the orchestrator applies to its own stats/rejection-records
    bookkeeping, so ``run_pipeline`` doesn't need to know anything about how
    the gate itself is configured or invoked (see ``EligibilityGate.run_stage``).

    ``ran`` is False when the stage was skipped (disabled in config, --ai not
    passed, or no profile available) or when it failed and fell back to
    passing every job through unchanged — in both cases ``survivors`` equals
    the input jobs and ``stats``/``error`` reflect why.
    """

    ran: bool
    survivors: list[Any]
    rejection_records: list[Any] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    passed: int = 0
    rejected: int = 0
    error: str | None = None
    # Per-job LLM verdicts {job_id: {"decision", "confidence", "reason", "status"}} for every job the
    # gate evaluated (survivors included). Observability only (decision journal): never read by the
    # gate or the pipeline. A REJECT that is also a survivor was downgraded (low confidence / unsupported).
    decisions: dict[str, dict[str, Any]] = field(default_factory=dict)


class EligibilityGate:
    """Evaluates jobs against a candidate profile for role-level compatibility.

    Takes a pre-configured LLM provider (typically a ``LocalLLMProvider``
    instance built with a small ``max_tokens`` — see ``run_stage``) and calls
    its public ``complete()`` primitive directly, the same seam
    ``RequirementExtractor._extract_llm`` uses — this module builds a
    different prompt/schema than ``JobUnderstanding``, so it doesn't reuse
    ``analyze_job``/``analyze_batch``.
    """

    def __init__(
        self,
        llm_provider: Any,
        max_workers: int = 4,
        prompt_version: str = ELIGIBILITY_PROMPT_VERSION,
    ):
        self._llm = llm_provider
        self._max_workers = max(1, max_workers)
        self.prompt_version = prompt_version

    def evaluate_job(
        self,
        job: "Job",
        requirements: "JobRequirements | None",
        profile: dict[str, Any],
    ) -> EligibilityResult:
        """Evaluate a single job. Never raises — failures become status='failed'."""
        try:
            evidence, _has_evidence = _assemble_evidence(job, requirements)
            prompt = build_eligibility_prompt(job.title or "", evidence, profile)
            raw_response = self._llm.complete(prompt)

            if not raw_response.strip():
                return EligibilityResult(
                    job_id=job.id,
                    decision=EligibilityDecision(),
                    prompt_version=self.prompt_version,
                    model_identifier=getattr(self._llm, "model_name", None),
                    status="failed",
                    error_message="LLM returned empty response",
                )

            decision = _parse_decision(raw_response)
            return EligibilityResult(
                job_id=job.id,
                decision=decision,
                prompt_version=self.prompt_version,
                model_identifier=getattr(self._llm, "model_name", None),
                status="success",
            )
        except Exception as exc:  # noqa: BLE001 — one job's failure must not abort the batch
            logger.warning("Eligibility gate failed for job %s: %s", job.id, exc)
            return EligibilityResult(
                job_id=job.id,
                decision=EligibilityDecision(),
                prompt_version=self.prompt_version,
                model_identifier=getattr(self._llm, "model_name", None),
                status="failed",
                error_message=str(exc),
            )

    def evaluate_batch(
        self,
        jobs: list["Job"],
        requirements_map: dict[str, "JobRequirements"],
        profile: dict[str, Any],
    ) -> list[EligibilityResult]:
        """Evaluate a batch of jobs, up to ``max_workers`` concurrently."""
        if self._max_workers <= 1 or len(jobs) <= 1:
            return [
                self.evaluate_job(job, requirements_map.get(job.id), profile)
                for job in jobs
            ]
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            return list(
                executor.map(
                    lambda job: self.evaluate_job(job, requirements_map.get(job.id), profile),
                    jobs,
                )
            )

    def run(
        self,
        jobs: list["Job"],
        requirements_map: dict[str, "JobRequirements"],
        profile: dict[str, Any],
        ai_storage: "AIAnalysisStorage",
        profile_version: str = "",
        reject_confidence: float = 0.75,
    ) -> tuple[list["Job"], dict[str, EligibilityResult], BatchEligibilityResult]:
        """Run the eligibility gate on a list of jobs, with caching.

        Returns:
            (surviving_jobs, decisions_by_job_id, stats)

        ``surviving_jobs`` preserves the input order and excludes only jobs
        whose decision is REJECT with confidence >= reject_confidence.
        UNCERTAIN, PASS, and any failed/cached-miss-but-unresolved job all
        survive — see the module docstring's conservative-by-design rule.
        """
        if not jobs:
            return [], {}, BatchEligibilityResult(total_jobs=0, successful=0, failed=0, results=[])

        t0 = time.perf_counter()
        job_ids = [job.id for job in jobs]
        content_hashes = {job.id: (job.content_hash or "") for job in jobs}

        cached = ai_storage.get_eligibility_for_jobs(
            job_ids, self.prompt_version, content_hashes, profile_version
        )

        needs_eval = [job for job in jobs if job.id not in cached]
        fresh: list[EligibilityResult] = []
        if needs_eval:
            fresh = self.evaluate_batch(needs_eval, requirements_map, profile)
            for fresh_result in fresh:
                ai_storage.upsert_eligibility(
                    fresh_result,
                    content_hash=content_hashes.get(fresh_result.job_id, ""),
                    profile_version=profile_version,
                )

        all_results: dict[str, EligibilityResult] = dict(cached)
        for fresh_result in fresh:
            all_results[fresh_result.job_id] = fresh_result

        survivors: list[Job] = []
        successful = failed = uncertain = passed = rejected = unsupported_reject = 0

        for job in jobs:
            result = all_results.get(job.id)
            if result is None or result.status != "success":
                # Missing result or an LLM/parsing failure — fail-open, the
                # job is never removed on account of the gate itself failing.
                if result is not None:
                    failed += 1
                survivors.append(job)
                continue

            successful += 1
            decision = result.decision
            is_high_confidence_reject = (
                decision.decision == "REJECT" and decision.confidence >= reject_confidence
            )
            if is_high_confidence_reject:
                requirements = requirements_map.get(job.id)
                # Recomputed fresh from (job, requirements) rather than stored
                # on the cached result — a cached row only persists the
                # decision/status columns (see AIAnalysisStorage), not this
                # flag, and _assemble_evidence is pure/cheap (no LLM call), so
                # recomputing it here is correct for cached AND fresh results
                # alike, with no schema migration needed.
                _, has_evidence = _assemble_evidence(job, requirements)
                if not has_evidence:
                    # No textual anchor at all (no education/experience/
                    # seniority/level evidence was even assembled for this
                    # job) — a free-form hallucination, not a grounded
                    # decision (see the 2026-09-05 real-job benchmark's
                    # hallucinated-seniority findings). Downgrade: the job
                    # survives, exactly like a low-confidence or UNCERTAIN
                    # result, per the module's conservative-by-design rule.
                    unsupported_reject += 1
                    survivors.append(job)
                    continue
                downgrade_reason = _deterministic_downgrade_reason(requirements, decision.reason)
                if downgrade_reason is not None:
                    # Real textual evidence exists, but the deterministic
                    # extractor already classified it as non-mandatory (or an
                    # OR-alternative) — the eligibility gate must not
                    # contradict that authoritative modality. See
                    # _deterministic_downgrade_reason's docstring.
                    logger.info(
                        "Eligibility gate: REJECT for job %s downgraded (%s) — AI reason was '%s'",
                        job.id, downgrade_reason, decision.reason,
                    )
                    unsupported_reject += 1
                    survivors.append(job)
                    continue
            if is_high_confidence_reject:
                rejected += 1
            else:
                if decision.decision == "UNCERTAIN":
                    uncertain += 1
                else:
                    passed += 1
                survivors.append(job)

        elapsed = time.perf_counter() - t0
        stats = BatchEligibilityResult(
            total_jobs=len(jobs),
            successful=successful,
            failed=failed,
            passed=passed,
            rejected=rejected,
            uncertain=uncertain,
            unsupported_reject=unsupported_reject,
            cached=len(cached),
            time_seconds=elapsed,
            results=list(all_results.values()),
        )
        return survivors, all_results, stats

    @staticmethod
    def run_stage(
        jobs: list["Job"],
        requirements_map: dict[str, "JobRequirements"],
        profile: Any,
        profile_hash: str,
        db_path: str,
        use_ai: bool,
    ) -> EligibilityStageResult:
        """Run the optional LLM semantic eligibility gate as a pipeline stage.

        Follows ``src.pipeline.preflight.run_preflight``'s pattern: a single
        entry point the orchestrator calls, encapsulating provider
        construction, per-job evaluation, rejection-record construction, and
        statistics bookkeeping that previously lived inline in
        ``run_pipeline()``. Never raises — any failure is captured in the
        returned result's ``error`` field and every input job passes through
        unchanged (fail-open, matching the gate's own conservative-by-design
        rule: an uncorroborated or failed decision must never cost a job).

        Gated on ``use_ai`` (needs a running local LLM server, same
        precondition as Phase 2) AND ``profile.matching.llm_eligibility_enabled``
        (off by default) AND a profile actually being available — any of
        these being false returns immediately with ``ran=False`` and the
        input jobs as survivors, without constructing an LLM provider.
        """
        if not (
            use_ai
            and profile is not None
            and getattr(profile.matching, "llm_eligibility_enabled", False)
        ):
            return EligibilityStageResult(
                ran=False, survivors=list(jobs), passed=len(jobs), rejected=0,
            )

        try:
            from src.ai.config import load_phase2_config
            from src.ai.interfaces import LocalLLMProvider
            from src.ai.storage import AIAnalysisStorage
            # Deferred to avoid an import cycle: orchestrator.py lazily
            # imports EligibilityGate from this module at call time, so by
            # the time run_stage executes, src.pipeline.orchestrator is
            # already fully loaded and this import is safe.
            from src.pipeline.orchestrator import RejectionRecord

            elig_cfg = load_phase2_config()
            elig_ai_storage = AIAnalysisStorage(db_path=db_path)
            elig_ai_storage.init_db()

            # Small max_tokens: the eligibility gate's schema is a 3-field
            # {decision, confidence, reason} object, not the verbose Phase 2
            # JobUnderstanding schema. Concurrency lives on EligibilityGate
            # itself (max_workers), not on this provider instance.
            elig_llm_provider = LocalLLMProvider(
                endpoint=elig_cfg.llm_endpoint,
                model_name=elig_cfg.llm_model_name,
                temperature=elig_cfg.llm_temperature,
                max_tokens=100,
                timeout_seconds=elig_cfg.llm_timeout_seconds,
                thinking_enabled=elig_cfg.llm_thinking_enabled,
            )
            gate = EligibilityGate(llm_provider=elig_llm_provider, max_workers=elig_cfg.max_workers)
            reject_confidence = getattr(
                profile.matching, "llm_eligibility_reject_confidence", 0.75
            )

            survivors, decisions, elig_stats = gate.run(
                jobs,
                requirements_map,
                profile.model_dump(),
                elig_ai_storage,
                profile_version=profile_hash,
                reject_confidence=reject_confidence,
            )

            job_map = {job.id: job for job in jobs}
            survivor_ids = {job.id for job in survivors}
            rejection_records: list[Any] = []
            for job_id, job in job_map.items():
                if job_id in survivor_ids:
                    continue
                result = decisions.get(job_id)
                detail = (
                    json.dumps({
                        "decision": result.decision.decision,
                        "confidence": result.decision.confidence,
                        "reason": result.decision.reason,
                    })
                    if result is not None
                    else json.dumps({"decision": "REJECT", "reason": "llm_eligibility_reject"})
                )
                rejection_records.append(RejectionRecord(
                    job_id=job.id,
                    title=job.title,
                    company=job.company,
                    location=job.location,
                    source=job.source,
                    rejection_stage="llm_eligibility",
                    rejection_detail=detail,
                ))

            stats_dict = {
                "successful": elig_stats.successful,
                "failed": elig_stats.failed,
                "uncertain": elig_stats.uncertain,
                "passed": elig_stats.passed,
                "rejected": elig_stats.rejected,
                "unsupported_reject": elig_stats.unsupported_reject,
                "cached": elig_stats.cached,
                "time_seconds": elig_stats.time_seconds,
            }
            logger.info(
                "LLM eligibility gate: %d passed, %d rejected "
                "(uncertain=%d passed-through, unsupported_reject=%d passed-through, failed=%d fail-open)",
                len(survivors), elig_stats.rejected,
                elig_stats.uncertain, elig_stats.unsupported_reject, elig_stats.failed,
            )
            elig_ai_storage.close()

            return EligibilityStageResult(
                ran=True,
                survivors=survivors,
                decisions={
                    jid: {"decision": r.decision.decision, "confidence": r.decision.confidence,
                          "reason": r.decision.reason, "status": r.status}
                    for jid, r in decisions.items()
                },
                rejection_records=rejection_records,
                stats=stats_dict,
                passed=len(survivors),
                rejected=elig_stats.rejected,
            )
        except Exception as exc:  # noqa: BLE001 — a gate failure must never abort the pipeline
            logger.warning("LLM eligibility gate failed — proceeding without it: %s", exc)
            return EligibilityStageResult(
                ran=False,
                survivors=list(jobs),
                stats={"error": str(exc)},
                passed=len(jobs),
                rejected=0,
                error=str(exc),
            )
