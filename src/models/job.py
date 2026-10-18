"""Job model using Pydantic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Job(BaseModel):
    """Normalized job listing from any source.

    All fields are required by the pipeline; nullable fields have sensible defaults.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: str = ""
    source_job_id: str | None = None
    canonical_url: str | None = None
    apply_url: str | None = None
    company: str | None = None
    title: str | None = None
    description: str | None = None
    location: str | None = None
    countries: list[str] = []
    # French INSEE department code (e.g. "75", "69", "2A", "971"), when known.
    # Populated only by sources that expose structured location codes
    # (currently France Travail). None means unknown, not "no department".
    department: str | None = None
    remote: bool = False
    # One of "remote" | "hybrid" | "on_site", when the source gives a
    # confident, structured signal. None means unknown — callers must not
    # infer on-site from an absent value (the boolean `remote` field above
    # remains the backward-compatible signal for that).
    work_mode: str | None = None
    employment_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = "EUR"
    published_at: datetime | None = None
    updated_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    skills: list[str] = []
    # All sources where this job was observed; populated by cross-source dedup.
    # The primary `source` field is always included here after deduplication.
    sources: list[str] = []
    raw_payload: dict[str, Any] = {}
    content_hash: str = ""
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    status: str = "active"
    application_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary (converts datetimes)."""
        d = self.model_dump()
        for k in ("published_at", "updated_at", "discovered_at", "first_seen_at", "last_seen_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        """Reconstruct a Job from a dictionary (e.g. from SQLite)."""
        for k in ("published_at", "updated_at", "discovered_at", "first_seen_at", "last_seen_at"):
            if data.get(k) is not None and isinstance(data[k], str):
                data[k] = datetime.fromisoformat(data[k])

        # Deserialize JSON fields stored from SQLite
        for k in ("countries", "skills", "raw_payload"):
            if data.get(k) is not None and isinstance(data[k], str):
                import json
                try:
                    data[k] = json.loads(data[k])
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep as-is if not valid JSON

        # Coerce NULL content_hash from pre-migration rows or direct SQL inserts.
        if data.get("content_hash") is None:
            data["content_hash"] = ""

        return cls(**data)

    def __str__(self) -> str:
        """Return a concise, human-readable string representation of the job."""
        title = self.title or "(no title)"
        company = self.company or "(unknown company)"
        location = self.location or "(location not specified)"
        return f"{title} at {company} — {location}"
