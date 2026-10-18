"""Phase LATER / I13 regression tests: pipeline-history retention.

Scope guard for every test here: retention ONLY ever deletes rows from
pipeline_runs/job_scores/job_rejections — it must never touch the `jobs`
table (current/active job data), never delete a 'running' run, and never
drop a run inside the keep_min_runs floor regardless of age.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from src.storage.runs import RunStorage, load_retention_config
from src.storage.sqlite import Storage


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def rs(tmp_db: str):
    store = RunStorage(db_path=tmp_db)
    store.init_db()
    yield store
    store.close()


def _make_run(
    rs: RunStorage,
    *,
    age_days: float,
    status: str = "completed",
    n_scores: int = 2,
    n_rejections: int = 2,
) -> str:
    """Create a run backdated by `age_days`, with n_scores/n_rejections rows."""
    run_id = str(uuid.uuid4())
    rs.create_run(run_id)
    started_at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    conn = rs._get_conn()
    if status == "completed":
        conn.execute(
            "UPDATE pipeline_runs SET started_at = ?, status = 'completed', completed_at = ? WHERE id = ?",
            (started_at, started_at, run_id),
        )
    elif status == "failed":
        conn.execute(
            "UPDATE pipeline_runs SET started_at = ?, status = 'failed' WHERE id = ?",
            (started_at, run_id),
        )
    else:  # running
        conn.execute(
            "UPDATE pipeline_runs SET started_at = ? WHERE id = ?", (started_at, run_id)
        )
    conn.commit()

    if n_scores:
        rs.store_job_scores(
            run_id,
            [
                {"job_id": f"job-{run_id}-{i}", "deterministic_score": 0.5,
                 "semantic_score": 0.1, "final_score": 0.5, "score_breakdown": {}}
                for i in range(n_scores)
            ],
        )
    if n_rejections:
        rs.store_rejections(
            run_id,
            [
                {"job_id": f"rej-{run_id}-{i}", "rejection_stage": "filter",
                 "rejection_detail": "test", "title": "T", "company": "C",
                 "location": "L", "source": "greenhouse"}
                for i in range(n_rejections)
            ],
        )
    return run_id


class TestOldVsRecentRecords:
    def test_old_run_beyond_floor_is_deleted(self, rs: RunStorage) -> None:
        old_run = _make_run(rs, age_days=60)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 1
        assert deleted["job_scores_deleted"] == 2
        assert deleted["job_rejections_deleted"] == 2
        assert rs.get_run(old_run) is None

    def test_recent_run_is_preserved(self, rs: RunStorage) -> None:
        recent_run = _make_run(rs, age_days=1)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 0
        assert rs.get_run(recent_run) is not None


class TestKeepMinRunsFloor:
    def test_old_run_within_keep_min_runs_floor_is_preserved(self, rs: RunStorage) -> None:
        """Even a very old run must survive if it's among the N most recent
        (e.g. every run in a database with fewer than N total runs)."""
        old_run = _make_run(rs, age_days=365)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=100)
        assert deleted["runs_deleted"] == 0
        assert rs.get_run(old_run) is not None

    def test_floor_counts_by_recency_not_just_age(self, rs: RunStorage) -> None:
        """With keep_min_runs=2 and 3 old runs, exactly the single OLDEST
        (by started_at) must be deleted — the 2 most recent survive."""
        oldest = _make_run(rs, age_days=100)
        middle = _make_run(rs, age_days=90)
        newest = _make_run(rs, age_days=80)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=2)
        assert deleted["runs_deleted"] == 1
        assert rs.get_run(oldest) is None
        assert rs.get_run(middle) is not None
        assert rs.get_run(newest) is not None


class TestRunningRunsNeverDeleted:
    def test_old_running_run_is_never_deleted(self, rs: RunStorage) -> None:
        old_running = _make_run(rs, age_days=365, status="running")
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 0
        assert rs.get_run(old_running) is not None
        assert rs.get_run(old_running)["status"] == "running"

    def test_old_failed_run_beyond_floor_is_deleted(self, rs: RunStorage) -> None:
        old_failed = _make_run(rs, age_days=365, status="failed")
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 1
        assert rs.get_run(old_failed) is None


class TestRetentionBoundary:
    def test_run_exactly_at_cutoff_is_preserved(self, rs: RunStorage) -> None:
        """`started_at < cutoff` is a strict inequality — a run whose age
        equals retention_days almost exactly must not be treated as older
        than itself; give it a hair of slack so this is deterministic."""
        run_id = _make_run(rs, age_days=30 - 0.0001)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 0
        assert rs.get_run(run_id) is not None

    def test_run_just_past_cutoff_is_deleted(self, rs: RunStorage) -> None:
        run_id = _make_run(rs, age_days=30.01)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 1
        assert rs.get_run(run_id) is None


class TestMissingRelationships:
    def test_run_with_no_scores_or_rejections_still_deletes_cleanly(self, rs: RunStorage) -> None:
        _make_run(rs, age_days=60, n_scores=0, n_rejections=0)
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert deleted["runs_deleted"] == 1
        assert deleted["job_scores_deleted"] == 0
        assert deleted["job_rejections_deleted"] == 0


class TestRepeatedCleanupIsIdempotent:
    def test_second_call_deletes_nothing_more(self, rs: RunStorage) -> None:
        _make_run(rs, age_days=60)
        first = rs.apply_retention(retention_days=30, keep_min_runs=0)
        second = rs.apply_retention(retention_days=30, keep_min_runs=0)
        assert first["runs_deleted"] == 1
        assert second == {"runs_deleted": 0, "job_scores_deleted": 0, "job_rejections_deleted": 0}

    def test_running_repeatedly_on_empty_database_is_safe(self, rs: RunStorage) -> None:
        for _ in range(3):
            deleted = rs.apply_retention()
            assert deleted == {"runs_deleted": 0, "job_scores_deleted": 0, "job_rejections_deleted": 0}


class TestNeverTouchesJobsTable:
    def test_current_jobs_survive_retention_regardless_of_run_age(
        self, tmp_db: str, rs: RunStorage
    ) -> None:
        storage = Storage(db_path=tmp_db)
        storage.init_db()
        job = Job(id="active-job-1", source="greenhouse", title="Still Active")
        storage.upsert_job(job)

        _make_run(rs, age_days=9999)  # ancient run, well past any retention window
        rs.apply_retention(retention_days=1, keep_min_runs=0)

        assert storage.get_job_by_id("active-job-1") is not None
        storage.close()


class TestLargerBatch:
    def test_many_old_runs_all_pruned_beyond_floor(self, rs: RunStorage) -> None:
        run_ids = [_make_run(rs, age_days=100 + i, n_scores=1, n_rejections=1) for i in range(30)]
        deleted = rs.apply_retention(retention_days=30, keep_min_runs=5)
        assert deleted["runs_deleted"] == 25
        assert deleted["job_scores_deleted"] == 25
        assert deleted["job_rejections_deleted"] == 25
        surviving = [rid for rid in run_ids if rs.get_run(rid) is not None]
        assert len(surviving) == 5


class TestLoadRetentionConfig:
    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_retention_config(str(tmp_path / "does-not-exist.yaml"))
        assert cfg == {"retention_days": 30, "keep_min_runs": 100}

    def test_real_config_file_loads(self) -> None:
        cfg = load_retention_config("config/retention.yaml")
        assert cfg["retention_days"] > 0
        assert cfg["keep_min_runs"] > 0

    def test_non_mapping_yaml_raises_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "retention.yaml"
        path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")
        with pytest.raises(TypeError):
            load_retention_config(str(path))

    def test_partial_override_keeps_other_default(self, tmp_path: Path) -> None:
        path = tmp_path / "retention.yaml"
        path.write_text("retention_days: 7\n", encoding="utf-8")
        cfg = load_retention_config(str(path))
        assert cfg == {"retention_days": 7, "keep_min_runs": 100}
