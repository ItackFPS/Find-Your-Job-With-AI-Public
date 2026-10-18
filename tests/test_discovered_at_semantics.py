"""How ``discovered_at`` and ``first_seen_at`` are set and persisted, and what the freshness filter
reads (issue #105, ADR 0012).

``discovered_at`` means "last collected": it is re-stamped on every collection and is never an age
signal. ``first_seen_at`` records the first time this pipeline stored the listing and is preserved on
every upsert; it is the freshness fallback when the source gives no usable ``published_at``.
Only temporary SQLite files are used.

Code paths exercised:
* ``src/models/job.py``      ``discovered_at = Field(default_factory=now)``
* ``src/pipeline/normalize.py``  builds a NEW ``Job`` per call and never reads a source ``discovered_at``
* ``src/storage/sqlite.py``  ``upsert_jobs_batch`` (the orchestrator's path) overwrites ``discovered_at``
                              on conflict but preserves the stored ``first_seen_at``
* ``src/pipeline/filter.py`` ``matches_published_age`` reads ``published_at``, else ``first_seen_at``
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from src.models.job import Job
from src.pipeline import orchestrator
from src.pipeline.filter import matches_published_age
from src.pipeline.normalize import normalize_job
from src.storage.sqlite import Storage

T_OLD = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
T_NEW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


@pytest.fixture()
def storage(tmp_path):
    s = Storage(str(tmp_path / "jobs.db"))
    s.init_db()
    yield s
    s.close()


def _job(**kw) -> Job:
    return Job(id="same-id", source="s", source_job_id="1", title="Python developer", **kw)


def test_discovered_at_defaults_to_the_construction_time():
    before = datetime.now(UTC)
    job = Job(id="a", title="x")
    assert before <= job.discovered_at <= datetime.now(UTC)


def test_normalising_the_same_payload_twice_gives_the_same_id_but_a_new_discovered_at():
    payload = {"title": "Python developer", "description": "x", "location": "Paris", "source_job_id": "42"}
    first = normalize_job("src", dict(payload))
    second = normalize_job("src", dict(payload))
    assert first.id == second.id                                  # stable id -> same DB row on re-collection
    assert second.discovered_at >= first.discovered_at            # re-stamped on every normalisation


def test_a_discovered_at_supplied_by_the_source_is_ignored_by_normalisation():
    job = normalize_job("src", {"title": "x", "description": "y", "source_job_id": "1",
                                "discovered_at": "2020-01-01T00:00:00+00:00"})
    assert job.discovered_at > datetime(2026, 1, 1, tzinfo=UTC)


def test_batch_upsert_overwrites_discovered_at_but_preserves_first_seen_at(storage):
    """The path used by the orchestrator (``upsert_jobs_batch``)."""
    storage.upsert_jobs_batch([_job(discovered_at=T_OLD)])
    stored = storage.get_job_by_id("same-id")
    assert stored is not None and stored.discovered_at == T_OLD
    first_seen = stored.first_seen_at
    assert first_seen is not None

    storage.upsert_jobs_batch([_job(discovered_at=T_NEW, first_seen_at=T_NEW)])
    again = storage.get_job_by_id("same-id")
    assert again is not None
    assert again.discovered_at == T_NEW            # overwritten: it means "last collected", not "first seen"
    assert again.first_seen_at == first_seen       # preserved: this is the field that records first discovery


def test_single_upsert_also_overwrites_discovered_at(storage):
    storage.upsert_job(_job(discovered_at=T_OLD))
    storage.upsert_job(_job(discovered_at=T_NEW))
    stored = storage.get_job_by_id("same-id")
    assert stored is not None and stored.discovered_at == T_NEW


def test_undated_listing_ages_out_from_its_first_sighting_not_from_its_last_collection():
    """An undated posting re-collected today is stamped 'now' in ``discovered_at``; the filter ages it
    from the stored ``first_seen_at`` instead, so a listing known for 90 days is stale."""
    now = datetime.now(UTC)
    cfg = {"posted_within_days": 30}
    recollected = normalize_job("src", {"title": "Python developer", "description": "x", "source_job_id": "9"})
    assert recollected.published_at is None
    assert matches_published_age(recollected, cfg)                                   # never seen: unknown age
    known_for_90_days = recollected.model_copy(update={"first_seen_at": now - timedelta(days=90)})
    assert not matches_published_age(known_for_90_days, cfg)


def test_discovered_at_never_changes_the_freshness_verdict():
    """Behavioural pin: whatever ``discovered_at`` says, the verdict is the same."""
    now = datetime.now(UTC)
    cfg = {"posted_within_days": 30}
    for published, first_seen in [(None, None), (None, now - timedelta(days=60)), (None, now - timedelta(days=2)),
                                  (now - timedelta(days=60), None), (now - timedelta(days=2), None)]:
        verdicts = {matches_published_age(_job(published_at=published, first_seen_at=first_seen, discovered_at=d), cfg)
                    for d in (now, now - timedelta(days=400))}
        assert len(verdicts) == 1, (published, first_seen)


def test_the_orchestrator_persists_with_the_batch_upsert():
    """Pins which upsert the pipeline uses, so the test above stays the relevant one."""
    assert "upsert_jobs_batch" in inspect.getsource(orchestrator)
