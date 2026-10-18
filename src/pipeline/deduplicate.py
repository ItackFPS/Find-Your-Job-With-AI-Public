"""Deduplication layer.

Handles intra-source and cross-source deduplication of job listings.

Identity hierarchy
------------------
Intra-source (same ``source`` value):
  1. (source, source_job_id)   — definite duplicate; source guarantees uniqueness.
  2. (source, canonical_url)   — definite duplicate when source_job_id is absent.
  3. Neither available         — keep the job; do not collapse on title or company.

Cross-source (across different ``source`` values):
  1. canonical_url match       — URL is a globally unique identifier.
  2. content_hash match        — identical hashed title+description text.
  3. (company_norm, title_norm, location_norm, employment_type)
                               — content identity, but ONLY when company is non-empty.
                                 Jobs from anonymous/unknown employers are never merged
                                 on content alone.

Two jobs are NOT duplicates solely because company + title match.
Different locations are treated as distinct postings (Part 2: multi-location jobs
are preserved separately unless a stronger signal collapses them).
Different employment types are treated as distinct postings.

Source attribution
------------------
When a cross-source duplicate is found, the duplicate's ``source`` is appended
to the kept job's ``sources`` list so that provenance is preserved.
The ``source`` field on the kept job always reflects where it was first seen.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Callable

from src.models.job import Job

logger = logging.getLogger(__name__)

# Regex for common French/English gender markers appended to job titles.
# Stripped before the location-agnostic dedup key so that:
#   "Développeur(euse) web junior" == "Développeur web junior"
#   "Technicien H/F" == "Technicien"
_GENDER_MARKER_RE = re.compile(
    r"\s*[\(\[]\s*(?:h|f|m|x|he|she|they|e|se|euse|eur|rice|trice)\s*/?"
    r"\s*(?:f|h|m|x|she|he|they|e|se|euse|eur|rice|trice)?\s*[\)\]]"
    r"|\s*\(?(?:H|F|M|X)/(?:F|H|M|X)\)?"
    r"|\s*\((?:e|se|euse|eur|rice|trice)\)",
    re.IGNORECASE,
)

# Legal entity suffixes to strip from company names before comparison.
# Anchored to end-of-string after basic cleanup.
_COMPANY_LEGAL_SUFFIX_RE = re.compile(
    # Longer alternatives first so the engine does not match a short prefix of a
    # longer suffix (e.g. "sa" in "sas").  s\.?a\.? covers both "SA" and "S.A.".
    r"\b(?:s\.?a\.?s\.?|sarl|eurl|sca|sci|snc|s\.?a\.?|inc\.?|llc\.?|ltd\.?|"
    r"gmbh|ag|nv|bv|pty\.?|corp\.?|co\.?|group|groupe|international|france)\b\.?$",
    re.IGNORECASE,
)


def _norm_url(url: str) -> str:
    return url.lower().strip()


def _norm_str(s: str | None) -> str:
    return s.lower().strip() if s else ""


def _strip_accents(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _norm_title_simple(title: str | None) -> str:
    """Return a simplified title key: lowercase, accent-stripped, gender markers removed.

    Used for the location-agnostic cross-source dedup tier so that
    "Développeur(euse) web junior" and "Développeur web junior" from different
    sources collapse to the same key.
    """
    if not title:
        return ""
    without_markers = _GENDER_MARKER_RE.sub("", title).strip()
    ascii_only = _strip_accents(without_markers)
    # Collapse punctuation/extra whitespace
    ascii_only = re.sub(r"[^\w\s]", " ", ascii_only)
    return re.sub(r"\s+", " ", ascii_only).lower().strip()


def _norm_company(company: str | None) -> str:
    """Return a normalized company key for dedup comparison.

    Strips accents, lowercases, removes legal entity suffixes, and collapses
    punctuation/whitespace so that e.g. "THALES S.A." == "thales" and
    "Société Générale" == "societe generale".
    """
    if not company:
        return ""
    s = _strip_accents(company.lower().strip())
    # Strip legal suffixes BEFORE removing punctuation so that "S.A." still
    # matches the regex (which expects dots, not spaces).
    s = _COMPANY_LEGAL_SUFFIX_RE.sub("", s).strip()
    # Now remove remaining punctuation and collapse whitespace.
    s = re.sub(r"[^\w\s-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def intra_source_deduplicate(
    jobs: list[Job],
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[Job]:
    """Remove duplicates within the same source.

    Uses source_job_id first, canonical_url as fallback, then content_hash.
    Jobs with neither identifier are kept unconditionally.

    The content_hash tier collapses same-source location variants — e.g. LinkedIn
    posts the same vacancy under different job IDs for each office location.  The
    first occurrence is kept; later ones are treated as duplicates.

    ``progress_callback``, if given, is called ``(processed, total)`` after
    each job — purely observational, never affects which jobs are kept.
    """
    seen: set[tuple[str, str, str]] = set()
    seen_intra_hashes: set[tuple[str, str]] = set()  # (source, content_hash)
    unique: list[Job] = []
    total = len(jobs)
    for i, job in enumerate(jobs, start=1):
        if progress_callback is not None:
            progress_callback(i, total)
        if job.source_job_id:
            key = (job.source, "id", job.source_job_id)
        elif job.canonical_url:
            key = (job.source, "url", _norm_url(job.canonical_url))
        else:
            # No strong intra-source identifier — keep the job.
            unique.append(job)
            continue
        if key not in seen:
            # Content-hash tier: collapse same-source location variants that have
            # different source_job_ids but identical content (title + company or
            # title + description).  Checked after the primary key to avoid
            # discarding jobs that have distinct content and just happen to share
            # a job ID format (impossible per-source but guarded defensively).
            if job.content_hash:
                hash_key = (job.source, job.content_hash)
                if hash_key in seen_intra_hashes:
                    continue  # location variant — skip
                seen_intra_hashes.add(hash_key)
            seen.add(key)
            unique.append(job)
    return unique


def cross_source_deduplicate(
    jobs: list[Job],
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[Job]:
    """Remove near-duplicates across sources, preserving source attribution.

    Tier 1 — canonical URL: same URL on different sources is the same posting.
    Tier 2 — content hash: identical title+description hash across sources.
    Tier 3 — content identity: (company_norm, title_norm, location_norm, employment_type)
              when company is non-empty. Null/empty company jobs require a URL or hash
              match. Title is normalised (lowercased, accent-stripped, gender markers
              removed) so that "Développeur(euse) web" and "Développeur web" collapse
              when posted at the same company and location.

    Different locations are treated as distinct postings (multi-location vacancies
    at the same company are preserved unless a stronger tier already collapses them).

    Source attribution: when a duplicate is found the duplicate's ``source`` is merged
    into the kept job's ``sources`` list; provenance is never silently lost.
    """
    # Maps from dedup key → index in `unique` list, so we can locate the kept
    # job to merge source attribution.
    seen_urls: dict[str, int] = {}
    seen_hashes: dict[str, int] = {}
    seen_content: dict[tuple[str, str, str, str], int] = {}
    unique: list[Job] = []
    total = len(jobs)

    for i, job in enumerate(jobs, start=1):
        if progress_callback is not None:
            progress_callback(i, total)
        keeper_idx: int | None = None

        # Tier 1: canonical URL
        url_key: str | None = None
        if job.canonical_url:
            url_key = _norm_url(job.canonical_url)
            if url_key in seen_urls:
                keeper_idx = seen_urls[url_key]

        # Tier 2: content hash (only when non-empty; hashes can collide so this
        # is kept as a secondary signal, not a primary identifier)
        if keeper_idx is None and job.content_hash:
            if job.content_hash in seen_hashes:
                keeper_idx = seen_hashes[job.content_hash]

        # Tier 3: content identity (only when company is known)
        content_key: tuple[str, str, str, str] | None = None
        if keeper_idx is None and job.company:
            company_norm = _norm_company(job.company)
            if company_norm:  # guard against normalizing to empty string
                content_key = (
                    company_norm,
                    _norm_title_simple(job.title),
                    _norm_str(job.location),
                    job.employment_type or "",
                )
                if content_key in seen_content:
                    keeper_idx = seen_content[content_key]

        if keeper_idx is not None:
            # Merge source attribution into the kept job.
            kept = unique[keeper_idx]
            if job.source and job.source not in kept.sources:
                kept.sources.append(job.source)
        else:
            # New unique job — record its index and initialize sources list.
            idx = len(unique)
            if job.source and job.source not in job.sources:
                # Ensure the job's own source is in its sources list.
                job.sources = [job.source] + [s for s in job.sources if s != job.source]
            unique.append(job)

            if url_key is not None:
                seen_urls[url_key] = idx
            if job.content_hash:
                seen_hashes[job.content_hash] = idx
            if content_key is not None:
                seen_content[content_key] = idx

    return unique


def deduplicate_jobs(
    jobs: list[Job],
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[Job], int]:
    """Full deduplication pipeline.

    ``progress_callback``, if given, is called ``(processed, total)`` where
    ``total`` is the original input count. Internally this stage makes two
    passes (intra-source then cross-source); their sub-progress is combined
    onto one monotonic 0..total scale so callers never see "processed" reset
    partway through — purely observational bookkeeping, no effect on which
    jobs are removed.

    Returns:
        (deduplicated_jobs, number_of_duplicates_removed)
    """
    original_count = len(jobs)
    half = max(1, original_count // 2)

    def _intra_progress(i: int, _total: int) -> None:
        if progress_callback is not None:
            progress_callback(min(i, half), original_count)

    jobs = intra_source_deduplicate(jobs, progress_callback=_intra_progress)
    after_intra = len(jobs)

    def _cross_progress(i: int, _total: int) -> None:
        if progress_callback is not None:
            progress_callback(min(half + i, original_count), original_count)

    jobs = cross_source_deduplicate(jobs, progress_callback=_cross_progress)
    after_cross = len(jobs)

    intra_removed = original_count - after_intra
    cross_removed = after_intra - after_cross
    total_removed = original_count - after_cross

    logger.info(
        "Deduplication: intra-source removed %d, cross-source removed %d, total %d",
        intra_removed,
        cross_removed,
        total_removed,
    )
    return jobs, total_removed
