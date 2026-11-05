"""Contracts and vocabularies of the feedback journal."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from src.feedback import FEEDBACK_SCHEMA_VERSION, REASON_VOCAB_VERSION

SignalClass = Literal["explicit", "behavior", "outcome", "list"]
Source = Literal["ui", "api", "cli", "import", "system"]

# explicit: what the user SAYS about the matching
# ``unsure``: the user looked and cannot tell (answers a review item; a ``borderline`` label, no stance)
EXPLICIT_TYPES = ("interested", "not_interested", "ranking_feedback", "unsure")
# behavior: what the user DOES with the job
BEHAVIOR_TYPES = ("viewed", "apply_link_opened")
# outcome: what really happened after acting (application status transitions)
OUTCOME_TYPES = ("application_status_changed",)
# list: list management (ADR 0016, ADR 0017). Hide is not a judgement: it removes the offer from
# every list and produces no generalising negative; its optional reason is routed to that feature only.
LIST_TYPES = ("saved", "unsaved", "hidden", "unhidden")

# State families (ADR 0016 per-offer ordering): within a family, the current state of an offer is
# its latest non-superseded event in insert order.
STATE_FAMILIES: dict[str, str] = {
    "saved": "save", "unsaved": "save",
    "hidden": "hide", "unhidden": "hide",
    "interested": "stance", "not_interested": "stance", "unsure": "stance",
    "application_status_changed": "status",
}

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
    "unsure": frozenset({"other"}),
    # the optional "Pourquoi ?" chip of Hide (ADR 0017)
    "hidden": frozenset({"too_far", "too_senior", "off_domain", "salary", "contract_type", "other"}),
    "saved": frozenset(), "unsaved": frozenset(), "unhidden": frozenset(),
}

# Context keys a client may attach to an event (ADR 0016 "Event context"), stored in metadata.
CONTEXT_KEYS = ("surface", "position", "sort_key", "randomized", "session_id")

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
    # Delivery (ADR 0016). All set at insert, never updated (the journal stays append-only).
    client_event_id: str | None = None
    generation: int | None = None
    client_instance_id: str | None = None
    client_seq: int | None = None
    client_created_at: str | None = None
    client_time_trusted: bool | None = None
    superseded: bool = False
