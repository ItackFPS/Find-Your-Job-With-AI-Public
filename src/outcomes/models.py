"""I18 — outcome event model."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class OutcomeEventType(str, Enum):
    """Only event types the product can genuinely emit today.

    Deliberately excludes anything the UI has no control for yet (e.g. a
    "dismiss"/"save" action — see the I18 inspection: no such button exists).
    Do not add a new value here without first adding the UI action that
    triggers it — an event type with no real trigger would be a fabricated
    label, which I18 explicitly forbids.
    """

    VIEWED = "viewed"
    APPLY_LINK_OPENED = "apply_link_opened"
    APPLICATION_STATUS_CHANGED = "application_status_changed"


@dataclass
class OutcomeEvent:
    job_id: str
    event_type: OutcomeEventType
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
