"""JoobleCollector (#312): Jooble's official REST API with the user's France key. No network."""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from src.collectors.base import FIXTURES_DIR, BaseCollector
from src.collectors.jooble import JoobleCollector, map_job, parse_search_response


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(BaseCollector, "_sleep", staticmethod(lambda s: None))


def _answer(jobs: list[dict], total: int | None = None, status: int = 200) -> httpx.Response:
    body = {"totalCount": len(jobs) if total is None else total, "jobs": jobs}
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://fr.jooble.org/api/k"))


def _job(i: int, title: str = "Infirmier") -> dict:
    return {"id": i, "title": title, "company": "Clinique", "location": "Lyon (69)", "snippet": "<b>Soins</b>",
            "salary": "", "source": "x", "type": "CDI", "link": f"https://fr.jooble.org/desc/{i}",
            "updated": "2026-11-20T08:00:00"}


def test_fixture_rows_map_to_the_pipeline_contract():
    rows = parse_search_response((FIXTURES_DIR / "jooble_search.json").read_text(encoding="utf-8"))
    assert len(rows) == 2
    first = rows[0]
    assert first["title"] == "Infirmier diplômé d'État H/F"              # tags and entities removed
    assert first["description"].startswith("...Vous rejoignez")
    assert (first["type"], first["countries"], first["company"]) == ("full-time", ["FR"], "Centre hospitalier exemple")
    assert first["published_at"].startswith("2026-11-20T08:15:00")
    assert rows[1]["type"] == "full-time" and rows[1]["salary_text"] is None


def test_without_a_key_nothing_is_requested():
    with patch("src.collectors.jooble.httpx.post") as post:
        result = JoobleCollector({"discovery_queries": ["infirmier"]}).collect()
    post.assert_not_called()
    assert result.jobs == [] and "no API key" in result.errors[0]


def test_queries_page_until_a_short_page_and_dedup_by_id():
    answers = [_answer([_job(i) for i in range(3)], total=5), _answer([_job(2), _job(3)], total=5)]
    with patch("src.collectors.jooble.httpx.post", side_effect=answers) as post:
        result = JoobleCollector({"api_key": "k", "discovery_queries": ["infirmier"], "results_per_page": 3,
                                  "max_pages_per_query": 5}).collect()
    assert [j["source_job_id"] for j in result.jobs] == ["0", "1", "2", "3"]
    assert post.call_count == 2
    body = post.call_args_list[0].kwargs["json"]
    assert body == {"keywords": "infirmier", "location": "France", "page": 1, "ResultOnPage": 3}
    assert post.call_args_list[0].args[0] == "https://fr.jooble.org/api/k"
    assert result.metadata["per_query_status"]["infirmier @ France"]["stop"] == "exhausted"


def test_the_request_budget_is_respected():
    with patch("src.collectors.jooble.httpx.post", return_value=_answer([_job(i) for i in range(2)])) as post:
        result = JoobleCollector({"api_key": "k", "discovery_queries": ["a", "b", "c"], "results_per_page": 2,
                                  "max_pages_per_query": 3, "max_requests": 4}).collect()
    assert post.call_count == 4 and result.metadata["requests"] == 4


@pytest.mark.parametrize(("status", "words"), [(403, "refused the key"), (429, "daily quota")])
def test_a_refused_key_or_spent_quota_stops_every_search(status, words):
    with patch("src.collectors.jooble.httpx.post", return_value=_answer([], status=status)) as post:
        result = JoobleCollector({"api_key": "k", "discovery_queries": ["a", "b"]}).collect()
    assert post.call_count == 1
    assert len(result.errors) == 1 and words in result.errors[0]


def test_a_failed_page_is_reported_and_the_next_query_runs():
    answers = [httpx.ConnectError("down"), _answer([_job(9)])]
    with patch("src.collectors.jooble.httpx.post", side_effect=answers):
        result = JoobleCollector({"api_key": "k", "discovery_queries": ["a", "b"]}).collect()
    assert [j["source_job_id"] for j in result.jobs] == ["9"]
    assert len(result.errors) == 1 and "Jooble [a @ France] page 1" in result.errors[0]


def test_malformed_answer_maps_to_nothing():
    assert parse_search_response(json.dumps({"jobs": "x"})) == []
    assert map_job({"id": 1, "title": "<i></i>"})["title"] == ""
