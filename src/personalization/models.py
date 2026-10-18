"""Pydantic models for Phase 4: Personalized Ranking and Feedback."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class FeedbackType(str, Enum):
    """Supported user feedback signals for a job."""

    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    APPLIED = "applied"
    REJECTED = "rejected"
    IGNORED = "ignored"


class Feedback(BaseModel):
    """A single piece of user feedback about a job.

    A denormalized snapshot of the job's key attributes (company, title,
    skills, remote) is stored alongside the feedback itself so the
    personalization engine can keep learning preferences even if the
    underlying job row is later updated or removed from the ``jobs`` table.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    job_id: str
    feedback_type: FeedbackType
    note: str | None = None
    company: str | None = None
    title: str | None = None
    skills: list[str] = Field(default_factory=list)
    remote: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SignalMatch(BaseModel):
    """One matched preference signal that contributed to a job's adjustment."""

    signal_type: str  # "company" | "skill" | "title_keyword" | "remote"
    value: str
    feedback_count: int
    average_weight: float
    contribution: float


class PersonalizationExplanation(BaseModel):
    """Explains how a job's personalization adjustment was computed.

    This is the "explainability" artifact: it is safe to print or serialize
    directly to show a user why a job's score moved.
    """

    job_id: str
    raw_adjustment: float = 0.0
    clamped_adjustment: float = 0.0
    matches: list[SignalMatch] = Field(default_factory=list)
    feedback_considered: int = 0
    cold_start: bool = False
    excluded_reason: str | None = None

    def summary(self) -> str:
        """Return a short, human-readable explanation string."""
        if self.excluded_reason:
            return f"excluded ({self.excluded_reason})"
        if self.cold_start or not self.matches:
            return "no personalization applied (not enough feedback yet)"
        parts = [f"{m.signal_type}={m.value} ({m.contribution:+.3f})" for m in self.matches]
        return f"{self.clamped_adjustment:+.3f} from " + ", ".join(parts)
