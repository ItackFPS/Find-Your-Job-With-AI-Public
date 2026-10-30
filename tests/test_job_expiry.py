"""Issue #86, slice 2: offers that closed are marked expired.

* An end date (schema.org validThrough) is read leniently and stored in UTC.
* After a run, an offer not seen by a fully collected source gets a missed run;
  ``max_missed`` in a row expire it ("not_seen"); a passed end date expires it
  ("valid_through"); seen again, it is active again.
* The user can report an offer as no longer available; that report survives the
  offer being collected again, and can be undone.
* Expired offers are hidden from the Jobs list unless asked for.

Temporary databases only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.models.job import Job
from src.pipeline.normalize import normalize_job
from src.storage.sqlite import Storage

NOW = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)


def _storage(path: Path) -> Storage:
    Storage._initialized_paths.discard(str(path.resolve()))
    s = Storage(db_path=str(path))
    s.init_db()
    return s


def _job(job_id: str, source: str = "france-travail", **kw) -> Job:
    return Job(id=job_id, source=source, title=f"Offre {job_id}", sources=[source], **kw)


def _status(s: Storage, job_id: str) -> tuple[str, str | None]:
    job = s.get_job_by_id(job_id)
    assert job is not None
    return job.status, job.expiry_reason


# ---------------------------------------------------------------------------
# validThrough
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2026-11-30", datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)),
    ("2026-11-30T18:00:00+01:00", datetime(2026, 11, 30, 17, 0, tzinfo=UTC)),
    ("2026-11-30T18:00:00Z", datetime(2026, 11, 30, 18, 0, tzinfo=UTC)),
    ("not a date", None),
    (None, None),
])
def test_end_date_is_read_leniently_in_utc(raw: str | None, expected: datetime | None) -> None:
    job = normalize_job("hellowork", {"title": "Développeur", "description": "Python", "valid_through": raw,
                                      "source_job_id": "1", "canonical_url": "https://x.example/1"})
    assert job is not None
    assert job.valid_through == expected
    if expected is not None:
        assert job.valid_through.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# Expiry after a run
# ---------------------------------------------------------------------------

def _seed(s: Storage) -> None:
    jobs = [
        _job("seen"),
        _job("gone"),                                   # listed by a fully collected source
        _job("partial", source="eures"),                # its source did not complete
        _job("ended", valid_through=NOW - timedelta(days=1)),
        _job("future", valid_through=NOW + timedelta(days=5)),
    ]
    for job in jobs:
        s.upsert_job(job)
    s.record_provenance(jobs, NOW - timedelta(days=1))


def test_offers_expire_after_missed_runs_or_their_end_date(tmp_path: Path) -> None:
    s = _storage(tmp_path / "jobs.db")
    _seed(s)
    complete = {"france-travail"}
    for run in range(3):
        out = s.apply_expiry({"seen", "future"}, complete, NOW + timedelta(days=run), max_missed=3)
        if run < 2:
            assert _status(s, "gone") == ("active", None)
    assert _status(s, "gone") == ("expired", "not_seen")
    assert out["expired_not_seen"] == 1
    assert _status(s, "seen") == ("active", None)
    assert _status(s, "partial") == ("active", None)        # eures never completed
    assert _status(s, "ended") == ("expired", "valid_through")
    assert _status(s, "future") == ("active", None)
    expired = s.get_job_by_id("gone")
    assert expired is not None and expired.expired_at is not None
    s.close()


def test_a_miss_streak_is_reset_by_a_sighting(tmp_path: Path) -> None:
    s = _storage(tmp_path / "jobs.db")
    _seed(s)
    s.apply_expiry(set(), {"france-travail"}, NOW, max_missed=3)
    s.apply_expiry(set(), {"france-travail"}, NOW, max_missed=3)
    s.apply_expiry({"gone"}, {"france-travail"}, NOW, max_missed=3)
    s.apply_expiry(set(), {"france-travail"}, NOW, max_missed=3)
    assert _status(s, "gone") == ("active", None)
    s.close()


def test_an_expired_offer_seen_again_is_active_again(tmp_path: Path) -> None:
    s = _storage(tmp_path / "jobs.db")
    _seed(s)
    for _ in range(3):
        s.apply_expiry(set(), {"france-travail"}, NOW, max_missed=3)
    assert _status(s, "gone") == ("expired", "not_seen")
    out = s.apply_expiry({"gone"}, {"france-travail"}, NOW, max_missed=3)
    assert out["reactivated"] == 1
    assert _status(s, "gone") == ("active", None)
    s.close()


def test_a_user_report_survives_a_new_sighting_and_can_be_undone(tmp_path: Path) -> None:
    s = _storage(tmp_path / "jobs.db")
    _seed(s)
    assert s.set_reported_expired("seen", expired=True, now=NOW)
    s.upsert_job(_job("seen"))                              # collected again
    s.upsert_jobs_batch([_job("seen")])
    s.apply_expiry({"seen"}, {"france-travail"}, NOW, max_missed=3)
    assert _status(s, "seen") == ("expired", "user_report")

    assert s.set_reported_expired("seen", expired=False, now=NOW)
    assert _status(s, "seen") == ("active", None)
    assert not s.set_reported_expired("missing", expired=True, now=NOW)
    s.close()


def test_a_new_sighting_clears_an_automatic_expiry(tmp_path: Path) -> None:
    s = _storage(tmp_path / "jobs.db")
    _seed(s)
    for _ in range(3):
        s.apply_expiry(set(), {"france-travail"}, NOW, max_missed=3)
    s.upsert_jobs_batch([_job("gone")])
    assert _status(s, "gone") == ("active", None)
    s.close()


def test_expire_after_missed_runs_config(tmp_path: Path) -> None:
    from src.pipeline.orchestrator import EXPIRY_EXCLUDED_SOURCES, expire_after_missed_runs

    assert expire_after_missed_runs(tmp_path) == 3
    (tmp_path / "retention.yaml").write_text("expire_after_missed_runs: 5\n", encoding="utf-8")
    assert expire_after_missed_runs(tmp_path) == 5
    (tmp_path / "retention.yaml").write_text("expire_after_missed_runs: 0\n", encoding="utf-8")
    assert expire_after_missed_runs(tmp_path) == 1
    (tmp_path / "retention.yaml").write_text("expire_after_missed_runs: soon\n", encoding="utf-8")
    assert expire_after_missed_runs(tmp_path) == 3
    assert {"linkedin", "hellowork"} <= EXPIRY_EXCLUDED_SOURCES
    assert expire_after_missed_runs("config") == 3          # the shipped value


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_expired_offers_are_hidden_unless_asked_for(isolated_app) -> None:
    from fastapi.testclient import TestClient

    s = _storage(Path(isolated_app.state.db_path))
    _seed(s)
    s.apply_expiry({"seen", "future", "gone", "partial"}, set(), NOW, max_missed=3)
    s.close()
    client = TestClient(isolated_app)

    ids = {j["id"] for j in client.get("/api/jobs").json()}
    assert "ended" not in ids and "seen" in ids
    every = {j["id"]: j for j in client.get("/api/jobs?include_expired=true").json()}
    assert every["ended"]["status"] == "expired"
    assert every["ended"]["expiry_reason"] == "valid_through"


def test_the_user_reports_availability(isolated_app) -> None:
    from fastapi.testclient import TestClient

    s = _storage(Path(isolated_app.state.db_path))
    _seed(s)
    s.close()
    client = TestClient(isolated_app)

    r = client.post("/api/jobs/seen/availability", json={"available": False})
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["expiry_reason"]) == ("expired", "user_report")
    r = client.post("/api/jobs/seen/availability", json={"available": True})
    assert (r.json()["status"], r.json()["expiry_reason"]) == ("active", None)
    assert client.post("/api/jobs/nope/availability", json={"available": False}).status_code == 404
    assert client.post("/api/jobs/seen/availability", json={"available": "no"}).status_code == 422


# ---------------------------------------------------------------------------
# Only a complete collection is evidence of absence
# ---------------------------------------------------------------------------

_COMPLETE = {"status": "completed", "collected": 12, "listing_complete": True}


@pytest.mark.parametrize("row,counts", [
    (_COMPLETE, True),
    ({**_COMPLETE, "listing_complete": False}, False),        # first page / newest N only
    ({k: v for k, v in _COMPLETE.items() if k != "listing_complete"}, False),
    ({**_COMPLETE, "status": "partial"}, False),
    ({**_COMPLETE, "status": "failed"}, False),
    ({**_COMPLETE, "status": "paused"}, False),
    ({**_COMPLETE, "request_cap_reached": True}, False),
    ({**_COMPLETE, "collected": 0}, False),                   # an empty answer is not a census
    ({**_COMPLETE, "yield_anomaly": {"kind": "drop"}}, False),
])
def test_a_source_counts_misses_only_after_a_complete_collection(row: dict, counts: bool) -> None:
    from src.pipeline.orchestrator import sources_counting_misses

    assert sources_counting_misses({"france-travail": row}) == ({"france-travail"} if counts else set())


def test_staged_sources_never_count_misses() -> None:
    from src.pipeline.orchestrator import sources_counting_misses

    assert sources_counting_misses({"linkedin": dict(_COMPLETE), "hellowork": dict(_COMPLETE)}) == set()


def _pipeline_config(tmp: Path) -> Path:
    import shutil

    from tests.fixtures.profiles import TEST_PROFILE_PATH

    root = Path(__file__).resolve().parents[1]
    cfg = tmp / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(root / "config" / name, cfg / name)
    (cfg / "sources.yaml").write_text(
        "sources:\n  - name: board\n    type: fake_board\n    domain: board.example\n"
        "    careers_url: https://board.example/jobs\n    active: true\n", encoding="utf-8")
    return cfg


def test_an_offer_that_fails_validation_is_still_seen(tmp_path: Path, monkeypatch) -> None:
    """A source that read its whole board lists offer 'thin' with too little text to pass
    validation: it was seen, so it gets no miss — only the offer the board dropped does."""
    from unittest.mock import patch

    from src.collectors.base import BaseCollector, CollectorResult
    from src.collectors.registry import registry
    from src.embeddings.embedding import EmbeddingService
    from src.pipeline.orchestrator import run_pipeline

    def raw(job_id: str, **kw) -> dict:
        return {"source_job_id": job_id, "canonical_url": f"https://board.example/{job_id}",
                "title": "Développeur Python", "company": "Acme", **kw}

    class FakeBoard(BaseCollector):
        def __init__(self, config=None) -> None:
            super().__init__()

        def collect(self) -> CollectorResult:
            return CollectorResult(source_name="board", listing_complete=True, jobs=[
                raw("full", description="Python, FastAPI, SQL. CDI à Lyon.", location="Lyon"),
                raw("thin"),
            ])

    class FakeEmbeddings(EmbeddingService):
        def __init__(self, *a, **k) -> None:
            super().__init__(model_name="fake")

        def embed(self, text):                       # type: ignore[override]
            return [0.5] * 8

        def embed_batch(self, texts, ids=None):      # type: ignore[override]
            from src.embeddings.embedding import BatchEmbeddingResult
            return BatchEmbeddingResult(ids=list(ids or range(len(texts))), vectors=[[0.5] * 8 for _ in texts])

    real_get = registry.get_collector_class
    monkeypatch.setattr(registry, "get_collector_class",
                        lambda t: FakeBoard if t == "fake_board" else real_get(t))
    cfg = _pipeline_config(tmp_path)
    db = tmp_path / "jobs.db"
    # Both offers stored by an earlier run, with the ids the pipeline gives them.
    earlier = [normalize_job("board", raw(job_id, description="Python", location="Lyon"))
               for job_id in ("thin", "gone")]
    assert all(earlier)
    s = _storage(db)
    for job in earlier:
        s.upsert_job(job)
    s.record_provenance(earlier, NOW)
    s.close()
    ids = {earlier[0].id: "thin", earlier[1].id: "gone"}

    with patch("src.embeddings.embedding.EmbeddingService", FakeEmbeddings):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg))
    assert result.status == "completed", result.error
    assert result.stats.by_source["board"].get("listing_complete") is True

    s = _storage(db)
    missed = {ids[r[0]]: r[1] for r in s._get_conn().execute("SELECT id, missed_runs FROM jobs").fetchall()
              if r[0] in ids}
    s.close()
    assert missed == {"thin": 0, "gone": 1}
