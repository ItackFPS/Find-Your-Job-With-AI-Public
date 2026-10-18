"""Freshness policy (issue #105, ADR 0012).

* ``published_at``  — what the source claims; authoritative when plausible.
* ``first_seen_at`` — when this pipeline first stored the listing; preserved on every upsert.
* ``discovered_at`` — when the listing was last collected; re-stamped on every collection, so it
  says nothing about how long the listing has been online and is never a freshness signal.

Reference date for the ``published_age`` filter: a plausible ``published_at``, else the stored
``first_seen_at``, else unknown (passes: unknown is not a rejection). A missing, unparseable,
implausibly old (before 2000) or future (more than one day ahead) ``published_at`` is ignored.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.evaluation.runner import frozen_clock
from src.models.job import Job
from src.pipeline.filter import freshness_reference, matches_published_age
from src.pipeline.normalize import normalize_job
from src.storage.sqlite import Storage

NOW = datetime(2026, 10, 4, 20, 16, 9, tzinfo=UTC)
CFG = {"posted_within_days": 30}
CUTOFF = NOW - timedelta(days=30)


def _passes(job: object, cfg: dict | None = None) -> bool:
    with frozen_clock(NOW):
        return matches_published_age(job, cfg or CFG)  # type: ignore[arg-type]


def _ref(job: object) -> tuple[datetime | None, str]:
    with frozen_clock(NOW):
        return freshness_reference(job)  # type: ignore[arg-type]


def _job(**kw: object) -> Job:
    kw.setdefault("discovered_at", NOW)
    return Job(id="t", title="Python developer", **kw)


# -- reference date and its basis -------------------------------------------------------------

def test_a_plausible_published_at_is_the_reference():
    assert _ref(_job(published_at=NOW - timedelta(days=3), first_seen_at=NOW - timedelta(days=90))) == (
        NOW - timedelta(days=3), "published_at")


def test_without_published_at_the_stored_first_seen_at_is_the_reference():
    assert _ref(_job(first_seen_at=NOW - timedelta(days=9))) == (NOW - timedelta(days=9), "first_seen_at")


def test_without_either_date_the_age_is_unknown_even_with_an_old_discovered_at():
    assert _ref(_job(discovered_at=NOW - timedelta(days=400))) == (None, "unknown")


@pytest.mark.parametrize("bad", [datetime(1970, 1, 1, tzinfo=UTC), datetime(1999, 12, 31, 23, 59, tzinfo=UTC),
                                 NOW + timedelta(days=1, seconds=1), NOW + timedelta(days=365)])
def test_an_implausible_published_at_is_ignored(bad):
    assert _ref(_job(published_at=bad)) == (None, "unknown")
    assert _ref(_job(published_at=bad, first_seen_at=NOW - timedelta(days=2)))[1] == "first_seen_at"


def test_a_slightly_future_published_at_is_kept_clock_skew_and_timezones():
    assert _ref(_job(published_at=NOW + timedelta(hours=20)))[1] == "published_at"
    assert _ref(_job(published_at=NOW + timedelta(days=1)))[1] == "published_at"     # tolerance is inclusive


def test_a_stub_without_first_seen_at_is_handled():
    assert _ref(SimpleNamespace(published_at=None, discovered_at=NOW)) == (None, "unknown")


# -- filter outcomes ---------------------------------------------------------------------------

def test_published_at_wins_over_first_seen_at_both_ways():
    assert not _passes(_job(published_at=NOW - timedelta(days=60), first_seen_at=NOW - timedelta(days=1)))
    assert _passes(_job(published_at=NOW - timedelta(days=2), first_seen_at=NOW - timedelta(days=200)))


def test_an_undated_listing_first_seen_long_ago_is_stale():
    assert not _passes(_job(first_seen_at=NOW - timedelta(days=60)))


def test_an_undated_listing_first_seen_recently_passes():
    assert _passes(_job(first_seen_at=NOW - timedelta(days=5)))


def test_an_undated_never_seen_listing_passes_whatever_its_discovered_at():
    assert _passes(_job(discovered_at=NOW - timedelta(days=60)))


def test_first_seen_at_boundary_is_inclusive():
    assert _passes(_job(first_seen_at=CUTOFF))
    assert not _passes(_job(first_seen_at=CUTOFF - timedelta(microseconds=1)))


def test_an_implausible_published_at_falls_back_to_first_seen_at():
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert _passes(_job(published_at=epoch))                                          # unknown -> pass
    assert _passes(_job(published_at=epoch, first_seen_at=NOW - timedelta(days=3)))
    assert not _passes(_job(published_at=epoch, first_seen_at=NOW - timedelta(days=45)))


def test_a_far_future_published_at_cannot_keep_a_known_old_listing_fresh():
    assert not _passes(_job(published_at=NOW + timedelta(days=300), first_seen_at=NOW - timedelta(days=45)))


def test_an_unparseable_date_is_undated_then_the_stored_first_seen_at_decides():
    job = normalize_job("src", {"title": "Python developer", "description": "x", "source_job_id": "9",
                                "published_at": "not-a-date"})
    assert job.published_at is None and job.first_seen_at is None
    assert _passes(job.model_copy(update={"discovered_at": NOW}))
    assert not _passes(job.model_copy(update={"discovered_at": NOW, "first_seen_at": NOW - timedelta(days=31)}))


# -- storage: the stored first_seen_at is available before filtering ------------------------------

@pytest.fixture()
def storage(tmp_path):
    s = Storage(str(tmp_path / "jobs.db"))
    s.init_db()
    yield s
    s.close()


def test_get_first_seen_at_returns_stored_values_only(storage):
    t0 = NOW - timedelta(days=50)
    storage.upsert_jobs_batch([Job(id="known", title="x", first_seen_at=t0, discovered_at=t0)])
    assert storage.get_first_seen_at(["known", "unknown"]) == {"known": t0}
    assert storage.get_first_seen_at([]) == {}


def test_get_first_seen_at_survives_a_recollection_upsert(storage):
    t0 = NOW - timedelta(days=50)
    storage.upsert_jobs_batch([Job(id="known", title="x", first_seen_at=t0, discovered_at=t0)])
    storage.upsert_jobs_batch([Job(id="known", title="x", discovered_at=NOW)])     # re-collected today
    assert storage.get_first_seen_at(["known"]) == {"known": t0}


def test_get_first_seen_at_chunks_large_id_lists(storage):
    t0 = NOW - timedelta(days=2)
    storage.upsert_jobs_batch([Job(id=f"j{i}", title="x", first_seen_at=t0) for i in range(1200)])
    got = storage.get_first_seen_at([f"j{i}" for i in range(1200)] + ["missing"])
    assert len(got) == 1200 and set(got.values()) == {t0}


# -- end to end: the real run_pipeline, run twice ------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


def _run(cfg: Path, db: Path, jobs: list[Job]):
    from src.pipeline.orchestrator import run_pipeline
    from tests.test_decision_journal_orchestrator import _FakeEmbeddingService

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg), prefetched_linkedin_jobs=jobs)
    assert result.status == "completed", result.error
    return result


def _published_age_rejections(result) -> set[str]:
    return {r.job_id for r in result.rejection_records
            if r.rejection_stage == "filter" and "published_age" in (r.rejection_detail or "")}


def test_recollected_undated_listing_ages_out_from_its_first_sighting_but_a_new_one_does_not(tmp_path):
    from src.evaluation.dataset import load_dataset
    from tests.fixtures.profiles import TEST_PROFILE_PATH

    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    (cfg / "decisions.yaml").write_text("enabled: false\n", encoding="utf-8")
    db = tmp_path / "jobs.db"

    undated = [j.model_copy(update={"published_at": None}) for j in load_dataset(DS).build_jobs()]
    first = _run(cfg, db, undated)
    accepted = sorted(s.job_id for s in first.scored_jobs)
    assert len(accepted) >= 2 and not _published_age_rejections(first)

    # Pretend the first half of the accepted listings were first stored 60 days ago.
    old, recent = accepted[: len(accepted) // 2], accepted[len(accepted) // 2:]
    long_ago = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.executemany("UPDATE jobs SET first_seen_at = ? WHERE id = ?", [(long_ago, j) for j in old])

    second = _run(cfg, db, [j.model_copy(update={"discovered_at": datetime.now(UTC)}) for j in undated])
    assert _published_age_rejections(second) == set(old)
    assert set(recent) <= {s.job_id for s in second.scored_jobs}
    # Storage still records the original first sighting (the filter only read it).
    with sqlite3.connect(db) as conn:
        stored = dict(conn.execute(f"SELECT id, first_seen_at FROM jobs WHERE id IN ({','.join('?' * len(old))})", old))
    assert set(stored.values()) == {long_ago}
