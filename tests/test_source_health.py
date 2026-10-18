"""Phase NOW / I16 regression tests for src.notifications.source_health.

Pure-function tests only — no DB, no Discord, no network. See
tests/test_notification_engine.py for the wiring-level test proving
NotificationEngine actually sends an alert via the existing #errors channel
for a newly-detected anomaly.
"""

from __future__ import annotations

from src.notifications.source_health import detect_source_yield_anomalies


def _completed_run(by_source: dict) -> dict:
    return {"status": "completed", "stats": {"by_source": by_source}}


def _healthy_history(source: str, values: list[int]) -> list[dict]:
    """Most-recent-first history where `source` yielded each of `values`
    (first element = most recent prior run)."""
    return [_completed_run({source: {"collected": v, "accepted": 0, "errors": 0}}) for v in values]


class TestNormalYield:
    def test_yield_in_line_with_baseline_is_not_an_anomaly(self) -> None:
        current = {"apec": {"collected": 48, "accepted": 10, "errors": 0}}
        history = _healthy_history("apec", [50, 45, 52, 49, 47])
        result = detect_source_yield_anomalies(current, history)
        assert result == []

    def test_yield_above_baseline_is_not_an_anomaly(self) -> None:
        current = {"apec": {"collected": 80, "accepted": 20, "errors": 0}}
        history = _healthy_history("apec", [50, 45, 52, 49, 47])
        result = detect_source_yield_anomalies(current, history)
        assert result == []


class TestZeroYield:
    def test_zero_yield_against_healthy_baseline_is_flagged(self) -> None:
        current = {"indeed": {"collected": 0, "accepted": 0, "errors": 1}}
        history = _healthy_history("indeed", [500, 480, 510, 495, 505])
        result = detect_source_yield_anomalies(current, history)
        assert len(result) == 1
        anomaly = result[0]
        assert anomaly.source == "indeed"
        assert anomaly.severity == "zero"
        assert anomaly.current_yield == 0
        assert anomaly.is_new is True

    def test_zero_yield_against_tiny_baseline_is_not_flagged(self) -> None:
        """A source that normally yields ~2 jobs going to 0 is normal noise
        (min_baseline_yield guard)."""
        current = {"niche-board": {"collected": 0, "accepted": 0, "errors": 0}}
        history = _healthy_history("niche-board", [2, 3, 1, 2, 4])
        result = detect_source_yield_anomalies(current, history)
        assert result == []


class TestSignificantDrop:
    def test_large_drop_below_threshold_is_flagged(self) -> None:
        current = {"eures": {"collected": 15, "accepted": 3, "errors": 0}}
        history = _healthy_history("eures", [200, 210, 195, 205, 190])
        result = detect_source_yield_anomalies(current, history)
        assert len(result) == 1
        assert result[0].severity == "drop"
        assert result[0].current_yield == 15

    def test_moderate_drop_within_normal_fluctuation_is_not_flagged(self) -> None:
        """A 30% drop is a real dip but not the ~80%+ drop this detector
        targets — normal week-to-week variance in a live job market."""
        current = {"eures": {"collected": 140, "accepted": 30, "errors": 0}}
        history = _healthy_history("eures", [200, 210, 195, 205, 190])
        result = detect_source_yield_anomalies(current, history)
        assert result == []


class TestInsufficientHistory:
    def test_no_history_is_not_flagged(self) -> None:
        current = {"new-source": {"collected": 0, "accepted": 0, "errors": 0}}
        result = detect_source_yield_anomalies(current, history=[])
        assert result == []

    def test_below_min_baseline_runs_is_not_flagged(self) -> None:
        current = {"apec": {"collected": 0, "accepted": 0, "errors": 0}}
        history = _healthy_history("apec", [50, 48])  # only 2, default requires 3
        result = detect_source_yield_anomalies(current, history)
        assert result == []

    def test_exactly_min_baseline_runs_is_evaluated(self) -> None:
        current = {"apec": {"collected": 0, "accepted": 0, "errors": 0}}
        history = _healthy_history("apec", [50, 48, 52])  # exactly 3
        result = detect_source_yield_anomalies(current, history)
        assert len(result) == 1


