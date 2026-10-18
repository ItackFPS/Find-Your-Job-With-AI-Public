"""Direct tests of the ``published_age`` hard filter (``matches_published_age``).

The clock is frozen with the evaluation harness's own ``frozen_clock`` so every boundary is exact.
The fallback for a missing or unusable ``published_at`` is the stored ``first_seen_at`` (ADR 0012,
issue #105); ``discovered_at`` is never an age signal. See also ``tests/test_freshness_policy.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.evaluation.runner import frozen_clock
from src.models.job import Job
from src.pipeline.filter import matches_published_age
from src.pipeline.normalize import normalize_job

NOW = datetime(2026, 10, 4, 20, 16, 9, tzinfo=UTC)
CFG = {"posted_within_days": 30}
CUTOFF = NOW - timedelta(days=30)


def _passes(job: object, cfg: dict | None = None) -> bool:
    with frozen_clock(NOW):
        return matches_published_age(job, cfg or CFG)  # type: ignore[arg-type]


def _job(**kw: object) -> Job:
    kw.setdefault("discovered_at", NOW)          # the model default is the real clock: always pin it
    return Job(id="t", title="Python developer", **kw)


# -- cutoff -----------------------------------------------------------------------------

def test_exactly_at_the_cutoff_passes_the_bound_is_inclusive():
    assert _passes(_job(published_at=CUTOFF))


def test_one_microsecond_older_than_the_cutoff_is_rejected():
    assert not _passes(_job(published_at=CUTOFF - timedelta(microseconds=1)))


def test_one_second_fresher_than_the_cutoff_passes():
    assert _passes(_job(published_at=CUTOFF + timedelta(seconds=1)))


def test_a_listing_19_minutes_past_the_cutoff_is_rejected():
    """The real-v1 'Salaise' case: 30 d 0 h 18 min 51 s old at the benchmark's as_of."""
    published = datetime(2026, 9, 4, 19, 57, 18, tzinfo=UTC)
    assert NOW - published == timedelta(days=30, minutes=18, seconds=51)
    assert not _passes(_job(published_at=published))


@pytest.mark.parametrize("days,expected", [(7, False), (30, False), (31, True), (45, True)])
def test_window_comes_from_the_config_not_a_constant(days, expected):
    """A listing 30 d + 1 s old: rejected by a 7- or 30-day window, accepted by a 31- or 45-day one."""
    job = _job(published_at=NOW - timedelta(days=30, seconds=1))
    assert _passes(job, {"posted_within_days": days}) is expected


def test_window_defaults_to_30_days_when_the_key_is_absent():
    assert _passes(_job(published_at=NOW - timedelta(days=29)), {})
    assert not _passes(_job(published_at=NOW - timedelta(days=31)), {})


# -- timezone equivalence -----------------------------------------------------------------

@pytest.mark.parametrize("offset_hours", [-5, 0, 2, 9])
def test_the_same_instant_gives_the_same_verdict_in_any_utc_offset(offset_hours):
    tz = timezone(timedelta(hours=offset_hours))
    at_cutoff = CUTOFF.astimezone(tz)
    assert at_cutoff.utcoffset() == timedelta(hours=offset_hours)
    assert _passes(_job(published_at=at_cutoff))
    assert not _passes(_job(published_at=(CUTOFF - timedelta(seconds=1)).astimezone(tz)))


def test_a_local_calendar_day_does_not_matter_only_the_instant():
    """23:30 on the previous day in UTC-5 is the same instant as 04:30 UTC: no off-by-one on dates."""
    tz = timezone(timedelta(hours=-5))
    published = datetime(2026, 9, 4, 23, 30, tzinfo=tz)          # = 2026-09-05 04:30 UTC, fresher than cutoff
    assert published.astimezone(UTC) > CUTOFF
    assert _passes(_job(published_at=published))


def test_naive_datetimes_are_not_handled_by_the_filter_itself():
    """Characterisation: the filter compares against an aware cutoff and raises on a naive value.

    Production never reaches this: ``normalize_job`` attaches UTC (see the normalisation tests below).
    """
    with pytest.raises(TypeError):
        _passes(_job(published_at=(NOW - timedelta(days=1)).replace(tzinfo=None)))


# -- published_at precedence over discovered_at ---------------------------------------------

def test_an_old_published_at_wins_over_a_fresh_discovered_at():
    assert not _passes(_job(published_at=NOW - timedelta(days=60), discovered_at=NOW))


def test_a_fresh_published_at_wins_over_an_old_discovered_at():
    assert _passes(_job(published_at=NOW - timedelta(days=2), discovered_at=NOW - timedelta(days=400)))


# -- missing dates, old discovered_at ---------------------------------------------------------

def test_missing_published_at_and_never_seen_before_passes():
    assert _passes(_job(published_at=None, discovered_at=NOW))


def test_missing_published_at_with_an_old_discovered_at_is_not_aged_by_it():
    """``discovered_at`` is the last collection time, not an age: only ``first_seen_at`` ages an undated listing."""
    assert _passes(_job(published_at=None, discovered_at=NOW - timedelta(days=60)))
    assert not _passes(_job(published_at=None, discovered_at=NOW, first_seen_at=NOW - timedelta(days=60)))


def test_no_date_at_all_is_unknown_not_a_rejection():
    """``Job.discovered_at`` is mandatory on the model, so the branch is reached with a stub."""
    assert _passes(SimpleNamespace(published_at=None, discovered_at=None))


# -- normalisation of raw dates (what actually reaches the filter in production) -----------------

def _normalised(raw: object) -> Job:
    payload: dict = {"title": "Python developer", "description": "x", "location": "Paris", "source_job_id": "s1"}
    if raw is not None:
        payload["published_at"] = raw
    return normalize_job("test-source", payload)


def test_normalisation_attaches_utc_to_a_naive_iso_string():
    job = _normalised("2026-09-20T10:00:00")
    assert job.published_at == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    assert _passes(job)


def test_normalisation_accepts_a_trailing_z_and_an_explicit_offset():
    assert _normalised("2026-09-20T10:00:00Z").published_at == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    assert _normalised("2026-09-20T12:00:00+02:00").published_at == datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def test_a_date_only_string_becomes_midnight_utc():
    assert _normalised("2026-09-20").published_at == datetime(2026, 9, 20, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["not-a-date", "20/09/2026", "", "   "])
def test_an_unparseable_or_empty_date_becomes_none_without_raising(raw):
    assert _normalised(raw).published_at is None


def test_an_unparseable_date_is_then_treated_as_undated():
    """An invalid date downgrades to 'undated'; a listing never stored before has an unknown age and
    passes (the stored ``first_seen_at`` decides on later collections, see test_freshness_policy.py)."""
    job = _normalised("not-a-date")
    assert job.published_at is None
    assert _passes(job.model_copy(update={"discovered_at": NOW}))
