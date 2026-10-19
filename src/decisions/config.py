"""Decision-journal configuration (``config/decisions.yaml``; all keys optional)."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


class DecisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    # For a job rejected by the hard filter, also evaluate the checks AFTER the deciding one so every
    # failing check is visible (extra work per rejected job: off by default).
    all_filter_failures: bool = False
    # For accepted jobs, record one evidence item per hard-filter check (satisfied / unknown).
    record_passing_filter_evidence: bool = True
    # Also journal jobs dropped by validation / deduplication (metadata only, never text).
    record_upstream_rejections: bool = True
    # Keep the job text of REJECTED jobs so false negatives can be audited later.
    store_rejected_text: bool = True
    store_accepted_text: bool = False
    compress_text: bool = True
    # Text is purged after this many days (metadata + decision stay); 0 = keep forever.
    text_retention_days: int = Field(default=60, ge=0)
    # Whole records are deleted after this many days; 0 = keep forever.
    record_retention_days: int = Field(default=365, ge=0)
    # Rejection audit sample (src/decisions/audit.py): at most this many rule-backed rejections are
    # open in the review queue at once, so false rejections can surface; 0 = off. Changes no outcome.
    rejection_audit_max_open: int = Field(default=3, ge=0, le=50)


def load_decision_config(config_dir: str | Path) -> DecisionConfig:
    path = Path(config_dir) / "decisions.yaml"
    if not path.is_file():
        return DecisionConfig()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return DecisionConfig(**data)
    except (OSError, yaml.YAMLError, TypeError, ValidationError) as exc:
        # A bad journal config must never block the pipeline: fall back to defaults, loudly.
        logger.warning("Invalid %s (%s); using defaults", path, exc)
        return DecisionConfig()
