"""Background pipeline runner for the web API.

Wraps the shared pipeline orchestrator in a daemon thread so that
POST /api/pipeline/run returns immediately with a run_id, and
GET /api/pipeline/runs/{id} reflects live progress as the thread updates
the pipeline_runs row in the database.

Only one run at a time is supported.  Concurrent run requests receive
a 409 response.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.api.staged_sources import (
    HelloWorkStagedSourceAdapter,
    LinkedInStagedSourceAdapter,
)
from src.pipeline.orchestrator import run_pipeline
from src.storage.runs import RunStorage, load_retention_config

if TYPE_CHECKING:
    from src.api.progress_stream import ProgressBus
    from src.api.staged_sources import StagedSourceAdapter

logger = logging.getLogger(__name__)


class PipelineRunner:
    """Thread-based pipeline runner.  One instance is shared by the FastAPI app."""

    def __init__(
        self,
        db_path: str = "data/jobs.db",
        config_dir: str = "config",
        progress_bus: "ProgressBus | None" = None,
    ) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._lock = threading.Lock()
        self._current_run_id: str | None = None
        self._progress_bus = progress_bus

    # -- progress events --------------------------------------------------

    def _publish_event(
        self,
        run_id: str,
        event_type: str,
        status: str,
        stats: dict[str, Any],
        error: str | None = None,
    ) -> None:
        """Best-effort SSE progress publish. Never raises, never blocks the run.

        Purely observational: mirrors the same stats/error already persisted
        to RunStorage by ``progress_callback`` / ``complete_run`` /
        ``fail_run`` below — this does not introduce a second authoritative
        state.
        """
        if self._progress_bus is None:
            return
        try:
            self._progress_bus.publish({
                "type": event_type,
                "run_id": run_id,
                "status": status,
                "stage": stats.get("current_stage", ""),
                "stats": stats,
                "error": error,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Progress event publish failed (non-fatal): %s", exc)

    # -- public API -----------------------------------------------------------

    def start_run(self, use_ai: bool = False) -> str:
        """Trigger a background pipeline run.

        Args:
            use_ai: When True, runs Phase 2 (local LLM job understanding) —
                same flag the CLI's ``--ai`` switch controls. Defaults to
                False, preserving the pre-existing (AI-disabled) behavior for
                any caller that omits it.

        Returns:
            The run_id to poll for status.

        Raises:
            RuntimeError: If a run is already in progress.
        """
        import uuid

        # Generate the run_id and reserve the slot atomically under the lock.
        # This closes the race window where two concurrent requests could both
        # pass the "is running?" check before either sets _current_run_id.
        with self._lock:
            if self._current_run_id is not None:
                raise RuntimeError("A pipeline run is already in progress")
            run_id = str(uuid.uuid4())
            self._current_run_id = run_id

        # Create the DB row outside the lock (I/O).  If it fails, clear the
        # slot so future start_run calls can proceed.
        try:
            rs = RunStorage(db_path=self._db_path)
            rs.init_db()
            rs.create_run(run_id)
            # Recorded immediately (before collection even starts) so the
            # dashboard/API can show whether this run was AI-enabled from
            # the very first poll, not just once stats start accumulating.
            rs.update_run_stats(run_id, {"use_ai": use_ai})
            rs.close()
        except Exception:
            with self._lock:
                self._current_run_id = None
            raise

        self._publish_event(run_id, "pipeline.started", "running", {"use_ai": use_ai, "current_stage": ""})

        t = threading.Thread(
            target=self._execute,
            args=(run_id, use_ai),
            daemon=True,
            name=f"pipeline-{run_id[:8]}",
        )
        t.start()
        return run_id

    def is_running(self) -> bool:
        with self._lock:
            return self._current_run_id is not None

    def current_run_id(self) -> str | None:
        with self._lock:
            return self._current_run_id

    # -- internal -------------------------------------------------------------

    def _execute(self, run_id: str, use_ai: bool = False) -> None:
        rs = RunStorage(db_path=self._db_path)
        rs.init_db()

        # -- Staged-source snapshots --------------------------------------------
        # LinkedIn and HelloWork are both collected outside the normal
        # BaseCollector.collect() path and staged separately (see
        # src/api/staged_sources.py for why and the exact snapshot/finalize
        # contract). Iterating over adapters here replaces what used to be two
        # copy-pasted snapshot blocks — each adapter still owns its own
        # source-specific enablement check and staging schema.
        staged_adapters: list["StagedSourceAdapter"] = [
            LinkedInStagedSourceAdapter(self._db_path, self._config_dir),
            HelloWorkStagedSourceAdapter(self._db_path, self._config_dir),
        ]
        prefetched_by_source: dict[str, list[Any]] = {
            adapter.name: adapter.snapshot_for_run(run_id) for adapter in staged_adapters
        }

        # -- Progress callback ------------------------------------------------
        def progress_callback(stage: str, stats: dict[str, Any]) -> None:
            stats["current_stage"] = stage
            try:
                rs.update_run_stats(run_id, stats)
            except Exception as exc:
                logger.warning("Could not update run stats at stage '%s': %s", stage, exc)
            self._publish_event(run_id, "pipeline.progress", "running", stats)

        # -- Pipeline execution -----------------------------------------------
        pipeline_succeeded = False
        try:
            result = run_pipeline(
                db_path=self._db_path,
                config_dir=self._config_dir,
                progress_callback=progress_callback,
                use_ai=use_ai,
                prefetched_linkedin_jobs=prefetched_by_source.get("linkedin", []),
                prefetched_hellowork_jobs=prefetched_by_source.get("hellowork", []),
                run_id=run_id,          # one id for the run row and the decisions journaled for it
            )

            # Persist job scores
            score_rows = [
                {
                    "job_id": rec.job_id,
                    "deterministic_score": rec.deterministic_score,
                    "semantic_score": rec.semantic_score,
                    "final_score": rec.final_score,
                    "score_breakdown": rec.factors,
                }
                for rec in result.scored_jobs
            ]
            if score_rows:
                rs.store_job_scores(run_id, score_rows)

            # Persist rejection records
            rejection_rows = [
                {
                    "job_id": r.job_id,
                    "rejection_stage": r.rejection_stage,
                    "rejection_detail": r.rejection_detail,
                    "title": r.title,
                    "company": r.company,
                    "location": r.location,
                    "source": r.source,
                }
                for r in result.rejection_records
            ]
            if rejection_rows:
                rs.store_rejections(run_id, rejection_rows)

            final_stats = result.stats.to_dict()
            if result.status == "failed":
                error_msg = result.error or "Unknown error"
                rs.fail_run(run_id, error_msg, final_stats)
                self._publish_event(run_id, "pipeline.failed", "failed", final_stats, error=error_msg)
            else:
                pipeline_succeeded = True
                rs.complete_run(run_id, final_stats)
                self._publish_event(run_id, "pipeline.completed", "completed", final_stats)

            # Pipeline-history retention (Phase LATER / I13) — pipeline
            # lifecycle is the natural, smallest hook: no new scheduler/worker
            # needed. Runs after every pipeline execution (success or
            # failure) since it prunes the AGGREGATE history, not anything
            # specific to this run. Best-effort: never affects pipeline
            # status, and never touches the `jobs` table.
            try:
                retention_cfg = load_retention_config(
                    str(Path(self._config_dir) / "retention.yaml")
                )
                deleted = rs.apply_retention(**retention_cfg)
                if deleted["runs_deleted"]:
                    logger.info("Retention: pruned %s", deleted)
            except Exception as exc:
                logger.warning("Retention sweep failed (non-fatal): %s", exc)

        except Exception as exc:
            logger.exception("Runner thread failed for run %s: %s", run_id, exc)
            try:
                rs.fail_run(run_id, str(exc))
            except Exception:
                pass
            self._publish_event(run_id, "pipeline.failed", "failed", {"current_stage": ""}, error=str(exc))

        finally:
            # -- Staged-source finalisation -----------------------------------
            # Always runs, even if an exception escaped the try block above.
            # On success: mark jobs that reached the scoring stage as
            # 'transferred' (they are in the jobs table), and mark the rest
            # as 'processed' (pipeline ran but the job was filtered /
            # deduplicated / failed validation). On failure: each adapter
            # resets its own snapshotted records back to 'waiting'.
            for adapter in staged_adapters:
                transferred_ids: set[str] = set()
                if pipeline_succeeded:
                    transferred_ids = {
                        rec.job_id
                        for rec in result.scored_jobs  # type: ignore[union-attr]
                        if rec.job_source == adapter.name
                    }
                adapter.finalize(run_id, transferred_ids, pipeline_succeeded)
                adapter.close()

            with self._lock:
                self._current_run_id = None
            try:
                rs.close()
            except Exception:
                pass
