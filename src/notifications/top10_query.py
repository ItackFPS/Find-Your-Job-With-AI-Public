"""Compute each active location's current top-10 ranked jobs.

Factored out of NotificationEngine for independent testability — takes
explicit db_path/location_preferences/desired_slugs rather than reading
NotificationEngine's own config, so it can be tested against a temp SQLite
database with no Discord/config dependency at all.

Reuses src.pipeline.location_match.resolve_job_location — the SAME resolver
notification routing (NotificationEngine._route_notification_candidates) and
scoring use —
so a job's top10 bucket membership is always consistent with which channel
it would be sent to as a "new job", never a second, divergent notion of
"belongs to location X".

Scoped to the latest completed pipeline run's job_scores — same "current
pipeline output" boundary GET /api/jobs and GET /api/jobs/digest/weekly
already use (RunStorage.get_latest_completed_run + get_job_ids_for_run).
``jobs.status`` alone is NOT a usable freshness signal: nothing in this
codebase ever flips a job's status away from 'active' when it stops being
re-collected/re-scored (src/storage/sqlite.py only ever writes ``status``
via the (re-)collection UPSERT) — a job scored once, weeks ago, and never
seen again stays 'active' forever. Scanning the whole jobs table by status
alone (the previous implementation) therefore surfaced arbitrarily stale
jobs with ancient scores, completely invisible to the run-scoped frontend,
while the frontend showed current-run jobs Discord had never even
considered.
"""

from __future__ import annotations

from typing import Any

from src.models.job import Job
from src.pipeline.departments import extract_department_code
from src.pipeline.location_match import resolve_job_location
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


def _fetch_active_jobs_by_slug(
    db_path: str,
    location_preferences: list[dict[str, Any]],
    desired_slugs: set[str],
) -> tuple[dict[str, list[Job]], dict[str, dict[str, Any]]]:
    """Shared first pass for compute_top10_by_slug and
    compute_notification_backfill_candidates: fetch every active job scored
    in the latest completed pipeline run, resolve each to its location slug
    (same department-fallback-extraction + resolve_job_location resolution
    both callers rely on), and bucket by slug.

    Returns ``(buckets, scores_by_id)`` — ``buckets`` only contains slugs
    that had at least one matching active job (never an empty list value);
    ``scores_by_id`` covers exactly the job ids present across all buckets.
    Both are empty when there is no completed run yet, or it scored no jobs.
    """
    if not desired_slugs:
        return {}, {}

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    try:
        latest_run = rs.get_latest_completed_run()
        if latest_run is None:
            return {}, {}
        run_id: str = latest_run["id"]
        run_job_ids = rs.get_job_ids_for_run(run_id)
    finally:
        rs.close()
    if not run_job_ids:
        return {}, {}

    s = Storage(db_path=db_path)
    s.init_db()
    try:
        conn = s._get_conn()  # noqa: SLF001 - same access pattern as GET /api/jobs/digest/weekly
        placeholders = ",".join("?" * len(run_job_ids))
        rows = conn.execute(
            f"SELECT * FROM jobs WHERE id IN ({placeholders}) AND status = 'active'",
            run_job_ids,
        ).fetchall()
    finally:
        s.close()
    jobs = [Job.from_dict(dict(r)) for r in rows]

    buckets: dict[str, list[Job]] = {slug: [] for slug in desired_slugs}
    for job in jobs:
        dept = job.department or extract_department_code(job.location or "", job.countries)
        match = resolve_job_location(dept, job.remote, job.countries, location_preferences)
        if match is None or match.slug not in buckets:
            continue
        buckets[match.slug].append(job)

    non_empty = {slug: bucket for slug, bucket in buckets.items() if bucket}
    if not non_empty:
        return {}, {}

    all_job_ids = [j.id for bucket in non_empty.values() for j in bucket]
    rs = RunStorage(db_path=db_path)
    rs.init_db()
    try:
        # Scoped to run_id specifically (not get_latest_scores_for_jobs)
        # so a concurrent in-progress run's partial scores can never leak
        # in — same reasoning as GET /api/jobs/digest/weekly.
        scores_by_id = rs.get_scores_for_run(run_id, all_job_ids)
    finally:
        rs.close()

    return non_empty, scores_by_id


