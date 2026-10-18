"""Contracts and vocabularies of the feedback journal."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.feedback import FEEDBACK_SCHEMA_VERSION, REASON_VOCAB_VERSION

SignalClass = Literal["explicit", "behavior", "outcome"]
Source = Literal["ui", "api", "cli", "import", "system"]

# explicit: what the user SAYS about the matching
EXPLICIT_TYPES = ("interested", "not_interested", "ranking_feedback")
# behavior: what the user DOES with the job
BEHAVIOR_TYPES = ("viewed", "apply_link_opened")
# outcome: what really happened after acting (application status transitions)
OUTCOME_TYPES = ("application_status_changed",)

REASONS = (
    "too_far", "too_senior", "insufficient_skills", "off_domain", "salary", "contract_type",
    "interesting_despite_low_score", "mis_ranked", "other",
)
# Reasons allowed per explicit type (reason vocabulary v1)
ALLOWED_REASONS: dict[str, frozenset[str]] = {
    "interested": frozenset({"interesting_despite_low_score", "other"}),
    "not_interested": frozenset({
        "too_far", "too_senior", "insufficient_skills", "off_domain", "salary", "contract_type",
        "mis_ranked", "other",
    }),
    "ranking_feedback": frozenset({"mis_ranked", "interesting_despite_low_score", "other"}),
}

# Application-status vocabulary. Legacy values stay stored verbatim in jobs.application_status;
# the canonical value is derived by an explicit, versioned mapping (never a silent rewrite).
CANONICAL_STATUSES = (
    "to_apply", "applied", "interview", "offer", "accepted_offer", "employer_rejected", "withdrawn",
)
LEGACY_STATUSES = ("to_apply", "applied", "waiting", "interview", "accepted", "rejected", "denied", "withdrawn")
# Unambiguous legacy values. ``accepted``, ``rejected`` and ``denied`` are deliberately NOT mapped
# by default: nothing in the code, docs or tests defines them (``rejected`` vs ``denied`` have
# distinct buttons with identical styling). They map only via config/feedback.yaml once the
# owner has stated what each meant in their own data.
DEFAULT_LEGACY_MAPPING: dict[str, str] = {
    "to_apply": "to_apply", "applied": "applied", "waiting": "applied", "interview": "interview",
    "withdrawn": "withdrawn",
}


class FeedbackEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = FEEDBACK_SCHEMA_VERSION
    reason_vocab_version: int = REASON_VOCAB_VERSION
    event_id: str
    job_id: str
    # The decision (and strategy) the user was reacting to; None when the job predates the journal.
    decision_id: str | None = None
    strategy_version: str | None = None
    signal_class: SignalClass
    type: str
    reasons: list[str] = Field(default_factory=list)
    note: str | None = None
    source: Source
    occurred_at: str
    metadata: dict[str, Any] = Field(default_factory=dict)
