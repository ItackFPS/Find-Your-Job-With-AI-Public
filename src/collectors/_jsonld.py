"""Shared schema.org/JobPosting JSON-LD parsing helpers.

Used by src/collectors/hellowork.py, which discovers jobs via server-
rendered pages embedding structured data rather than a dedicated API. Pure
functions only -- no network I/O, no browser dependency -- so they are
trivially unit-testable against fixture HTML.

Note: src/collectors/wttj.py implements equivalent JSON-LD parsing logic
independently (it predates this module and is a disabled browser-based
collector). Left untouched here to avoid touching an unrelated, disabled
collector for a shared-abstraction refactor.
"""

from __future__ import annotations

import json
import re
from typing import Any

_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Employment type mapping: schema.org canonical value -> normalizer value.
EMPLOYMENT_TYPE_MAP: dict[str, str] = {
    "FULL_TIME": "full-time",
    "PART_TIME": "part-time",
    "CONTRACTOR": "contract",
    "TEMPORARY": "contract",
    "INTERN": "internship",
    "VOLUNTEER": "contract",
    "PER_DIEM": "contract",
    "OTHER": "contract",
}


def iter_jsonld_blocks(html: str) -> list[Any]:
    """Return every parsed JSON-LD object/array found in the HTML."""
    blocks: list[Any] = []
    for m in _JSON_LD_RE.finditer(html):
        try:
            blocks.append(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValueError):
            continue
    return blocks


def extract_jobposting_ld(html: str) -> dict[str, Any] | None:
    """Return the first schema.org/JobPosting JSON-LD object from HTML, or None."""
    for data in iter_jsonld_blocks(html):
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
    return None


def extract_itemlist_urls(html: str) -> list[str]:
    """Return job-detail URLs from a schema.org/ItemList JSON-LD block, if present."""
    urls: list[str] = []
    for data in iter_jsonld_blocks(html):
        candidates = [data] if isinstance(data, dict) else (data if isinstance(data, list) else [])
        for item in candidates:
            if not isinstance(item, dict) or item.get("@type") != "ItemList":
                continue
            for entry in item.get("itemListElement") or []:
                if isinstance(entry, dict):
                    url = entry.get("url")
                    if isinstance(url, str) and url:
                        urls.append(url)
    return urls


def detect_work_mode(description: str) -> str | None:
    """Return 'remote', 'hybrid', or None based on description text.

    Conservative keyword matching on lowercased text. Only returns 'remote'
    for explicit full-remote signals, 'hybrid' for partial-remote signals.
    Returns None (unknown) rather than assuming on-site.
    """
    text = description.lower()
    full_remote_signals = (
        "100% remote", "fully remote", "full remote",
        "100% télétravail", "full-remote", "télétravail complet",
        "100% en télétravail",
    )
    hybrid_signals = (
        "télétravail", "telework", "remote possible", "hybrid", "hybride",
        "travail à distance", "remote autorisé",
    )
    if any(s in text for s in full_remote_signals):
        return "remote"
    if any(s in text for s in hybrid_signals):
        return "hybrid"
    return None


def map_jobposting_ld_to_raw(ld: dict[str, Any], canonical_url: str, source_job_id: str | None) -> dict[str, Any]:
    """Map a schema.org/JobPosting JSON-LD object to the normalizer raw-dict contract."""
    org: dict[str, Any] = ld.get("hiringOrganization") or {}
    if not isinstance(org, dict):
        org = {}

    raw_locations = ld.get("jobLocation") or []
    if isinstance(raw_locations, dict):
        raw_locations = [raw_locations]
    addr: dict[str, Any] = {}
    if raw_locations and isinstance(raw_locations[0], dict):
        addr = raw_locations[0].get("address") or {}
    if not isinstance(addr, dict):
        addr = {}

    city: str = (addr.get("addressLocality") or "").strip()
    region: str = (addr.get("addressRegion") or "").strip()
    country_code: str = (addr.get("addressCountry") or "").strip()
    if isinstance(addr.get("addressCountry"), dict):
        country_code = (addr["addressCountry"].get("name") or "").strip()
    postal_code: str = str(addr.get("postalCode") or "").strip()

    location_parts = [p for p in [city, region] if p]
    if country_code and country_code not in location_parts:
        location_parts.append(country_code)
    location = ", ".join(location_parts)

    department_code: str | None = None
    if postal_code and country_code == "FR":
        dept = postal_code[:3] if postal_code.startswith("97") else postal_code[:2]
        if dept and (dept.isdigit() or dept in ("2A", "2B")):
            department_code = dept

    raw_emp = ld.get("employmentType")
    if isinstance(raw_emp, list):
        raw_emp = raw_emp[0] if raw_emp else ""
    raw_emp = str(raw_emp or "").strip().upper()
    employment_type: str | None = EMPLOYMENT_TYPE_MAP.get(raw_emp) or (raw_emp.lower() or None)

    description: str = ld.get("description") or ""
    # Prefer the standard schema.org jobLocationType field when present (a
    # structured signal) over free-text keyword matching on the description.
    work_mode: str | None
    location_type = ld.get("jobLocationType")
    if isinstance(location_type, list):
        location_type = location_type[0] if location_type else None
    if isinstance(location_type, str) and "TELECOMMUTE" in location_type.upper():
        # With a workplace given, TELECOMMUTE is not proof of full remote: HelloWork sets it on
        # "Télétravail partiel" jobs. Full remote then needs the description to say so.
        work_mode = "remote" if not city or detect_work_mode(description) == "remote" else "hybrid"
    else:
        work_mode = detect_work_mode(description)

    return {
        "title": (ld.get("title") or "").strip(),
        "company": (org.get("name") or "").strip() or None,
        "description": description,
        "location": location,
        "countries": [country_code] if country_code else [],
        "department_code": department_code,
        "canonical_url": canonical_url,
        "source_job_id": source_job_id,
        "published_at": ld.get("datePosted") or None,
        "type": employment_type,
        "remote": work_mode == "remote",
        "work_mode": work_mode,
        "skills": [],
    }
