"""HelloWork background scanner.

Runs the (slow, ~30 minute) HelloWorkCollector synchronously and persists
results to HelloWorkStagingStorage, mirroring src/linkedin/scanner.py's
LinkedInScanner so the main pipeline run never waits on a live HelloWork
collection -- see src/collectors/hellowork.py's module docstring for why
that collection is inherently slow (one HTTP request per job, no bulk
endpoint exists).

Public interface:
  run_scan()    -- run synchronously in the calling thread (used by the
                   generic scheduler, see src/scheduler/service.py)
  is_scanning() -- thread-safe check
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any

from src.hellowork.staging import HelloWorkStagingStorage, job_to_staging_record
from src.scan_control import (
    STATE_PAUSED,
    STATE_RATE_LIMITED,
    AdaptivePacer,
    StopSignal,
    config_fingerprint,
    cooling_until,
    interrupted_checkpoint_id,
    persist_pause,
    record_rate_limit,
    record_shutdown_stop,
    resumable_scan_id,
    scan_state,
)

logger = logging.getLogger(__name__)
_MAX_ERROR_SAMPLES = 5


class HelloWorkScanner:
    """Collects HelloWork jobs and persists them to staging.

    Args:
        config: The hellowork source's `config` sub-dict from sources.yaml
            (domains, metier_slugs, max_jobs_per_slug, rate_limit_delay,
            timeout) -- the same dict HelloWorkCollector itself accepts.
        staging: Shared HelloWorkStagingStorage instance.
    """

    def __init__(
        self,
        config: dict[str, Any],
        staging: HelloWorkStagingStorage,
        shutdown_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.staging = staging
        self._scan_lock = threading.Lock()
        self._current_scan_id: str | None = None
        # Set at the end of _do_scan to reflect whether the most recent
        # run_scan() call genuinely finished (vs. stopped for shutdown or an
        # internally-caught failure). Read by HelloWorkHandler.dispatch()
        # (src/scheduler/service.py) so SchedulerService can record
        # schedule_executions.status accurately instead of assuming
        # "dispatch() didn't raise" means "completed". Does not change
        # run_scan()'s own return value/contract at all.
        self._last_scan_completed = False
        # Checked between slugs/job-detail requests so a backend shutdown
        # (uvicorn --reload restart, or a real stop) can let an in-flight
        # scan finish its current unit of work cleanly and exit at a
        # checkpointed boundary, instead of being abandoned mid-request when
        # the process dies. Mirrors LinkedInScanner's _shutdown_event
        # exactly — see src/linkedin/scanner.py for the same pattern. Does
        # not change checkpoint save/load semantics at all.
        self._shutdown_event = shutdown_event if shutdown_event is not None else threading.Event()
        # User pause (issue #81): private to this scanner, honoured at exactly
        # the same checkpointed boundaries as a shutdown. Cleared whenever a
        # new scan claims the lock, so a stale request never pauses a resume.
        self._pause_event = threading.Event()
        self._last_scan_paused = False
        self._last_scan_rate_limited = False

    def is_scanning(self) -> bool:
        with self._scan_lock:
            return self._current_scan_id is not None

    def current_scan_id(self) -> str | None:
        with self._scan_lock:
            return self._current_scan_id

    def last_scan_completed(self) -> bool:
        """Whether the most recently finished run_scan() genuinely completed.

        False for a cooperative shutdown stop, an internally-caught failure,
        or if no scan has run yet in this process.
        """
        return self._last_scan_completed

    def shutdown_requested(self) -> bool:
        """Whether the backend asked running scans to stop (the scheduler
        records such a scan as interrupted, not failed — issue #68)."""
        return self._shutdown_event.is_set()

    # -- pause / resume (issue #81) ---------------------------------------------

    def request_pause(self) -> bool:
        """Ask the running scan to stop at its next checkpoint boundary.

        Returns False (and does nothing) when no scan is running, so a pause
        racing a completion, or a repeated pause, is a harmless no-op.
        """
        with self._scan_lock:
            if self._current_scan_id is None:
                return False
            if not self._pause_event.is_set():
                self._pause_event.set()
                logger.info(
                    "HelloWork scan %s: pause requested — stopping at the next slug boundary.",
                    self._current_scan_id,
                )
            return True

    def pause_requested(self) -> bool:
        return self.is_scanning() and self._pause_event.is_set()

    def last_scan_paused(self) -> bool:
        """Whether the most recently finished run_scan() stopped for a user pause."""
        return self._last_scan_paused

    def last_scan_rate_limited(self) -> bool:
        """Whether the most recently finished run_scan() stopped on a 429 (F6.4)."""
        return self._last_scan_rate_limited

    def rate_limited_until(self) -> str | None:
        """ISO time a rate-limited scan may resume, while that is still ahead (F6.4)."""
        if self.is_scanning():
            return None
        until = cooling_until(self.staging.get_scan_checkpoint())
        return until.isoformat() if until is not None else None

    def scan_state(self) -> str:
        """idle | running | pausing | paused | interrupted (see src/scan_control.py)."""
        return scan_state(
            self.is_scanning(), self._pause_event.is_set(), self.staging.get_scan_checkpoint()
        )

    def is_paused(self) -> bool:
        return self.scan_state() == STATE_PAUSED

    def interrupted_checkpoint_id(self) -> str | None:
        """Identity of an interrupted checkpoint the scheduler may resume
        (see src/scan_control.py); None while scanning or when there is none."""
        if self.is_scanning():
            return None
        return interrupted_checkpoint_id(self.staging.get_scan_checkpoint())

    def has_interrupted_checkpoint(self) -> bool:
        return self.interrupted_checkpoint_id() is not None

    def progress(self) -> dict[str, Any] | None:
        """Durable progress from the checkpoint, or None when there is none.

        The total slug count is only known after the slug list is resolved
        from the network inside collect(), so no total/percentage is given.
        """
        cp = self.staging.get_scan_checkpoint()
        if cp is None:
            return None
        return {
            "slugs_completed": len(json.loads(cp.get("completed_slugs_json") or "[]")),
            "updated_at": cp.get("updated_at"),
        }

    def _config_fingerprint(self) -> str:
        """Settings that define what a "completed slug" contains."""
        return config_fingerprint({"max_jobs_per_slug": self.config.get("max_jobs_per_slug")})

    def _set_checkpoint_state_quietly(self, scan_id: str, state: str) -> None:
        """Label an existing checkpoint; never creates one, never raises."""
        try:
            self.staging.set_checkpoint_state(scan_id, state, create_if_missing=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "HelloWork: could not mark checkpoint %s for scan %s: %s", state, scan_id, exc
            )

    def _next_scan_id(self) -> str:
        """The resumable logical scan's id when there is one (its history row
        is reopened, issue #86), else a fresh id for a new run."""
        try:
            checkpoint = self.staging.get_scan_checkpoint()
            run = self.staging.get_scan_run(checkpoint["scan_id"]) if checkpoint else None
            resumed = resumable_scan_id(checkpoint, run, self._config_fingerprint())
        except Exception as exc:  # noqa: BLE001
            logger.warning("HelloWork: could not read the checkpoint to reuse its scan run: %s", exc)
            resumed = None
        return resumed or str(uuid.uuid4())

    def run_scan(self) -> str | None:
        """Run a full HelloWork collection synchronously.

        Returns the scan_id, or None if a scan is already in progress.
        Never raises -- failures are recorded on the scan_run row and logged.
        Used by the generic scheduler (src/scheduler/service.py), which
        already dispatches this call inside its own daemon thread.
        """
        next_id = self._next_scan_id()
        with self._scan_lock:
            if self._current_scan_id is not None:
                logger.warning("HelloWork: scan already in progress (%s), skipping.", self._current_scan_id)
                return None
            scan_id = next_id
            self._current_scan_id = scan_id
            self._pause_event.clear()
        try:
            self._do_scan(scan_id)
        finally:
            with self._scan_lock:
                self._current_scan_id = None
        return scan_id

    def start_scan(self) -> str:
        """Trigger a scan in a daemon thread. Raises RuntimeError if already scanning.

        Used by the manual "scan now" API endpoint (src/api/routes/hellowork.py),
        which needs to return immediately rather than block for ~30 minutes.
        """
        next_id = self._next_scan_id()
        with self._scan_lock:
            if self._current_scan_id is not None:
                raise RuntimeError("A HelloWork scan is already in progress")
            scan_id = next_id
            self._current_scan_id = scan_id
            self._pause_event.clear()
        t = threading.Thread(
            target=self._run_and_unlock,
            args=(scan_id,),
            daemon=True,
            name=f"hellowork-scan-{scan_id[:8]}",
        )
        t.start()
        return scan_id

    def _run_and_unlock(self, scan_id: str) -> None:
        try:
            self._do_scan(scan_id)
        finally:
            with self._scan_lock:
                self._current_scan_id = None

    def _do_scan(self, scan_id: str) -> None:
        from src.collectors.hellowork import HelloWorkCollector
        from src.pipeline.normalize import normalize_job

        self._last_scan_paused = False
        self._last_scan_rate_limited = False
        # create_scan_run() must be inside this try: a transient "database is
        # locked" from a concurrent writer (pipeline, API request, or the
        # sibling scanner) previously propagated straight out of this method,
        # uncaught, when it happened on this first call. That killed the
        # background thread silently: _run_and_unlock()'s finally still
        # cleared _current_scan_id (releasing the scan lock) but no scan_run
        # row was ever created/failed, so the very next trigger (scheduler
        # tick or a manual "Scan Now") started a brand new scan_id from
        # scratch — indistinguishable from the scan having "reset to the
        # beginning". Catching it here lets the scan retry cleanly next time
        # with a properly recorded failure instead.
        try:
            # jobs_found/jobs_new are the logical scan's running totals: a
            # reopened run continues from the counts its earlier attempts
            # saved alongside each slug checkpoint (issue #86).
            jobs_found = 0
            jobs_new = 0
            if self.staging.reopen_scan_run(scan_id):
                run = self.staging.get_scan_run(scan_id) or {}
                jobs_found = int(run.get("jobs_found") or 0)
                jobs_new = int(run.get("jobs_new") or 0)
                logger.info("HelloWork: scan %s resumed (same logical scan).", scan_id)
            else:
                self.staging.create_scan_run(scan_id)
                logger.info("HelloWork: scan %s started.", scan_id)

            # Resume support: if a previous scan was interrupted (process
            # died mid-collection, ~30 minutes long), its checkpoint lists
            # every metier slug already fully processed. Skipping those
            # avoids redundant listing/detail-page fetches on top of the
            # job-level dedup insert_job() already provides. A checkpoint
            # only exists when the last scan never reached a clean finish —
            # see clear_scan_checkpoint() below and reset_stale_checkpoint()
            # at startup for the corresponding cross-process safety net.
            fingerprint = self._config_fingerprint()
            checkpoint = self.staging.get_scan_checkpoint()
            if checkpoint and checkpoint.get("config_fingerprint") not in (None, fingerprint):
                # Slugs "completed" under a different per-slug job cap may
                # hold fewer jobs than the current config asks for: start
                # over rather than skip them (issue #81).
                logger.warning(
                    "HelloWork scan %s: checkpoint was taken under a different "
                    "configuration — discarding it and starting from the first slug.",
                    scan_id,
                )
                if checkpoint["scan_id"] != scan_id:
                    self.staging.abandon_scan_run(checkpoint["scan_id"])
                self.staging.clear_scan_checkpoint()
                checkpoint = None
            completed_slugs: set[str] = set()
            # After a rate limit, resume slowly (F6.4: no burst after a resume).
            after_limit = bool(checkpoint) and (checkpoint or {}).get("state") == STATE_RATE_LIMITED
            if checkpoint:
                self.staging.set_checkpoint_state(scan_id, "running", create_if_missing=False)
                completed_slugs = set(json.loads(checkpoint.get("completed_slugs_json") or "[]"))
                if completed_slugs:
                    logger.info(
                        "HelloWork: scan %s resuming — skipping %d metier slug(s) "
                        "already completed in a previous interrupted attempt.",
                        scan_id, len(completed_slugs),
                    )

            def _stage_slug(slug: str, raw_jobs: list[dict[str, Any]]) -> None:
                nonlocal jobs_found, jobs_new
                for raw in raw_jobs:
                    try:
                        job = normalize_job("hellowork", raw)
                    except (ValueError, TypeError, KeyError) as exc:
                        logger.warning("HelloWork: failed to normalize a job during scan %s: %s", scan_id, exc)
                        continue
                    if not job.title or not job.canonical_url:
                        continue
                    record = job_to_staging_record(job, scan_id)
                    try:
                        is_new = self.staging.insert_job(record)
                    except Exception as exc:  # noqa: BLE001
                        # Mirrors src/linkedin/scanner.py's per-row handling: a
                        # transient write conflict (e.g. "database is locked"
                        # from a concurrent pipeline/API writer) must not abort
                        # the whole scan and lose every job still queued behind
                        # this one — skip just this job and keep going.
                        logger.warning("HelloWork: could not stage job row: %s", exc)
                        continue
                    jobs_found += 1
                    if is_new:
                        jobs_new += 1

                # This slug is fully done (staged above, whether 0 or more
                # jobs) — check it off immediately so a crash right after
                # this point resumes from the *next* slug, not this one.
                completed_slugs.add(slug)
                try:
                    self.staging.save_scan_checkpoint(
                        scan_id, sorted(completed_slugs), fingerprint=fingerprint,
                        jobs_found=jobs_found, jobs_new=jobs_new,
                    )
                except Exception as exc:  # noqa: BLE001
                    # A failed checkpoint write only means this slug may be
                    # redundantly re-fetched on the next resume (wasted
                    # network work, not data loss — insert_job() above
                    # already committed every job from this slug).
                    logger.warning(
                        "HelloWork: could not persist scan checkpoint after slug %r: %s", slug, exc
                    )

            collector = HelloWorkCollector(config=self.config)
            # Background scan (F6.4): stop on the first 429, pace requests adaptively.
            collector.stop_on_rate_limit = True
            collector.pacer = AdaptivePacer(getattr(collector, "rate_limit_delay", 1.0), resumed=after_limit)
            stop = StopSignal(self._shutdown_event, self._pause_event)
            result = collector.collect(
                on_slug_done=_stage_slug,
                skip_slugs=completed_slugs,
                shutdown_event=stop,
            )

            # Only a pause the collector actually stopped for counts: a pause
            # requested after the last slug finished is a no-op (the scan
            # completes normally). A user pause wins over a concurrent
            # shutdown so the intent to stay paused survives the restart.
            if stop.observed and self._pause_event.is_set():
                # Reported as paused only once the pause is durable (see
                # persist_pause); otherwise it was recorded as a failed run.
                self._last_scan_completed = False
                self._last_scan_paused = persist_pause(self.staging, scan_id, "HelloWork")
                if not self._last_scan_paused:
                    return
                logger.info(
                    "HelloWork scan %s paused — checkpoint: %d metier slug(s) done.",
                    scan_id, len(completed_slugs),
                )
                return

            limited = getattr(collector, "rate_limited", None)
            if limited:
                # HelloWork answered 429: stop and resume after the cooldown; the slug in
                # progress was not checked off, so the resume re-fetches it.
                logger.warning("HelloWork scan %s: %s", scan_id, limited)
                self._last_scan_completed = False
                self._last_scan_rate_limited = record_rate_limit(self.staging, scan_id, "HelloWork") is not None
                return

            if self._shutdown_event.is_set():
                logger.info(
                    "HelloWork scan %s stopped cleanly for backend shutdown — "
                    "checkpoint preserved, will resume on next scan.",
                    scan_id,
                )
                record_shutdown_stop(self.staging, scan_id, "HelloWork")
                self._set_checkpoint_state_quietly(scan_id, "interrupted")
                self._last_scan_completed = False
                return

            stats_json = json.dumps({
                "collector_errors": len(result.errors),
                # The reasons, not only the count: a changed page format must be diagnosable from the UI.
                "collector_error_samples": result.errors[:_MAX_ERROR_SAMPLES],
                "collection_duration_seconds": result.duration_seconds,
                "metier_slugs_queried": result.metadata.get("metier_slugs_queried"),
            })
            self.staging.complete_scan_run(scan_id, jobs_found=jobs_found, jobs_new=jobs_new, stats_json=stats_json)
            # Full success — nothing left to resume. Clears the checkpoint so
            # the next *scheduled* scan starts from the first slug again,
            # rather than skipping everything because of stale progress.
            self.staging.clear_scan_checkpoint()
            self._last_scan_completed = True
            logger.info(
                "HelloWork: scan %s completed -- %d job(s) found (%d new), %d collector error(s).",
                scan_id, jobs_found, jobs_new, len(result.errors),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("HelloWork: scan %s failed: %s", scan_id, exc)
            try:
                self.staging.fail_scan_run(scan_id, str(exc))
            except Exception:  # noqa: BLE001
                pass
            self._set_checkpoint_state_quietly(scan_id, "failed")
            self._last_scan_completed = False
            # Checkpoint intentionally left in place (not cleared) so the
            # next attempt — manual retry or the next scheduler tick — can
            # resume from whatever slugs were already completed.
