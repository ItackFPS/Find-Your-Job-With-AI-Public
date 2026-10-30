"""Data models for the generic scheduler."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScheduleDefinition:
    """What should be scheduled for a given source."""

    id: str                     # e.g. "linkedin_daily"
    source_id: str              # matches handler registry key, e.g. "linkedin"
    label: str                  # human-readable
    enabled: bool
    startup_policy: str         # "run_once_per_day" | "when_opted_in" | "disabled"
    retry_max: int              # 0 = no retry on failure
    retry_delay_seconds: int    # seconds between retry attempts
    created_at: str             # ISO UTC
    updated_at: str             # ISO UTC


@dataclass
class ScheduleExecution:
    """What actually happened for one scheduled run."""

    id: str                             # UUID
    definition_id: str                  # FK → ScheduleDefinition.id
    trigger: str                        # "startup" | "scheduled" | "manual"
    status: str                         # "running" | "completed" | "failed"
    attempt: int                        # 1-based retry counter
    started_at: str                     # ISO UTC
    finished_at: str | None = None      # ISO UTC
    duration_seconds: float | None = None
    error: str | None = None
    run_ref: str | None = None          # scan_id or pipeline run_id
