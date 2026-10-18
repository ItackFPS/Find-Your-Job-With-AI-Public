"""Profile RequirementExtractor.extract() — the actual hot loop inside the
orchestrator's "requirements_gate" stage (227.9s in the 2026-09-05 full
pipeline benchmark, the second-largest cost after Phase 2).

Read-only: loads jobs already sitting in data/jobs.db, does not run the
pipeline or trigger a live run. Uses the real RequirementExtractor / profile
config exactly as src/pipeline/orchestrator.py wires them (heuristic-only,
matching llm_extraction_enabled: false in profile.yaml — the current
production setting).

Usage:
    python scripts/profile_requirements_gate.py [--limit 5000]
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
DB_PATH = str(ROOT / "data" / "jobs.db")
CONFIG_DIR = str(ROOT / "config")


def _load_sample_jobs(limit: int) -> list:
    from src.storage.sqlite import Storage

    storage = Storage(db_path=DB_PATH)
    storage.init_db()
    conn = storage._get_conn()  # read-only usage, same pattern as other scripts
    rows = conn.execute(
        "SELECT id FROM jobs WHERE status = 'active' ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    jobs = []
    for row in rows:
        job = storage.get_job_by_id(row[0])
        if job is not None:
            jobs.append(job)
    storage.close()
    return jobs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    from src.pipeline.filter import load_profile
    from src.requirements.extractor import RequirementExtractor

    profile = load_profile(str(Path(CONFIG_DIR) / "profile.yaml"))
    print(f"llm_extraction_enabled: {profile.matching.llm_extraction_enabled}")

    jobs = _load_sample_jobs(args.limit)
    print(f"Loaded {len(jobs)} real jobs from {DB_PATH}")
    if not jobs:
        print("No jobs found — nothing to profile.")
        return 1

    extractor = RequirementExtractor(
        user_skills=list(profile.target_skills),
        llm_provider=None,  # heuristic-only — matches current production config
    )

    # Warm up (first call may pay import/regex-compile costs) before timing.
    extractor.extract(jobs[0])

    t0 = time.perf_counter()
    profiler = cProfile.Profile()
    profiler.enable()
    for job in jobs:
        extractor.extract(job)
    profiler.disable()
    elapsed = time.perf_counter() - t0

    print(f"\nTotal: {elapsed:.3f}s for {len(jobs)} jobs ({elapsed / len(jobs) * 1000:.3f} ms/job)")
    print(f"Extrapolated to 9,131 jobs (the benchmark's requirements_gate_input): "
          f"{elapsed / len(jobs) * 9131:.1f}s")

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.strip_dirs()

    print("\n=== Top 25 by CUMULATIVE time ===")
    stats.sort_stats("cumulative").print_stats(25)
    print(stream.getvalue())

    stream2 = io.StringIO()
    stats2 = pstats.Stats(profiler, stream=stream2)
    stats2.strip_dirs()
    print("=== Top 25 by TOTAL (self) time ===")
    stats2.sort_stats("tottime").print_stats(25)
    print(stream2.getvalue())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
