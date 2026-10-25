"""Remote job board collector.

Fetches jobs from remote job boards. Dispatches on the configured source
``type``:

- ``rss``: fetch the configured ``careers_url`` and parse it as an RSS/Atom
  feed (e.g. We Work Remotely: https://weworkremotely.com/jobs/rss).
- anything else: treat ``careers_url`` as a JSON REST API endpoint (existing
  behavior, preserved for any future non-RSS "remote" source), also
  requiring a configured URL rather than a hardcoded/guessed one.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import feedparser
import httpx

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture
from src.models.job import Job

logger = logging.getLogger(__name__)


class RemoteCollector(BaseCollector):
    """Collects jobs from remote job boards (RSS feeds or JSON APIs)."""

    # public_page: a job board's RSS feed or JSON endpoint whose URL the user configures
    # (careers_url). Nothing says it is meant for reuse, so robots.txt is checked live.
    access_class = "public_page"
    robots_policy = True
    # One feed or endpoint per run.
    rate_policy = RatePolicy(min_delay_seconds=1.0, max_requests_per_run=20)

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("remote_feed.rss", self._load_fixture("remote_feed.rss"), parse_rss_feed)

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self.config = config or {}
        self.api_key = self.config.get("api_key")
        self.careers_url = self.config.get("careers_url")
        self.source_type = str(self.config.get("type") or "").lower()

    def collect(self) -> CollectorResult:
        """Fetch jobs from the configured remote job board.

        The configured source is always the source of truth: this method
        never falls back to a hardcoded URL. If ``careers_url`` is missing,
        collection is skipped with a clear error instead of guessing one.
        """
        start_time = time.time()

        if self.source_type == "rss":
            jobs, errors = self._collect_rss()
        else:
            jobs, errors = self._collect_api()

        duration = time.time() - start_time
        self._log_collection("remote", len(jobs), errors, duration)
        return CollectorResult(
            source_name="remote",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    # -- RSS ------------------------------------------------------------------

    def _collect_rss(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch and parse the configured RSS feed (e.g. We Work Remotely)."""
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        if not self.careers_url:
            errors.append(
                "RSS collector requires a configured 'careers_url'; "
                "none was provided in the source configuration. Skipping collection."
            )
            return jobs, errors

        try:
            response = self._fetch_with_retry(
                self.careers_url,
                headers={"Accept": "application/rss+xml, application/xml, text/xml"},
            )
        except (httpx.HTTPError, RuntimeError) as exc:
            errors.append(f"Collection failed: {exc}")
            return jobs, errors

        parsed = feedparser.parse(response.text)

        if getattr(parsed, "bozo", False) and not parsed.entries:
            bozo_exc = getattr(parsed, "bozo_exception", "unknown parse error")
            errors.append(f"Failed to parse RSS feed from {self.careers_url}: {bozo_exc}")
            return jobs, errors

        if not parsed.entries:
            self._note_empty_result()  # a well-formed feed with no item
        for entry in parsed.entries:
            jobs.append(self._rss_entry_to_raw_job(entry))

        return jobs, errors

    @staticmethod
    def _rss_entry_to_raw_job(entry: Any) -> dict[str, Any]:
        """Convert a feedparser RSS entry into a generic raw job dict.

        The dict uses the field names expected by the pipeline's generic
        normalizer (src/pipeline/normalize.py), not a per-source schema.
        """
        raw_title = str(getattr(entry, "title", "") or "").strip()

        # We Work Remotely (and similar boards) format entry titles as
        # "Company: Job Title". Split it out when that pattern is present;
        # otherwise fall back to using the whole string as the title.
        company = ""
        title = raw_title
        if ":" in raw_title:
            maybe_company, _, maybe_title = raw_title.partition(":")
            if maybe_company.strip() and maybe_title.strip():
                company = maybe_company.strip()
                title = maybe_title.strip()

        description = str(getattr(entry, "summary", "") or getattr(entry, "description", "") or "")
        link = str(getattr(entry, "link", "") or "")
        entry_id = str(getattr(entry, "id", "") or link)

        categories: list[str] = []
        for tag in getattr(entry, "tags", []) or []:
            term = tag.get("term") if isinstance(tag, dict) else None
            if term:
                categories.append(str(term))

        return {
            "title": title,
            "company": company,
            "description": description,
            "location": "Remote",
            "remote": True,
            "url": link,
            "source_job_id": entry_id,
            "categories": categories,
        }

    # -- JSON REST API (non-RSS remote sources) --------------------------------

    def _collect_api(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch jobs from a configured JSON REST API endpoint.

        Preserves the pre-existing JSON-API collection behavior for any
        "remote"-type source that isn't RSS, but — like the RSS path —
        always uses the configured URL rather than a hardcoded one.
        """
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        if not self.careers_url:
            errors.append(
                "Remote API collector requires a configured 'careers_url'; "
                "none was provided in the source configuration. Skipping collection."
            )
            return jobs, errors

        try:
            headers = {"Accept": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = self._fetch_with_retry(self.careers_url, headers=headers)
            data = response.json()

            # Remote API returns a list of job objects
            if isinstance(data, list):
                raw_jobs = data
            else:
                errors.append(f"Unexpected API response format: {type(data)}")
                return jobs, errors

            # Collect raw job dicts; normalization happens later in the
            # dedicated NORMALIZATION pipeline stage (src/pipeline/normalize.py).
            for raw in raw_jobs:
                if isinstance(raw, dict):
                    jobs.append(raw)
                else:
                    errors.append(f"Skipping non-dict job entry: {type(raw)}")

        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"Collection failed: {exc}")

        return jobs, errors

    def _normalize_job(self, raw: dict[str, Any]) -> Job | None:
        """Convert Remote API response to a normalized Job model."""
        try:
            # Extract company info
            company = ""
            if "company" in raw and isinstance(raw["company"], dict):
                company = (raw["company"].get("name") or "").strip()

            # Extract location info
            location = ""
            countries = []
            if "location" in raw and isinstance(raw["location"], dict):
                loc_data = raw["location"]
                city = loc_data.get("city") or loc_data.get("address", "")
                country = loc_data.get("country") or loc_data.get("region", "")
                location = (city or "").strip()
                if country:
                    countries.append(country)

            # Extract salary info
            salary_min = None
            salary_max = None
            currency = "USD"
            if "salary_range" in raw and isinstance(raw["salary_range"], dict):
                range_data = raw["salary_range"]
                min_val = range_data.get("min")
                max_val = range_data.get("max")
                if min_val is not None:
                    salary_min = float(min_val)
                if max_val is not None:
                    salary_max = float(max_val)

            # Extract skills
            skills = []
            if "skills" in raw and isinstance(raw["skills"], list):
                for skill in raw["skills"]:
                    if isinstance(skill, dict):
                        skills.append(skill.get("name", ""))
                    elif isinstance(skill, str):
                        skills.append(skill)

            # Build the Job model
            job = Job(
                source="remote",
                company=company.strip(),
                title=(raw.get("title") or "").strip(),
                description=(raw.get("description") or "").strip(),
                location=location.strip(),
                countries=countries,
                remote=bool(raw.get("remote")),
                employment_type=(raw.get("employment_type") or "").strip(),
                salary_min=salary_min,
                salary_max=salary_max,
                salary_currency=currency,
            )

            # Store raw payload for debugging
            job.raw_payload = raw
            return job

        except (KeyError, TypeError, ValueError) as exc:
            logger.error("Error normalizing Remote job: %s", exc)
            return None


if __name__ == "__main__":
    import time
    collector = RemoteCollector()
    result = collector.collect()
    print(f"Collected {len(result.jobs)} jobs from Remote")


def parse_rss_feed(text: str) -> list[dict[str, Any]]:
    """The offers in one RSS feed, mapped like the RSS mode of collect() maps them."""
    return [RemoteCollector._rss_entry_to_raw_job(entry) for entry in feedparser.parse(text).entries]
