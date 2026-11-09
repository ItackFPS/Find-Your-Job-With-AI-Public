"""Employers' boards found from stored offers and read through public APIs (mocked)."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.ats_boards import AtsBoardsCollector, map_ashby, map_recruitee
from src.collectors.registry import CollectorRegistry
from src.discovery.ats_boards import boards_from_offers, boards_in_text


def test_links_name_the_board() -> None:
    assert boards_in_text("Postuler: https://jobs.lever.co/acme/123 ou https://jobs.ashbyhq.com/Mistral") == {
        ("lever", "acme"), ("ashby", "mistral")}
    assert boards_in_text("https://acme.recruitee.com/o/x") == {("recruitee", "acme")}
    assert boards_in_text("https://boards.greenhouse.io/acme/jobs/1") == {("greenhouse", "acme")}
    assert boards_in_text("https://api.lever.co/v0/postings") == set()


def _db(path: Path, rows: list[tuple[str, str]]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (canonical_url TEXT, apply_url TEXT, description TEXT, raw_payload TEXT)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?, '', '{}')", rows)
    conn.commit()
    conn.close()
    return path


def test_boards_from_offers_ranks_by_how_often_they_appear(tmp_path: Path) -> None:
    db = _db(tmp_path / "t.db", [
        ("https://jobs.lever.co/acme/1", ""), ("https://jobs.lever.co/acme/2", ""),
        ("https://x", "https://jobs.smartrecruiters.com/beta/3"), ("https://nothing.example/1", ""),
    ])
    assert boards_from_offers(db) == [{"ats": "lever", "slug": "acme"}, {"ats": "smartrecruiters", "slug": "beta"}]


def test_unreadable_database_gives_no_boards(tmp_path: Path) -> None:
    assert boards_from_offers(tmp_path / "absent.db") == []


def test_ashby_mapping() -> None:
    job = map_ashby({"id": "1", "title": "Technicien", "location": "Paris, France", "jobUrl": "https://a/1",
                     "descriptionPlain": "Texte", "publishedAt": "2026-11-01T10:00:00Z", "isRemote": False}, "acme")
    assert job["title"] == "Technicien" and job["countries"] == ["France"] and job["published_at"].year == 2026
    assert job["company"] == "Acme" and job["description"] == "Texte"


def test_recruitee_mapping() -> None:
    job = map_recruitee({"id": 7, "title": "Comptable", "city": "Lyon", "country": "France", "company_name": "Beta",
                         "careers_url": "https://b/7", "description": "A", "requirements": "B", "remote": True}, "beta")
    assert job["countries"] == ["France"] and job["company"] == "Beta" and job["work_mode"] == "remote"
    assert job["description"] == "A B" and job["source_job_id"] == "7"


def _response(payload: object) -> MagicMock:
    response = MagicMock()
    response.json.return_value = payload
    return response


def test_no_board_is_not_a_failure() -> None:
    result = AtsBoardsCollector({"boards": []}).collect()
    assert result.jobs == [] and result.errors == []


def test_collect_reads_each_board_and_keeps_going_after_a_failure() -> None:
    collector = AtsBoardsCollector({"boards": [{"ats": "ashby", "slug": "gone"}, {"ats": "ashby", "slug": "acme"}],
                                    "rate_limit_delay": 0})
    request = httpx.Request("GET", "https://api.ashbyhq.com/x")

    def fetch(url: str, **_: object) -> MagicMock:
        if url.endswith("/gone"):
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        return _response({"jobs": [{"id": "1", "title": "Technicien", "location": "Paris"},
                                   {"id": "2", "title": "Masqué", "isListed": False}]})

    with patch.object(AtsBoardsCollector, "_fetch_with_retry", side_effect=fetch), \
            patch.object(AtsBoardsCollector, "_enforce_rate_limit"):
        result = collector.collect()
    assert [j["title"] for j in result.jobs] == ["Technicien"]
    assert len(result.errors) == 1 and "ashby/gone" in result.errors[0]


def test_lever_board_is_read_by_the_lever_collector() -> None:
    collector = AtsBoardsCollector({"boards": [{"ats": "lever", "slug": "acme"}], "rate_limit_delay": 0})
    with patch("src.collectors.lever.LeverCollector._fetch_with_retry",
               return_value=_response([{"id": "1", "text": "Admin", "categories": {"location": "Paris"}}])), \
            patch("src.collectors.lever.LeverCollector._enforce_rate_limit"), \
            patch.object(AtsBoardsCollector, "_enforce_rate_limit"):
        result = collector.collect()
    assert [j["title"] for j in result.jobs] == ["Admin"] and result.errors == []


def test_registered() -> None:
    assert CollectorRegistry().get_collector_class("ats_boards") is AtsBoardsCollector
