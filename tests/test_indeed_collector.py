"""IndeedCollector through python-jobspy (plan F6.3): mapping, fixture, budget, errors.

jobspy is replaced by a fake ``scrape`` callable (no network); the live smoke test at the
end carries the ``network`` mark.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from src.collectors.base import BaseCollector
from src.collectors.indeed import IndeedCollector, _map_row, parse_jobspy_rows


class FakeFrame:
    """The two things the collector reads from jobspy's DataFrame."""

    def __init__(self, rows: list[dict], error: str | None = None):
        self._rows = rows
        self.attrs = {"errors": {"indeed": error} if error else {}, "warnings": {}}

    def to_dict(self, orient: str) -> list[dict]:
        assert orient == "records"
        return list(self._rows)


def _row(key: str, title: str = "Développeur Python", **extra) -> dict:
    return {"id": f"in-{key}", "site": "indeed", "job_url": f"https://fr.indeed.com/viewjob?jk={key}",
            "job_url_direct": None, "title": title, "company": "Acme", "location": "Lyon, ARA, France",
            "date_posted": date(2026, 10, 20), "job_type": "fulltime", "min_amount": float("nan"),
            "max_amount": float("nan"), "currency": None, "is_remote": False,
            "description": "<p>offre</p>", "company_url": None, **extra}


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(BaseCollector, "_sleep", staticmethod(lambda s: None))


def _collector(scrape, **cfg) -> IndeedCollector:
    return IndeedCollector({"queries": [{"keyword": "python", "location": "Lyon"}], **cfg}, scrape=scrape)


def test_fixture_rows_map_with_stable_ids_and_urls():
    from src.collectors.base import FIXTURES_DIR

    rows = parse_jobspy_rows((FIXTURES_DIR / "indeed_jobspy.json").read_text(encoding="utf-8"))
    assert len(rows) == 3
    first = rows[0]
    assert first["source_job_id"] == "0a1b2c3d4e5f6a7b"          # jobspy's "in-" prefix dropped
    assert first["canonical_url"] == "https://fr.indeed.com/viewjob?jk=0a1b2c3d4e5f6a7b"
    assert (first["salary_min"], first["salary_max"], first["currency"]) == (42000.0, 50000.0, "EUR")
    assert first["type"] == "full-time" and first["countries"] == ["FR"]
    assert first["published_at"].startswith("2025-10-25")
    assert (rows[1]["work_mode"], rows[1]["remote"]) == ("remote", True)
    assert rows[2]["type"] == "internship" and rows[0]["work_mode"] is None   # unknown, not on-site


def test_nan_and_missing_values_become_none():
    raw = _map_row(_row("k1"))
    assert raw["salary_min"] is None and raw["salary_max"] is None and raw["currency"] == "EUR"
    assert raw["published_at"] == "2026-10-20T00:00:00+00:00"


def test_collect_passes_the_query_to_jobspy_and_dedups_across_queries():
    calls = []

    def scrape(**kw):
        calls.append(kw)
        return FakeFrame([_row("a"), _row("b")])

    c = IndeedCollector({"queries": [{"keyword": "python", "location": "Lyon"}],
                         "discovery_queries": ["data engineer"], "max_pages_per_query": 2,
                         "date_filter_hours": 720}, scrape=scrape)
    result = c.collect()
    assert [j["source_job_id"] for j in result.jobs] == ["a", "b"] and not result.errors
    assert calls[0]["site_name"] == ["indeed"] and calls[0]["description_format"] == "html"
    assert (calls[0]["search_term"], calls[0]["location"], calls[0]["country_indeed"]) == ("python", "Lyon", "france")
    assert calls[0]["results_wanted"] == 200 and calls[0]["hours_old"] == 720
    assert calls[1]["search_term"] == "data engineer" and calls[1]["location"] == "France"
    assert c.health()["requests"] == 4                         # 2 pages budgeted per query


def test_jobspy_error_is_reported_and_the_next_query_still_runs():
    frames = iter([FakeFrame([], error="response status code 403 (blocked: use a proxy)"),
                   FakeFrame([_row("c")])])
    c = IndeedCollector({"queries": [{"keyword": "a"}, {"keyword": "b"}]}, scrape=lambda **kw: next(frames))
    result = c.collect()
    assert [j["source_job_id"] for j in result.jobs] == ["c"]
    assert result.errors == ["indeed [a @ France]: response status code 403 (blocked: use a proxy)"]
    assert c.health()["network_errors"] == 1


def test_an_exception_in_jobspy_never_stops_the_run():
    def boom(**kw):
        raise ValueError("Invalid search_term")

    result = _collector(boom).collect()
    assert result.jobs == [] and "Invalid search_term" in result.errors[0]


def test_empty_answer_is_a_genuine_empty_result():
    c = _collector(lambda **kw: FakeFrame([]))
    assert c.collect().errors == [] and c.health()["empty_results"] == 1


def test_page_budget_stops_the_run_before_jobspy_is_called():
    calls = []
    c = IndeedCollector({"queries": [{"keyword": f"q{i}"} for i in range(200)]},
                        scrape=lambda **kw: calls.append(kw) or FakeFrame([]))
    result = c.collect()
    assert len(calls) == 100                                    # 500 pages / 5 per query
    assert result.errors and "request cap of 500" in result.errors[-1]
    assert c.health()["request_cap_reached"] is True


def test_queries_are_spaced_by_the_rate_limit(monkeypatch):
    waits = []
    monkeypatch.setattr(BaseCollector, "_sleep", staticmethod(waits.append))
    monkeypatch.setattr(BaseCollector, "_clock", staticmethod(lambda: 100.0))
    c = IndeedCollector({"queries": [{"keyword": "a"}, {"keyword": "b"}], "rate_limit_delay": 0.1},
                        scrape=lambda **kw: FakeFrame([]))
    assert c.rate_limit_delay == 2.0                             # the policy floor
    c.collect()
    assert waits == [2.0]


def test_no_queries_is_an_error_without_calling_jobspy():
    result = IndeedCollector({}, scrape=lambda **kw: pytest.fail("called")).collect()
    assert result.errors == ["No queries configured"]


def test_parser_canary_reads_the_fixture():
    fx = IndeedCollector({}).fixture()
    assert fx.parse(fx.content)
    assert parse_jobspy_rows(json.dumps([{"id": None, "title": "x"}])) == []


@pytest.mark.network
def test_live_smoke_indeed_through_jobspy():
    result = IndeedCollector({"queries": [{"keyword": "développeur python", "location": "Lyon"}],
                              "max_jobs_per_query": 5}).collect()
    assert not result.errors, result.errors
    assert result.jobs and all(j["canonical_url"].startswith("https://fr.indeed.com/viewjob?jk=") for j in result.jobs)
