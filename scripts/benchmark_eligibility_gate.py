"""Benchmark: the LLM semantic eligibility gate (src/ai/eligibility.py) against
the false-rejection-prone synthetic cases in
``tests/fixtures/eligibility_benchmark/``.

Answers two questions:
  1. Latency — how fast is the compact 3-field prompt vs. the old verbose
     Phase 2 job-understanding prompt (see the engineering report / plan for
     the baseline numbers this compares against)?
  2. Accuracy — does the gate correctly PASS jobs whose title alone looks
     risky but whose context (alternance/internship/explicit lower level/
     description targeting juniors) makes them accessible, while still
     REJECTing genuinely mismatched implicit-senior/implicit-Bac+5 roles?
     Precision of rejection matters most here — a false REJECT silently
     removes a viable job from the candidate's results.

Drives the REAL production code path: RequirementExtractor (heuristic-only,
exactly as production runs it before the eligibility gate) for evidence,
then EligibilityGate.evaluate_job for the decision — same classes
orchestrator.py wires together.

If no local LLM server is reachable, this script reports that plainly and
exits without fabricating numbers.

Usage:
    python scripts/benchmark_eligibility_gate.py [--config config]
        [--endpoint http://localhost:1234] [--model-name qwen/qwen3-8b]
        [--reject-confidence 0.75] [--report-path <path>]
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

from src.ai.eligibility import EligibilityGate
from src.models.job import Job
from src.pipeline.filter import load_profile
from src.requirements.extractor import RequirementExtractor

from tests.fixtures.eligibility_benchmark.jobs import JOBS
from tests.fixtures.eligibility_benchmark.ground_truth import GROUND_TRUTH


class _TimingProvider:
    """Wraps a real LocalLLMProvider to record per-call latency without
    touching production code — instrumentation lives only in this script."""

    def __init__(self, inner):
        self._inner = inner
        self.model_name = inner.model_name
        self.latencies: list[float] = []

    def _call_llm(self, prompt: str) -> str:
        t0 = time.perf_counter()
        try:
            return self._inner._call_llm(prompt)
        finally:
            self.latencies.append(time.perf_counter() - t0)


@dataclass
class Confusion:
    tp: int = 0  # should accept, did pass through (PASS or UNCERTAIN)
    tn: int = 0  # should reject, did reject (high-confidence REJECT)
    fp: int = 0  # should reject, did pass through — the dangerous case
    fn: int = 0  # should accept, did reject — the "don't lose viable jobs" case
    uncertain: int = 0

    @property
    def scored_total(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.scored_total if self.scored_total else 0.0

    @property
    def rejection_precision(self) -> float:
        denom = self.tn + self.fp
        return self.tn / denom if denom else 0.0

    @property
    def acceptance_recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0


def _check_llm_reachable(endpoint: str) -> bool:
    try:
        httpx.get(f"{endpoint}/v1/models", timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


def run_benchmark(
    config_dir: str = "config",
    endpoint: str = "http://localhost:1234",
    model_name: str = "qwen/qwen3-8b",
    reject_confidence: float = 0.75,
) -> dict[str, Any]:
    jobs = [Job(**kwargs) for kwargs in JOBS]
    job_ids = [j["id"] for j in JOBS]
    assert set(job_ids) == set(GROUND_TRUTH.keys()), "jobs.py and ground_truth.py ID sets diverged"

    profile = load_profile(str(Path(config_dir) / "profile.yaml"))

    # Same heuristic extraction production runs before the eligibility gate —
    # no LLM involved here, matches orchestrator.py's requirements_map exactly.
    extractor = RequirementExtractor(user_skills=list(profile.target_skills), llm_provider=None)
    requirements_map = {job.id: extractor.extract(job) for job in jobs}

    ai_available = _check_llm_reachable(endpoint)
    if not ai_available:
        return {"job_ids": job_ids, "ai_available": False, "results": {}, "latencies": []}

    from src.ai.interfaces import LocalLLMProvider

    base_provider = LocalLLMProvider(
        endpoint=endpoint, model_name=model_name,
        temperature=0.0, max_tokens=100, timeout_seconds=30, thinking_enabled=False,
    )
    timing_provider = _TimingProvider(base_provider)
    gate = EligibilityGate(llm_provider=timing_provider, max_workers=1)

    profile_dict = profile.model_dump()
    results = {}
    for job in jobs:
        result = gate.evaluate_job(job, requirements_map.get(job.id), profile_dict)
        results[job.id] = result

    return {
        "job_ids": job_ids,
        "ai_available": True,
        "results": results,
        "latencies": timing_provider.latencies,
    }


def _confusion(job_ids: list[str], results: dict, reject_confidence: float) -> Confusion:
    c = Confusion()
    for jid in job_ids:
        expected = GROUND_TRUTH[jid]["expected"]
        result = results[jid]
        if result.status != "success":
            # A gate failure fails open (never rejects) — score as "accepted".
            did_reject = False
        else:
            d = result.decision
            did_reject = d.decision == "REJECT" and d.confidence >= reject_confidence
            if d.decision == "UNCERTAIN":
                c.uncertain += 1

        should_reject = expected == "SHOULD_REJECT"
        if should_reject and did_reject:
            c.tn += 1  # correctly rejected a mismatched job
        elif should_reject and not did_reject:
            c.fp += 1  # missed a real mismatch — let a bad job through
        elif not should_reject and did_reject:
            c.fn += 1  # wrongly rejected a viable job — the dangerous case
        else:
            c.tp += 1  # correctly passed through a viable job
    return c


def _build_report(job_ids: list[str], results: dict, latencies: list[float], reject_confidence: float) -> str:
    lines: list[str] = []
    lines.append("# LLM Eligibility Gate Benchmark\n")
    lines.append(f"reject_confidence threshold: {reject_confidence}\n")

    lines.append("\n## Per-job results\n")
    lines.append("| ID | Category | Expected | Decision | Confidence | Reason | Latency |")
    lines.append("|---|---|---|---|---|---|---|")
    for jid in job_ids:
        gt = GROUND_TRUTH[jid]
        r = results[jid]
        if r.status != "success":
            lines.append(f"| {jid} | {gt['category']} | {gt['expected']} | FAILED ({r.error_message}) | - | - | - |")
            continue
        d = r.decision
        lines.append(
            f"| {jid} | {gt['category']} | {gt['expected']} | {d.decision} | {d.confidence:.2f} | {d.reason} | - |"
        )

    c = _confusion(job_ids, results, reject_confidence)
    lines.append("\n## Confusion matrix\n")
    lines.append(f"- Accuracy: {c.accuracy:.1%} ({c.tp + c.tn}/{c.scored_total})")
    lines.append(f"- Rejection precision (of rejected, truly should-reject): {c.rejection_precision:.1%}")
    lines.append(f"- Acceptance recall (of should-accept, actually passed through): {c.acceptance_recall:.1%}")
    lines.append(f"- True negatives (correctly rejected): {c.tn}")
    lines.append(f"- False negatives (viable job wrongly rejected — the dangerous case): {c.fn}")
    lines.append(f"- False positives (mismatched job wrongly accepted): {c.fp}")
    lines.append(f"- UNCERTAIN rate: {c.uncertain}/{len(job_ids)}")

    if latencies:
        ls = sorted(latencies)
        p50 = ls[len(ls) // 2]
        p95 = ls[min(len(ls) - 1, int(len(ls) * 0.95))]
        lines.append("\n## Latency\n")
        lines.append(f"- mean: {sum(latencies)/len(latencies):.2f}s")
        lines.append(f"- p50: {p50:.2f}s")
        lines.append(f"- p95: {p95:.2f}s")
        lines.append(f"- n: {len(latencies)}")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config")
    parser.add_argument("--endpoint", default="http://localhost:1234")
    parser.add_argument("--model-name", default="qwen/qwen3-8b")
    parser.add_argument("--reject-confidence", type=float, default=0.75)
    parser.add_argument("--report-path", default="scripts/eligibility_benchmark_report.md")
    args = parser.parse_args()

    result = run_benchmark(
        config_dir=args.config, endpoint=args.endpoint,
        model_name=args.model_name, reject_confidence=args.reject_confidence,
    )

    if not result["ai_available"]:
        print(f"\nNo local LLM server reachable at {args.endpoint}. Cannot run — no numbers fabricated.\n")
        sys.exit(2)

    report = _build_report(result["job_ids"], result["results"], result["latencies"], args.reject_confidence)
    report_path = Path(args.report_path)
    report_path.write_text(report + "\n", encoding="utf-8")
    print(report.encode("ascii", errors="replace").decode("ascii"))
    print(f"\nFull report written to {report_path}")


if __name__ == "__main__":
    main()
