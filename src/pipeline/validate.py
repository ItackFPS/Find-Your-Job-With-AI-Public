"""Validation layer.

Ensures every job meets minimum quality thresholds before it enters the pipeline.
"""

from __future__ import annotations

import logging
from typing import Callable

from src.models.job import Job

logger = logging.getLogger(__name__)


class ValidationErrors:
    """Container for validation errors on a single job."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def add_error(self, message: str) -> None:
        self.errors.append(message)


def validate_job(job: Job) -> ValidationErrors:
    """Validate a single job against minimum quality thresholds.

    Returns a ValidationErrors object with any issues found.
    """
    errors = ValidationErrors()

    # Required fields
    if not job.title or len(job.title.strip()) == 0:
        errors.add_error("Title is empty")
    # company=None means the source did not provide a name (e.g. France Travail
    # anonymous postings) — this is acceptable.  company="" (explicit empty string)
    # should never reach here after normalisation but is still rejected.
    if job.company is not None and len(job.company.strip()) == 0:
        errors.add_error("Company is explicitly empty")

    # At least one of description, location, or salary should be present
    has_description = bool(job.description and job.description.strip())
    has_location = bool(job.location and job.location.strip())
    has_salary = job.salary_min is not None or job.salary_max is not None
    if not (has_description or has_location or has_salary):
        errors.add_error("Job lacks description, location, or salary information")

    # Remote flag should be consistent with location
    if job.remote and job.location:
        # Some remote jobs have no location - that's fine
        pass  # Allow flexibility here

    return errors


def validate_jobs(
    jobs: list[Job],
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[Job], list[str]]:
    """Validate a batch of jobs.

    Args:
        jobs: Jobs to validate.
        progress_callback: Optional ``(processed, total)`` callback invoked
            after each job — purely observational, never affects which jobs
            are considered valid.

    Returns:
        (valid_jobs, error_messages)
    """
    valid = []
    errors = []
    total = len(jobs)
    for i, job in enumerate(jobs, start=1):
        errs = validate_job(job)
        if errs.is_valid:
            valid.append(job)
        else:
            errors.extend(errs.errors)
        if progress_callback is not None:
            progress_callback(i, total)
    return valid, errors
