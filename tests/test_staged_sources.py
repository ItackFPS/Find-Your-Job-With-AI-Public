"""Focused tests for the StagedSourceAdapter lifecycle (src/api/staged_sources.py).

Uses fake staging stores (not real SQLite) to isolate the adapter's own
snapshot/finalize/enablement logic from the actual LinkedInStagingStorage /
HelloWorkStagingStorage implementations, which already have their own
extensive test coverage (tests/test_linkedin_staging.py,
tests/test_hellowork_staging.py) and integration coverage through
PipelineRunner (tests/test_linkedin_persistence.py,
tests/test_hellowork_persistence.py).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any


from src.api.staged_sources import (
    HelloWorkStagedSourceAdapter,
    LinkedInStagedSourceAdapter,
    StagedSourceAdapter,
)
from src.models.job import Job


# ---------------------------------------------------------------------------
# Fake staging stores
# ---------------------------------------------------------------------------

class FakeStaging:
    """Records every call it receives; returns canned data configured by the test."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.snapshot_rows: list[dict[str, Any]] = []
        self.snapshot_calls: list[str] = []
        self.transferred_calls: list[tuple[str, set[str]]] = []
        self.processed_calls: list[str] = []
        self.reset_calls: list[str] = []
        self.closed = False
        self.init_db_called = False

    def init_db(self) -> None:
        self.init_db_called = True

    def snapshot_waiting(self, run_id: str) -> list[dict[str, Any]]:
        self.snapshot_calls.append(run_id)
        return self.snapshot_rows

    def mark_transferred_by_stable_ids(self, run_id: str, stable_ids: set[str]) -> int:
        self.transferred_calls.append((run_id, stable_ids))
        return len(stable_ids)

    def mark_remaining_as_processed(self, run_id: str) -> int:
        self.processed_calls.append(run_id)
        return 1

    def reset_snapshot(self, run_id: str) -> int:
        self.reset_calls.append(run_id)
        return 1

    def close(self) -> None:
        self.closed = True


