"""SmartRecruiters job board collector.

Fetches job postings from a single company's public SmartRecruiters
Posting API: https://api.smartrecruiters.com/v1/companies/{company}/postings

No authentication is required, but the public postings feed is opt-in per
customer (only enabled on some SmartRecruiters plans), and the exact
``companyIdentifier`` (case-sensitive) must be known in advance — there is
no directory/list endpoint to discover it.

Verified 2026-08-27: identifiers must match SmartRecruiters' own casing
(e.g. "BoschGroup", "Believe") — plain company names often return HTTP 200
with an empty result set rather than 404, so a working identifier can look
"valid" while returning zero postings.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_POSTINGS_URL_TEMPLATE = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
_JOB_URL_TEMPLATE = "https://jobs.smartrecruiters.com/{company}/{posting_id}"


class SmartRecruitersCollector(BaseCollector):
    """Collects jobs from a single company's SmartRecruiters postings feed."""

    # official: SmartRecruiters' documented public Posting API, enabled per customer.
    access_class = "official"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self.config = config or {}
        self.company_identifier = self.config.get("board_identifier") or self.config.get("company_identifier")
        self.max_jobs: int | None = self.config.get("max_jobs")
        # Optional: restrict collection to specific countries via the API's
        # 'country' filter (verified 2026-08-27: it accepts exactly one
        # ISO-3166-1 alpha-2 code per request; multiple values are AND-ed
        # together and yield zero results, so multi-country coverage requires
        # one paginated request sequence per code). Intended for very large,
        # geographically broad boards (e.g. BoschGroup) where most postings
        # fall outside this project's target geography. Names are matched
        # case-insensitively against _COUNTRY_CODE_MAP.
        self.countries: list[str] | None = self.config.get("countries")

    def collect(self) -> CollectorResult:
        """Fetch jobs from the configured SmartRecruiters company feed.

        Requires ``board_identifier`` (the company's SmartRecruiters
        identifier) to be set in the source configuration. Without it,
        collection is skipped with a clear error.
        """
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []

        if not self.company_identifier:
            errors.append(
                "SmartRecruiters collector requires a configured "
                "'board_identifier' (the company's SmartRecruiters "
                "identifier). None was provided in the source "
                "configuration; skipping collection."
            )
            duration = time.time() - start_time
            self._log_collection("smartrecruiters", 0, errors, duration)
            return CollectorResult(
                source_name="smartrecruiters", jobs=jobs, errors=errors, duration_seconds=duration,
            )

        url = _POSTINGS_URL_TEMPLATE.format(company=self.company_identifier)

        try:
            if self.countries:
                codes = _country_names_to_codes(self.countries)
                for code in codes:
                    self._collect_pages(url, jobs, errors, extra_params={"country": code})
                    if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                        break
            else:
                self._collect_pages(url, jobs, errors, extra_params={})

        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            errors.append(f"Collection failed: {exc}")

        duration = time.time() - start_time
        self._log_collection("smartrecruiters", len(jobs), errors, duration)
        return CollectorResult(
            source_name="smartrecruiters", jobs=jobs, errors=errors, duration_seconds=duration,
        )

    def _collect_pages(
        self,
        url: str,
        jobs: list[dict[str, Any]],
        errors: list[str],
        extra_params: dict[str, str],
    ) -> None:
        """Paginate through one filtered postings feed, appending into ``jobs``."""
        limit = 100
        offset = 0
        while True:
            response = self._fetch_with_retry(
                url,
                headers={"Accept": "application/json"},
                params={"limit": limit, "offset": offset, **extra_params},
            )
            data = response.json()

            if not isinstance(data, dict) or "content" not in data:
                errors.append(f"Unexpected API response format: {type(data)}")
                return

            content = data.get("content") or []
            for raw in content:
                if isinstance(raw, dict):
                    jobs.append(self._normalize_fields(raw, self.company_identifier))
                else:
                    errors.append(f"Skipping non-dict job entry: {type(raw)}")
                if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                    return

            total_found = data.get("totalFound", 0)
            offset += limit
            if len(content) < limit or offset >= total_found:
                return

    @staticmethod
    def _normalize_fields(raw: dict[str, Any], company_identifier: str | None) -> dict[str, Any]:
        """Map SmartRecruiters Posting API field names to the normalizer contract."""
        out: dict[str, Any] = dict(raw)
        out["title"] = raw.get("name") or ""

        company = raw.get("company") or {}
        out["company"] = company.get("name") or company_identifier or ""

        location = raw.get("location") or {}
        city = location.get("city") or ""
        full_location = location.get("fullLocation") or city
        out["location"] = full_location or ""
        out["remote"] = bool(location.get("remote"))

        country_code = str(location.get("country") or "").upper()
        out["countries"] = [_COUNTRY_CODE_MAP.get(country_code, country_code)] if country_code else []

        employment = raw.get("typeOfEmployment") or {}
        emp_label = str(employment.get("label") or "").lower()
        if "full" in emp_label:
            out["employment_type"] = "full-time"
        elif "part" in emp_label:
            out["employment_type"] = "part-time"
        elif "intern" in emp_label:
            out["employment_type"] = "internship"
        elif "contract" in emp_label or "temporary" in emp_label:
            out["employment_type"] = "contract"

        posting_id = str(raw.get("id") or "")
        out["source_job_id"] = posting_id
        if company_identifier and posting_id:
            out["url"] = _JOB_URL_TEMPLATE.format(company=company_identifier, posting_id=posting_id)

        out["published_at"] = raw.get("releasedDate") or ""
        return out


_COUNTRY_CODE_MAP: dict[str, str] = {
    "fr": "France", "de": "Germany", "gb": "United Kingdom", "uk": "United Kingdom",
    "es": "Spain", "it": "Italy", "nl": "Netherlands", "be": "Belgium",
    "pt": "Portugal", "pl": "Poland", "se": "Sweden", "ch": "Switzerland",
    "at": "Austria", "ie": "Ireland", "lu": "Luxembourg", "dk": "Denmark",
    "fi": "Finland", "no": "Norway", "cz": "Czech Republic", "us": "United States",
    "ro": "Romania", "hu": "Hungary", "gr": "Greece", "hr": "Croatia",
    "sk": "Slovakia", "si": "Slovenia", "bg": "Bulgaria", "ee": "Estonia",
    "lv": "Latvia", "lt": "Lithuania", "mt": "Malta", "cy": "Cyprus",
}
_COUNTRY_CODE_MAP.update({k.upper(): v for k, v in list(_COUNTRY_CODE_MAP.items())})

# Reverse of _COUNTRY_CODE_MAP (country name, lowercased -> lowercase code),
# used to translate a source-config `countries` list (full names, as written
# in config/sources.yaml) into the codes the Posting API's `country` filter
# expects.
_COUNTRY_NAME_TO_CODE: dict[str, str] = {
    name.lower(): code.lower() for code, name in _COUNTRY_CODE_MAP.items() if code.islower()
}


def _country_names_to_codes(names: list[str]) -> list[str]:
    """Map configured country names to API country codes, skipping unknowns."""
    codes = []
    for name in names:
        code = _COUNTRY_NAME_TO_CODE.get(str(name).lower())
        if code:
            codes.append(code)
        else:
            logger.warning("Unknown country name in 'countries' config, skipping: %s", name)
    return codes


if __name__ == "__main__":
    collector = SmartRecruitersCollector(config={"board_identifier": "Believe"})
    result = collector.collect()
    print(f"Collected {len(result.jobs)} jobs from SmartRecruiters")
