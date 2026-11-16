"""Staged-source adapters consumed by PipelineRunner.

LinkedIn and HelloWork are both collected independently of the normal
BaseCollector.collect() path — a background scanner writes discovered jobs
into a per-source staging table (src/linkedin/staging.py,
src/hellowork/staging.py) on its own schedule, and PipelineRunner picks up
whatever is waiting there when a pipeline run starts.

Both sources need the exact same two-step lifecycle around a run:

  1. snapshot_for_run(run_id) — atomically lock the waiting staging rows into
     this run and convert them to Job objects. Previously stored active jobs
     stay visible between scans because the pipeline carries the stored
     offers of every enabled source into each run (src/pipeline/carryover.py,
     issue #253); before that, these adapters re-injected them themselves.
  2. finalize(run_id, transferred_ids, succeeded) — after the pipeline
     finishes, mark the snapshotted rows 'transferred'/'processed' on
     success, or reset them all back to 'waiting' on failure so the next run
     retries them.

This module abstracts only that shared shape (StagedSourceAdapter). Whether
a source is on is the single rule of src/source_enablement.py (Phase B B4-A):
a source that is off gives no snapshot, and the pipeline carries no offer of it,
so its stored offers are not scored (owner decision D1). The staging schema is genuinely
source-specific: LinkedInStagedSourceAdapter and HelloWorkStagedSourceAdapter are
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
        them as Jobs (the stored offers are carried by the pipeline, #253).

        Returns ``[]`` and holds no open staging handle when the source is
        off (src/source_enablement.py), or the snapshot itself fails — never
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

    def __init__(self, db_path: str, config_dir: str, settings_path: str | Path | None = None) -> None:
        from src.source_enablement import settings_path_for

        self._db_path = db_path
        self._config_dir = config_dir
        self._settings_path = Path(settings_path) if settings_path else settings_path_for(db_path)
        self._staging: "LinkedInStagingStorage | None" = None
        self._snapshot_taken = False

    def snapshot_for_run(self, run_id: str) -> list["Job"]:
        jobs: list["Job"] = []
        try:
            from src.source_enablement import source_enabled

            if not source_enabled("linkedin", self._config_dir, self._settings_path):
                return []

            from src.linkedin.staging import LinkedInStagingStorage, staging_row_to_job

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

            # Stored active LinkedIn offers are carried into every run by the pipeline itself
            # (src/pipeline/carryover.py, issue #253), like every enabled source's: a carried offer
            # is not a sighting, so it is no longer mixed into this snapshot.
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

    def __init__(self, db_path: str, config_dir: str, settings_path: str | Path | None = None) -> None:
        from src.source_enablement import settings_path_for

        self._db_path = db_path
        self._config_dir = config_dir
        self._settings_path = Path(settings_path) if settings_path else settings_path_for(db_path)
        self._staging: "HelloWorkStagingStorage | None" = None
        self._snapshot_taken = False

    def snapshot_for_run(self, run_id: str) -> list["Job"]:
        jobs: list["Job"] = []
        try:
            from src.source_enablement import source_enabled

            if not source_enabled("hellowork", self._config_dir, self._settings_path):
                return []

            from src.hellowork.staging import HelloWorkStagingStorage, staging_row_to_job

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

            # Stored active HelloWork offers are carried by the pipeline (issue #253), see above.
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
