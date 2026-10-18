"""Source model using Pydantic."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Source(BaseModel):
    """Configuration for a job source (company, domain, ATS type, etc.)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    type: str = ""  # e.g. "greenhouse", "ashby", "rss", "ats"
    company: str | None = None
    domain: str | None = None
    careers_url: str | None = None
    ats: str | None = None
    board_identifier: str | None = None
    config: dict[str, Any] = {}
    active: bool = True
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        d = self.model_dump()
        for k in ("last_success_at", "last_error_at"):
            if d[k] is not None:
                d[k] = d[k].isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Source:
        for k in ("last_success_at", "last_error_at"):
            if data.get(k) is not None and isinstance(data[k], str):
                data[k] = datetime.fromisoformat(data[k])

        # Handle config field - it may be stored as a JSON string
        if "config" in data:
            val = data["config"]
            if isinstance(val, str):
                import json
                try:
                    data["config"] = json.loads(val)
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep as-is if not valid JSON

        return cls(**data)
