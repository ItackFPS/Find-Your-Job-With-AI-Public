"""Staged-source adapters consumed by PipelineRunner.

LinkedIn and HelloWork are both collected independently of the normal
BaseCollector.collect() path — a background scanner writes discovered jobs
into a per-source staging table (src/linkedin/staging.py,
src/hellowork/staging.py) on its own schedule, and PipelineRunner picks up
whatever is waiting there when a pipeline run starts.

Both sources need the exact same two-step lifecycle around a run:

  1. snapshot_for_run(run_id) — atomically lock the waiting staging rows into
     this run, convert them to Job objects, and re-inject previously stored
     active jobs from the main jobs table so they remain visible in the API
     between scans (without this, only newly staged jobs would appear in the
     latest run's result set, and previously accepted jobs would "disappear").
  2. finalize(run_id, transferred_ids, succeeded) — after the pipeline
     finishes, mark the snapshotted rows 'transferred'/'processed' on
     success, or reset them all back to 'waiting' on failure so the next run
     retries them.

This module abstracts only that shared shape (StagedSourceAdapter). The
enablement check and staging schema are genuinely source-specific and stay
that way: LinkedInStagedSourceAdapter and HelloWorkStagedSourceAdapter are
separate, independent wrappers around their own staging.py module — this is
deliberately NOT a merge of LinkedIn/HelloWork domain logic into one module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from src.hellowork.staging import HelloWorkStagingStorage
    from src.linkedin.staging import LinkedInStagingStorage
    from src.models.job import Job

logger = logging.getLogger(__name__)


@runtime_checkable
class StagedSourceAdapter(Protocol):
    """Shared snapshot/finalize lifecycle for a pre-staged job source.

    See the module docstring for why this exists and what it does NOT
    abstract (source-specific enablement checks and staging schemas stay in
    each concrete adapter).
    """

    name: str

    def snapshot_for_run(self, run_id: str) -> list["Job"]:
        """Snapshot this source's waiting rows into ``run_id`` and return
        every Job this run should see for this source (fresh snapshot plus
        re-injected previously-active jobs).

        Returns ``[]`` and holds no open staging handle when the source is
        disabled in its own config, or the snapshot itself fails — never
        raises (fail-open, matching the pipeline's other optional stages).
        """
        ...

    def finalize(self, run_id: str, transferred_ids: set[str], succeeded: bool) -> None:
        """Finalize the snapshot ``snapshot_for_run`` took for this run.

        ``succeeded=True``: mark ``transferred_ids`` as 'transferred' (made
        it into the jobs table) and every other snapshotted row as
        'processed' (pipeline ran; filtered/deduped/invalid).
        ``succeeded=False``: reset every snapshotted row back to 'waiting'.

        A no-op if ``snapshot_for_run`` never took a snapshot for this
        source (disabled, or nothing was waiting). Never raises.
        """
        ...

    def close(self) -> None:
        """Release the underlying staging storage connection, if one is open."""
        ...


class LinkedInStagedSourceAdapter:
    """Wraps LinkedInStagingStorage's snapshot/finalize lifecycle."""

    name = "linkedin"

    def __init__(self, db_path: str, config_dir: str) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._staging: "LinkedInStagingStorage | None" = None
        self._snapshot_taken = False

    def snapshot_for_run(self, run_id: str) -> list["Job"]:
        jobs: list["Job"] = []
        try:
            import yaml

            li_config_path = Path(self._config_dir) / "linkedin.yaml"
            if not li_config_path.exists():
                return []
            with li_config_path.open() as f:
                li_cfg = yaml.safe_load(f) or {}
            if not li_cfg.get("enabled", False):
                return []

            from src.linkedin.staging import LinkedInStagingStorage, staging_row_to_job
            from src.storage.sqlite import Storage as _Storage

            self._staging = LinkedInStagingStorage(db_path=self._db_path)
            self._staging.init_db()
            snapshot_rows = self._staging.snapshot_waiting(run_id)
            if snapshot_rows:
                jobs = [staging_row_to_job(r) for r in snapshot_rows]
                self._snapshot_taken = True
                logger.info(
                    "LinkedIn: snapshotted %d new job(s) for pipeline run %s.",
                    len(jobs), run_id,
                )

            # Re-inject previously stored active LinkedIn jobs so they are
            # re-scored in this run and remain visible in the API. Without
            # this, only newly staged jobs appear in the latest-run result set,
            # causing previously accepted jobs to "disappear" from the UI.
            try:
                _store = _Storage(db_path=self._db_path)
                _store.init_db()
                snapshot_ids = {j.id for j in jobs}
                existing_li_jobs = _store.get_active_jobs_by_source(
                    "linkedin", exclude_ids=snapshot_ids
                )
                if existing_li_jobs:
                    jobs.extend(existing_li_jobs)
                    logger.info(
                        "LinkedIn: re-injecting %d existing active job(s) into run %s.",
                        len(existing_li_jobs), run_id,
                    )
            except Exception as exc:
                logger.warning(
                    "LinkedIn: failed to load existing active jobs — "
                    "previously stored jobs may not appear in this run: %s", exc,
                )

            return jobs
        except Exception as exc:
            logger.warning(
                "LinkedIn staging snapshot failed — continuing without LinkedIn jobs: %s", exc
            )
            self._staging = None
            self._snapshot_taken = False
            return []

    def finalize(self, run_id: str, transferred_ids: set[str], succeeded: bool) -> None:
        if self._staging is None or not self._snapshot_taken:
            return
        try:
            if succeeded:
                transferred = self._staging.mark_transferred_by_stable_ids(run_id, transferred_ids)
                processed = self._staging.mark_remaining_as_processed(run_id)
                logger.info(
                    "LinkedIn: run %s — %d job(s) transferred (in jobs table), "
                    "%d job(s) processed (filtered/deduped/invalid).",
                    run_id, transferred, processed,
                )
            else:
                count = self._staging.reset_snapshot(run_id)
                logger.info(
                    "LinkedIn: reset %d snapshotted job(s) to waiting (run %s failed).",
                    count, run_id,
                )
        except Exception as exc:
            logger.error("LinkedIn: failed to finalise snapshot for run %s: %s", run_id, exc)

    def close(self) -> None:
        if self._staging is not None:
            try:
                self._staging.close()
            except Exception:
                pass


class HelloWorkStagedSourceAdapter:
    """Wraps HelloWorkStagingStorage's snapshot/finalize lifecycle.

    HelloWork's own scanner (src/hellowork/scanner.py) collects independently
    on its own daily schedule (config/schedules.yaml: hellowork_daily)
    because a live collection takes ~30 minutes (src/collectors/hellowork.py)
    — far longer than this pipeline's normal ~200-250s run. This adapter just
    grabs whatever is already staged, same mechanism as LinkedIn above.
    """

    name = "hellowork"

    def __init__(self, db_path: str, config_dir: str) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._staging: "HelloWorkStagingStorage | None" = None
        self._snapshot_taken = False

    def snapshot_for_run(self, run_id: str) -> list["Job"]:
        jobs: list["Job"] = []
        try:
            sources_path = Path(self._config_dir) / "sources.yaml"
            from src.discovery.registry import SourceRegistry
            if not (
                sources_path.exists()
                and SourceRegistry(config_path=str(sources_path)).get_source("hellowork")
            ):
                return []

            from src.hellowork.staging import HelloWorkStagingStorage, staging_row_to_job
            from src.storage.sqlite import Storage as _Storage

            self._staging = HelloWorkStagingStorage(db_path=self._db_path)
            self._staging.init_db()
            snapshot_rows = self._staging.snapshot_waiting(run_id)
            if snapshot_rows:
                jobs = [staging_row_to_job(r) for r in snapshot_rows]
                self._snapshot_taken = True
                logger.info(
                    "HelloWork: snapshotted %d new job(s) for pipeline run %s.",
                    len(jobs), run_id,
                )

            # Re-inject previously stored active HelloWork jobs so they are
            # re-scored in this run and remain visible in the API (same
            # reasoning as the LinkedIn re-injection above).
            try:
                _store = _Storage(db_path=self._db_path)
                _store.init_db()
                snapshot_ids = {j.id for j in jobs}
                existing_hw_jobs = _store.get_active_jobs_by_source(
                    "hellowork", exclude_ids=snapshot_ids
                )
                if existing_hw_jobs:
                    jobs.extend(existing_hw_jobs)
                    logger.info(
                        "HelloWork: re-injecting %d existing active job(s) into run %s.",
                        len(existing_hw_jobs), run_id,
                    )
            except Exception as exc:
                logger.warning(
                    "HelloWork: failed to load existing active jobs — "
                    "previously stored jobs may not appear in this run: %s", exc,
                )

            return jobs
        except Exception as exc:
            logger.warning(
                "HelloWork staging snapshot failed — continuing without HelloWork jobs: %s", exc
            )
            self._staging = None
            self._snapshot_taken = False
            return []

    def finalize(self, run_id: str, transferred_ids: set[str], succeeded: bool) -> None:
        if self._staging is None or not self._snapshot_taken:
            return
        try:
            if succeeded:
                transferred = self._staging.mark_transferred_by_stable_ids(run_id, transferred_ids)
                processed = self._staging.mark_remaining_as_processed(run_id)
                logger.info(
                    "HelloWork: run %s — %d job(s) transferred (in jobs table), "
                    "%d job(s) processed (filtered/deduped/invalid).",
                    run_id, transferred, processed,
                )
            else:
                count = self._staging.reset_snapshot(run_id)
                logger.info(
                    "HelloWork: reset %d snapshotted job(s) to waiting (run %s failed).",
                    count, run_id,
                )
        except Exception as exc:
            logger.error("HelloWork: failed to finalise snapshot for run %s: %s", run_id, exc)

    def close(self) -> None:
        if self._staging is not None:
            try:
                self._staging.close()
            except Exception:
                pass
