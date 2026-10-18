"""50-job synthetic benchmark: does the LM Studio/local-LLM layer improve
the job filter over the deterministic-only baseline?

This is an EVALUATION harness, not a production code path, and it never
modifies production filtering behavior. It drives the REAL production
functions (``src.pipeline.filter.filter_jobs``,
``src.requirements.extractor.RequirementExtractor``,
``src.requirements.gate.HardConstraintGate``) against 50 synthetic job
postings (``tests/fixtures/ai_filter_benchmark/jobs.py``) using the
project's actual ``config/profile.yaml`` / ``config/filters.yaml``.

BLIND EVALUATION — no benchmark leakage
----------------------------------------
The ground truth (``tests/fixtures/ai_filter_benchmark/ground_truth.py``) is
joined to the results ONLY AFTER both the deterministic and the AI-enhanced
paths have finished producing verdicts. Neither the filter, the requirement
extractor, nor the LLM prompt ever receives the ground truth, the benchmark
category, or any indication that these are benchmark jobs at all — they are
fed through exactly the same ``Job`` objects and the exact same code paths a
real collected job would go through.

Two paths are compared, mirroring the ONLY two configurations the codebase
actually supports today (``profile.yaml``'s ``matching.llm_extraction_enabled``):

  A. Deterministic only  — domain/keyword filter, then requirements gate
     with heuristic-only extraction (no LLM).
  B. Deterministic + AI  — the SAME domain/keyword filter result (the AI
     never touches that stage — see the module-level note in
     src/pipeline/filter.py), then requirements gate with heuristic+LLM
     extraction (RequirementExtractor's evidence-corroboration safety net
     still applies — the LLM can only ever ADD corroborated evidence, never
     override a heuristic finding or manufacture an uncorroborated hard
     reject; see src/requirements/extractor.py's module docstring).

A third, informational-only measurement is also run when the LLM is
reachable: Phase 2 "local AI job understanding"
(src/ai/pipeline.py / JobUnderstanding.profile_match_score). This signal is
NOT wired into any accept/reject decision in production today — it only
enriches embeddings for semantic ranking — so it is reported separately as
"if this were wired into filtering, would it help with keyword traps and
role understanding", not blended into the primary accuracy numbers.

Usage:
    python scripts/benchmark_ai_filter_50.py [--config config]
        [--endpoint http://localhost:1234] [--model-name qwen/qwen3-8b]
        [--skip-ai] [--report-path <path>]

If no local LLM server is reachable at --endpoint, the script still runs the
complete deterministic baseline and reports plainly that the AI comparison
could not be executed — it never fabricates AI results.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.models.job import Job
from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
from src.requirements.extractor import RequirementExtractor
from src.requirements.education import parse_education_level
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import GateResult

from tests.fixtures.ai_filter_benchmark.jobs import JOBS
from tests.fixtures.ai_filter_benchmark.ground_truth import GROUND_TRUTH


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------

@dataclass
class JobVerdict:
    job_id: str
    verdict: str                 # "ACCEPT" | "REJECT"
    stage: str                   # "filter" | "requirements_gate" | "passed"
    reasons: list[str] = field(default_factory=list)
    gate_flags: list[str] = field(default_factory=list)
    education_found: str | None = None
    education_importance: str | None = None
    principal_mismatches: list[str] = field(default_factory=list)
    extraction_method: str = "heuristic"


@dataclass
class Phase2Verdict:
    job_id: str
    status: str
    profile_match_score: float | None = None
    domain: str | None = None
    profile_match_reasons: list[str] = field(default_factory=list)
    profile_mismatch_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path A / B execution — both call the SAME production functions
# ---------------------------------------------------------------------------

def _run_domain_filter(
    jobs: list[Job], config_dir: str, profile_path: str | None = None
) -> tuple[list[Job], dict[str, str], Any]:
    """Run the production domain/keyword filter exactly once.

    Shared by both paths A and B — the AI layer never touches this stage
    (see build_requirements_llm_provider's wiring point, which only affects
    the requirements gate below), so re-running it per path would be pure
    waste and could never produce a different result.

    ``profile_path`` overrides ``<config_dir>/profile.yaml`` (the regression
    test pins tests/fixtures/ai_filter_benchmark/profile.yaml).
    """
    profile = load_profile(profile_path or str(Path(config_dir) / "profile.yaml"))
    engine_cfg = load_filters(str(Path(config_dir) / "filters.yaml"))
    filters_config = build_filter_config(profile, engine_cfg)
    passing, rejection_reasons = filter_jobs(jobs, filters_config)
    return passing, rejection_reasons, profile


def _run_requirements_path(
    jobs: list[Job],
    profile: Any,
    llm_provider: object | None,
) -> dict[str, JobVerdict]:
    """Run RequirementExtractor + HardConstraintGate — the actual production
    wiring orchestrator.py uses — for every job that passed the domain filter.

    ``llm_provider=None`` reproduces Path A (heuristic-only); a real
    LocalLLMProvider reproduces Path B (heuristic+LLM), exactly as enabling
    ``matching.llm_extraction_enabled: true`` in profile.yaml would in
    production.
    """
    user_max_edu = parse_education_level(profile.max_education) if profile.max_education else None
    user_max_years = getattr(profile.experience, "max_years", None)

    extractor = RequirementExtractor(user_skills=list(profile.target_skills), llm_provider=llm_provider)
    gate = HardConstraintGate(
        user_max_education_level=user_max_edu,
        user_max_years_experience=user_max_years,
        education_hard_constraint=profile.matching.education_hard_constraint,
        principal_skill_gate=profile.matching.principal_skill_gate,
        mandatory_skill_gate=profile.matching.mandatory_central_skill_gate,
        profile_version_hash="benchmark",
    )
    edu_penalty = profile.matching.education_preferred_penalty

    requirements_map = {job.id: extractor.extract(job) for job in jobs}
    _passing, gate_results = gate_jobs(jobs, requirements_map, gate, education_preferred_penalty=edu_penalty)

    verdicts: dict[str, JobVerdict] = {}
    for job in jobs:
        req = requirements_map[job.id]
        result: GateResult = gate_results[job.id]
        reasons = [r.message for r in result.reasons]
        verdicts[job.id] = JobVerdict(
            job_id=job.id,
            verdict="ACCEPT" if result.passes else "REJECT",
            stage="passed" if result.passes else "requirements_gate",
            reasons=reasons,
            gate_flags=list(result.flags),
            education_found=req.education.level_label if req.education else None,
            education_importance=req.education.importance.value if req.education else None,
            principal_mismatches=list(req.principal_skill_mismatches),
            extraction_method=req.extraction_method,
        )
    return verdicts


def _check_llm_reachable(endpoint: str) -> bool:
    try:
        httpx.get(f"{endpoint}/v1/models", timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


def _run_phase2(jobs: list[Job], profile: Any, endpoint: str, model_name: str) -> dict[str, Phase2Verdict]:
    """Informational-only: Phase 2 job-understanding profile_match_score.

    NOT wired into accept/reject in production (see module docstring) —
    reported separately from the primary A/B comparison.
    """
    from src.ai.interfaces import LocalLLMProvider

    provider = LocalLLMProvider(
        endpoint=endpoint, model_name=model_name,
        temperature=0.0, max_tokens=800, timeout_seconds=45, thinking_enabled=False,
    )
    profile_dict = profile.model_dump()
    results: dict[str, Phase2Verdict] = {}
    for job in jobs:
        r = provider.analyze_job(job, profile=profile_dict)
        results[job.id] = Phase2Verdict(
            job_id=job.id,
            status=r.status,
            profile_match_score=r.analysis.profile_match_score,
            domain=r.analysis.domain,
            profile_match_reasons=r.analysis.profile_match_reasons,
            profile_mismatch_reasons=r.analysis.profile_mismatch_reasons,
        )
    return results


# ---------------------------------------------------------------------------
# Evaluation (ground truth joined here, and ONLY here)
# ---------------------------------------------------------------------------

def _final_verdict(
    job_id: str,
    filter_rejection_reasons: dict[str, str],
    gate_verdicts: dict[str, JobVerdict] | None,
) -> JobVerdict:
    if job_id in filter_rejection_reasons:
        return JobVerdict(
            job_id=job_id,
            verdict="REJECT",
            stage="filter",
            reasons=[f"domain/keyword filter: {filter_rejection_reasons[job_id]}"],
        )
    if gate_verdicts is not None and job_id in gate_verdicts:
        return gate_verdicts[job_id]
    # Passed the domain filter but the gate never ran for this job (e.g. no
    # profile loaded) — should not happen in this harness, but fail loud.
    return JobVerdict(job_id=job_id, verdict="ACCEPT", stage="passed")


def _score(expected: str, verdict: str) -> str:
    """Return 'correct' | 'incorrect' | 'n/a' (ground truth is BORDERLINE)."""
    if expected == "BORDERLINE":
        return "n/a"
    predicted_accept = verdict == "ACCEPT"
    expected_accept = expected == "SHOULD_ACCEPT"
    return "correct" if predicted_accept == expected_accept else "incorrect"


@dataclass
class Confusion:
    tp: int = 0  # should accept, did accept
    tn: int = 0  # should reject, did reject
    fp: int = 0  # should reject, did accept  (false positive: bad job let through)
    fn: int = 0  # should accept, did reject  (false negative: good job lost)
    borderline: int = 0

    @property
    def scored_total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.scored_total if self.scored_total else 0.0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0


def _confusion(job_ids: list[str], verdicts: dict[str, JobVerdict]) -> Confusion:
    c = Confusion()
    for jid in job_ids:
        expected = GROUND_TRUTH[jid]["expected"]
        v = verdicts[jid].verdict
        if expected == "BORDERLINE":
            c.borderline += 1
            continue
        should_accept = expected == "SHOULD_ACCEPT"
        did_accept = v == "ACCEPT"
        if should_accept and did_accept:
            c.tp += 1
        elif not should_accept and not did_accept:
            c.tn += 1
        elif not should_accept and did_accept:
            c.fp += 1
        else:
            c.fn += 1
    return c


def _category_breakdown(job_ids: list[str], verdicts: dict[str, JobVerdict]) -> dict[str, Confusion]:
    by_cat: dict[str, list[str]] = {}
    for jid in job_ids:
        by_cat.setdefault(GROUND_TRUTH[jid]["category"], []).append(jid)
    return {cat: _confusion(ids, verdicts) for cat, ids in by_cat.items()}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _build_report(
    job_ids: list[str],
    det_verdicts: dict[str, JobVerdict],
    ai_verdicts: dict[str, JobVerdict] | None,
    phase2: dict[str, Phase2Verdict] | None,
    ai_available: bool,
) -> str:
    lines: list[str] = []
    lines.append("# AI Filter Benchmark — 50 synthetic jobs\n")
    lines.append(f"AI (requirements-gate LLM) path executed: {'yes' if ai_available else 'NO — LM Studio unreachable'}\n")

    lines.append("\n## Per-job results\n")
    header = "| ID | Category | Ground Truth | Det. Result | AI Result | Agreement | Det. Correct | AI Correct |"
    sep = "|---|---|---|---|---|---|---|---|"
    lines.append(header)
    lines.append(sep)
    for jid in job_ids:
        gt = GROUND_TRUTH[jid]
        det = det_verdicts[jid]
        ai = ai_verdicts[jid] if ai_verdicts else None
        agreement = "n/a" if ai is None else ("same" if det.verdict == ai.verdict else "DIFFER")
        det_c = _score(gt["expected"], det.verdict)
        ai_c = "n/a" if ai is None else _score(gt["expected"], ai.verdict)
        lines.append(
            f"| {jid} | {gt['category']} | {gt['expected']} | {det.verdict} ({det.stage}) | "
            f"{(ai.verdict + ' (' + ai.stage + ')') if ai else 'n/a'} | {agreement} | {det_c} | {ai_c} |"
        )

    lines.append("\n## Confusion matrix — deterministic\n")
    det_c = _confusion(job_ids, det_verdicts)
    lines.append(_confusion_block(det_c))

    if ai_verdicts is not None:
        lines.append("\n## Confusion matrix — deterministic + AI\n")
        ai_c = _confusion(job_ids, ai_verdicts)
        lines.append(_confusion_block(ai_c))
        lines.append(f"\n**Overall improvement: deterministic {det_c.tp + det_c.tn}/{det_c.scored_total} "
                      f"→ AI-enhanced {ai_c.tp + ai_c.tn}/{ai_c.scored_total} "
                      f"({(ai_c.tp + ai_c.tn) - (det_c.tp + det_c.tn):+d} jobs)**\n")

    lines.append("\n## Category breakdown — deterministic\n")
    for cat, c in _category_breakdown(job_ids, det_verdicts).items():
        lines.append(f"- **{cat}**: {c.tp + c.tn}/{c.scored_total} correct "
                      f"(TP={c.tp} TN={c.tn} FP={c.fp} FN={c.fn} borderline={c.borderline})")

    if ai_verdicts is not None:
        lines.append("\n## Category breakdown — deterministic + AI\n")
        for cat, c in _category_breakdown(job_ids, ai_verdicts).items():
            lines.append(f"- **{cat}**: {c.tp + c.tn}/{c.scored_total} correct "
                          f"(TP={c.tp} TN={c.tn} FP={c.fp} FN={c.fn} borderline={c.borderline})")

    if ai_verdicts is not None:
        lines.append("\n## Disagreements between deterministic and AI paths\n")
        disagreements = [jid for jid in job_ids if det_verdicts[jid].verdict != ai_verdicts[jid].verdict]
        if not disagreements:
            lines.append("None — the AI path never changed a single accept/reject decision.\n")
        else:
            for jid in disagreements:
                gt = GROUND_TRUTH[jid]
                lines.append(
                    f"- **{jid}** ({gt['category']}, expected {gt['expected']}): "
                    f"det={det_verdicts[jid].verdict} → ai={ai_verdicts[jid].verdict}. "
                    f"Det. reasons: {det_verdicts[jid].reasons or det_verdicts[jid].gate_flags}. "
                    f"AI reasons: {ai_verdicts[jid].reasons or ai_verdicts[jid].gate_flags}."
                )

    if phase2 is not None:
        lines.append("\n## Informational: Phase 2 profile_match_score (NOT wired into accept/reject)\n")
        for jid in job_ids:
            gt = GROUND_TRUTH[jid]
            p2 = phase2.get(jid)
            if p2 is None or p2.status != "success":
                lines.append(f"- {jid}: Phase 2 status={p2.status if p2 else 'missing'}")
                continue
            lines.append(
                f"- {jid} ({gt['category']}, expected {gt['expected']}): "
                f"profile_match_score={p2.profile_match_score} domain={p2.domain!r}"
            )

    return "\n".join(lines)


def _confusion_block(c: Confusion) -> str:
    return (
        f"- Accuracy: {c.accuracy:.1%} ({c.tp + c.tn}/{c.scored_total} scored jobs; "
        f"{c.borderline} borderline excluded)\n"
        f"- Precision (of accepted, truly should-accept): {c.precision:.1%}\n"
        f"- Recall (of should-accept, actually accepted): {c.recall:.1%}\n"
        f"- True positives (correctly accepted): {c.tp}\n"
        f"- True negatives (correctly rejected): {c.tn}\n"
        f"- False positives (bad job wrongly accepted): {c.fp}\n"
        f"- False negatives (good job wrongly rejected): {c.fn}\n"
    )


# ---------------------------------------------------------------------------
# Public entry point (importable by the pytest wrapper)
# ---------------------------------------------------------------------------

def run_benchmark(
    config_dir: str = "config",
    endpoint: str = "http://localhost:1234",
    model_name: str = "local-model",
    run_ai: bool = True,
    run_phase2: bool = True,
    profile_path: str | None = None,
) -> dict[str, Any]:
    """Run the full benchmark and return structured results (no printing)."""
    jobs = [Job(**kwargs) for kwargs in JOBS]
    job_ids = [j["id"] for j in JOBS]
    assert set(job_ids) == set(GROUND_TRUTH.keys()), "jobs.py and ground_truth.py ID sets diverged"

    passing_jobs, filter_rejections, profile = _run_domain_filter(jobs, config_dir, profile_path)

    det_gate_verdicts = _run_requirements_path(passing_jobs, profile, llm_provider=None)
    det_verdicts = {
        jid: _final_verdict(jid, filter_rejections, det_gate_verdicts) for jid in job_ids
    }

    ai_verdicts: dict[str, JobVerdict] | None = None
    phase2_results: dict[str, Phase2Verdict] | None = None
    ai_available = False

    if run_ai:
        ai_available = _check_llm_reachable(endpoint)
        if ai_available:
            from src.ai.interfaces import LocalLLMProvider
            llm_provider = LocalLLMProvider(
                endpoint=endpoint, model_name=model_name,
                temperature=0.0, max_tokens=512, timeout_seconds=30, thinking_enabled=False,
            )
            ai_gate_verdicts = _run_requirements_path(passing_jobs, profile, llm_provider=llm_provider)
            ai_verdicts = {
                jid: _final_verdict(jid, filter_rejections, ai_gate_verdicts) for jid in job_ids
            }
            if run_phase2:
                phase2_results = _run_phase2(passing_jobs, profile, endpoint, model_name)

    return {
        "job_ids": job_ids,
        "det_verdicts": det_verdicts,
        "ai_verdicts": ai_verdicts,
        "phase2_results": phase2_results,
        "ai_available": ai_available,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config")
    parser.add_argument(
        "--profile",
        default=None,
        help="profile YAML to use instead of <config>/profile.yaml "
        "(e.g. tests/fixtures/ai_filter_benchmark/profile.yaml)",
    )
    parser.add_argument("--endpoint", default="http://localhost:1234")
    parser.add_argument("--model-name", default="local-model")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--skip-phase2", action="store_true")
    parser.add_argument("--report-path", default="scripts/ai_filter_benchmark_report.md")
    args = parser.parse_args()

    t0 = time.perf_counter()
    result = run_benchmark(
        config_dir=args.config,
        profile_path=args.profile,
        endpoint=args.endpoint,
        model_name=args.model_name,
        run_ai=not args.skip_ai,
        run_phase2=not args.skip_phase2,
    )
    elapsed = time.perf_counter() - t0

    if not result["ai_available"] and not args.skip_ai:
        print(f"\nNo local LLM server reachable at {args.endpoint}.")
        print("Deterministic baseline WILL be reported; the AI-enhanced comparison could NOT be executed.\n")

    report = _build_report(
        result["job_ids"], result["det_verdicts"], result["ai_verdicts"],
        result["phase2_results"], result["ai_available"],
    )

    # Write the file FIRST — the report is the deliverable; a console encoding
    # quirk (Windows cp1252 can't print some punctuation) must never cost us
    # the file itself.
    report_path = Path(args.report_path)
    report_path.write_text(report + f"\n\n(benchmark wall time: {elapsed:.1f}s)\n", encoding="utf-8")

    print(report.encode("ascii", errors="replace").decode("ascii"))
    print(f"\n(benchmark wall time: {elapsed:.1f}s)")
    print(f"\nFull report written to {report_path}")


if __name__ == "__main__":
    main()
