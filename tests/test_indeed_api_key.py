"""The Indeed app key is configuration, never source.

It is Indeed's credential (the key of its iOS app), so the public repository does
not ship it: the collector reads INDEED_API_KEY at request time and, without it,
makes no request at all.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.indeed import API_KEY_ENV, IndeedCollector

SOURCE = Path(__file__).resolve().parents[1] / "src" / "collectors" / "indeed.py"


def test_source_ships_no_key():
    text = SOURCE.read_text(encoding="utf-8")
    assert not re.search(r"\b[0-9a-f]{32,}\b", text), "the Indeed key belongs in INDEED_API_KEY"
    assert not re.search(r"""["']indeed-api-key["']\s*:\s*["']""", text)


def test_missing_key_makes_no_request(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    with patch("httpx.post") as post:
        result = IndeedCollector({"queries": [{"keyword": "python", "location": "Paris"}]}).collect()
    post.assert_not_called()
    assert result.jobs == []
    assert result.errors == [f"indeed: {API_KEY_ENV} is not set; see .env.example"]


def test_blank_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "   ")
    with patch("httpx.post") as post:
        result = IndeedCollector({"queries": [{"keyword": "python"}]}).collect()
    post.assert_not_called()
    assert API_KEY_ENV in result.errors[0]


def test_key_from_environment_is_sent(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, " key-from-env ")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {"data": {"jobSearch": {"pageInfo": {"nextCursor": None}, "results": []}}}
    with patch("httpx.post", return_value=resp) as post:
        IndeedCollector({"queries": [{"keyword": "python"}], "rate_limit_delay": 0}).collect()
    assert post.call_args.kwargs["headers"]["indeed-api-key"] == "key-from-env"


def test_rejected_key_names_the_variable(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "revoked")
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 401
    with patch("httpx.post", return_value=resp):
        result = IndeedCollector({"queries": [{"keyword": "python"}], "rate_limit_delay": 0}).collect()
    assert any(f"Update {API_KEY_ENV}" in e for e in result.errors)
