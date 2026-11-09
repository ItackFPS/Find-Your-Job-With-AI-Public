"""Parameters of the personal model (ADR 0018), read from ``config/personalization.yaml``.

Every value is an **initial, unapproved** value (v5 §9): none is approved until measured. A missing
file or section gives the defaults below; unknown keys are ignored.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# AD-3.1 signal table (initial weights).
DEFAULT_WEIGHTS: dict[str, float] = {
    "opened": 0.25,
    "saved": 1.0,
    "to_apply": 1.0,
    "apply_link_opened": 1.2,
    "applied": 1.5,
    "interview": 2.0,
    "offer": 2.0,
    "accepted_offer": 2.0,
    "interested": 1.2,
    "not_interested": -1.2,
    "hidden_with_reason": -1.2,
    "ignored": 0.0,           # logged only at launch (owner decision)
}


@dataclass(frozen=True)
class PersonalModelConfig:
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    clamp: float = 2.0                    # |y| per offer
    half_life_days: float = 90.0          # recency on effective time
    min_recency: float = 0.05
    profile_change_discount: float = 0.5  # signals older than the last profile change
    prior_strength: float = 4.0           # ridge λ around the profile embedding
    shrinkage: float = 3.0                # pseudo-count for per-value structured adjustments
    cap_max: float = 0.15                 # ceiling of the evidence-scaled cap on the final score
    cap_half_n: float = 20.0              # offers with a signal at which cap(n) reaches cap_max / 2
    live_ranking: float = 1.0             # 0 keeps the production order (ADR 0030)
    live_min_strong: float = 3.0          # strong signals (|y| >= 1) before the personal order applies

    def cap(self, n: int) -> float:
        """Evidence-scaled cap: 0 at n = 0, grows smoothly towards ``cap_max``; no threshold."""
        return 0.0 if n <= 0 else self.cap_max * n / (n + self.cap_half_n)

    def digest(self) -> str:
        """Short hash of every parameter: part of ``personal_model_version``."""
        blob = json.dumps(dataclasses.asdict(self), sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]


def load_personal_model_config(path: str | Path = "config/personalization.yaml") -> PersonalModelConfig:
    try:
        data: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return PersonalModelConfig()
    section = data.get("personal_model") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return PersonalModelConfig()
    base = PersonalModelConfig()
    weights = dict(base.weights)
    if isinstance(section.get("weights"), dict):
        weights.update({str(k): float(v) for k, v in section["weights"].items()})
    known = {f.name for f in dataclasses.fields(PersonalModelConfig)} - {"weights"}
    values = {k: float(v) for k, v in section.items() if k in known}
    return dataclasses.replace(base, weights=weights, **values)
