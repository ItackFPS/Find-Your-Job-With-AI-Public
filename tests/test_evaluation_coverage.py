"""France Travail web coverage of the API offers (P2.7): matched by offer number, read-only."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.evaluation.coverage import API_SOURCE, WEB_SOURCE, ft_web_coverage
from src.storage.sqlite import Storage

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def _db(tmp_path, rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "jobs.db"
    Storage(db_path=str(path)).init_db()           # the app's own schema and migrations
    conn = sqlite3.connect(path)
    for i, (source, number, days_ago) in enumerate(rows):
        seen = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO job_provenance (job_id, source, source_job_id, url, first_seen_at, last_seen_at) "
                     "VALUES (?, ?, ?, NULL, ?, ?)", (f"job{i}", source, number, seen, seen))
    conn.commit()
    conn.close()
    return path


def test_share_of_api_offers_the_web_collector_also_saw(tmp_path):
    rows = [(API_SOURCE, f"19{n}", n % 7) for n in range(10)]          # 10 API offers over 7 days
    rows += [(WEB_SOURCE, f"19{n}", n % 7) for n in range(6)]           # the web saw 6 of them
    rows += [(WEB_SOURCE, "web-only", 1), (API_SOURCE, "old", 9)]       # not an API offer / outside the window
    before = (tmp_path / "jobs.db").exists()
    r = ft_web_coverage(_db(tmp_path, rows), now=NOW)
    assert not before
    assert (r["status"], r["api_offers"], r["web_offers"], r["found_by_web"], r["days_with_both"]) == ("measured", 10, 7, 6, 6) or \
        (r["status"], r["api_offers"], r["found_by_web"]) == ("too_early", 10, 6)
    assert r["share"] == pytest.approx(0.6) and r["ci95"][0] < 0.6 < r["ci95"][1]


def test_fewer_days_than_the_window_is_too_early(tmp_path):
    rows = [(API_SOURCE, "1", 0), (API_SOURCE, "2", 1), (WEB_SOURCE, "1", 0), (WEB_SOURCE, "2", 1)]
    r = ft_web_coverage(_db(tmp_path, rows), now=NOW)
    assert (r["status"], r["days_with_both"], r["share"]) == ("too_early", 2, 1.0)


def test_missing_sources_are_named(tmp_path):
    assert ft_web_coverage(_db(tmp_path / "a", [(WEB_SOURCE, "1", 0)]), now=NOW)["status"] == "no_api_offers"
    assert ft_web_coverage(_db(tmp_path / "b", [(API_SOURCE, "1", 0)]), now=NOW)["status"] == "no_web_offers"


def test_the_database_is_opened_read_only(tmp_path):
    path = _db(tmp_path, [(API_SOURCE, "1", 0)])
    before = path.read_bytes()
    ft_web_coverage(path, now=NOW)
    assert path.read_bytes() == before


def test_api_reports_the_coverage_of_the_app_database(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import app

    path = _db(tmp_path, [(API_SOURCE, "1", 0), (WEB_SOURCE, "1", 0), (API_SOURCE, "2", 0)])
    saved = app.state.db_path
    app.state.db_path = str(path)
    try:
        r = TestClient(app).get("/api/quality/ft-coverage").json()
    finally:
        app.state.db_path = saved
    assert (r["status"], r["api_offers"], r["found_by_web"]) == ("too_early", 2, 1)
