"""Run outcomes and the visible-list rule (Phase B B7-A, open question OQ-3).

Temporary databases and fake collectors; no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.collectors.base import BaseCollector, CollectorResult
from src.pipeline.run_outcome import failed_sources, run_outcome

OK = {"status": "completed"}
DOWN = {"status": "failed"}


@pytest.mark.parametrize(("status", "by_source", "accepted", "new", "previous", "expected"), [
    ("completed", {"a": OK, "b": OK}, 5, 3, True, "successful"),
    ("completed", {"a": OK, "b": DOWN}, 5, 3, True, "partial"),
    ("completed", {"a": OK, "b": {"status": "partial"}}, 5, 3, True, "partial"),
    ("completed", {"a": OK}, 5, 0, True, "no_new_offers"),
    ("completed", {"a": DOWN, "b": DOWN}, 0, 0, True, "no_successful_source"),
    ("completed", {"a": {"status": "failed", "network_outage": True}}, 0, 0, True, "no_successful_source"),
    ("completed", {"a": {"status": "skipped"}, "b": {"status": "paused"}}, 0, 0, True, "no_successful_source"),
    ("completed", {}, 0, 0, False, "no_successful_source"),
    ("failed", {"a": OK}, 5, 3, True, "failed"),
    ("completed", {"a": OK}, 0, 0, False, "first_run_empty"),
    # a skipped source does not count either way
    ("completed", {"a": OK, "b": {"status": "skipped"}}, 5, 2, True, "successful"),
    # nothing new is never claimed when the count is unknown
    ("completed", {"a": OK}, 5, None, True, "successful"),
])
def test_the_six_outcomes(status, by_source, accepted, new, previous, expected) -> None:  # noqa: ANN001
    assert run_outcome(status, by_source, accepted=accepted, stored_new=new, has_previous_list=previous) == expected


@pytest.mark.parametrize(("by_source", "accepted", "new", "expected"), [
    # issue #253: a source that came back empty after returning offers is incomplete, not "nothing new"
    ({"a": OK, "b": {"status": "completed", "suspect_empty": True}}, 5, 0, "partial"),
    ({"a": {"status": "completed", "suspect_empty": True}}, 0, 0, "no_successful_source"),
    ({"a": OK, "b": {"status": "completed", "network_outage": True}}, 5, 1, "partial"),
    # a source that is empty and was empty before is complete
    ({"a": OK, "b": {"status": "completed", "collected": 0}}, 5, 0, "no_new_offers"),
])
def test_suspect_and_offline_sources_are_incomplete(by_source, accepted, new, expected) -> None:  # noqa: ANN001
    assert run_outcome("completed", by_source, accepted=accepted, stored_new=new, has_previous_list=True) == expected


def test_incomplete_sources_are_named_with_their_reason() -> None:
    from src.pipeline.run_outcome import incomplete_sources

    assert incomplete_sources({
        "a": OK, "b": DOWN, "c": {"status": "partial"}, "d": {"status": "completed", "suspect_empty": True},
        "e": {"status": "failed", "network_outage": True}, "f": {"status": "skipped"}, "g": {"status": "paused"},
    }) == {"b": "failed", "c": "partial", "d": "suspect_empty", "e": "network_outage"}


def test_a_source_is_suspect_only_after_returning_offers() -> None:
    from src.pipeline.carryover import mark_suspect_empty

    history = [{"stats": {"by_source": {"a": {"status": "completed", "collected": 4},
                                        "b": {"status": "completed", "collected": 0},
                                        "c": {"status": "skipped", "collected": 0}}}},
               {"stats": {"by_source": {"c": {"status": "completed", "collected": 9}}}}]
    rows = {"a": {"status": "completed", "collected": 0}, "b": {"status": "completed", "collected": 0},
            "c": {"status": "completed", "collected": 0}, "d": {"status": "completed", "collected": 0},
            "e": {"status": "completed", "collected": 0, "from_staging": True},
            "f": {"status": "failed", "collected": 0}}
    assert mark_suspect_empty(rows, history) == ["a", "c"]   # c: its last real run returned 9
    assert "suspect_empty" not in rows["b"] and "suspect_empty" not in rows["d"]


def test_failed_sources_are_named() -> None:
    assert failed_sources({"b": DOWN, "a": {"status": "partial"}, "c": OK, "d": {"status": "skipped"}}) == ["a", "b"]


# -- the visible list ----------------------------------------------------------------------

def _store(db: str, run_id: str, started: str, status: str, outcome: str | None) -> None:
    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=db)
    rs.init_db()
    conn = rs._get_conn()
    stats = "{}" if outcome is None else '{"outcome": "%s"}' % outcome
    conn.execute("INSERT INTO pipeline_runs (id, started_at, status, stats_json) VALUES (?, ?, ?, ?)",
                 (run_id, started, status, stats))
    conn.commit()
    rs.close()


def _visible(db: str, exclude: str | None = None) -> str | None:
    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=db)
    try:
        run = rs.get_latest_visible_run(exclude=exclude)
        return run["id"] if run else None
    finally:
        rs.close()


def test_a_run_where_no_source_succeeded_keeps_the_previous_list(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    _store(db, "old", "2026-11-01T08:00:00+00:00", "completed", None)          # before outcomes existed
    _store(db, "good", "2026-11-02T08:00:00+00:00", "completed", "partial")
    _store(db, "down", "2026-11-03T08:00:00+00:00", "completed", "no_successful_source")
    _store(db, "broken", "2026-11-04T08:00:00+00:00", "failed", "failed")
    assert _visible(db) == "good"
    assert _visible(db, exclude="good") == "old"


def test_an_empty_first_run_and_a_run_with_nothing_new_replace_the_list(tmp_path: Path) -> None:
    db = str(tmp_path / "jobs.db")
    _store(db, "first", "2026-11-01T08:00:00+00:00", "completed", "first_run_empty")
    assert _visible(db) == "first"
    _store(db, "same", "2026-11-02T08:00:00+00:00", "completed", "no_new_offers")
    assert _visible(db) == "same"


# -- in a pipeline run ---------------------------------------------------------------------

class _Source(BaseCollector):
    jobs: list[dict[str, Any]] = []
    errors: list[str] = []

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        self._before_request("https://example.test/search", check_robots=False)  # asked the source (#314)
        return CollectorResult(source_name="src", jobs=list(_Source.jobs), errors=list(_Source.errors))


def _job(job_id: str) -> dict[str, Any]:
    return {"source_job_id": job_id, "url": f"https://example.test/{job_id}", "title": f"Python Developer {job_id}",
            "company": f"Acme {job_id}", "location": "Paris", "description": "Python developer position."}


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch):  # noqa: ANN201
    from src.api.runner import PipelineRunner
    from src.collectors import freeze
    from src.collectors.registry import registry

    monkeypatch.setattr(registry, "get_collector_class", lambda _type: _Source)
    monkeypatch.setattr(freeze, "ENFORCED", False)  # only the fake collector above runs
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(
        "target_domains: [software_development]\ntarget_skills: [python]\n", encoding="utf-8")
    (config_dir / "filters.yaml").write_text(
        "domain_keywords:\n  software_development:\n    title: [developer]\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text("sources:\n  - {name: src, type: src, active: true}\n",
                                              encoding="utf-8")
    db = str(tmp_path / "jobs.db")

    def run(jobs: list[dict[str, Any]], errors: list[str] | None = None) -> tuple[str, dict[str, Any]]:
        from src.storage.runs import RunStorage

        monkeypatch.setattr(_Source, "jobs", jobs)
        monkeypatch.setattr(_Source, "errors", errors or [])
        rs = RunStorage(db_path=db)
        rs.init_db()
        run_id = f"run-{len(rs._get_conn().execute('SELECT id FROM pipeline_runs').fetchall())}"
        rs.create_run(run_id)
        rs.close()
        PipelineRunner(db_path=db, config_dir=str(config_dir))._execute(run_id)   # the product's run path
        rs = RunStorage(db_path=db)
        try:
            return run_id, (rs.get_run(run_id) or {}).get("stats") or {}
        finally:
            rs.close()

    run.db = db  # type: ignore[attr-defined]
    return run


def _visible_run(db: str) -> str | None:
    return _visible(db)


def test_a_run_sequence_records_each_outcome_and_keeps_the_right_list(pipeline) -> None:  # noqa: ANN001
    empty, first = pipeline([])
    assert first["outcome"] == "first_run_empty" and first["stored_new"] == 0
    assert _visible_run(pipeline.db) == empty            # an empty first run is shown, with its reason

    found, good = pipeline([_job("a1"), _job("a2")])
    assert good["outcome"] == "successful" and good["stored_new"] == 2 and good["personalized"] > 0
    assert _visible_run(pipeline.db) == found

    same, again = pipeline([_job("a1"), _job("a2")])
    assert again["outcome"] == "no_new_offers" and again["stored_new"] == 0
    assert _visible_run(pipeline.db) == same             # the same offers, "nothing new"

    _, down = pipeline([], errors=["HTTP 503"])
    assert down["outcome"] == "no_successful_source"
    assert _visible_run(pipeline.db) == same             # the previous list is kept
