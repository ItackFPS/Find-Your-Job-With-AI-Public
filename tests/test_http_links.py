"""Offer links are http(s) only, from collection to the API (audit D12).

A job board can put any URL in an offer: ``javascript:``, ``data:``, ``file:`` or a custom
protocol would launch something on a click. They are dropped when the offer is normalised
and never served by the API, stored rows included.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.api.schemas import JobOut, ProvenanceOut
from src.models.job import http_url
from src.pipeline.normalize import normalize_job

BAD = ["javascript:alert(1)", "JavaScript:alert(1)", " data:text/html,<b>x</b>", "file:///C:/Windows/notepad.exe",
       "ms-settings:privacy", "vbscript:x", "//evil.example/x", "/relative/path", "https://", "", None, 42]


@pytest.mark.parametrize("value", BAD)
def test_anything_but_an_http_url_is_dropped(value) -> None:
    assert http_url(value) is None


@pytest.mark.parametrize("value", ["https://jobs.example/1", "HTTP://jobs.example/1?a=b", " https://x.example/a "])
def test_http_urls_are_kept(value: str) -> None:
    assert http_url(value) == value.strip()


def test_normalisation_drops_a_dangerous_apply_link() -> None:
    job = normalize_job("lever", {"title": "Développeur", "description": "Python", "source_job_id": "1",
                                  "canonical_url": "https://jobs.example/1", "apply_url": "javascript:alert(1)"})
    assert job is not None
    assert (job.canonical_url, job.apply_url) == ("https://jobs.example/1", None)


def test_the_api_never_serves_a_dangerous_link() -> None:
    out = JobOut(id="1", source="lever", discovered_at=datetime(2026, 10, 30, tzinfo=UTC),
                 canonical_url="data:text/html,x", apply_url="https://jobs.example/1/apply")
    assert (out.canonical_url, out.apply_url) == (None, "https://jobs.example/1/apply")
    seen = ProvenanceOut(source="eures", url="file:///etc/passwd", first_seen_at=datetime(2026, 10, 1, tzinfo=UTC),
                         last_seen_at=datetime(2026, 10, 2, tzinfo=UTC))
    assert seen.url is None
