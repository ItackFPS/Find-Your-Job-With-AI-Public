"""Normalization layer.

Maps source-specific job data into the unified Job model.
Each collector produces raw data; this module normalizes it.
"""

from __future__ import annotations

import hashlib
import html as _html_module
import logging
import re
from html.parser import HTMLParser
from typing import Any

from src.models.job import Job
from src.requirements.principal_skill import extract_known_technologies

logger = logging.getLogger(__name__)


class _HTMLStripper(HTMLParser):
    """Minimal HTML parser that strips tags and preserves readable text structure."""

    _BLOCK_TAGS = {
        "p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "ul", "ol", "tr", "td", "th", "section", "article",
        "header", "footer", "blockquote",
    }
    _SKIP_TAGS = {"style", "script", "head", "noscript"}

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip: int = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip += 1
        elif not self._skip and t in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in self._SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif not self._skip and t in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _strip_html(text: str) -> str:
    """Strip HTML tags and inline CSS from a description string.

    Preserves paragraph / list structure by inserting newlines at block-level
    tags. Non-HTML strings are returned unchanged.
    """
    if not text or "<" not in text:
        return text
    try:
        stripper = _HTMLStripper()
        stripper.feed(_html_module.unescape(text))
        result = stripper.get_text()
        return result if result else text
    except Exception:
        return text


def normalize_job(source_name: str, raw_data: dict[str, Any]) -> Job:
    """Normalize a single job from any source into the unified model.

    Args:
        source_name: Name of the source (e.g., "greenhouse", "ashby").
        raw_data: Source-specific dictionary containing job fields.

    Returns:
        A normalized Job instance.
    """
    # Extract common fields with sensible defaults.
    # company is intentionally None (not "") when absent — the Job model allows it,
    # and many sources (e.g. France Travail anonymous postings) legitimately omit it.
    _company_raw = raw_data.get("company") or raw_data.get("organization")
    _company_stripped = _company_raw.strip() if _company_raw else ""
    company: str | None = _company_stripped if _company_stripped else None
    title = raw_data.get("title") or raw_data.get("position") or ""
    description = raw_data.get("description") or raw_data.get("summary") or ""

    # Strip HTML from description so the UI always receives clean plain text.
    description = _strip_html(description)

    # Parse location
    location = raw_data.get("location") or raw_data.get("work_location") or ""
    countries = _parse_countries(raw_data.get("countries") or raw_data.get("country") or [])
    # NOTE: the raw contract key is "department_code", not "department" —
    # several sources (e.g. Ashby) already have an unrelated "department"
    # key in their raw payload (org/team department, not geography), and
    # collectors pass their full raw dict through via dict(raw).
    department = raw_data.get("department_code") or None

    # Work mode: normalize any source text to a canonical value.
    # Unknown/missing → None; never infer on_site from absence.
    work_mode = normalize_work_mode(raw_data.get("work_mode"))

    # Parse employment type
    employment_type = _normalize_employment_type(raw_data.get("type") or "")

    # Parse salary
    salary_min = _parse_salary(raw_data.get("salary_min"))
    salary_max = _parse_salary(raw_data.get("salary_max"))
    currency = raw_data.get("currency") or "EUR"

    # Preserve the original job URL — collectors may provide it as 'url',
    # 'canonical_url', or 'apply_url'. We try each in priority order so that
    # the best available link is always surfaced in the UI.
    canonical_url: str | None = (
        raw_data.get("canonical_url") or raw_data.get("url") or None
    )
    # Empty-string sentinel from collectors → treat as absent.
    if not canonical_url:
        canonical_url = None

    apply_url: str | None = raw_data.get("apply_url") or raw_data.get("apply") or None
    if not apply_url:
        apply_url = None

    # Source-specific job identifier (slug, numeric id, …)
    source_job_id: str | None = (
        raw_data.get("source_job_id") or raw_data.get("slug") or None
    )
    if not source_job_id:
        source_job_id = None

    # Skills provided directly by the collector (France Travail, etc.)
    raw_skills = raw_data.get("skills") or []
    skills = _parse_skills(raw_skills) if not isinstance(raw_skills, list) else [
        str(s).strip() for s in raw_skills if str(s).strip()
    ]

    # Sources with no structured skills field (Indeed, EURES, Hellowork,
    # Greenhouse, Lever, most LinkedIn listings) leave `skills` empty —
    # fall back to scanning title+description for known technology mentions
    # rather than storing no skill data at all for the majority of jobs.
    if not skills:
        skills = extract_known_technologies(f"{title} {description}")


    # Publication date — collectors may surface this as 'published_at'
    published_at_raw = raw_data.get("published_at")
    published_at = None
    if published_at_raw:
        from datetime import datetime, timezone
        try:
            if isinstance(published_at_raw, datetime):
                published_at = published_at_raw
            else:
                published_at = datetime.fromisoformat(str(published_at_raw).replace("Z", "+00:00"))
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass

    # Derive a stable, deterministic job ID when the source provides a
    # reliable external identifier.  The same source+source_job_id always
    # produces the same ID, so repeated pipeline runs update the existing DB
    # row via INSERT OR REPLACE instead of inserting a new duplicate row.
    # Jobs with no source_job_id fall back to the UUID default on the model.
    stable_id: str | None = None
    if source_job_id:
        stable_id = hashlib.sha256(
            f"{source_name}:{source_job_id}".encode()
        ).hexdigest()[:32]

    # Build the Job model
    id_kwarg: dict[str, Any] = {"id": stable_id} if stable_id is not None else {}
    job = Job(
        **id_kwarg,
        source=source_name,
        source_job_id=source_job_id,
        company=company,
        title=title.strip(),
        description=description.strip(),
        location=location.strip(),
        countries=countries,
        department=department,
        remote=bool(raw_data.get("remote", False)),
        work_mode=work_mode,
        employment_type=employment_type,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        canonical_url=canonical_url,
        apply_url=apply_url,
        skills=skills,
        published_at=published_at,
    )

    # Store raw payload for debugging and future parsing
    job.raw_payload = raw_data
    job.content_hash = _compute_content_hash(title + " " + description)

    logger.debug("Normalized job from %s: title=%s", source_name, title[:50])
    return job


