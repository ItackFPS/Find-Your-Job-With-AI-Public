"""Single-run pipeline profiler with sub-stage timing.

Usage:
    python scripts/profile_pipeline.py [--db data/jobs.db] [--config config]

Runs the full pipeline once with verbose per-stage and sub-stage timing.
Reports a detailed breakdown table and credential presence (TRUE/FALSE only,
never prints actual values).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Add project root to sys.path so imports work when run from repo root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Show cache-hit lines from semantic matching and personalization
for name in ("src.pipeline.semantic_matching", "src.personalization.engine"):
    logging.getLogger(name).setLevel(logging.INFO)

from src.pipeline.orchestrator import run_pipeline  # noqa: E402


def _credential_present(env_var: str) -> str:
    val = os.environ.get(env_var, "")
    return "TRUE" if bool(val and val.strip()) else "FALSE"


def _fmt(seconds: float) -> str:
    if seconds >= 1.0:
        return f"{seconds:7.2f}s"
    return f"{seconds*1000:7.1f}ms"


def _pct(part: float, total: float) -> str:
    if total <= 0:
        return "   n/a"
    return f"{100.0 * part / total:5.1f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile a single pipeline run")
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--config", default="config")
    args = parser.parse_args()

    print("\n" + "=" * 70)
    print("  CREDENTIAL CHECK (TRUE/FALSE only — no values printed)")
    print("=" * 70)
    print(f"  FRANCE_TRAVAIL_CLIENT_ID     : {_credential_present('FRANCE_TRAVAIL_CLIENT_ID')}")
    print(f"  FRANCE_TRAVAIL_CLIENT_SECRET : {_credential_present('FRANCE_TRAVAIL_CLIENT_SECRET')}")
    print()

    print("Running pipeline (this may take a while on first run) …\n")
    result = run_pipeline(db_path=args.db, config_dir=args.config)

    if result.status == "failed":
        print(f"\nPipeline FAILED: {result.error}")
        sys.exit(1)

    s = result.stats
    total = s.duration_seconds

    # Ordered list of (label, key_or_value, input_count, output_count)
    stage_rows = [
        ("collect",        "collect",        None,            s.collected),
        ("validate",       "validate",       s.collected,     s.validated),
        ("deduplicate",    "deduplicate",    s.validated,     s.deduplicated),
        ("filter",         "filter",         s.deduplicated,  s.filtered),
        ("score+storage",  "score+storage",  s.filtered,      s.scored),
        ("semantic (total)","semantic",       s.scored,        s.semantically_matched),
        ("  ↳ cache lookup","sem.embed_cache_lookup", None,  None),
        ("  ↳ embed_batch", "sem.embed_batch_compute", None, None),
        ("  ↳ cache write", "sem.embed_cache_write",  None,  None),
        ("  ↳ cosine sim",  "sem.cosine_similarity",  None,  None),
        ("pers (total)",   "personalization",s.semantically_matched, s.personalized),
        ("  ↳ feedback load","pers.pers_feedback_load", None, None),
        ("  ↳ cache lookup","pers.pers_cache_lookup",  None,  None),
        ("  ↳ compute loop","pers.pers_compute_loop",  None,  None),
        ("  ↳ cache write", "pers.pers_cache_write",   None,  None),
    ]

    print("\n" + "=" * 70)
    print(f"  STAGE BREAKDOWN  (total wall-clock: {total:.1f}s)")
    print("=" * 70)
    header = f"  {'Stage':<24}  {'Time':>8}  {'% total':>7}  {'Input':>7}  {'Output':>7}"
    print(header)
    print("  " + "-" * 66)

    for label, key, inp, out in stage_rows:
        t = s.stage_times.get(key, 0.0)
        inp_s = str(inp) if inp is not None else ""
        out_s = str(out) if out is not None else ""
        print(f"  {label:<24}  {_fmt(t)}  {_pct(t, total)}  {inp_s:>7}  {out_s:>7}")

    print()
    print("=" * 70)
    print("  CACHE STATISTICS")
    print("=" * 70)
    emb_total = s.embedding_cache_hits + s.embedding_cache_misses
    pers_total = s.personalization_cache_hits + s.personalization_cache_misses
    emb_hr = f"{100*s.embedding_cache_hits/emb_total:.1f}%" if emb_total else "n/a"
    pers_hr = f"{100*s.personalization_cache_hits/pers_total:.1f}%" if pers_total else "n/a"

    print(f"  Embedding cache    hits={s.embedding_cache_hits:>5}  misses={s.embedding_cache_misses:>5}  hit-rate={emb_hr}")
    print(f"  Pers. score cache  hits={s.personalization_cache_hits:>5}  misses={s.personalization_cache_misses:>5}  hit-rate={pers_hr}")

    print()
    print("=" * 70)
    print("  JOB COUNTS")
    print("=" * 70)
    print(f"  Collected:           {s.collected}")
    print(f"  Validated:           {s.validated}")
    print(f"  Deduplicated:        {s.deduplicated}")
    print(f"  Passed filter:       {s.filtered}")
    print(f"  Scored:              {s.scored}")
    print(f"  Semantically ranked: {s.semantically_matched}")
    print(f"  Personalized:        {s.personalized}")

    print()
    print("=" * 70)
    print("  ERRORS")
    print("=" * 70)
    if s.errors:
        for err in s.errors[:20]:
            print(f"  {err}")
        if len(s.errors) > 20:
            print(f"  … and {len(s.errors) - 20} more")
    else:
        print("  (none)")
    print()


if __name__ == "__main__":
    main()
