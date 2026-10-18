"""Benchmark: heuristic-only vs heuristic+LLM requirements extraction.

Answers the question the LLM upgrade must earn before it is ever enabled by
default: does it materially improve detection, or is it just extra code?

Samples real jobs from the local jobs database (read-only — never writes to
it) and runs both extraction modes through the same RequirementExtractor /
HardConstraintGate code path production uses, then reports concrete counts:

  - education requirements detected
  - mandatory / preferred skills detected
  - principal-tech detected
  - jobs where the gate decision (pass/reject) differs between modes
  - "false hard rejections": jobs that pass heuristic-only but are rejected
    in combined mode SOLELY because of an LLM claim (should be ~0 by
    construction — the evidence-authority rule blocks uncorroborated claims
    from rejecting; a non-zero count here is a design regression, not a
    quality tradeoff)
  - UNKNOWN rate (GateResult.uncertainty)
  - number of jobs where LLM enrichment actually changed anything
  - corroborated vs uncorroborated LLM claims

Usage:
    python scripts/benchmark_requirements_llm.py [--db data/jobs.db]
        [--sample-size 200] [--endpoint http://localhost:1234] [--seed 42]

If no local LLM server is reachable at --endpoint, the script reports that
plainly and exits without fabricating numbers — pass --mock-llm to instead
run a self-consistency check of the harness itself against a canned
response (clearly NOT a quality measurement of a real model).
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from src.models.job import Job
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate
from src.requirements.models import EvidenceSource


class _CallCountingProvider:
    """Wraps a real LLM provider (or a mock) to count invocations and time
    each one, without touching production code — instrumentation lives only
    here in the benchmark harness.
    """

    def __init__(self, inner):
        self._inner = inner
        self.call_count = 0
        self.call_latencies: list[float] = []

    def _call_llm(self, prompt: str) -> str:
        self.call_count += 1
        t0 = time.perf_counter()
        try:
            return self._inner._call_llm(prompt)
        finally:
            self.call_latencies.append(time.perf_counter() - t0)

# A representative candidate profile: broad enough that most jobs have some
# hard-filter-relevant content, narrow enough that principal-skill mismatches
# actually occur (mirrors config/profile.yaml's spirit, not its exact values).
_CANDIDATE_SKILLS = ["python", "javascript", "sql", "docker", "git", "linux"]
_CANDIDATE_MAX_EDU = 2  # Bac+2


@dataclass
class ModeMetrics:
    label: str
    jobs_evaluated: int = 0
    education_detected: int = 0
    mandatory_skills_detected: int = 0
    preferred_skills_detected: int = 0
    principal_tech_detected: int = 0
    rejected: int = 0
    passed_with_uncertainty: int = 0
    passed_clean: int = 0
    rejected_job_ids: set[str] = field(default_factory=set)


def _load_jobs(db_path: str, sample_size: int, seed: int) -> list[Job]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT id, source, title, company, description, location, countries, "
        "remote, employment_type, discovered_at, skills, raw_payload "
        "FROM jobs WHERE description IS NOT NULL AND length(description) > 200 "
        "AND status = 'active'"
    )
    rows = cur.fetchall()
    conn.close()

    random.Random(seed).shuffle(rows)
    rows = rows[:sample_size]

    jobs: list[Job] = []
    for row in rows:
        try:
            jobs.append(Job(
                id=row["id"],
                source=row["source"],
                title=row["title"] or "",
                company=row["company"] or "",
                description=row["description"] or "",
                location=row["location"] or "",
                countries=json.loads(row["countries"] or "[]"),
                remote=bool(row["remote"]),
                employment_type=row["employment_type"],
                discovered_at=row["discovered_at"],
                skills=json.loads(row["skills"] or "[]"),
                raw_payload={},  # fresh dict per job — never mutate the DB row
            ))
        except Exception:
            continue  # skip malformed rows; this is a benchmark, not a migration
    return jobs


def _check_llm_reachable(endpoint: str) -> bool:
    try:
        httpx.get(endpoint, timeout=3.0)
        return True
    except httpx.HTTPError:
        return False


def _evaluate(jobs: list[Job], extractor: RequirementExtractor, label: str) -> tuple[ModeMetrics, dict]:
    metrics = ModeMetrics(label=label)
    requirements_by_job = {}
    corroborated_claims = 0
    uncorroborated_claims = 0
    combined_count = 0

    for job in jobs:
        req = extractor.extract(job)
        requirements_by_job[job.id] = req
        metrics.jobs_evaluated += 1

        if req.extraction_method == "combined":
            combined_count += 1

        if req.education is not None:
            metrics.education_detected += 1
            if req.education.source == EvidenceSource.LLM_CORROBORATED:
                corroborated_claims += 1
            elif req.education.source == EvidenceSource.LLM_UNCORROBORATED:
                uncorroborated_claims += 1

        for sk in req.skills:
            if sk.source == EvidenceSource.LLM_CORROBORATED:
                corroborated_claims += 1
            elif sk.source == EvidenceSource.LLM_UNCORROBORATED:
                uncorroborated_claims += 1
            if sk.importance.value == "mandatory":
                metrics.mandatory_skills_detected += 1
            elif sk.importance.value == "preferred":
                metrics.preferred_skills_detected += 1

        metrics.principal_tech_detected += len(req.principal_skill_mismatches)
        if req.principal_skill_uncorroborated:
            uncorroborated_claims += len(req.principal_skill_uncorroborated)

        gate = HardConstraintGate(
            user_max_education_level=_CANDIDATE_MAX_EDU,
            profile_version_hash="benchmark",
        )
        result = gate.check(req)
        if not result.passes:
            metrics.rejected += 1
            metrics.rejected_job_ids.add(job.id)
        elif result.uncertainty:
            metrics.passed_with_uncertainty += 1
        else:
            metrics.passed_clean += 1

    extras = {
        "combined_extractions": combined_count,
        "corroborated_claims": corroborated_claims,
        "uncorroborated_claims": uncorroborated_claims,
        "requirements_by_job": requirements_by_job,
    }
    return metrics, extras


def _print_report(
    heuristic: ModeMetrics, heuristic_extras: dict,
    combined: ModeMetrics, combined_extras: dict,
) -> None:
    n = heuristic.jobs_evaluated
    print("\n" + "=" * 70)
    print(f"  REQUIREMENTS-GATE LLM BENCHMARK — {n} real jobs sampled")
    print("=" * 70)
    print(f"{'metric':<32} {'heuristic':>15} {'heuristic+llm':>15}")
    print("-" * 70)

    def row(label, a, b):
        print(f"{label:<32} {a:>15} {b:>15}")

    row("education detected", heuristic.education_detected, combined.education_detected)
    row("mandatory skills detected", heuristic.mandatory_skills_detected, combined.mandatory_skills_detected)
    row("preferred skills detected", heuristic.preferred_skills_detected, combined.preferred_skills_detected)
    row("principal-tech detected", heuristic.principal_tech_detected, combined.principal_tech_detected)
    row("rejected (hard filter)", heuristic.rejected, combined.rejected)
    row("passed w/ uncertainty", heuristic.passed_with_uncertainty, combined.passed_with_uncertainty)
    row("passed clean", heuristic.passed_clean, combined.passed_clean)
    print("-" * 70)
    row("jobs LLM-enriched (combined)", "n/a", combined_extras["combined_extractions"])
    row("corroborated LLM claims", "n/a", combined_extras["corroborated_claims"])
    row("uncorroborated LLM claims", "n/a", combined_extras["uncorroborated_claims"])
    print("-" * 70)
    row("LLM calls made", "n/a", combined_extras["llm_calls"])
    row("LLM call % of jobs", "n/a", f"{combined_extras['llm_call_pct']:.1f}%")
    row("mean latency/call made", "n/a", f"{combined_extras['mean_call_latency']:.2f}s")
    row("wall time (combined pass)", "n/a", f"{combined_extras['combined_wall_time']:.1f}s")
    print("-" * 70)

    only_heuristic_rejects = heuristic.rejected_job_ids - combined.rejected_job_ids
    only_combined_rejects = combined.rejected_job_ids - heuristic.rejected_job_ids

    print(f"jobs rejected by heuristic but NOT by combined: {len(only_heuristic_rejects)}")
    print(
        f"jobs rejected by combined but NOT by heuristic "
        f"('false hard rejections' if caused by an uncorroborated LLM claim — "
        f"should be 0 by construction): {len(only_combined_rejects)}"
    )
    if only_combined_rejects:
        print("  [!] Investigate these -- the safety rule may have a gap:")
        for jid in list(only_combined_rejects)[:10]:
            print(f"    - {jid}")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--endpoint", default="http://localhost:1234")
    parser.add_argument("--model-name", default="local-model")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--mock-llm", action="store_true",
        help="Run with a canned mock response instead of a real model — proves the "
             "harness mechanics only, NOT a quality measurement.",
    )
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Database not found: {args.db}")
        sys.exit(1)

    jobs = _load_jobs(args.db, args.sample_size, args.seed)
    if not jobs:
        print("No eligible jobs found in the database (need description length > 200, status='active').")
        sys.exit(1)
    print(f"Loaded {len(jobs)} real jobs from {args.db}")

    llm_provider = None
    if args.mock_llm:
        from unittest.mock import MagicMock
        print(
            "\n*** --mock-llm: using a canned response for every job. This proves the "
            "benchmark harness and safety-gate wiring work end-to-end; it is NOT a "
            "measurement of a real model's extraction quality. ***\n"
        )
        llm_provider = MagicMock()
        llm_provider._call_llm.return_value = json.dumps({
            "education": None,
            "required_skills": [],
            "preferred_skills": [],
            "is_principal_tech_role": False,
            "principal_technologies": [],
        })
    else:
        reachable = _check_llm_reachable(args.endpoint)
        if not reachable:
            print(
                f"\nNo local LLM server reachable at {args.endpoint}.\n"
                "Cannot produce a real heuristic-vs-LLM comparison without one — "
                "start LM Studio/Ollama at this endpoint (or pass --endpoint) and "
                "re-run, or pass --mock-llm to verify the harness mechanics only.\n"
            )
            sys.exit(2)
        from src.ai.interfaces import LocalLLMProvider
        llm_provider = LocalLLMProvider(
            endpoint=args.endpoint, model_name=args.model_name,
            temperature=0.0, max_tokens=512, timeout_seconds=20,
        )

    counting_provider = _CallCountingProvider(llm_provider)

    heuristic_extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS, llm_provider=None)
    combined_extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS, llm_provider=counting_provider)

    heuristic_metrics, heuristic_extras = _evaluate(jobs, heuristic_extractor, "heuristic")

    t_start = time.perf_counter()
    combined_metrics, combined_extras = _evaluate(jobs, combined_extractor, "combined")
    combined_wall_time = time.perf_counter() - t_start

    combined_extras["llm_calls"] = counting_provider.call_count
    combined_extras["llm_call_pct"] = (
        100.0 * counting_provider.call_count / len(jobs) if jobs else 0.0
    )
    combined_extras["combined_wall_time"] = combined_wall_time
    combined_extras["mean_call_latency"] = (
        sum(counting_provider.call_latencies) / len(counting_provider.call_latencies)
        if counting_provider.call_latencies else 0.0
    )

    _print_report(heuristic_metrics, heuristic_extras, combined_metrics, combined_extras)


if __name__ == "__main__":
    main()
