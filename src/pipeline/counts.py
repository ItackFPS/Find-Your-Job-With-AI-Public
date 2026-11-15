"""What each collection count means, per source and per run (Phase B B8-A).

One name per count, so a yield is never computed on an ambiguous number. Each term is a key of
a source's row in a run's ``by_source`` statistics:

| Term | Key | Definition |
|---|---|---|
| Requests | ``requests`` | requests the collector sent (robots-refused URLs and retries not counted); absent when the collector does not count them |
| Collected | ``collected`` | offers the source returned, after normalisation |
| Normalized | ``validated`` | collected offers that pass validation |
| Deduplicated | ``unique`` | normalized offers left after deduplication (the kept copy's source) |
| Eligible | ``passed_filter`` | deduplicated offers that pass the hard filter |
| Accepted | ``accepted`` | eligible offers that pass the requirements gate (and the AI eligibility gate when it runs), ranked in the run |
| Retained | ``accepted`` | accepted offers stored with a ``jobs`` row: every scored offer is stored before ranking, so retained = accepted when the run completes |
| Newly discovered | ``new`` | accepted offers stored for the first time by this run |
| Useful | ``useful`` | accepted offers at or above the visibility tier (``relevant``), by the run's final score |
| Newly discovered useful | ``new_useful`` | useful offers that are also newly discovered |

Yield (``source_yield``): newly discovered useful offers per request and per second of the
source's collection time. A yield whose denominator is unknown or zero is ``None``, never 0.
"""

from __future__ import annotations

from typing import Any

USEFUL_MIN_TIER = "relevant"

# (term, key in a by_source row), in funnel order.
COUNT_TERMS: tuple[tuple[str, str], ...] = (
    ("requests", "requests"),
    ("collected", "collected"),
    ("normalized", "validated"),
    ("deduplicated", "unique"),
    ("eligible", "passed_filter"),
    ("accepted", "accepted"),
    ("retained", "accepted"),
    ("newly_discovered", "new"),
    ("useful", "useful"),
    ("newly_discovered_useful", "new_useful"),
)


def is_useful(final_score: float) -> bool:
    """Whether an offer with this final score (0-1) is at or above the visibility tier."""
    from src.pipeline.scoring import score_to_tier, tier_meets_minimum

    return tier_meets_minimum(score_to_tier(round(float(final_score or 0.0) * 100)), USEFUL_MIN_TIER)


def source_counts(row: dict[str, Any] | None) -> dict[str, int | None]:
    """The terms of one source's row; ``None`` for a count the run did not record."""
    row = row or {}
    out: dict[str, int | None] = {}
    for term, key in COUNT_TERMS:
        value = row.get(key)
        out[term] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return out


def _ratio(numerator: int | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator or denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def source_yield(row: dict[str, Any] | None) -> dict[str, float | None]:
    """Newly discovered useful offers per request and per second for one source's row."""
    counts = source_counts(row)
    seconds = (row or {}).get("seconds")
    seconds = float(seconds) if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) else None
    return {
        "new_useful_per_request": _ratio(counts["newly_discovered_useful"], counts["requests"]),
        "new_useful_per_second": _ratio(counts["newly_discovered_useful"], seconds),
    }
