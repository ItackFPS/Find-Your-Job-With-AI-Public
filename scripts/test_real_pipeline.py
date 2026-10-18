"""Real-world pipeline test: RemoteOK → full pipeline → temporary SQLite.

This script proves that the end-to-end pipeline works against a live
Internet source.  It NEVER writes to data/jobs.db.

Usage:
    python scripts/test_real_pipeline.py

What it does:
    1. Fetches up to 10 real jobs from the RemoteOK public API.
    2. Runs them through: normalize → validate → deduplicate → filter →
       score → semantic matching → personalization → store.
    3. Prints how many jobs survived each stage and the final job list.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

MAX_COLLECTED = 10

# ---------------------------------------------------------------------------
# Test-specific filter config (permissive — proves pipeline mechanics).
# The personal-preference config/filters.yaml restricts to France/Europe with
# junior seniority; that would reject most RemoteOK jobs (US-targeted) and
# prevent us from verifying downstream stages.  We use a looser config here
# that still exercises every filter check without being overly restrictive.
# ---------------------------------------------------------------------------
_TEST_FILTER_CFG: dict = {
    "locations": {"include": []},          # No location restriction
    "remote": {"allowed": []},             # Allow any remote status
    "seniority": {"include": []},          # No seniority restriction
    "title_keywords": {
        "include": [],                     # No include restriction
        "exclude": [],
    },
    "posted_within_days": 365,             # Accept anything up to a year old
    "salary_requirements": {
        "min_salary": None,
        "max_salary": None,
    },
}

_PROFILE_INFO = {
    "seniority": "junior",
    "employment_type": "full-time",
    "location": "Europe",
    "languages": ["English"],
    "skills": ["Python", "software", "developer", "engineer"],
    "responsibilities": "software development, backend, engineering",
}


def _separator(label: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print("─" * 60)


def main() -> None:  # noqa: PLR0912, PLR0914, PLR0915
    import logging

    logging.basicConfig(
        level=logging.WARNING,   # Suppress info noise during the demo
        format="%(levelname)s %(name)s: %(message)s",
    )

    print("=" * 60)
    print("  REAL-WORLD PIPELINE TEST — RemoteOK → Full Pipeline")
    print("=" * 60)

    # -- STEP 1: COLLECT -------------------------------------------------------
    _separator("COLLECT  (RemoteOK public API)")

    from src.collectors.remoteok import RemoteOKCollector

    collector = RemoteOKCollector(config={"max_jobs": MAX_COLLECTED})
    result = collector.collect()

    print(f"  Collected     : {len(result.jobs)} raw jobs")
    if result.errors:
        for err in result.errors:
            print(f"  [ERROR] {err}")
        if not result.jobs:
            print("\nCollection failed entirely. Stopping.")
            sys.exit(1)

    raw_jobs = result.jobs

    # -- STEP 2: NORMALIZE -----------------------------------------------------
    _separator("NORMALIZE")

    from src.pipeline.normalize import normalize_jobs

    normalized = normalize_jobs("remoteok", raw_jobs)
    print(f"  Normalized    : {len(normalized)}")

    # -- STEP 3: VALIDATE ------------------------------------------------------
    _separator("VALIDATE")

    from src.pipeline.validate import validate_jobs

    valid, val_errors = validate_jobs(normalized)
    print(f"  Valid         : {len(valid)}  (rejected: {len(normalized) - len(valid)})")
    for err in val_errors:
        print(f"  [REJECTED] {err}")

    if not valid:
        print("\nNo jobs passed validation. Stopping.")
        sys.exit(1)

    # -- STEP 4: DEDUPLICATE ---------------------------------------------------
    _separator("DEDUPLICATE")

    from src.pipeline.deduplicate import deduplicate_jobs

    deduped, dupes_removed = deduplicate_jobs(valid)
    print(f"  After dedup   : {len(deduped)}  (removed: {dupes_removed})")

    # -- STEP 5: FILTER --------------------------------------------------------
    _separator("FILTER  (test-permissive config)")

    from src.pipeline.filter import filter_jobs

    filtered, rejected = filter_jobs(deduped, _TEST_FILTER_CFG)
    print(f"  After filter  : {len(filtered)}  (rejected: {len(rejected)})")
    for jid, reason in rejected.items():
        job = next((j for j in deduped if j.id == jid), None)
        title = job.title if job else jid
        print(f"  [REJECTED] {title!r} → {reason}")

    if not filtered:
        print("\nNo jobs survived filtering. Stopping.")
        sys.exit(1)

    # -- STEP 6: SCORE ---------------------------------------------------------
    _separator("SCORE")

    from src.pipeline.scoring import load_scoring_config, score_jobs

    scoring_cfg = load_scoring_config("config/scoring.yaml")
    scored = score_jobs(filtered, scoring_cfg)
    print(f"  Scored        : {len(scored)}")
    for job, score in scored[:5]:
        print(f"    {score:.3f}  {job.title!r}  — {job.company}")

    # -- STEP 7: SEMANTIC MATCHING ---------------------------------------------
    _separator("SEMANTIC MATCHING")

    from src.embeddings.embedding import EmbeddingService
    from src.pipeline.semantic_matching import (
        apply_semantic_matching,
        build_profile_embedding,
        integrate_semantic_into_ranking,
        load_semantic_config,
    )

    sem_cfg = load_semantic_config("config/semantic_config.yaml")
    svc = EmbeddingService(
        model_name=sem_cfg["embedding"]["model_name"],
        cache_max_entries=sem_cfg["embedding"]["cache_max_entries"],
        device=sem_cfg["embedding"].get("device", "auto"),
    )

    profile_emb = build_profile_embedding(_PROFILE_INFO, svc)
    jobs_only = [job for job, _ in scored]
    semantic_results, below_threshold = apply_semantic_matching(
        jobs_only, profile_emb, sem_cfg, svc
    )
    final_ranked = integrate_semantic_into_ranking(scored, semantic_results)
    print(f"  Semantic      : {len(semantic_results)} matched  ({below_threshold} below threshold)")

    # -- STEP 8: PERSONALIZATION -----------------------------------------------
    _separator("PERSONALIZATION")

    from src.personalization.config import load_personalization_config
    from src.personalization.engine import apply_personalization
    from src.personalization.storage import PersonalizationStorage

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_db = str(Path(tmp_dir) / "test_pipeline.db")

        p_storage = PersonalizationStorage(db_path=tmp_db)
        p_storage.init_db()
        p_cfg = load_personalization_config("config/personalization.yaml")

        personalized, _ = apply_personalization(final_ranked, p_storage, p_cfg)
        feedback_count = p_storage.count_feedback()
        p_storage.close()

        print(f"  Personalized  : {len(personalized)}  (feedback events: {feedback_count})")

        # -- STEP 9: STORE ---------------------------------------------------------
        _separator("STORE  (temporary SQLite — NOT data/jobs.db)")

        from src.storage.sqlite import Storage

        storage = Storage(db_path=tmp_db)
        storage.init_db()
        for job, _ in personalized:
            storage.upsert_job(job)
        stored_count = storage.count_jobs()
        final_jobs = storage.get_jobs(limit=50)
        storage.close()

        print(f"  Stored        : {stored_count}")

        # -- FINAL REPORT ----------------------------------------------------------
        _separator("FINAL JOBS")

        if final_jobs:
            for i, job in enumerate(final_jobs, 1):
                score_val = next(
                    (s for j, s in personalized if j.id == job.id), None
                )
                score_str = f"{score_val:.3f}" if score_val is not None else "N/A"
                print(f"\n  [{i}] Score: {score_str}")
                print(f"       Title   : {job.title}")
                print(f"       Company : {job.company}")
                print(f"       Location: {job.location}  (remote={job.remote})")
                url = job.raw_payload.get("url") or job.canonical_url or job.apply_url or ""
                if url:
                    print(f"       URL     : {url}")
        else:
            print("  (no jobs stored)")

        # Verify data/jobs.db was NOT modified
        real_db = Path("data/jobs.db")
        if real_db.exists():
            from src.storage.sqlite import Storage as _S

            s = _S(db_path=str(real_db))
            s.init_db()
            count = s.count_jobs()
            s.close()
            print(f"\n  data/jobs.db untouched (still has {count} jobs)")

    print("\n" + "=" * 60)
    print("  PIPELINE STAGE SUMMARY")
    print("=" * 60)
    print(f"  Collected          : {len(raw_jobs)}")
    print(f"  Normalized         : {len(normalized)}")
    print(f"  Validated          : {len(valid)}")
    print(f"  Deduplicated       : {len(deduped)}")
    print(f"  Filtered           : {len(filtered)}")
    print(f"  Scored             : {len(scored)}")
    print(f"  Semantic matched   : {len(semantic_results)}")
    print(f"  Personalized       : {len(personalized)}")
    print(f"  Stored             : {stored_count}")
    print("=" * 60)
    print("\nReal-world pipeline test COMPLETE.\n")


if __name__ == "__main__":
    main()