class FakeJobStore:
    """Fake for src.storage.sqlite.Storage — only the method adapters use."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.active_jobs: list[Job] = []
        self.calls: list[tuple[str, set[str]]] = []

    def init_db(self) -> None:
        pass

    def get_active_jobs_by_source(self, source: str, exclude_ids: set[str] | None = None) -> list[Job]:
        exclude_ids = exclude_ids or set()
        self.calls.append((source, exclude_ids))
        return [j for j in self.active_jobs if j.id not in exclude_ids]


def _job(job_id: str, title: str = "Developer") -> Job:
    return Job(id=job_id, source="test", title=title, discovered_at=datetime.now(UTC))


def _staging_row(row_id: str, title: str = "Developer") -> dict[str, Any]:
    return {"id": row_id, "title": title}


def _fake_row_to_job(row: dict[str, Any]) -> Job:
    return _job(row["id"], title=row.get("title", "Developer"))


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    def test_linkedin_adapter_satisfies_protocol(self, tmp_path: Path):
        adapter = LinkedInStagedSourceAdapter(db_path="x", config_dir=str(tmp_path))
        assert isinstance(adapter, StagedSourceAdapter)
        assert adapter.name == "linkedin"

    def test_hellowork_adapter_satisfies_protocol(self, tmp_path: Path):
        adapter = HelloWorkStagedSourceAdapter(db_path="x", config_dir=str(tmp_path))
        assert isinstance(adapter, StagedSourceAdapter)
        assert adapter.name == "hellowork"


# ---------------------------------------------------------------------------
# LinkedIn adapter
# ---------------------------------------------------------------------------

class TestLinkedInStagedSourceAdapter:
    def _enable_linkedin(self, tmp_path: Path, enabled: bool = True) -> None:
        (tmp_path / "linkedin.yaml").write_text(f"enabled: {str(enabled).lower()}\n", encoding="utf-8")

    def _patch_stores(self, monkeypatch, fake_staging: FakeStaging, fake_job_store: FakeJobStore) -> None:
        monkeypatch.setattr(
            "src.linkedin.staging.LinkedInStagingStorage", lambda db_path: fake_staging
        )
        monkeypatch.setattr("src.linkedin.staging.staging_row_to_job", _fake_row_to_job)
        monkeypatch.setattr("src.storage.sqlite.Storage", lambda db_path: fake_job_store)

    def test_disabled_when_config_file_missing(self, tmp_path: Path, monkeypatch):
        """No linkedin.yaml at all -> disabled, no staging store touched."""
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        assert jobs == []
        assert fake_staging.init_db_called is False
        assert fake_staging.snapshot_calls == []

    def test_disabled_when_enabled_false(self, tmp_path: Path, monkeypatch):
        self._enable_linkedin(tmp_path, enabled=False)
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        assert jobs == []
        assert fake_staging.snapshot_calls == []

    def test_enabled_snapshots_waiting_rows_and_reinjects_active(self, tmp_path: Path, monkeypatch):
        self._enable_linkedin(tmp_path, enabled=True)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("new-1"), _staging_row("new-2")]
        fake_job_store = FakeJobStore("x")
        fake_job_store.active_jobs = [_job("existing-1")]
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        ids = {j.id for j in jobs}
        assert ids == {"new-1", "new-2", "existing-1"}
        assert fake_staging.init_db_called is True
        assert fake_staging.snapshot_calls == ["run-1"]
        # exclude_ids passed to the re-injection query must be the fresh snapshot's IDs.
        assert fake_job_store.calls == [("linkedin", {"new-1", "new-2"})]

    def test_active_job_already_in_snapshot_is_not_duplicated(self, tmp_path: Path, monkeypatch):
        """get_active_jobs_by_source is called with exclude_ids covering the
        snapshot — a real store would already dedupe on that, but the fake
        wiring here specifically proves the adapter passes the right set."""
        self._enable_linkedin(tmp_path, enabled=True)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("dup-1")]
        fake_job_store = FakeJobStore("x")
        fake_job_store.active_jobs = [_job("dup-1")]  # would be excluded by a real store
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")

        assert fake_job_store.calls[0][1] == {"dup-1"}

    def test_finalize_noop_when_no_snapshot_was_taken(self, tmp_path: Path, monkeypatch):
        """Disabled (or nothing waiting) -> finalize must not touch anything,
        even if called with succeeded=True."""
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")  # no linkedin.yaml -> disabled
        adapter.finalize("run-1", {"x"}, succeeded=True)

        assert fake_staging.transferred_calls == []
        assert fake_staging.reset_calls == []

    def test_finalize_on_success_marks_transferred_and_processed(self, tmp_path: Path, monkeypatch):
        self._enable_linkedin(tmp_path, enabled=True)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("j1")]
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")
        adapter.finalize("run-1", {"j1"}, succeeded=True)

        assert fake_staging.transferred_calls == [("run-1", {"j1"})]
        assert fake_staging.processed_calls == ["run-1"]
        assert fake_staging.reset_calls == []

    def test_finalize_on_failure_resets_snapshot(self, tmp_path: Path, monkeypatch):
        self._enable_linkedin(tmp_path, enabled=True)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("j1")]
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")
        adapter.finalize("run-1", set(), succeeded=False)

        assert fake_staging.reset_calls == ["run-1"]
        assert fake_staging.transferred_calls == []

    def test_close_closes_the_staging_store(self, tmp_path: Path, monkeypatch):
        self._enable_linkedin(tmp_path, enabled=True)
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")
        adapter.close()

        assert fake_staging.closed is True

    def test_close_is_a_noop_when_never_snapshotted(self, tmp_path: Path):
        """No exception when close() is called without ever calling snapshot_for_run."""
        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.close()  # must not raise

    def test_snapshot_failure_fails_open(self, tmp_path: Path, monkeypatch):
        """Any exception during snapshotting must be swallowed — the run
        continues with an empty job list for this source, never propagating."""
        self._enable_linkedin(tmp_path, enabled=True)

        def _boom(db_path):
            raise RuntimeError("db locked")

        monkeypatch.setattr("src.linkedin.staging.LinkedInStagingStorage", _boom)

        adapter = LinkedInStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        assert jobs == []
        adapter.close()  # must not raise either


# ---------------------------------------------------------------------------
# HelloWork adapter — mirrors the LinkedIn suite above, differing only in
# its source-specific enablement check (sources.yaml registry lookup
# instead of a dedicated linkedin.yaml enabled flag).
# ---------------------------------------------------------------------------

class FakeSourceRegistry:
    def __init__(self, config_path: str, active: bool = True) -> None:
        self.config_path = config_path
        self._active = active

    def get_source(self, name: str) -> dict[str, Any] | None:
        return {"name": name} if self._active else None


class TestHelloWorkStagedSourceAdapter:
    def _write_sources_yaml(self, tmp_path: Path) -> None:
        (tmp_path / "sources.yaml").write_text("sources: []\n", encoding="utf-8")

    def _patch_stores(
        self, monkeypatch, fake_staging: FakeStaging, fake_job_store: FakeJobStore, active: bool = True
    ) -> None:
        monkeypatch.setattr(
            "src.hellowork.staging.HelloWorkStagingStorage", lambda db_path: fake_staging
        )
        monkeypatch.setattr("src.hellowork.staging.staging_row_to_job", _fake_row_to_job)
        monkeypatch.setattr("src.storage.sqlite.Storage", lambda db_path: fake_job_store)
        monkeypatch.setattr(
            "src.discovery.registry.SourceRegistry",
            lambda config_path: FakeSourceRegistry(config_path, active=active),
        )

    def test_disabled_when_sources_yaml_missing(self, tmp_path: Path, monkeypatch):
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store)

        adapter = HelloWorkStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        assert jobs == []
        assert fake_staging.snapshot_calls == []

    def test_disabled_when_source_not_registered(self, tmp_path: Path, monkeypatch):
        self._write_sources_yaml(tmp_path)
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store, active=False)

        adapter = HelloWorkStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        assert jobs == []
        assert fake_staging.snapshot_calls == []

    def test_enabled_snapshots_waiting_rows_and_reinjects_active(self, tmp_path: Path, monkeypatch):
        self._write_sources_yaml(tmp_path)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("hw-new-1")]
        fake_job_store = FakeJobStore("x")
        fake_job_store.active_jobs = [_job("hw-existing-1")]
        self._patch_stores(monkeypatch, fake_staging, fake_job_store, active=True)

        adapter = HelloWorkStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        jobs = adapter.snapshot_for_run("run-1")

        ids = {j.id for j in jobs}
        assert ids == {"hw-new-1", "hw-existing-1"}
        assert fake_job_store.calls == [("hellowork", {"hw-new-1"})]

    def test_finalize_on_success_marks_transferred_and_processed(self, tmp_path: Path, monkeypatch):
        self._write_sources_yaml(tmp_path)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("hw-1")]
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store, active=True)

        adapter = HelloWorkStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")
        adapter.finalize("run-1", {"hw-1"}, succeeded=True)

        assert fake_staging.transferred_calls == [("run-1", {"hw-1"})]
        assert fake_staging.processed_calls == ["run-1"]

    def test_finalize_on_failure_resets_snapshot(self, tmp_path: Path, monkeypatch):
        self._write_sources_yaml(tmp_path)
        fake_staging = FakeStaging("x")
        fake_staging.snapshot_rows = [_staging_row("hw-1")]
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store, active=True)

        adapter = HelloWorkStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")
        adapter.finalize("run-1", set(), succeeded=False)

        assert fake_staging.reset_calls == ["run-1"]

    def test_finalize_noop_when_disabled(self, tmp_path: Path, monkeypatch):
        fake_staging = FakeStaging("x")
        fake_job_store = FakeJobStore("x")
        self._patch_stores(monkeypatch, fake_staging, fake_job_store, active=False)

        adapter = HelloWorkStagedSourceAdapter(db_path="db", config_dir=str(tmp_path))
        adapter.snapshot_for_run("run-1")
        adapter.finalize("run-1", {"x"}, succeeded=True)

        assert fake_staging.transferred_calls == []
        assert fake_staging.reset_calls == []