def compute_top10_by_slug(
    db_path: str,
    location_preferences: list[dict[str, Any]],
    desired_slugs: set[str],
    limit: int = 10,
) -> dict[str, list[dict[str, Any]]]:
    """One DB pass: fetch every active job scored in the latest completed
    pipeline run, resolve each to its location slug, bucket by slug, and
    return the top ``limit`` per slug (by final_score DESC, discovered_at
    DESC tiebreak — same ordering as the weekly digest).

    Only slugs in ``desired_slugs`` are computed; a slug with zero matching
    active jobs is simply absent from the returned dict (never an empty
    list) so callers can treat "not present" as "nothing to send" without
    checking length. Likewise absent entirely when there is no completed
    run yet (fresh install) — nothing to compute a leaderboard from.
    """
    non_empty, scores_by_id = _fetch_active_jobs_by_slug(db_path, location_preferences, desired_slugs)
    if not non_empty:
        return {}

    def _sort_key(job: Job) -> tuple[float, float]:
        score = float(scores_by_id.get(job.id, {}).get("final_score") or 0.0)
        return (-score, -job.discovered_at.timestamp())

    # Deferred: src.pipeline's package __init__ eagerly imports
    # sentence-transformers/torch — see src/notifications/events.py's
    # JobSummary.to_dict for the same concern. score_to_tier is the single
    # canonical tier classifier (src.pipeline.scoring) — never re-derived
    # here, so a leaderboard entry's tier always matches what routing/digest
    # endpoints would compute for the same score_100.
    from src.pipeline.scoring import score_to_tier

    result: dict[str, list[dict[str, Any]]] = {}
    for slug, bucket in non_empty.items():
        bucket.sort(key=_sort_key)
        top = bucket[:limit]
        entries: list[dict[str, Any]] = []
        for rank, job in enumerate(top, 1):
            final_score = round(float(scores_by_id.get(job.id, {}).get("final_score") or 0.0), 4)
            score_100 = round(final_score * 100)
            entries.append({
                "rank": rank,
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "remote": job.remote,
                "final_score": final_score,
                "score_100": score_100,
                "tier": score_to_tier(score_100),
                "canonical_url": job.canonical_url,
                "apply_url": job.apply_url,
            })
        result[slug] = entries
    return result


def compute_notification_backfill_candidates(
    db_path: str,
    location_preferences: list[dict[str, Any]],
    desired_slugs: set[str],
) -> list[dict[str, Any]]:
    """Companion to compute_top10_by_slug for NotificationEngine's Discord
    "new arrivals" routing: every currently active job (same latest-run
    scope as the leaderboard — see _fetch_active_jobs_by_slug) that matches
    one of ``desired_slugs``, score-descending. Deliberately UNFILTERED by
    notification history and UNCAPPED per slug — the caller
    (NotificationEngine._route_notification_candidates) already applies its
    own per-job "already sent to this destination" dedup and
    rules.max_notifications_per_location_per_run cap on the merged candidate
    pool (this function's output plus run-over-run new_jobs), and must apply
    dedup BEFORE capping so an already-sent top scorer can never crowd out a
    not-yet-sent lower scorer for the same location. Doing that filtering
    here instead, before the caller ever sees the full ranked pool, would
    silently reintroduce exactly that bug.

    Exists to answer "which jobs could still need a Discord location
    notification" WITHOUT relying on whether a job happens to be
    new-to-the-database since the previous pipeline run: a job can be
    long-present in the database yet never have been sent to ITS location's
    channel — e.g. because that location was only just added to the
    profile, or its Discord channel wasn't configured yet the one time the
    job was run-over-run "new". Job freshness (is this job new in the DB)
    and destination notification state (has this job been sent to ITS
    channel) are deliberately independent concerns; this function only
    answers the first half (which active jobs currently match a configured
    location) — see _route_notification_candidates for the second half.

    Returns full job field dicts (title/company/location/department/
    countries/remote/work_mode/employment_type/canonical_url/apply_url/
    final_score) — richer than compute_top10_by_slug's leaderboard entries —
    since the caller still applies its own score-tier floor and builds the
    final embed payload from these.
    """
    non_empty, scores_by_id = _fetch_active_jobs_by_slug(db_path, location_preferences, desired_slugs)
    if not non_empty:
        return []

    def _sort_key(job: Job) -> tuple[float, float]:
        score = float(scores_by_id.get(job.id, {}).get("final_score") or 0.0)
        return (-score, -job.discovered_at.timestamp())

    candidates: list[dict[str, Any]] = []
    for bucket in non_empty.values():
        bucket.sort(key=_sort_key)
        for job in bucket:
            final_score = float(scores_by_id.get(job.id, {}).get("final_score") or 0.0)
            candidates.append({
                "job_id": job.id,
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "department": job.department,
                "countries": list(job.countries or []),
                "remote": job.remote,
                "work_mode": job.work_mode,
                "employment_type": job.employment_type,
                "final_score": final_score,
                "canonical_url": job.canonical_url,
                "apply_url": job.apply_url,
            })
    return candidates
