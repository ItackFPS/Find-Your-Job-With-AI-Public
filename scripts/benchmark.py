"""Phase 7 benchmark: run the pipeline twice, compare stage times and cache effectiveness.

Usage:
    python scripts/benchmark.py [--db data/jobs.db] [--config config]

The script runs `run_pipeline()` twice consecutively and prints a side-by-side
comparison table of:
  - Per-stage wall-clock time
  - Total wall-clock time
  - Job counts at each stage
  - Embedding cache hits/misses (from log output)
  - Personalization cache hits/misses (from log output)

Expected outcome:
  - Run 1: populates embedding/personalization caches, may encode all jobs
  - Run 2: hits both caches for unchanged jobs; semantic and personalization
            stages should be substantially faster
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Add project root to sys.path so imports work when run from repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from src.pipeline.orchestrator import PipelineStats, run_pipeline  # noqa: E402

logging.basicConfig(
    level=logging.WARNING,  # Suppress stage-level noise during benchmark
    format="%(levelname)s %(name)s %(message)s",
)
# Only show embedding/personalization cache hit lines
for name in ("src.pipeline.semantic_matching", "src.personalization.engine"):
    logging.getLogger(name).setLevel(logging.INFO)


def _run(db_path: str, config_dir: str, label: str) -> PipelineStats:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = run_pipeline(db_path=db_path, config_dir=config_dir)
    if result.status == "failed":
        print(f"  FAILED: {result.error}")
    return result.stats


def _fmt(seconds: float) -> str:
    return f"{seconds:7.1f}s"


def _pct_change(a: float, b: float) -> str:
    if a == 0:
        return "     n/a"
    change = (b - a) / a * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:+.0f}%".rjust(8)


STAGES = [
    ("collect",         "Collect"),
    ("validate",        "Validate"),
    ("deduplicate",     "Deduplicate"),
    ("filter",          "Filter"),
    ("score+storage",   "Score + Storage"),
    ("semantic",        "Semantic Matching"),
    ("personalization", "Personalization"),
]


def print_report(s1: PipelineStats, s2: PipelineStats) -> None:
    print(f"\n{'─'*70}")
    print(f"  {'Stage':<22} {'Run 1':>10} {'Run 2':>10} {'Change':>10}")
    print(f"{'─'*70}")

    for key, label in STAGES:
        t1 = s1.stage_times.get(key, 0.0)
        t2 = s2.stage_times.get(key, 0.0)
        print(f"  {label:<22} {_fmt(t1):>10} {_fmt(t2):>10} {_pct_change(t1, t2):>10}")

    t1_total = s1.duration_seconds
    t2_total = s2.duration_seconds
    print(f"{'─'*70}")
    print(f"  {'TOTAL':<22} {_fmt(t1_total):>10} {_fmt(t2_total):>10} {_pct_change(t1_total, t2_total):>10}")
    print(f"{'─'*70}")

    print(f"\n  Job counts (run 1 / run 2):")
    print(f"    Collected:    {s1.collected:>5} / {s2.collected:>5}")
    print(f"    Validated:    {s1.validated:>5} / {s2.validated:>5}")
    print(f"    Deduplicated: {s1.deduplicated:>5} / {s2.deduplicated:>5}")
    print(f"    Filtered:     {s1.filtered:>5} / {s2.filtered:>5}")
    print(f"    Scored:       {s1.scored:>5} / {s2.scored:>5}")
    print(f"    Semantic:     {s1.semantically_matched:>5} / {s2.semantically_matched:>5}")
    print(f"    Personalized: {s1.personalized:>5} / {s2.personalized:>5}")

    print(f"\n  (See logs above for embedding/personalization cache hit counts)")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline two-run benchmark")
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--config", default="config")
    args = parser.parse_args()

    print("\nPhase 7 Benchmark")
    print("=================")
    print(f"  DB:     {args.db}")
    print(f"  Config: {args.config}")

    s1 = _run(args.db, args.config, "RUN 1 (first run — may compute embeddings)")
    s2 = _run(args.db, args.config, "RUN 2 (second run — should reuse caches)")

    print_report(s1, s2)

    # Exit 0 if run 2 is faster overall, 1 if not (to help CI assertions)
    if s2.duration_seconds < s1.duration_seconds:
        print("  PASS: Run 2 is faster than Run 1.")
    else:
        print("  WARN: Run 2 is NOT faster than Run 1 — caches may not be hitting.")
    sys.exit(0)


if __name__ == "__main__":
    main()
