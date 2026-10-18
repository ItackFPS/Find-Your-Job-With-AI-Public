"""CLI entry point for the job discovery pipeline.

Parses command-line arguments and delegates to the shared pipeline
orchestrator in src/pipeline/orchestrator.py.  The web API
(src/api/runner.py) calls the same orchestrator, so the pipeline
runs identically from both entry points.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from src.pipeline.orchestrator import run_pipeline


def main(argv: list[str] | None = None) -> None:
    """Run a single collection cycle via the CLI.

    Default (no flags): Phase 1 → Phase 3 → Phase 4
    With --ai:          Phase 1 → Phase 2 → Phase 3 → Phase 4

    The ``argv`` parameter is exposed for testing. Pass ``None`` (default)
    to read from ``sys.argv`` as normal.
    """
    parser = argparse.ArgumentParser(
        description="Find Your Job With AI — job discovery pipeline",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help=(
            "Enable Phase 2 local AI job understanding via Ollama/LM Studio. "
            "Requires the configured LLM server to be running. "
            "AI analysis enriches Phase 3 semantic scoring. Also activates the "
            "lightweight LLM semantic eligibility gate when "
            "matching.llm_eligibility_enabled is set in profile.yaml (see "
            "src/ai/eligibility.py) — runs before Phase 2, on requirements-gate "
            "survivors, to reduce how many jobs reach the more expensive call."
        ),
    )
    args = parser.parse_args(argv)

    result = run_pipeline(use_ai=args.ai)

    # ---- Output (mirrors the previous main.py output format) ----
    s = result.stats

    preflight_text = s.preflight.get("report_text") if s.preflight else None
    if preflight_text:
        print("\n" + preflight_text)

    if not (s.preflight and s.preflight.get("ready", True)):
        print(f"\nERROR: {result.error}")
        print("=" * 60 + "\n")
        return

    print("\n" + "=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)

    print(f"Total jobs collected:  {s.collected}")
    print(f"After validation:      {s.validated}")
    print(f"After deduplication:   {s.deduplicated}")
    print(f"After filtering:       {s.filtered}")
    print(f"After scoring:         {s.scored}")
    print(f"Semantically matched:  {s.semantically_matched}")
    print(f"After personalization: {s.personalized}")
    print(f"Duration:              {s.duration_seconds:.1f}s")

    if result.status == "failed":
        print(f"\nERROR: {result.error}")

    if result.scored_jobs:
        print("\nTop ranked jobs:")
        for rec in result.scored_jobs[:10]:
            print(
                f"  {rec.final_score:.3f}  {rec.job_title or '(no title)'}  —  "
                f"{rec.job_company or '(unknown)'}"
            )

    if s.errors:
        print(f"\nErrors ({len(s.errors)}):")
        for err in s.errors[:5]:
            print(f"  {err}")

    print("=" * 60 + "\n")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    main()
