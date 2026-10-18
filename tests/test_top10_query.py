"""Tests for src.notifications.top10_query.compute_top10_by_slug — the
active-jobs-ranked-by-score-per-location query the top10 leaderboard
channels are built on.

Mirrors tests/test_weekly_digest.py's fixture/helper style (same Storage/
RunStorage setup) since this query serves the same underlying data, just
without a time window and bucketed across ALL desired locations in one pass
instead of one department at a time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from src.models.job import Job
from src.notifications.top10_query import (
    compute_notification_backfill_candidates,
    compute_top10_by_slug,
)
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


def _job(
    title: str,
    location: str,
    countries: list[str],
    department: str | None = None,
    remote: bool = False,
    status: str = "active",
) -> Job:
    return Job(
        id=str(uuid.uuid4()),
        title=title,
        company="TestCo",
        location=location,
        countries=countries,
        department=department,
        source="test",
        remote=remote,
        status=status,
        discovered_at=datetime.now(UTC),
    )


def _store(s: Storage, rs: RunStorage, jobs: list[Job], scores: list[float]) -> None:
    s.upsert_jobs_batch(jobs)
    run_id = str(uuid.uuid4())
    rs.create_run(run_id)
    rs.store_job_scores(
        run_id,
        [
            {
                "job_id": j.id,
                "deterministic_score": scores[i],
                "semantic_score": scores[i],
                "final_score": scores[i],
                "score_breakdown": {},
            }
            for i, j in enumerate(jobs)
        ],
    )
    rs.complete_run(run_id, {"personalized": len(jobs)})


def _dept_pref(value: str) -> dict:
    return {"type": "department", "value": value, "status": "preferred"}


def _remote_pref() -> dict:
    return {"type": "remote", "value": "remote_france", "status": "preferred"}


class TestBasicRanking:
    def test_ranks_by_score_descending(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            low = _job("Low", "Toulon", ["France"], department="83")
            high = _job("High", "Toulon", ["France"], department="83")
            mid = _job("Mid", "Toulon", ["France"], department="83")
            _store(s, rs, [low, high, mid], [0.5, 0.9, 0.7])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        titles = [e["title"] for e in result["dept-83"]]
        assert titles == ["High", "Mid", "Low"]

    def test_limits_to_top_n(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            jobs = [_job(f"Job{i}", "Toulon", ["France"], department="83") for i in range(15)]
            _store(s, rs, jobs, [i / 20 for i in range(15)])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"}, limit=10)
        assert len(result["dept-83"]) == 10
        assert result["dept-83"][0]["title"] == "Job14"  # highest score

    def test_ranks_are_sequential(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            jobs = [_job(f"Job{i}", "Toulon", ["France"], department="83") for i in range(3)]
            _store(s, rs, jobs, [0.9, 0.5, 0.7])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        assert [e["rank"] for e in result["dept-83"]] == [1, 2, 3]


class TestMultiLocationSinglePass:
    def test_buckets_jobs_into_correct_locations(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            var_job = _job("Var Dev", "Toulon", ["France"], department="83")
            ain_job = _job("Ain Dev", "Bourg", ["France"], department="01")
            remote_job = _job("Remote Dev", "Anywhere", ["France"], remote=True)
            _store(s, rs, [var_job, ain_job, remote_job], [0.8, 0.7, 0.9])
        finally:
            s.close()
            rs.close()

        prefs = [_dept_pref("83"), _dept_pref("01"), _remote_pref()]
        result = compute_top10_by_slug(
            db_path, prefs, {"dept-83", "dept-01", "remote-remote_france"}
        )
        assert result["dept-83"][0]["title"] == "Var Dev"
        assert result["dept-01"][0]["title"] == "Ain Dev"
        assert result["remote-remote_france"][0]["title"] == "Remote Dev"

    def test_only_desired_slugs_are_computed(self, tmp_path):
        """A department not in desired_slugs must not appear in the result
        even if matching jobs exist for it."""
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            var_job = _job("Var Dev", "Toulon", ["France"], department="83")
            ain_job = _job("Ain Dev", "Bourg", ["France"], department="01")
            _store(s, rs, [var_job, ain_job], [0.8, 0.7])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(
            db_path, [_dept_pref("83"), _dept_pref("01")], {"dept-83"}
        )
        assert "dept-83" in result
        assert "dept-01" not in result


class TestEdgeCases:
    def test_empty_desired_slugs_returns_empty_without_querying(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        # No DB file created at all — if this queried, it would raise.
        assert compute_top10_by_slug(db_path, [], set()) == {}

    def test_no_matching_jobs_slug_is_absent_not_empty_list(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            other = _job("Other", "Lyon", ["France"], department="69")
            _store(s, rs, [other], [0.8])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        assert "dept-83" not in result

    def test_inactive_job_is_excluded(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            expired = _job("Expired", "Toulon", ["France"], department="83", status="expired")
            _store(s, rs, [expired], [0.9])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        assert "dept-83" not in result

    def test_excluded_preference_never_matches(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            job = _job("Var Dev", "Toulon", ["France"], department="83")
            _store(s, rs, [job], [0.9])
        finally:
            s.close()
            rs.close()

        excluded_pref = {"type": "department", "value": "83", "status": "excluded"}
        result = compute_top10_by_slug(db_path, [excluded_pref], {"dept-83"})
        assert "dept-83" not in result


class TestTierPropagation:
    """Regression coverage for the bug where compute_top10_by_slug's entries
    never carried a `tier` key, causing every Discord top10 leaderboard entry
    to silently fall back to "relevant" regardless of actual score (see
    src/notifications/discord.py:_top10_leaderboard_embed's `entry.get("tier")
    or "relevant"` fallback). Every boundary below mirrors
    src.pipeline.scoring._TIER_THRESHOLDS exactly — this test must be updated
    if those thresholds are ever recalibrated.
    """

    def test_every_entry_has_a_tier_key(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            job = _job("Var Dev", "Toulon", ["France"], department="83")
            _store(s, rs, [job], [0.9])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        assert "tier" in result["dept-83"][0]

    def test_tier_matches_canonical_score_to_tier_at_each_threshold_boundary(self, tmp_path):
        from src.pipeline.scoring import score_to_tier

        # One job per tier boundary (score_100 = round(final_score * 100)):
        # 80 -> exceptional, 72 -> excellent, 65 -> strong, 60 -> relevant,
        # 59 -> low_priority. Distinct titles double as unambiguous ids.
        cases = [
            ("Exceptional", 0.80, "exceptional"),
            ("Excellent", 0.72, "excellent"),
            ("Strong", 0.65, "strong"),
            ("Relevant", 0.60, "relevant"),
            ("LowPriority", 0.59, "low_priority"),
        ]
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            jobs = [_job(title, "Toulon", ["France"], department="83") for title, _, _ in cases]
            _store(s, rs, jobs, [score for _, score, _ in cases])
        finally:
            s.close()
            rs.close()

        # limit=10 so the whole distinct-tier set survives ranking/truncation.
        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"}, limit=10)
        by_title = {e["title"]: e for e in result["dept-83"]}
        for title, _, expected_tier in cases:
            entry = by_title[title]
            assert entry["tier"] == expected_tier
            # Cross-check against the canonical function directly, so this
            # test also fails if the two ever drift for any reason.
            assert entry["tier"] == score_to_tier(entry["score_100"])


class TestRunScoping:
    """Regression coverage for the real-world Allier divergence bug: this
    query used to scan the WHOLE `jobs` table by `status='active'` alone,
    with no run scoping. Since nothing in this codebase ever flips a job's
    status away from 'active' when it stops being re-collected/re-scored
    (src/storage/sqlite.py only ever writes `status` via the collection
    UPSERT), a job scored once and never seen again stayed eligible
    forever, using its ancient score — while GET /api/jobs (already scoped
    to the latest completed run's job_scores) never showed it. Root cause
    confirmed against production data: 8/8 example Discord Allier jobs
    were `status='active'` but absent from the latest run's job_scores,
    last scored 15-20 days earlier.
    """

    def test_job_from_an_older_completed_run_is_excluded_even_though_still_active(self, tmp_path):
        """A job scored ONLY in an earlier completed run — never rescored
        since, but still status='active' — must not appear once a later
        run has completed without it, even though nothing ever touched its
        status column."""
        db_path = str(tmp_path / "t.db")

        # Older run: a job that will never be seen again.
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            stale = _job("StaleGhost", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [stale], [0.9])
        finally:
            s.close()
            rs.close()

        # Latest completed run: does not include the stale job at all.
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            fresh = _job("Fresh", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [fresh], [0.5])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        titles = [e["title"] for e in result["dept-83"]]
        assert titles == ["Fresh"]

    def test_stale_job_never_outranks_current_run_jobs_regardless_of_its_old_score(self, tmp_path):
        """Even a stale job with a much higher historical score than every
        current-run job must never appear — the current run's job_scores
        is the sole source of truth for "what's currently rankable", not a
        cross-run comparison of whoever has the highest score ever."""
        db_path = str(tmp_path / "t.db")

        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            stale_high_score = _job("StaleButHigh", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [stale_high_score], [0.99])
        finally:
            s.close()
            rs.close()

        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            current = [
                _job("CurrentA", "83 - Toulon", ["France"], department="83"),
                _job("CurrentB", "83 - Toulon", ["France"], department="83"),
            ]
            _store(s, rs, current, [0.6, 0.4])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        titles = [e["title"] for e in result["dept-83"]]
        assert titles == ["CurrentA", "CurrentB"]
        assert "StaleButHigh" not in titles

    def test_current_run_job_survives_regardless_of_how_much_stale_noise_exists(self, tmp_path):
        """Guards against reintroducing any unordered/truncating scan (the
        previous implementation's `LIMIT 5000` with no ORDER BY, which in
        production hid 99% of the actual latest-run jobs behind older,
        stale rows). Run scoping via an explicit `id IN (run_job_ids)`
        must include every current-run job regardless of how many stale
        active rows exist elsewhere in the table."""
        db_path = str(tmp_path / "t.db")

        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            noise = [
                _job(f"Noise{i}", "83 - Toulon", ["France"], department="83")
                for i in range(200)
            ]
            _store(s, rs, noise, [0.95] * 200)
        finally:
            s.close()
            rs.close()

        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            current = _job("CurrentOnly", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [current], [0.5])
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        titles = [e["title"] for e in result["dept-83"]]
        assert titles == ["CurrentOnly"]

    def test_no_completed_run_returns_empty(self, tmp_path):
        """Fresh install / no completed run yet — nothing to compute a
        leaderboard from, must not raise or fall back to an unscoped scan."""
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            job = _job("Orphan", "83 - Toulon", ["France"], department="83")
            s.upsert_jobs_batch([job])  # stored but never scored/run-completed
        finally:
            s.close()
            rs.close()

        result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        assert result == {}


class TestFrontendDiscordParity:
    """Cross-path regression guard: for the same department, GET /api/jobs
    (the frontend's data source) and compute_top10_by_slug (Discord's data
    source) must agree on the exact same job IDs, in the exact same order,
    with the exact same score values. This is the product invariant the
    Allier bug violated — same department + same job IDs + same score
    values + same ordering — and is the test that would have caught it
    directly, independent of either implementation's internals.
    """

    def test_same_department_same_ids_same_scores_same_order_as_frontend_api(self, tmp_path):
        from fastapi.testclient import TestClient

        from src.api.app import app
        from src.api.runner import PipelineRunner

        db_path = str(tmp_path / "t.db")

        # Older run: a stale job that would rank #1 by score if either path
        # leaked cross-run data — proves both paths agree on excluding it.
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            stale = _job("StaleGhost", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [stale], [0.99])
        finally:
            s.close()
            rs.close()

        # Latest completed run: the current pipeline output both UIs
        # should agree on, unsorted-input on purpose (score order must
        # come from the query, not insertion order).
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            current = [
                _job("Mid", "83 - Toulon", ["France"], department="83"),
                _job("Top", "83 - Toulon", ["France"], department="83"),
                _job("Bottom", "83 - Toulon", ["France"], department="83"),
            ]
            _store(s, rs, current, [0.7, 0.9, 0.5])
        finally:
            s.close()
            rs.close()

        app.state.db_path = db_path
        app.state.runner = PipelineRunner(db_path=db_path)
        client = TestClient(app)

        frontend_resp = client.get("/api/jobs", params={"department": "83", "limit": 50})
        assert frontend_resp.status_code == 200
        frontend_jobs = frontend_resp.json()

        discord_result = compute_top10_by_slug(db_path, [_dept_pref("83")], {"dept-83"})
        discord_entries = discord_result["dept-83"]

        # Same department.
        assert all(j["department"] == "83" for j in frontend_jobs)

        # Same job IDs, same ordering.
        frontend_ids = [j["id"] for j in frontend_jobs]
        discord_ids = [e["job_id"] for e in discord_entries]
        assert frontend_ids == discord_ids == [current[1].id, current[0].id, current[2].id]
        assert stale.id not in frontend_ids
        assert stale.id not in discord_ids

        # Same score values, per job, in that same order.
        frontend_scores = [round(j["score"]["final_score"], 4) for j in frontend_jobs]
        discord_scores = [e["final_score"] for e in discord_entries]
        assert frontend_scores == discord_scores == [0.9, 0.7, 0.5]


class TestNotificationBackfillCandidates:
    """compute_notification_backfill_candidates — the department-activation
    backfill fix (2026-09-15): unlike compute_top10_by_slug, this returns
    the FULL, UNCAPPED, score-sorted active-job pool per slug, with no
    notification-history filtering — the caller (NotificationEngine) is
    responsible for dedup and capping, and must apply dedup BEFORE the cap.
    """

    def test_uncapped_and_score_sorted(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            jobs = [_job(f"Job{i}", "Toulon", ["France"], department="83") for i in range(15)]
            _store(s, rs, jobs, [i / 20 for i in range(15)])
        finally:
            s.close()
            rs.close()

        result = compute_notification_backfill_candidates(db_path, [_dept_pref("83")], {"dept-83"})
        assert len(result) == 15  # NOT capped at compute_top10_by_slug's default limit=10
        scores = [c["final_score"] for c in result]
        assert scores == sorted(scores, reverse=True)

    def test_returns_full_field_set_needed_for_routing(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            job = _job("Var Dev", "Toulon", ["France"], department="83")
            _store(s, rs, [job], [0.8])
        finally:
            s.close()
            rs.close()

        result = compute_notification_backfill_candidates(db_path, [_dept_pref("83")], {"dept-83"})
        assert len(result) == 1
        entry = result[0]
        for key in (
            "job_id", "title", "company", "location", "department", "countries",
            "remote", "work_mode", "employment_type", "final_score",
            "canonical_url", "apply_url",
        ):
            assert key in entry

    def test_empty_desired_slugs_returns_empty_without_querying(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        # No DB file created at all — if this queried, it would raise.
        assert compute_notification_backfill_candidates(db_path, [], set()) == []

    def test_no_completed_run_returns_empty(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        try:
            job = _job("Orphan", "83 - Toulon", ["France"], department="83")
            s.upsert_jobs_batch([job])  # stored but never scored/run-completed
        finally:
            s.close()

        result = compute_notification_backfill_candidates(db_path, [_dept_pref("83")], {"dept-83"})
        assert result == []

    def test_multi_location_bucketing_matches_compute_top10(self, tmp_path):
        """Same resolution/bucketing as compute_top10_by_slug (both share
        _fetch_active_jobs_by_slug) — a job only ever appears under its own
        resolved slug."""
        db_path = str(tmp_path / "t.db")
        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            var_job = _job("Var Dev", "Toulon", ["France"], department="83")
            ain_job = _job("Ain Dev", "Bourg", ["France"], department="01")
            _store(s, rs, [var_job, ain_job], [0.8, 0.7])
        finally:
            s.close()
            rs.close()

        result = compute_notification_backfill_candidates(
            db_path, [_dept_pref("83"), _dept_pref("01")], {"dept-83", "dept-01"}
        )
        by_title = {c["title"]: c for c in result}
        assert by_title["Var Dev"]["department"] == "83"
        assert by_title["Ain Dev"]["department"] == "01"

    def test_stale_job_from_an_older_run_is_excluded(self, tmp_path):
        """Same run-scoping guarantee as compute_top10_by_slug (shared
        _fetch_active_jobs_by_slug) — a job scored only in an earlier
        completed run must not appear once a later run has completed
        without it."""
        db_path = str(tmp_path / "t.db")

        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            stale = _job("StaleGhost", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [stale], [0.9])
        finally:
            s.close()
            rs.close()

        s = Storage(db_path=db_path)
        s.init_db()
        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            fresh = _job("Fresh", "83 - Toulon", ["France"], department="83")
            _store(s, rs, [fresh], [0.5])
        finally:
            s.close()
            rs.close()

        result = compute_notification_backfill_candidates(db_path, [_dept_pref("83")], {"dept-83"})
        assert [c["title"] for c in result] == ["Fresh"]
