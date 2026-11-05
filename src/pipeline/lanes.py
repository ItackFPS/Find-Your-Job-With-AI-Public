"""Lanes for offers with weak evidence (ADR 0019 v5 AD-2b; stage L5.3).

An offer that reached scoring with a WEAK hard-filter flag (``weak_evidence_mode: rank``) never
enters the main list. The best of them (final score >= ``score_floor``, ``domain_relevance`` >=
``domain_floor``; at most ``size`` per run) form the collapsed "outside your criteria" lane, which
never notifies. The others stay stored and journaled as ``below_threshold``: not shown, never
discarded. Offers without a weak flag keep today's main list.

The parameters are the pre-registered values (docs/lab/preregistered/2026-11-05-ad2-weak-evidence.json),
read from ``scoring.yaml`` ``scoring.weak_evidence``; they are not approved values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

MAIN = "main"
OUTSIDE = "outside_criteria"
BELOW = "below_threshold"


@dataclass(frozen=True)
class LaneParams:
    size: int = 10
    score_floor: float = 0.60
    domain_floor: float = 0.7


def load_lane_params(scoring_yaml: str | Path) -> LaneParams:
    """``scoring.weak_evidence.outside_lane_*``; defaults on a missing file or bad values."""
    try:
        data = yaml.safe_load(Path(scoring_yaml).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return LaneParams()
    cfg = ((data.get("scoring") or {}).get("weak_evidence") or {})
    d = LaneParams()

    def num(key: str, default: float, lo: float, hi: float) -> float:
        v = cfg.get(key, default)
        return float(v) if isinstance(v, int | float) and not isinstance(v, bool) and lo <= v <= hi else default

    return LaneParams(size=int(num("outside_lane_size", d.size, 0, 50)),
                      score_floor=num("outside_score_floor", d.score_floor, 0.0, 1.0),
                      domain_floor=num("outside_domain_floor", d.domain_floor, 0.0, 1.0))


def has_weak_flags(breakdown: Mapping[str, Any] | None) -> bool:
    return bool((breakdown or {}).get("weak_flags"))


def assign_lanes(rows: Iterable[tuple[str, float, Mapping[str, Any] | None]],
                 params: LaneParams | None = None) -> dict[str, str]:
    """``(job_id, final_score, score_breakdown)`` of one run → {job_id: lane}.

    Deterministic: the outside lane takes the eligible weak-flagged offers by score, then job id.
    """
    p = params or LaneParams()
    lanes: dict[str, str] = {}
    eligible: list[tuple[float, str]] = []
    for job_id, score, breakdown in rows:
        if not has_weak_flags(breakdown):
            lanes[job_id] = MAIN
            continue
        lanes[job_id] = BELOW
        domain = (breakdown or {}).get("domain_relevance")
        if score >= p.score_floor and isinstance(domain, int | float) and domain >= p.domain_floor:
            eligible.append((score, job_id))
    for _score, job_id in sorted(eligible, key=lambda t: (-t[0], t[1]))[:p.size]:
        lanes[job_id] = OUTSIDE
    return lanes
