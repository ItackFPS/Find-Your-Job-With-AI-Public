"""Phase 2 pipeline stage: Local AI Job Understanding.

This module implements the Phase 2 pipeline stage that runs after the
deterministic Phase 1 pipeline (COLLECT → NORMALIZE → VALIDATE →
DEDUPLICATE → FILTER → SCORE → STORE).

The AI understanding stage is additive and non-blocking:
- If AI is disabled, it is skipped entirely.
- If the local LLM is unavailable, failures are logged and jobs continue.
- The original job data is never modified.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from src.ai.interfaces import LLMProvider
from src.ai.prompt import PERSONALIZED_PROMPT_VERSION
from src.ai.models import (
    BatchJobUnderstandingResult,
    JobUnderstanding,
    JobUnderstandingResult,
)
from src.ai.prompt import PROMPT_VERSION
from src.ai.storage import AIAnalysisStorage
from src.models.job import Job
from src.storage.sqlite import Storage

logger = logging.getLogger(__name__)


class Phase2Pipeline:
    """Phase 2 pipeline stage: Local AI Job Understanding.

    This class orchestrates the AI understanding stage and integrates with
    the existing Phase 1 storage layer. It is designed to be non-blocking
    — failures in this stage do not affect the deterministic pipeline.
    """

    def __init__(
        self,
        storage: Storage,
        ai_storage: AIAnalysisStorage,
        llm_provider: LLMProvider | None = None,
        prompt_version: str = PROMPT_VERSION,
    ):
        self.storage = storage
        self.ai_storage = ai_storage
        self.llm_provider = llm_provider
        self.prompt_version = prompt_version

    def run(
        self,
        jobs: list[Job],
        profile: dict[str, Any] | None = None,
        profile_version: str = "",
        on_progress: "Callable[[int, int, int], None] | None" = None,
    ) -> BatchJobUnderstandingResult:
        """Run Phase 2 AI understanding on a list of jobs.

        Args:
            jobs: Jobs that have already passed the Phase 1 pipeline
                  (COLLECT → NORMALIZE → VALIDATE → DEDUPLICATE → FILTER → SCORE).
            profile: Optional ``JobSearchProfile.model_dump()`` dict.  When provided,
                     the personalized matching prompt is used so the model evaluates
                     job fit against the user's preferences.  When absent, the standard
                     job-understanding prompt is used.
            profile_version: Fingerprint of the profile config in effect (e.g. the
                     orchestrator's ``stats.profile_hash``). Part of the cache
                     validity check alongside each job's own ``content_hash`` —
                     see ``AIAnalysisStorage`` — so a profile change invalidates
                     previously cached analyses instead of silently reusing them.
            on_progress: Optional callback forwarded to the LLM provider's
                     ``analyze_batch`` as ``(completed, total, failed_so_far)``
                     after each job finishes — lets the orchestrator surface
                     live Phase 2 progress. Only invoked for jobs that
                     actually need fresh analysis (cached jobs are resolved
                     before this point and never call the provider).

        Returns:
            A BatchJobUnderstandingResult containing the analysis for each job.
        """
        if not self.llm_provider:
            logger.info("Phase 2 AI is disabled — skipping analysis.")
            return self._build_skipped_results(jobs)

        t0 = time.perf_counter()

        # Select prompt version based on whether a profile is present.
        effective_version = (
            PERSONALIZED_PROMPT_VERSION if profile else self.prompt_version
        )

        # Check for cached results first. Cache validity requires the job's
        # current content_hash and the current profile_version to match what
        # was stored — see AIAnalysisStorage.get_analyses_for_jobs.
        job_ids = [job.id for job in jobs]
        content_hashes = {job.id: (job.content_hash or "") for job in jobs}
        cached = self.ai_storage.get_analyses_for_jobs(
            job_ids, effective_version, content_hashes, profile_version
        )

        # Determine which jobs need fresh analysis
        needs_analysis: list[Job] = []
        for job in jobs:
            if job.id in cached:
                logger.info(
                    "Phase 2: Job %s already has cached analysis — reusing.", job.id
                )
            else:
                needs_analysis.append(job)

        # Run AI analysis on jobs that need it, then merge with anything
        # that was already cached so cached jobs aren't silently dropped
        # from the batch result.
        fresh_results: list[JobUnderstandingResult] = []
        fresh_batch: BatchJobUnderstandingResult | None = None
        if needs_analysis:
            fresh_batch = self.llm_provider.analyze_batch(
                needs_analysis,
                effective_version,
                profile=profile,
                on_progress=on_progress,
            )
            fresh_results = fresh_batch.results
            for r in fresh_results:
                self.ai_storage.upsert_analysis(
                    r,
                    content_hash=content_hashes.get(r.job_id, ""),
                    profile_version=profile_version,
                )

        elapsed = time.perf_counter() - t0
        all_results = list(cached.values()) + fresh_results
        return BatchJobUnderstandingResult(
            total_jobs=len(jobs),
            successful=sum(1 for r in fresh_results if r.status == "success"),
            failed=sum(1 for r in fresh_results if r.status == "failed"),
            skipped=sum(1 for r in fresh_results if r.status == "skipped"),
            cached=len(cached),
            time_seconds=elapsed,
            retried=fresh_batch.retried if fresh_batch is not None else 0,
            failure_reasons=fresh_batch.failure_reasons if fresh_batch is not None else {},
            results=all_results,
        )

    def _build_skipped_results(self, jobs: list[Job]) -> BatchJobUnderstandingResult:
        """Build a result set where all jobs are marked as skipped."""
        results = [
            JobUnderstandingResult(
                job_id=job.id,
                analysis=JobUnderstanding(),
                prompt_version=self.prompt_version,
                model_identifier=None,
                status="skipped",
            )
            for job in jobs
        ]
        return BatchJobUnderstandingResult(
            total_jobs=len(jobs),
            successful=0,
            failed=0,
            skipped=len(jobs),
            results=results,
        )

    def get_summary(self) -> dict[str, Any]:
        """Return a summary of Phase 2 analysis results."""
        total = self.ai_storage.count_analyses()
        failed = len(self.ai_storage.get_failed_analyses())

        return {
            "total_analyses": total,
            "failed_analyses": failed,
            "successful_analyses": total - failed,
        }