class TestDisabledSourceNeverConsidered:
    def test_source_absent_from_current_run_is_never_flagged(self) -> None:
        """A source disabled since the last run (active: false) simply isn't
        in this run's by_source at all — nothing to special-case."""
        current = {"apec": {"collected": 50, "accepted": 10, "errors": 0}}
        history = _healthy_history("bundesagentur", [0, 0, 0, 0, 0])  # unrelated source's history
        result = detect_source_yield_anomalies(current, history)
        assert result == []

    def test_from_staging_sources_are_always_skipped(self) -> None:
        """LinkedIn/HelloWork staging-injected counts are not a health
        signal for this run (see module docstring)."""
        current = {"linkedin": {"collected": 0, "accepted": 0, "errors": 0, "from_staging": True}}
        history = [
            _completed_run({"linkedin": {"collected": 40, "accepted": 5, "errors": 0, "from_staging": True}})
            for _ in range(5)
        ]
        result = detect_source_yield_anomalies(current, history)
        assert result == []


class TestMissingOrInvalidHistoricalData:
    def test_missing_stats_key_in_history_entry_is_skipped_not_crashed(self) -> None:
        current = {"apec": {"collected": 0, "accepted": 0, "errors": 0}}
        history = [{"status": "completed"}] * 5  # no "stats" key at all
        result = detect_source_yield_anomalies(current, history)
        assert result == []  # treated as insufficient history, not a crash

    def test_non_completed_run_excluded_from_baseline(self) -> None:
        current = {"apec": {"collected": 0, "accepted": 0, "errors": 0}}
        history = [
            {"status": "failed", "stats": {"by_source": {"apec": {"collected": 50}}}},
            {"status": "running", "stats": {"by_source": {"apec": {"collected": 50}}}},
        ] + _healthy_history("apec", [48, 52])
        # Only 2 valid (completed) data points -> below default min of 3.
        result = detect_source_yield_anomalies(current, history)
        assert result == []

    def test_malformed_collected_value_excluded_not_crashed(self) -> None:
        current = {"apec": {"collected": 0, "accepted": 0, "errors": 0}}
        history = [
            _completed_run({"apec": {"collected": "not-a-number"}}),
            _completed_run({"apec": {"collected": None}}),
            _completed_run({"apec": {}}),  # missing "collected" entirely
        ] + _healthy_history("apec", [50, 48])
        # Only the 2 well-formed entries count -> below default min of 3.
        result = detect_source_yield_anomalies(current, history)
        assert result == []

    def test_current_run_malformed_entry_is_skipped_not_crashed(self) -> None:
        current = {"apec": {"collected": "oops"}}
        history = _healthy_history("apec", [50, 48, 52, 49, 47])
        result = detect_source_yield_anomalies(current, history)
        assert result == []

    def test_non_dict_history_entries_are_skipped_not_crashed(self) -> None:
        current = {"apec": {"collected": 0}}
        history = [None, "garbage", 42] + _healthy_history("apec", [50, 48, 52])  # type: ignore[list-item]
        # lookback_runs widened so all 3 well-formed entries fall inside the
        # window despite the 3 garbage entries ahead of them in history.
        result = detect_source_yield_anomalies(current, history, lookback_runs=10)
        assert len(result) == 1  # the 3 well-formed entries are still usable


class TestAlertDeduplicationNoisePrevention:
    def test_first_occurrence_is_new(self) -> None:
        current = {"indeed": {"collected": 0, "accepted": 0, "errors": 0}}
        # The run immediately prior was healthy (500) -> this is a fresh drop.
        history = _healthy_history("indeed", [500, 480, 510, 495, 505])
        result = detect_source_yield_anomalies(current, history)
        assert len(result) == 1
        assert result[0].is_new is True

    def test_continuing_anomaly_is_not_new(self) -> None:
        """The source was ALREADY at 0 last run (against ITS OWN healthy
        baseline before that) -> still broken, but already alerted."""
        current = {"indeed": {"collected": 0, "accepted": 0, "errors": 1}}
        # Most recent prior run was also 0; the runs before that were healthy.
        history = [
            _completed_run({"indeed": {"collected": 0, "accepted": 0, "errors": 1}}),
        ] + _healthy_history("indeed", [500, 480, 510, 495, 505])
        result = detect_source_yield_anomalies(current, history)
        assert len(result) == 1
        assert result[0].is_new is False

    def test_recovery_then_new_regression_is_new_again(self) -> None:
        """Broken, then healthy again, then broken again -> the second
        break is a fresh, newsworthy occurrence."""
        current = {"indeed": {"collected": 0, "accepted": 0, "errors": 1}}
        history = [
            _completed_run({"indeed": {"collected": 490, "accepted": 100, "errors": 0}}),  # recovered
        ] + _healthy_history("indeed", [500, 480, 510, 495, 505])
        result = detect_source_yield_anomalies(current, history)
        assert len(result) == 1
        assert result[0].is_new is True
