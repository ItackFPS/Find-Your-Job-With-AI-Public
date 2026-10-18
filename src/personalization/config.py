"""Configuration loading for Phase 4: Personalized Ranking and Feedback."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: Default configuration, used whenever config/personalization.yaml is
#: missing or a section is not overridden. Kept in sync with the shipped
#: config/personalization.yaml comments.
DEFAULT_PERSONALIZATION_CONFIG: dict[str, Any] = {
    "feedback_weights": {
        "interested": 1.0,
        "applied": 1.5,
        "not_interested": -1.0,
        "rejected": -0.3,
        "ignored": -0.2,
    },
    "feature_weights": {
        "company": 1.0,
        "skills": 1.0,
        "title_keywords": 0.6,
        "remote": 0.4,
    },
    "personalization": {
        # Maximum absolute score adjustment (+/-) personalization may apply,
        # so it can never dominate the deterministic + semantic base score.
        "max_abs_adjustment": 0.3,
        # Minimum number of stored feedback events before personalization
        # kicks in at all (avoids noisy adjustments from a single click).
        "min_feedback_count": 1,
        # If true, jobs the user already gave terminal feedback on
        # (applied / rejected / not_interested) are dropped from the final
        # ranked output instead of being shown again.
        "exclude_actioned_jobs": True,
        # Phase NEXT / I9: recency decay — see config/personalization.yaml's
        # comment for why 90 days / 0.05 floor were chosen as a starting
        # point. Kept in sync with that file's defaults.
        "recency_half_life_days": 90,
        "recency_min_factor": 0.05,
    },
}


def load_personalization_config(config_path: str) -> dict[str, Any]:
    """Load Phase 4 personalization configuration from YAML.

    Falls back to ``DEFAULT_PERSONALIZATION_CONFIG`` (merged per-section)
    when the file is missing or a section is not present, mirroring the
    fallback behaviour of ``load_semantic_config`` in Phase 3.
    """
    path = Path(config_path)
    if not path.exists():
        return {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULT_PERSONALIZATION_CONFIG.items()}

    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if data is None:
        data = {}

    if not isinstance(data, dict):
        raise TypeError(f"Expected a mapping in {config_path}")

    merged: dict[str, Any] = {}
    for key, default_value in DEFAULT_PERSONALIZATION_CONFIG.items():
        loaded_value = data.get(key)
        if isinstance(default_value, dict) and isinstance(loaded_value, dict):
            merged[key] = {**default_value, **loaded_value}
        elif loaded_value is not None:
            merged[key] = loaded_value
        else:
            merged[key] = dict(default_value) if isinstance(default_value, dict) else default_value
    return merged
