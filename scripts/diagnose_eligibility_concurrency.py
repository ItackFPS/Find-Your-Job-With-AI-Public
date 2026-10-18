"""Controlled experiment: LM Studio eligibility-gate concurrency reliability.

READ-ONLY DIAGNOSTIC. Never writes to data/jobs.db, never changes production
config or concurrency defaults. Builds the REAL eligibility prompts (via
src.ai.eligibility._assemble_evidence + src.ai.prompt.build_eligibility_prompt)
for the same 18 real jobs that reached the eligibility stage in the
2026-09-05 50-job benchmark, then calls the real LocalLLMProvider._call_llm
against those exact prompts at max_workers = 1, 2, 3, 4 in turn, recording
successes/failures/latency/wall time and any context/slot-related error text.

Usage:
    python scripts/diagnose_eligibility_concurrency.py
        [--endpoint http://localhost:1234] [--model-name qwen/qwen3-8b]
        [--db data/jobs.db] [--config config]
        [--job-ids-file scripts/real_jobs_50_raw_after_fix3.json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ai.eligibility import _assemble_evidence
from src.ai.interfaces import LocalLLMProvider
from src.ai.prompt import build_eligibility_prompt
from src.models.job import Job
from src.pipeline.filter import load_profile
from src.requirements.extractor import RequirementExtractor


def _load_jobs_by_ids(db_path: str, job_ids: list[str]) -> list[Job]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join(["?"] * len(job_ids))
    rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids).fetchall()
    conn.close()
    by_id = {r["id"]: dict(r) for r in rows}
    return [Job.from_dict(by_id[jid]) for jid in job_ids if jid in by_id]


def _build_real_prompts(jobs: list[Job], profile) -> list[str]:
    """Reconstruct the exact eligibility prompts EligibilityGate would build
    for these jobs — same evidence-assembly function, same prompt builder,
    heuristic-only requirements (sufficient: evidence text length is what
    drives prompt size, and heuristic extraction already finds the same
    education/experience spans an LLM-enriched pass would keep verbatim)."""
    extractor = RequirementExtractor(user_skills=list(profile.target_skills))
    profile_dict = profile.model_dump()
    prompts = []
    for job in jobs:
        req = extractor.extract(job)
        evidence, _has_evidence = _assemble_evidence(job, req)
        prompts.append(build_eligibility_prompt(job.title or "", evidence, profile_dict))
    return prompts


def _run_at_concurrency(
    provider: LocalLLMProvider, prompts: list[str], max_workers: int
) -> dict[str, Any]:
    latencies: list[float] = []
    errors: list[str] = []
    successes = 0

    def call(prompt: str) -> tuple[bool, float, str | None]:
        t0 = time.perf_counter()
        try:
            provider._call_llm(prompt)
            return True, time.perf_counter() - t0, None
        except Exception as exc:  # noqa: BLE001 — capturing for diagnostics, not production
            return False, time.perf_counter() - t0, str(exc)

    wall_t0 = time.perf_counter()
    if max_workers <= 1:
        results = [call(p) for p in prompts]
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(call, prompts))
    wall_elapsed = time.perf_counter() - wall_t0

    for ok, latency, err in results:
        if ok:
            successes += 1
            latencies.append(latency)
        else:
            errors.append(err or "unknown error")

    return {
        "max_workers": max_workers,
        "total_calls": len(prompts),
        "successful": successes,
        "failed": len(errors),
        "avg_latency_seconds": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "wall_clock_seconds": round(wall_elapsed, 2),
        "context_slot_errors": sorted({e for e in errors if "context" in e.lower() or "exceed" in e.lower()}),
        "other_errors": sorted({e for e in errors if "context" not in e.lower() and "exceed" not in e.lower()}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--endpoint", default="http://localhost:1234")
    parser.add_argument("--model-name", default="qwen/qwen3-8b")
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--config", default="config")
    parser.add_argument("--job-ids-file", default="scripts/real_jobs_50_raw_after_fix3.json")
    args = parser.parse_args()

    prior = json.loads(Path(args.job_ids_file).read_text(encoding="utf-8"))
    job_ids = [j["id"] for j in prior["jobs"] if j.get("eligibility") is not None]
    print(f"Loaded {len(job_ids)} real eligibility-stage job ids from {args.job_ids_file}")

    jobs = _load_jobs_by_ids(args.db, job_ids)
    print(f"Loaded {len(jobs)}/{len(job_ids)} jobs from {args.db}")

    profile = load_profile(str(Path(args.config) / "profile.yaml"))
    prompts = _build_real_prompts(jobs, profile)
    prompt_lens = [len(p) for p in prompts]
    print(f"Built {len(prompts)} real eligibility prompts "
          f"(length chars: min={min(prompt_lens)}, max={max(prompt_lens)}, avg={sum(prompt_lens)//len(prompt_lens)})")

    provider = LocalLLMProvider(
        endpoint=args.endpoint, model_name=args.model_name,
        temperature=0.0, max_tokens=100, timeout_seconds=60, thinking_enabled=False,
    )

    results = []
    for mw in (1, 2, 3, 4):
        print(f"\n--- max_workers={mw} ---")
        r = _run_at_concurrency(provider, prompts, mw)
        results.append(r)
        print(json.dumps(r, indent=2, ensure_ascii=False))

    print("\n=== Summary ===")
    print(f"{'max_workers':>11} | {'ok':>3} | {'failed':>6} | {'avg_latency_s':>13} | {'wall_s':>7} | context/slot errors")
    for r in results:
        print(
            f"{r['max_workers']:>11} | {r['successful']:>3} | {r['failed']:>6} | "
            f"{str(r['avg_latency_seconds']):>13} | {r['wall_clock_seconds']:>7} | "
            f"{len(r['context_slot_errors'])}"
        )

    Path("scripts/eligibility_concurrency_diagnostic.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print("\nWrote scripts/eligibility_concurrency_diagnostic.json")


if __name__ == "__main__":
    main()
