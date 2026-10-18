"""Feedback configuration (``config/feedback.yaml``; all keys optional)."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.feedback.models import CANONICAL_STATUSES, LEGACY_STATUSES

logger = logging.getLogger(__name__)


class FeedbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Mirror explicit interested/not_interested events into the legacy ``job_feedback`` table, which
    # the personalization stage reads. OFF by default: turning it on lets the user's first explicit
    # feedback start changing rankings, which is a product decision.
    personalization_bridge: bool = False
    # Owner-stated resolution of ambiguous legacy statuses (accepted / rejected / denied), e.g.
    # ``{"rejected": "withdrawn", "denied": "employer_rejected"}``. Unmapped values keep
    # ``canonical = None`` — their legacy value is always preserved in the event.
    legacy_status_mapping: dict[str, str] = Field(default_factory=dict)

    @field_validator("legacy_status_mapping")
    @classmethod
    def _valid_mapping(cls, v: dict[str, str]) -> dict[str, str]:
        for legacy, canonical in v.items():
            if legacy not in LEGACY_STATUSES:
                raise ValueError(f"unknown legacy status {legacy!r}")
            if canonical not in CANONICAL_STATUSES:
                raise ValueError(f"unknown canonical status {canonical!r}")
        return v


def load_feedback_config(config_dir: str | Path) -> FeedbackConfig:
    path = Path(config_dir) / "feedback.yaml"
    if not path.is_file():
        return FeedbackConfig()
    try:
        return FeedbackConfig(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    except (OSError, yaml.YAMLError, TypeError, ValidationError) as exc:
        logger.warning("Invalid %s (%s); using defaults", path, exc)
        return FeedbackConfig()
