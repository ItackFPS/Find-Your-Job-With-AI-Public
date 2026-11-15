"""Collection count terms and yield (Phase B B8-A).

Temporary databases and fake collectors; no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.collectors.base import BaseCollector, CollectorResult
from src.pipeline.counts import COUNT_TERMS, is_useful, source_counts, source_yield


def test_every_term_has_one_key_and_the_funnel_order() -> None:
    terms = [t for t, _ in COUNT_TERMS]
    assert terms == ["requests", "collected", "normalized", "deduplicated", "eligible", "accepted", "retained",
                     "newly_discovered", "useful", "newly_discovered_useful"]
    assert dict(COUNT_TERMS)["normalized"] == "validated" and dict(COUNT_TERMS)["deduplicated"] == "unique"


def test_a_count_the_run_did_not_record_is_unknown_not_zero() -> None:
    counts = source_counts({"collected": 10, "validated": 9, "accepted": 3, "requests": True})
    assert counts["collected"] == 10 and counts["normalized"] == 9 and counts["retained"] == 3
    assert counts["requests"] is None          # a bool is not a count
    assert counts["newly_discovered"] is None and counts["useful"] is None


@pytest.mark.parametrize(("row", "per_request", "per_second"), [
    ({"requests": 4, "seconds": 2.0, "new_useful": 2}, 0.5, 1.0),
    ({"requests": 0, "seconds": 0.0, "new_useful": 0}, None, None),     # no denominator, no yield
    ({"seconds": 4.0, "new_useful": 1}, None, 0.25),                    # requests not counted
    ({"requests": 3, "seconds": 1.0}, None, None),                      # new_useful not recorded
])
def test_yield_is_none_when_a_denominator_or_numerator_is_unknown(row, per_request, per_second) -> None:  # noqa: ANN001
    assert source_yield(row) == {"new_useful_per_request": per_request, "new_useful_per_second": per_second}


def test_useful_is_the_visibility_tier() -> None:
    assert is_useful(0.60) and is_useful(0.95)
    assert not is_useful(0.594) and not is_useful(0.0)


def test_storage_returns_the_ids_stored_for_the_first_time(tmp_path: Path) -> None:
    from src.pipeline.normalize import normalize_jobs
    from src.storage.sqlite import Storage

    jobs = normalize_jobs("src", [_job("a1"), _job("a2")])
    st = Storage(db_path=str(tmp_path / "jobs.db"))
    st.init_db()
    try:
        first = st.upsert_jobs_batch(jobs[:1], return_inserted_ids=True)
        both = st.upsert_jobs_batch(jobs, return_inserted_ids=True)
        plain = st.upsert_jobs_batch(jobs)
    finally:
        st.close()
    assert first["inserted_ids"] == [jobs[0].id]
    assert both["inserted_ids"] == [jobs[1].id]
    assert "inserted_ids" not in plain                     # the default shape is unchanged


# -- in a pipeline run ---------------------------------------------------------------------

class _Source(BaseCollector):
    jobs: list[dict[str, Any]] = []

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        for page in range(2):   # two counted requests; no socket is opened
            self._before_request(f"https://example.test/page/{page}", check_robots=False)
        return CollectorResult(source_name="src", jobs=list(_Source.jobs), errors=[])


class _Uncounted(_Source):
    counts_requests = False


def _job(job_id: str) -> dict[str, Any]:
    return {"source_job_id": job_id, "url": f"https://example.test/{job_id}", "title": f"Python Developer {job_id}",
            "company": f"Acme {job_id}", "location": "Paris",
            "description": "Python developer position, Python and SQL every day."}


@pytest.fixture
def pipeline(tmp_path: Path, monkeypatch):  # noqa: ANN201
    from src.api.runner import PipelineRunner
    from src.collectors import freeze
    from src.collectors.registry import registry

    collector = {"cls": _Source}
    monkeypatch.setattr(registry, "get_collector_class", lambda _type: collector["cls"])
    monkeypatch.setattr(freeze, "ENFORCED", False)  # only the fake collector above runs
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(
        "target_domains: [software_development]\ntarget_skills: [python, sql]\n", encoding="utf-8")
    (config_dir / "filters.yaml").write_text(
        "domain_keywords:\n  software_development:\n    title: [developer]\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text("sources:\n  - {name: src, type: src, active: true}\n",
                                              encoding="utf-8")
    db = str(tmp_path / "jobs.db")

    def run(jobs: list[dict[str, Any]], cls: type = _Source) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        from src.storage.runs import RunStorage

        collector["cls"] = cls
        monkeypatch.setattr(_Source, "jobs", jobs)
        rs = RunStorage(db_path=db)
        rs.init_db()
        run_id = f"run-{len(rs._get_conn().execute('SELECT id FROM pipeline_runs').fetchall())}"
        rs.create_run(run_id)
        rs.close()
        PipelineRunner(db_path=db, config_dir=str(config_dir))._execute(run_id)   # the product's run path
        rs = RunStorage(db_path=db)
        try:
            stats = (rs.get_run(run_id) or {}).get("stats") or {}
            return stats["by_source"]["src"], rs.get_jobs_for_run(run_id, limit=-1)
        finally:
            rs.close()

    return run


def test_a_run_records_requests_new_and_useful_per_source(pipeline) -> None:  # noqa: ANN001
    row, scores = pipeline([_job("a1"), _job("a2")])
    useful = sum(is_useful(s["final_score"]) for s in scores)
    assert row["requests"] == 2
    assert row["accepted"] == len(scores) == 2
    assert row["new"] == 2 and row["useful"] == useful and row["new_useful"] == useful
    assert source_yield(row)["new_useful_per_request"] == (round(useful / 2, 4) if useful else 0.0)

    again, _ = pipeline([_job("a1"), _job("a2"), _job("a3")])
    assert again["new"] == 1                                 # a1 and a2 were already stored
    assert again["new_useful"] <= again["useful"] and again["new_useful"] <= 1


def test_a_collector_that_does_not_count_its_requests_records_none(pipeline) -> None:  # noqa: ANN001
    row, _ = pipeline([_job("b1")], cls=_Uncounted)
    assert "requests" not in row
    assert source_yield(row)["new_useful_per_request"] is None