def normalize_jobs(source_name: str, raw_jobs: list[Any]) -> list[Job]:
    """Normalize a batch of jobs from a single source.

    Accepts either dicts (from collectors) or already-normalized Job objects.
    If a job is already a Job instance, it's returned as-is (no double-normalization).
    """
    results = []
    for raw in raw_jobs:
        try:
            if isinstance(raw, dict):
                job = normalize_job(source_name, raw)
            else:
                # Already normalized - return as-is to avoid double-processing
                job = raw
            results.append(job)
        except (ValueError, TypeError, KeyError) as exc:
            logger.warning("Failed to normalize job from %s: %s", source_name, exc)
    return results


# -- helpers ---------------------------------------------------------------

# Work-mode normalization ---------------------------------------------------

# Sets of exact lowercased tokens that map to each canonical value.
_REMOTE_EXACT: frozenset[str] = frozenset({
    "remote", "full remote", "fully remote", "100% remote",
    "télétravail", "teletravail", "télétravail complet", "teletravail complet",
    "full télétravail", "full teletravail",
    "à distance", "a distance",
    "remote work", "work from home", "wfh",
    "distributed",
})
_HYBRID_EXACT: frozenset[str] = frozenset({
    "hybrid", "hybride",
    "télétravail partiel", "teletravail partiel",
    "partial remote", "remote partiel", "semi-remote",
    "partially remote", "flexible",
})
_ON_SITE_EXACT: frozenset[str] = frozenset({
    "on-site", "onsite", "on site",
    "présentiel", "presentiel",
    "sur site", "in office", "in-office", "bureau",
    "on_site",
})


def normalize_work_mode(raw: str | None) -> str | None:
    """Map any source work-mode text to a canonical value.

    Returns one of 'remote', 'hybrid', 'on_site', or None (unknown).
    IMPORTANT: None (unknown) is never promoted to 'on_site'; absence of
    remote information does not imply on-site.
    """
    if not raw:
        return None
    s = raw.strip().lower()
    s = re.sub(r"\s+", " ", s)

    if s in _REMOTE_EXACT:
        return "remote"
    if s in _HYBRID_EXACT:
        return "hybrid"
    if s in _ON_SITE_EXACT:
        return "on_site"

    # Pattern-based fallbacks for compound strings not in the exact sets.
    if ("100%" in s or "complet" in s) and (
        "remote" in s or "télétravail" in s or "teletravail" in s
    ):
        return "remote"
    if "hybri" in s:  # hybrid, hybride, hybrids
        return "hybrid"
    if "partiel" in s or "partial" in s:
        return "hybrid"
    if "présentiel" in s or "presentiel" in s or "sur site" in s:
        return "on_site"

    return None


