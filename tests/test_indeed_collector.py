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
                         "date_filter_hours": 720, "partition": False}, scrape=scrape)
    result = c.collect()
    assert [j["source_job_id"] for j in result.jobs] == ["a", "b"] and not result.errors
    assert calls[0]["site_name"] == ["indeed"] and calls[0]["description_format"] == "html"
    assert (calls[0]["search_term"], calls[0]["location"], calls[0]["country_indeed"]) == ("python", "Lyon", "france")
    assert calls[0]["results_wanted"] == 200 and calls[0]["hours_old"] == 720
    assert calls[1]["search_term"] == "data engineer" and calls[1]["location"] == "France"
    assert c.health()["requests"] == 2                         # 1 page per query (2 rows each)


def test_jobspy_error_is_reported_and_the_next_query_still_runs():
    frames = iter([FakeFrame([], error="response status code 500"), FakeFrame([_row("c")])])
    c = IndeedCollector({"queries": [{"keyword": "a"}, {"keyword": "b"}], "partition": False},
                        scrape=lambda **kw: next(frames))
    result = c.collect()
    assert [j["source_job_id"] for j in result.jobs] == ["c"]
    assert result.errors == ["indeed [a @ France]: response status code 500"]
    health = c.health()
    assert health["network_errors"] == 0 and health["responses"] == 2   # Indeed answered (500), then 200


def test_a_403_stops_every_remaining_search_of_the_run():
    """#305: Indeed asked to slow down; no further search, said in the errors (never retried)."""
    calls = []

    def scrape(**kw):
        calls.append(kw)
        return FakeFrame([], error="response status code 403 (blocked: use a proxy)")

    c = IndeedCollector({"queries": [{"keyword": "a"}, {"keyword": "b"}]}, scrape=scrape)
    result = c.collect()
    assert len(calls) == 1
    assert result.errors == ["indeed [a @ France]: response status code 403 (blocked: use a proxy)",
                             "indeed: Indeed answered 403/429; the remaining searches of this run were not made"]
    assert result.metadata["blocked"] is True
    assert c.health()["network_errors"] == 0 and c.health()["responses"] == 1


def test_an_exception_in_jobspy_never_stops_the_run():
    def boom(**kw):
        raise ValueError("Invalid search_term")

    result = _collector(boom).collect()
    assert result.jobs == [] and "Invalid search_term" in result.errors[0]


def test_empty_answer_is_a_genuine_empty_result():
    c = _collector(lambda **kw: FakeFrame([]))
    assert c.collect().errors == [] and c.health()["empty_results"] == 1


def test_pages_returned_count_against_the_run_budget():
    calls = []

    def scrape(**kw):
        calls.append(kw)
        return FakeFrame([_row(f"{len(calls)}-{i}") for i in range(250)])   # 3 pages

    c = IndeedCollector({"queries": [{"keyword": f"q{i}"} for i in range(800)], "partition": False},
                        scrape=scrape)
    result = c.collect()
    assert len(calls) == 667                                    # 666 x 3 pages = 1998, then 1 + 2 > 2000
    assert result.errors and "request cap of 2000" in result.errors[-1]
    assert c.health()["request_cap_reached"] is True


def test_proxy_failure_is_no_answer_and_unreadable_rows_raise_the_canary():
    from src.collectors.canary import parser_canary

    c = _collector(lambda **kw: FakeFrame([], error="ProxyError('Tunnel connection failed: 403 Forbidden')"))
    c.collect()
    # "403 Forbidden" from the proxy is not "status code 403" from Indeed
    assert c.health()["network_errors"] == 1 and c.health()["responses"] == 0

    unreadable = [{"identifier": f"x{i}", "headline": "Dev", "blob": "y" * 600} for i in range(5)]
    c = _collector(lambda **kw: FakeFrame(unreadable))
    result = c.collect()
    assert result.jobs == [] and c.health()["empty_results"] == 0
    assert parser_canary(c, 0) == "parser_changed"


