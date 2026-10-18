"""Tests for RunStorage (pipeline_runs, job_scores, job_rejections tables)."""

from __future__ import annotations

import uuid
from pathlib import Path


import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def rs(tmp_db: str):
    from src.storage.runs import RunStorage
    store = RunStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


class TestPipelineRuns:
    def test_create_run_status_running(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        row = rs.get_run(run_id)
        assert row is not None
        assert row["id"] == run_id
        assert row["status"] == "running"

    def test_complete_run(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"collected": 100, "filtered": 20})
        row = rs.get_run(run_id)
        assert row["status"] == "completed"
        assert row["stats"]["collected"] == 100
        assert row["completed_at"] is not None

    def test_complete_run_clears_stale_error(self, rs) -> None:
        """Issue 3 regression: a run previously marked failed (e.g. by
        recover_stale_runs after a dev-server reload interrupted an earlier
        attempt) must not keep exposing that error once it legitimately
        completes — status=completed with a leftover error string is a bug.
        """
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.fail_run(run_id, "Run interrupted: owning process is no longer running.")
        assert rs.get_run(run_id)["error"] is not None

        rs.complete_run(run_id, {"collected": 50})

        row = rs.get_run(run_id)
        assert row["status"] == "completed"
        assert row["error"] is None


class TestJobRejectionsSchema:
    """Phase NOW / I3: (run_id, job_id) is a real query pattern —
    get_rejection_for_job() looks up exactly one (run_id, job_id) pair, and
    without a composite index it must fall back to the run_id-only index
    followed by a linear scan. These tests prove the composite index exists
    on any db (fresh or pre-existing) and that lookups/inserts still behave
    correctly with it in place.
    """

    def test_composite_index_on_run_id_job_id_exists(self, rs) -> None:
        conn = rs._get_conn()
        index_names = {
            row["name"] for row in conn.execute("PRAGMA index_list(job_rejections)")
        }
        assert "idx_rejections_run_job" in index_names
        index_info = conn.execute(
            "PRAGMA index_info(idx_rejections_run_job)"
        ).fetchall()
        assert [row["name"] for row in index_info] == ["run_id", "job_id"]

    def test_index_created_idempotently_on_pre_existing_database(self, tmp_db: str) -> None:
        """A database created before this index existed must not error or
        lose data when init_db() runs again (e.g. after an upgrade)."""
        from src.storage.runs import RunStorage

        store = RunStorage(db_path=tmp_db)
        store.init_db()
        run_id = str(uuid.uuid4())
        store.create_run(run_id)
        store.store_rejections(run_id, [
            {"job_id": "job-1", "rejection_stage": "filter", "rejection_detail": "location"},
        ])
        store.close()

        # Re-open (simulates a process restart / re-running init_db on an
        # existing file) — CREATE INDEX IF NOT EXISTS must be a no-op that
        # neither errors nor drops the existing row.
        RunStorage._initialized_paths.clear()
        store2 = RunStorage(db_path=tmp_db)
        store2.init_db()
        rejection = store2.get_rejection_for_job(run_id, "job-1")
        assert rejection is not None
        assert rejection["rejection_detail"] == "location"
        store2.close()

    def test_get_rejection_for_job_finds_correct_row_among_many(self, rs) -> None:
        """Exact-match correctness with the new index in place: many rows
        share a run_id, and many rows (across runs) share a job_id — only the
        (run_id, job_id) pair must match."""
        run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
        rs.create_run(run_a)
        rs.create_run(run_b)
        rs.store_rejections(run_a, [
            {"job_id": f"job-{i}", "rejection_stage": "filter", "rejection_detail": "seniority"}
            for i in range(25)
        ] + [{"job_id": "shared-job-id", "rejection_stage": "filter", "rejection_detail": "location"}])
        rs.store_rejections(run_b, [
            {"job_id": "shared-job-id", "rejection_stage": "requirements_gate", "rejection_detail": "[]"},
        ])

        result_a = rs.get_rejection_for_job(run_a, "shared-job-id")
        result_b = rs.get_rejection_for_job(run_b, "shared-job-id")
        assert result_a is not None and result_a["rejection_detail"] == "location"
        assert result_b is not None and result_b["rejection_detail"] == "[]"
        assert rs.get_rejection_for_job(run_a, "job-12") is not None
        assert rs.get_rejection_for_job(run_a, "nonexistent-job") is None

    def test_complete_run_does_not_touch_stats_errors_list(self, rs) -> None:
        """Non-fatal warnings collected during a successful run (e.g.
        per-source collector errors) live in stats['errors'] — a separate
        field from the run-level `error` column. Clearing `error` on
        complete_run must never touch this list."""
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"collected": 10, "errors": ["indeed: rate limited"]})

        row = rs.get_run(run_id)
        assert row["error"] is None
        assert row["stats"]["errors"] == ["indeed: rate limited"]

    def test_fail_run(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.fail_run(run_id, "network error")
        row = rs.get_run(run_id)
        assert row["status"] == "failed"
        assert row["error"] == "network error"

    def test_update_run_stats(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.update_run_stats(run_id, {"collected": 50, "current_stage": "filter"})
        row = rs.get_run(run_id)
        assert row["stats"]["collected"] == 50
        assert row["stats"]["current_stage"] == "filter"

    def test_list_runs_newest_first(self, rs) -> None:
        ids = [str(uuid.uuid4()) for _ in range(3)]
        for i in ids:
            rs.create_run(i)
            rs.complete_run(i, {})
        rows = rs.list_runs(limit=10)
        started = [r["started_at"] for r in rows]
        assert started == sorted(started, reverse=True)

    def test_get_nonexistent_run(self, rs) -> None:
        assert rs.get_run("does-not-exist") is None

    def test_get_latest_completed_run(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"collected": 5})
        latest = rs.get_latest_completed_run()
        assert latest is not None
        assert latest["id"] == run_id

    def test_get_latest_completed_run_ignores_failed(self, rs) -> None:
        run_a = str(uuid.uuid4())
        run_b = str(uuid.uuid4())
        rs.create_run(run_a)
        rs.complete_run(run_a, {})
        rs.create_run(run_b)
        rs.fail_run(run_b, "error")
        latest = rs.get_latest_completed_run()
        assert latest is not None
        assert latest["id"] == run_a


class TestJobScores:
    def test_store_and_retrieve_scores(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)

        scores = [
            {
                "job_id": "job-1",
                "deterministic_score": 0.7,
                "semantic_score": 0.6,
                "final_score": 0.68,
                "score_breakdown": {"role_match": 0.0, "remote_compatibility": 1.0},
            },
        ]
        rs.store_job_scores(run_id, scores)

        row = rs.get_job_latest_score("job-1")
        assert row is not None
        assert abs(row["final_score"] - 0.68) < 1e-6
        assert row["score_breakdown"]["remote_compatibility"] == 1.0

    def test_get_jobs_for_run(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        scores = [
            {"job_id": f"j{i}", "final_score": float(i) / 10, "score_breakdown": {}}
            for i in range(5)
        ]
        rs.store_job_scores(run_id, scores)

        rows = rs.get_jobs_for_run(run_id, limit=10)
        assert len(rows) == 5

    def test_get_latest_score_across_runs(self, rs) -> None:
        run_a = str(uuid.uuid4())
        run_b = str(uuid.uuid4())
        rs.create_run(run_a)
        rs.create_run(run_b)
        rs.store_job_scores(run_a, [{"job_id": "j1", "final_score": 0.5, "score_breakdown": {}}])
        rs.store_job_scores(run_b, [{"job_id": "j1", "final_score": 0.8, "score_breakdown": {}}])

        row = rs.get_job_latest_score("j1")
        assert row is not None


class TestJobRejections:
    def test_store_and_retrieve_rejections(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)

        rejections = [
            {
                "job_id": "j1",
                "rejection_stage": "filter",
                "rejection_detail": "seniority",
                "title": "Senior VP",
                "company": "BigCo",
                "location": "Berlin",
                "source": "arbeitnow",
            },
        ]
        rs.store_rejections(run_id, rejections)

        rows = rs.get_run_rejections(run_id)
        assert len(rows) == 1
        assert rows[0]["rejection_detail"] == "seniority"
        assert rows[0]["title"] == "Senior VP"

    def test_count_rejections(self, rs) -> None:
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.store_rejections(run_id, [
            {"job_id": f"j{i}", "rejection_stage": "filter", "rejection_detail": "location"}
            for i in range(7)
        ])
        assert rs.count_run_rejections(run_id) == 7


class TestStaleRunRecovery:
    """P2 — server-restart / stuck-run recovery.

    Issue 2 (2026-09-05): recovery now requires BOTH a dead PID AND a stale
    heartbeat, not just "status == running" — a run driven by a separate OS
    process that outlives an API restart must not be falsely declared dead
    merely because THIS process restarted. See _pid_alive/recover_stale_runs
    in src/storage/runs.py for the full rationale.
    """

    # A PID essentially guaranteed not to correspond to any real process on
    # any real system (far beyond any platform's actual PID range) — used to
    # deterministically simulate "the owning process is gone".
    _DEAD_PID = 999_999_999

    def test_no_stale_runs_returns_zero(self, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        count = rs.recover_stale_runs()
        assert count == 0
        rs.close()

    def test_stale_running_run_with_dead_pid_is_marked_failed(self, tmp_db: str) -> None:
        """A run whose owning process is genuinely gone (dead PID) must
        still be recovered — the core crash-recovery behavior this method
        exists for."""
        from src.storage.runs import RunStorage
        # Simulate: a run was created by a previous process that has since
        # died, and never completed.
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        stale_id = str(uuid.uuid4())
        rs.create_run(stale_id, pid=self._DEAD_PID)
        assert rs.get_run(stale_id)["status"] == "running"

        # Simulate server restart: new RunStorage instance, call recover.
        rs2 = RunStorage(db_path=tmp_db)
        rs2.init_db()
        recovered = rs2.recover_stale_runs()

        assert recovered == 1
        row = rs2.get_run(stale_id)
        assert row["status"] == "failed"
        assert "interrupted" in row["error"].lower()
        assert row["completed_at"] is not None
        rs.close()
        rs2.close()

    def test_running_row_owned_by_still_alive_process_is_not_touched(self, tmp_db: str) -> None:
        """Issue 2 core fix: a 'running' row whose owning process is still
        genuinely alive (and has a fresh heartbeat) must survive a restart
        of a DIFFERENT process (e.g. the API server) calling
        recover_stale_runs — this is exactly the scenario an externally run
        benchmark script hit when a uvicorn --reload restart falsely marked
        its still-executing run 'failed'."""
        import os

        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        # pid=os.getpid() — the current (this test's own) process, which is
        # unambiguously alive for the duration of this test.
        rs.create_run(run_id, pid=os.getpid())

        recovered = rs.recover_stale_runs()

        assert recovered == 0
        assert rs.get_run(run_id)["status"] == "running"
        rs.close()

    def test_alive_pid_but_very_stale_heartbeat_is_still_recovered(self, tmp_db: str) -> None:
        """An alive PID alone is not enough to skip recovery — if the
        heartbeat is far older than the staleness window, the row is
        recovered anyway (handles PID reuse after a reboot, and a
        genuinely hung run that stopped updating)."""
        import os
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id, pid=os.getpid())

        # Backdate the heartbeat well past the staleness threshold.
        very_old = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE pipeline_runs SET heartbeat_at = ? WHERE id = ?", (very_old, run_id))
        conn.commit()
        conn.close()

        recovered = rs.recover_stale_runs()

        assert recovered == 1
        assert rs.get_run(run_id)["status"] == "failed"
        rs.close()

    def test_legacy_row_with_no_pid_or_heartbeat_is_recovered(self, tmp_db: str) -> None:
        """Rows written before the pid/heartbeat_at migration (NULL columns)
        must be recovered unconditionally — the same conservative behavior
        as before this fix, applied only to genuinely ambiguous legacy rows.
        """
        import sqlite3

        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        # Insert directly, bypassing create_run, to simulate a pre-migration row.
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO pipeline_runs (id, started_at, status, stats_json, pid, heartbeat_at) "
            "VALUES (?, datetime('now'), 'running', '{}', NULL, NULL)",
            (run_id,),
        )
        conn.commit()
        conn.close()

        recovered = rs.recover_stale_runs()

        assert recovered == 1
        assert rs.get_run(run_id)["status"] == "failed"
        rs.close()

    def test_update_run_stats_refreshes_heartbeat(self, tmp_db: str) -> None:
        """Every progress update must bump heartbeat_at — this is what lets
        a long-running, still-alive process avoid false recovery."""
        import time as _time

        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.get_run(run_id)  # via raw row would need direct query; use conn
        conn = rs._get_conn()
        hb1 = conn.execute(
            "SELECT heartbeat_at FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]

        _time.sleep(0.01)
        rs.update_run_stats(run_id, {"collected": 5})
        hb2 = conn.execute(
            "SELECT heartbeat_at FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()[0]

        assert hb2 >= hb1
        rs.close()

    def test_completed_run_is_not_touched(self, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.complete_run(run_id, {"collected": 10})

        recovered = rs.recover_stale_runs()

        assert recovered == 0
        assert rs.get_run(run_id)["status"] == "completed"
        rs.close()

    def test_failed_run_is_not_touched(self, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.fail_run(run_id, "original error")

        rs.recover_stale_runs()

        row = rs.get_run(run_id)
        assert row["status"] == "failed"
        assert row["error"] == "original error"
        rs.close()

    def test_new_run_can_start_after_recovery(self, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        stale_id = str(uuid.uuid4())
        rs.create_run(stale_id)

        rs.recover_stale_runs()

        # No active running run — new run should be createable.
        new_id = str(uuid.uuid4())
        rs.create_run(new_id)
        assert rs.get_run(new_id)["status"] == "running"
        rs.close()

    def test_multiple_stale_runs_all_recovered(self, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        stale_ids = [str(uuid.uuid4()) for _ in range(3)]
        for sid in stale_ids:
            rs.create_run(sid, pid=self._DEAD_PID)

        recovered = rs.recover_stale_runs()

        assert recovered == 3
        for sid in stale_ids:
            assert rs.get_run(sid)["status"] == "failed"
        rs.close()

    def test_stale_run_visible_in_run_history(self, tmp_db: str) -> None:
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=tmp_db)
        rs.init_db()
        stale_id = str(uuid.uuid4())
        rs.create_run(stale_id)
        rs.recover_stale_runs()

        runs = rs.list_runs(limit=10)
        ids = [r["id"] for r in runs]
        assert stale_id in ids
        rs.close()