# ISO 3166-1 alpha-2 and alpha-3 → canonical full country name.
# Extend as new collectors or country sources are encountered.
_ISO_COUNTRY_CODES: dict[str, str] = {
    # Europe
    "fr": "France", "fra": "France",
    "de": "Germany", "deu": "Germany",
    "gb": "United Kingdom", "gbr": "United Kingdom",
    "uk": "United Kingdom",
    "es": "Spain", "esp": "Spain",
    "it": "Italy", "ita": "Italy",
    "be": "Belgium", "bel": "Belgium",
    "nl": "Netherlands", "nld": "Netherlands",
    "ch": "Switzerland", "che": "Switzerland",
    "at": "Austria", "aut": "Austria",
    "pl": "Poland", "pol": "Poland",
    "pt": "Portugal", "prt": "Portugal",
    "se": "Sweden", "swe": "Sweden",
    "no": "Norway", "nor": "Norway",
    "dk": "Denmark", "dnk": "Denmark",
    "fi": "Finland", "fin": "Finland",
    "ie": "Ireland", "irl": "Ireland",
    "lu": "Luxembourg", "lux": "Luxembourg",
    "ro": "Romania", "rou": "Romania",
    "cz": "Czech Republic", "cze": "Czech Republic",
    "hu": "Hungary", "hun": "Hungary",
    "sk": "Slovakia", "svk": "Slovakia",
    "hr": "Croatia", "hrv": "Croatia",
    # North America
    "us": "United States", "usa": "United States",
    "ca": "Canada", "can": "Canada",
    # Other
    "au": "Australia", "aus": "Australia",
    "in": "India", "ind": "India",
    "sg": "Singapore", "sgp": "Singapore",
    "jp": "Japan", "jpn": "Japan",
    "br": "Brazil", "bra": "Brazil",
    "mx": "Mexico", "mex": "Mexico",
}


def normalize_country_code(value: str) -> str:
    """Return the canonical full country name for an ISO code, or the input unchanged.

    Handles ISO 3166-1 alpha-2 (FR) and alpha-3 (FRA) codes, case-insensitively.
    Full country names (e.g. 'France') pass through unchanged.
    """
    return _ISO_COUNTRY_CODES.get(value.lower().strip(), value.strip())


def _parse_countries(data: Any) -> list[str]:
    """Extract country list from various formats, normalizing ISO codes to full names."""
    if isinstance(data, str):
        return [
            normalize_country_code(c)
            for c in data.replace("|", ",").split(",")
            if c.strip()
        ]
    if isinstance(data, (list, tuple)):
        return [
            normalize_country_code(str(c))
            for c in data
            if str(c).strip()
        ]
    return []


def _normalize_employment_type(raw: str) -> str | None:
    """Normalize employment type to canonical values."""
    raw = raw.lower().strip()
    mapping = {
        "full-time": "full-time",
        "ft": "full-time",
        "part-time": "part-time",
        "pt": "part-time",
        "contract": "contract",
        "freelance": "contract",
        "internship": "internship",
    }
    return mapping.get(raw, None)


def _parse_salary(value: Any) -> float | None:
    """Parse salary value into a float."""
    if value is None:
        return None
    try:
        # Handle strings like "$50k", "€60,000", "50000"
        cleaned = str(value).replace("$", "").replace("€", "").replace(",", "")
        if "k" in cleaned.lower():
            return float(cleaned.lower().replace("k", "")) * 1000
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _parse_skills(data: Any) -> list[str]:
    """Extract skills from various formats."""
    if isinstance(data, str):
        # Comma-separated or pipe-separated
        return [s.strip() for s in data.replace("|", ",").split(",") if s.strip()]
    if isinstance(data, (list, tuple)):
        return [str(s).strip() for s in data if str(s).strip()]
    return []


def _compute_content_hash(text: str) -> str:
    """Simple content hash using Python's built-in hash (for MVP)."""
    return hashlib.md5(text.encode()).hexdigest()