def test_jobspy_13_errors_are_read_from_its_log():
    import logging

    def scrape(**kw):                                           # 1.3.x: logs, no attrs
        logging.getLogger("JobSpy:Indeed").error("Indeed response status code 429")
        frame = FakeFrame([])
        frame.attrs = {}
        return frame

    c = _collector(scrape)
    result = c.collect()
    assert result.errors == ["indeed [python @ Lyon]: Indeed response status code 429",
                             "indeed: Indeed answered 403/429; the remaining searches of this run were not made"]
    assert c.health()["last_status"] is None and c.health()["responses"] == 1 and c.health()["empty_results"] == 0


def test_queries_are_spaced_by_the_rate_limit(monkeypatch):
    waits = []
    monkeypatch.setattr(BaseCollector, "_sleep", staticmethod(waits.append))
    monkeypatch.setattr(BaseCollector, "_clock", staticmethod(lambda: 100.0))
    c = IndeedCollector({"queries": [{"keyword": "a"}, {"keyword": "b"}], "rate_limit_delay": 0.1,
                         "partition": False}, scrape=lambda **kw: FakeFrame([]))
    assert c.rate_limit_delay == 2.0                             # the policy floor
    c.collect()
    assert waits == [2.0]                                        # measured from the end of query a


# --- partitions (#305) -------------------------------------------------------------------

def _recording_scrape(answers):
    """A fake jobspy: ``answers(kw) -> list of row keys``; records every call."""
    calls = []

    def scrape(**kw):
        calls.append(kw)
        return FakeFrame([_row(k) for k in answers(kw)])

    return scrape, calls


def test_a_nationwide_search_is_split_by_job_type_then_by_city():
    def answers(kw):
        if kw.get("job_type"):
            return [f"{kw['job_type']}-1", "base-1"]                 # one new offer per job type
        if kw["location"] != "France":
            return [f"{kw['location']}-1"]
        return ["base-1", "base-2"]

    scrape, calls = _recording_scrape(answers)
    c = IndeedCollector({"discovery_queries": ["commercial"], "partition_cities": ["Lyon", "Lille"]},
                        scrape=scrape)
    result = c.collect()
    assert [(k["location"], k.get("job_type"), k.get("distance")) for k in calls] == [
        ("France", None, None),
        ("France", "fulltime", None), ("France", "parttime", None),
        ("France", "internship", None), ("France", "contract", None),
        ("Lyon", None, 25), ("Lille", None, 25)]
    assert len(result.jobs) == 2 + 4 + 2 and not result.errors
    assert [s["new"] for s in result.metadata["searches"]] == [2, 1, 1, 1, 1, 1, 1]


def test_the_profile_home_replaces_the_city_list():
    scrape, calls = _recording_scrape(
        lambda kw: [f"{kw['location']}-{kw.get('job_type')}", "x"])
    c = IndeedCollector({"discovery_queries": ["infirmier"],
                         "local_area": {"name": "Clermont-Ferrand", "radius_km": 50}}, scrape=scrape)
    c.collect()
    assert calls[-1]["location"] == "Clermont-Ferrand" and calls[-1]["distance"] == 32   # 50 km
    assert len(calls) == 1 + 4 + 1


def test_no_local_split_when_job_types_bring_almost_nothing_new():
    """A complete-looking answer (développeur, 2026-11-24: cities added 5 of 344) stops at the job types."""
    base = [f"b{i}" for i in range(100)]
    scrape, calls = _recording_scrape(lambda kw: base if not kw.get("job_type") else base[:10])
    c = IndeedCollector({"discovery_queries": ["développeur"]}, scrape=scrape)
    c.collect()
    assert len(calls) == 1 + 4                                     # no city search


def test_a_base_search_at_its_cap_is_always_split_locally():
    scrape, calls = _recording_scrape(lambda kw: [f"k{i}" for i in range(100)])
    c = IndeedCollector({"discovery_queries": ["vendeur"], "max_pages_per_query": 1,
                         "partition_cities": ["Nantes"]}, scrape=scrape)
    c.collect()
    assert [k["location"] for k in calls][-1] == "Nantes"


def test_searches_with_their_own_location_are_not_split():
    scrape, calls = _recording_scrape(lambda kw: ["a"])
    _collector(scrape).collect()                                   # python @ Lyon
    assert len(calls) == 1


def test_default_cap_is_ten_pages():
    c = IndeedCollector({"discovery_queries": ["x"]}, scrape=lambda **kw: FakeFrame([]))
    assert c.max_jobs_per_query == 1000


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
