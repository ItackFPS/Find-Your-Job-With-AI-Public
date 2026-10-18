"""Source-yield health detection — Phase NOW / I16.

Detects a collector source silently producing abnormally few or zero jobs
(e.g. a revoked API key, a site layout change breaking a scraper) using data
the pipeline already computes every run: ``PipelineStats.by_source`` (see
src/pipeline/orchestrator.py), persisted verbatim in
``pipeline_runs.stats_json`` on every completed run. No new table, no new
background worker — this module is a pure function over that existing
history, called once per pipeline completion from
NotificationEngine._send_pipeline_completed (see engine.py), which already
owns the exact "#errors channel, dedup via notification_history" delivery
path this reuses for the "pipeline.warnings" event.

Design (thresholds are module-level constants with sensible defaults below,
not yet exposed via notifications.yaml — kept out of scope for this pass;
see the NEXT-phase config-schema-validation item for adding config-driven
tuning here):
  - A source is only ever considered if it appears in the CURRENT run's
    by_source dict — i.e. it actually ran this time. An intentionally
    disabled source (active: false in sources.yaml) never appears there at
    all, so it is never flagged: nothing to special-case.
  - `from_staging` sources (LinkedIn/HelloWork — see orchestrator.py) are
    always skipped. Their "collected" count reflects whatever a separate,
    independently-scheduled scan happened to have staged by the time this
    pipeline run started, which can legitimately be zero on many runs — an
    expected, scheduled pattern, not a health signal for THIS run.
  - A baseline is only computed from runs that themselves have status
    "completed" and a non-from_staging, well-formed integer "collected"
    value for this source — malformed/missing history entries are simply
    excluded from the average rather than raising or corrupting it.
  - Too little history, or a baseline that's inherently tiny, never alerts
    (see min_baseline_runs / min_baseline_yield below) — a source that
    normally yields 2-3 jobs swinging to 0-1 is normal noise, not a health
    signal.
  - Noise prevention: each anomaly is tagged `is_new`, computed by checking
    whether the SAME anomaly condition was already true one run earlier
    (using the same historical data, no persisted alert state needed) — a
    source that's been broken for a week is flagged once, not every run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Require at least this many valid historical data points before judging any
# anomaly — one or two prior runs is not a baseline.
_DEFAULT_MIN_BASELINE_RUNS = 3
# A source whose own healthy baseline never exceeds this is inherently
# low-volume; small integer fluctuations there are not a health signal.
_DEFAULT_MIN_BASELINE_YIELD = 5.0
# current < baseline * this ratio → "significant drop" (e.g. 0.2 = an 80%+ drop).
_DEFAULT_DROP_RATIO_THRESHOLD = 0.2
# How many prior completed runs to look back across when building the baseline.
_DEFAULT_LOOKBACK_RUNS = 5

Severity = Literal["zero", "drop"]


@dataclass(frozen=True)
class SourceYieldAnomaly:
    source: str
    current_yield: int
    baseline_yield: float
    baseline_run_count: int
    severity: Severity
    is_new: bool


def _extract_yield(run: dict[str, Any], source: str) -> int | None:
    """Return this run's "collected" count for `source`, or None if the run
    doesn't have usable data for it (wrong status, missing entry, malformed
    stats, staging-sourced) — callers filter out the None results rather
    than treating them as a zero data point."""
    if not isinstance(run, dict) or run.get("status") != "completed":
        return None
    stats = run.get("stats")
    if not isinstance(stats, dict):
        return None
    by_source = stats.get("by_source")
    if not isinstance(by_source, dict):
        return None
    entry = by_source.get(source)
    if not isinstance(entry, dict) or entry.get("from_staging"):
        return None
    collected = entry.get("collected")
    if isinstance(collected, bool) or not isinstance(collected, int) or collected < 0:
        return None
    return collected


def _check_source(
    source: str,
    current_yield: int,
    history_yields: list[int],
    *,
    min_baseline_runs: int,
    min_baseline_yield: float,
    drop_ratio_threshold: float,
) -> tuple[Severity, float, int] | None:
    """Pure anomaly check for one source given already-extracted history
    values (most-recent-first). Returns (severity, baseline, sample_size) or
    None if no anomaly / insufficient data."""
    sample = history_yields[:]
    if len(sample) < min_baseline_runs:
        return None
    baseline = sum(sample) / len(sample)
    if baseline < min_baseline_yield:
        return None
    if current_yield == 0:
        return "zero", baseline, len(sample)
    if current_yield < baseline * drop_ratio_threshold:
        return "drop", baseline, len(sample)
    return None


def detect_source_yield_anomalies(
    current_by_source: dict[str, dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    min_baseline_runs: int = _DEFAULT_MIN_BASELINE_RUNS,
    min_baseline_yield: float = _DEFAULT_MIN_BASELINE_YIELD,
    drop_ratio_threshold: float = _DEFAULT_DROP_RATIO_THRESHOLD,
    lookback_runs: int = _DEFAULT_LOOKBACK_RUNS,
) -> list[SourceYieldAnomaly]:
    """Detect per-source yield anomalies for the current run.

    Args:
        current_by_source: this run's ``stats.by_source`` dict (Job/error
            counts keyed by source name).
        history: prior runs, most-recent-first, in the shape
            ``RunStorage.list_runs()`` returns (each a dict with at least
            "status" and "stats"). Should NOT include the current run.
        min_baseline_runs / min_baseline_yield / drop_ratio_threshold /
            lookback_runs: see module docstring and
            config/notifications.yaml's ``source_health`` section — exposed
            as keyword args so the caller can thread config values through
            without this function reading config itself.

    Returns:
        One ``SourceYieldAnomaly`` per source with a detected anomaly this
        run (empty list if none). Callers should only actually send an
        alert for entries where ``is_new`` is True — see module docstring.
    """
    anomalies: list[SourceYieldAnomaly] = []
    recent_history = history[:lookback_runs]
    # One extra run of lookback so `_is_new` can independently recompute
    # "was the source ALSO anomalous one run ago" using ITS OWN preceding
    # window, without re-fetching anything.
    older_history = history[1 : lookback_runs + 1]

    for source, entry in current_by_source.items():
        if not isinstance(entry, dict) or entry.get("from_staging"):
            continue
        current_yield = entry.get("collected")
        if isinstance(current_yield, bool) or not isinstance(current_yield, int) or current_yield < 0:
            continue

        history_yields = [
            y for y in (_extract_yield(run, source) for run in recent_history) if y is not None
        ]
        result = _check_source(
            source, current_yield, history_yields,
            min_baseline_runs=min_baseline_runs,
            min_baseline_yield=min_baseline_yield,
            drop_ratio_threshold=drop_ratio_threshold,
        )
        if result is None:
            continue
        severity, baseline, sample_size = result

        # Was the immediately-preceding run ALSO anomalous for this source?
        # Recomputed from history alone — no persisted alert state.
        is_new = True
        if recent_history:
            prev_run = recent_history[0]
            prev_yield = _extract_yield(prev_run, source)
            if prev_yield is not None:
                prev_history_yields = [
                    y for y in (_extract_yield(run, source) for run in older_history) if y is not None
                ]
                prev_result = _check_source(
                    source, prev_yield, prev_history_yields,
                    min_baseline_runs=min_baseline_runs,
                    min_baseline_yield=min_baseline_yield,
                    drop_ratio_threshold=drop_ratio_threshold,
                )
                is_new = prev_result is None

        anomalies.append(SourceYieldAnomaly(
            source=source,
            current_yield=current_yield,
            baseline_yield=round(baseline, 1),
            baseline_run_count=sample_size,
            severity=severity,
            is_new=is_new,
        ))

    return anomalies
