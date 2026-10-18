"""Phase NEXT / I9 regression tests: personalization recency/decay.

Scope guard for every test here: decay only ever SHRINKS (never grows) a
feedback event's contribution relative to its undecayed weight, is a pure
function of (created_at, now, config) with no hidden state or randomness,
and never turns "we don't know this feedback's age" into a penalty — an
unknown/missing/invalid timestamp always falls back to full (undecayed)
weight rather than being treated as evidence of staleness. This is
explicitly NOT an outcome-tracking system (no I18): it only discounts
explicit feedback events the user already recorded, over time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.personalization.config import load_personalization_config
from src.personalization.engine import (
    _recency_decay_factor,
    build_preference_profile,
)
from src.personalization.models import Feedback, FeedbackType


@pytest.fixture
def default_config() -> dict:
    return load_personalization_config("config/does-not-exist.yaml")


def _fb(created_at: datetime, company: str = "Acme Corp") -> Feedback:
    return Feedback(
        job_id="j1",
        feedback_type=FeedbackType.INTERESTED,
        company=company,
        created_at=created_at,
    )


class TestFreshSignal:
    def test_zero_age_gets_full_weight(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        assert _recency_decay_factor(now, now, default_config) == 1.0

    def test_one_day_old_barely_decayed(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        one_day_ago = now - timedelta(days=1)
        factor = _recency_decay_factor(one_day_ago, now, default_config)
        assert 0.99 < factor < 1.0


class TestOldSignal:
    def test_exactly_one_half_life_is_half_weight(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        half_life_days = default_config["personalization"]["recency_half_life_days"]
        aged = now - timedelta(days=half_life_days)
        factor = _recency_decay_factor(aged, now, default_config)
        assert factor == pytest.approx(0.5, abs=1e-6)

    def test_two_half_lives_is_quarter_weight(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        half_life_days = default_config["personalization"]["recency_half_life_days"]
        aged = now - timedelta(days=2 * half_life_days)
        factor = _recency_decay_factor(aged, now, default_config)
        assert factor == pytest.approx(0.25, abs=1e-6)

    def test_decay_is_monotonically_non_increasing_with_age(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        ages_days = [0, 1, 10, 30, 90, 180, 365, 1000]
        factors = [
            _recency_decay_factor(now - timedelta(days=d), now, default_config)
            for d in ages_days
        ]
        assert factors == sorted(factors, reverse=True)


class TestDecayBoundaries:
    def test_never_exceeds_one(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        future = now + timedelta(days=5)  # clock skew / future timestamp
        assert _recency_decay_factor(future, now, default_config) == 1.0

    def test_never_goes_below_configured_floor(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        very_old = now - timedelta(days=100_000)
        factor = _recency_decay_factor(very_old, now, default_config)
        min_factor = default_config["personalization"]["recency_min_factor"]
        assert factor == pytest.approx(min_factor, abs=1e-9)

    def test_zero_half_life_disables_decay(self) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        very_old = now - timedelta(days=1000)
        config = {"personalization": {"recency_half_life_days": 0, "recency_min_factor": 0.05}}
        assert _recency_decay_factor(very_old, now, config) == 1.0


class TestMissingOrInvalidTimestamps:
    def test_missing_timestamp_gets_full_weight_not_penalized(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        assert _recency_decay_factor(None, now, default_config) == 1.0

    def test_naive_datetime_is_treated_as_utc_not_rejected(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        naive_one_day_ago = datetime(2026, 9, 12)  # no tzinfo
        factor = _recency_decay_factor(naive_one_day_ago, now, default_config)
        assert 0.99 < factor < 1.0

    def test_malformed_half_life_config_value_raises_clearly(self) -> None:
        """Matches this codebase's existing convention for numeric scoring
        config (see scoring.py's _get_float): a non-numeric configured value
        is a configuration bug and fails loudly and early, rather than
        silently guessing a fallback — it is the TIMESTAMP side (created_at)
        that must degrade gracefully, never the config side."""
        now = datetime(2026, 9, 13, tzinfo=UTC)
        old = now - timedelta(days=500)
        config = {"personalization": {"recency_half_life_days": "not-a-number"}}
        with pytest.raises(ValueError):
            _recency_decay_factor(old, now, config)


class TestNoPersonalizationHistory:
    def test_empty_feedback_list_yields_empty_profile_regardless_of_now(
        self, default_config: dict
    ) -> None:
        profile = build_preference_profile(
            [], default_config, version="v0", now=datetime(2026, 9, 13, tzinfo=UTC)
        )
        assert profile.feedback_count == 0
        assert profile.company == {}


class TestStableAcrossRepeatedRuns:
    def test_same_inputs_produce_identical_profile_every_time(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        records = [
            _fb(now - timedelta(days=5), company="Acme Corp"),
            _fb(now - timedelta(days=200), company="Acme Corp"),
        ]
        profile1 = build_preference_profile(records, default_config, version="v0", now=now)
        profile2 = build_preference_profile(records, default_config, version="v0", now=now)
        assert profile1.company["acme corp"].total_weight == profile2.company["acme corp"].total_weight
        assert profile1.company["acme corp"].count == profile2.company["acme corp"].count


class TestDecayIntegratesIntoPreferenceProfile:
    def test_old_feedback_contributes_less_than_fresh_feedback(self, default_config: dict) -> None:
        now = datetime(2026, 9, 13, tzinfo=UTC)
        fresh = build_preference_profile(
            [_fb(now, company="Acme Corp")], default_config, version="v-fresh", now=now
        )
        old = build_preference_profile(
            [_fb(now - timedelta(days=365), company="Acme Corp")],
            default_config, version="v-old", now=now,
        )
        assert old.company["acme corp"].average < fresh.company["acme corp"].average
        # Never fully suppressed to zero or below zero.
        assert old.company["acme corp"].average > 0

    def test_feedback_count_is_never_decayed_only_the_weight_is(self, default_config: dict) -> None:
        """Explainability: 'N feedback events' shown to the user must stay a
        real, whole count — decay applies only to the averaged weight."""
        now = datetime(2026, 9, 13, tzinfo=UTC)
        old = build_preference_profile(
            [
                _fb(now - timedelta(days=365), company="Acme Corp"),
                _fb(now - timedelta(days=365), company="Acme Corp"),
            ],
            default_config, version="v-old", now=now,
        )
        assert old.company["acme corp"].count == 2
