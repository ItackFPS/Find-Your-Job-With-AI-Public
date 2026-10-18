"""QUARANTINED — non-functional LinkedIn Jobs API collector. Not registered.

Phase-NOW / I4 audit (2026-09-13): this collector targets
``https://api.linkedin.com/v2/jobs/search``, which is not a real LinkedIn
public jobs-search endpoint (LinkedIn's actual v2 Partner APIs require a
signed partnership agreement and OAuth scopes this class never requests) —
this code has never been able to return real data. It is also NOT the
project's real LinkedIn integration: that is
``src.linkedin.scanner.LinkedInScanner``, a separate, working subsystem
(python-jobspy-based, staged into the pipeline via
``src.linkedin.staging`` / ``src.api.staged_sources`` — see
``config/linkedin.yaml``). Do not confuse the two.

It is also a genuine base-contract violation independent of the endpoint
issue: ``_normalize_job()`` below constructs a ``Job`` instance directly,
which ``src.collectors.base.BaseCollector``'s contract explicitly forbids
(rule 1 — normalization is the pipeline's job, not a collector's).

Moved out of ``src.collectors`` and removed from
``src.collectors.registry.CollectorRegistry`` so config/sources.yaml has no
way to reference it and no operator can mistake it for working, registered
coverage. Kept (not deleted) only in case a future real LinkedIn Jobs API
partnership makes the request-building code below a useful starting point —
the endpoint and ``_normalize_job`` would both need to be rewritten first.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult
from src.models.job import Job

logger = logging.getLogger(__name__)


class LinkedInCollector(BaseCollector):
    """Collects jobs from LinkedIn Jobs Search API."""

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self.config = config or {}
        self.api_key = self.config.get("api_key")

    def collect(self) -> CollectorResult:
        """Fetch jobs from LinkedIn API."""
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        try:
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"

            response = self._fetch_with_retry(
                "https://api.linkedin.com/v2/jobs/search",
                headers=headers,
            )
            data = response.json()

            # LinkedIn returns a paginated response with 'elements' array
            if isinstance(data, dict) and "elements" in data:
                raw_jobs = data["elements"]
            elif isinstance(data, list):
                raw_jobs = data
            else:
                errors.append(f"Unexpected API response format: {type(data)}")
                return CollectorResult(
                    source_name="linkedin",
                    jobs=jobs,
                    errors=errors,
                    duration_seconds=time.time() - start_time,
                )

            # Collect raw job dicts; normalization happens later in the
            # dedicated NORMALIZATION pipeline stage (src/pipeline/normalize.py).
            for raw in raw_jobs:
                if isinstance(raw, dict):
                    jobs.append(raw)
                else:
                    errors.append(f"Skipping non-dict job entry: {type(raw)}")

        except (httpx.HTTPError, RuntimeError) as exc:
            errors.append(f"Collection failed: {exc}")

        duration = time.time() - start_time
        self._log_collection("linkedin", len(jobs), errors, duration)
        return CollectorResult(
            source_name="linkedin",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    def _normalize_job(self, raw: dict[str, Any]) -> Job | None:
        """Convert LinkedIn API response to a normalized Job model."""
        try:
            # Extract company info
            company = ""
            if "company" in raw and isinstance(raw["company"], dict):
                company_info = raw["company"]
                company = (company_info.get("name") or "").strip()

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
                source="linkedin",
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
            logger.error("Error normalizing LinkedIn job: %s", exc)
            return None


if __name__ == "__main__":
    collector = LinkedInCollector()
    result = collector.collect()
    print(f"Collected {len(result.jobs)} jobs from LinkedIn")