"""HelloWork collector.

HelloWork has no public API or feed (robots.txt disallows /api/ and /rss/,
verified 2026-09-05), so discovery uses the site's own role ("metier")
listing pages, which are allowed by robots.txt and embed a schema.org/
ItemList JSON-LD block listing direct job-detail URLs. Job-detail pages
(/fr-fr/emplois/{id}.html) are also allowed and embed a full schema.org/
JobPosting JSON-LD block. No browser/JS rendering is required -- both page
types are plain server-rendered HTML, verified live via a bare httpx GET.

robots.txt (www.hellowork.com, verified 2026-09-05):
  Disallowed: /api/, /rss/, /emploi/recherche.html, /emplois/candidature.html,
              /emplois/ext/, /emplois/token/, /cv/, /compte/, and others.
  Allowed:    /emploi/metier_{slug}.html    (role listing pages -- discovery)
              /emploi/domaine_{slug}.html   (domain listing pages)
              /emploi/index-domaine_{slug}.html (per-domain metier taxonomy index)
              /emplois/{id}.html            (job detail pages -- extraction)
  Sitemap (horizon.xml) independently returns HTTP 403 to plain HTTP clients;
  unused here since the role/domain pages already provide a robots.txt-
  compliant discovery path.

TAXONOMY-DRIVEN DISCOVERY (verified live 2026-09-05):
  HelloWork publishes a stable, enumerable taxonomy: each domain has an
  index page (/fr-fr/emploi/index-domaine_{domain}.html) listing every
  "metier" (role) slug under that domain as plain <a href> links -- e.g.
  index-domaine_informatique.html lists ~230 IT role slugs (developpeur,
  developpeur-python, administrateur-reseau, ingenieur-securite-informatique,
  devops, dba, ...), and index-domaine_data-et-ia.html lists ~90 data/AI
  role slugs (data-analyst, data-engineer, data-scientist, machine-learning-
  engineer, ...). Configuring `domains` (rather than hand-listing individual
  metier slugs) lets the collector discover this full, current role list at
  runtime -- it stays correct as HelloWork adds/renames roles, and keeps
  configuration to a couple of domain names instead of scattering dozens of
  hard-coded slugs. Explicit `metier_slugs` entries (if any) are still
  merged in, e.g. to add a role from a domain not otherwise configured.

  A parallel "mot-cle" (keyword) taxonomy also exists (index-mot-cle.html)
  but was inspected and found to be a modifier axis (teletravail, cadre,
  senior, sans-diplome, fonction-publique, ...), not a role taxonomy --
  the one IT-relevant keyword found (securite-informatique) already has
  direct role-level coverage in the informatique domain's metier list
  (ingenieur-securite-informatique, consultant-securite-informatique,
  responsable-securite-informatique), so it is not used here.

Coverage ceiling (verified live 2026-09-05): each metier listing page's
ItemList JSON-LD exposes exactly 20 jobs -- there is no robots.txt-compliant
way to see more. robots.txt has a blanket "Disallow: /*?" for the general
user-agent, so any query-string pagination (the site's own "load more"
mechanism, if any) is off-limits; no path-based pagination scheme (e.g.
metier_developpeur_2.html) exists either. So breadth comes from the number
of distinct metier pages queried, not from paging any single one of them.

No bulk/list endpoint exists either: the metier listing page's ItemList
JSON-LD carries only job URLs (no title/company/etc.), and the interactive
job cards on that page are populated client-side from an endpoint under
/api/, which robots.txt disallows -- confirmed by inspecting the listing
page's raw HTML for an embedded data payload (Next.js/Nuxt-style hydration
JSON or similar) and finding none. So, unlike an API-backed collector
(e.g. Indeed's, which pages 100 jobs per call), there is no way to avoid
one HTTP request per job here; total request volume is strictly
`metier_pages + metier_pages * max_jobs_per_slug`.

Strategy: resolve the full metier-slug list (domain taxonomy expansion +
any explicit slugs), then for each slug fetch the role listing page,
extract job-detail URLs from its ItemList JSON-LD, then fetch each detail
page (bounded by max_jobs_per_slug and the optional global max_jobs) and
parse its JobPosting JSON-LD. Duplicate job IDs across slugs (observed to
be rare live -- HelloWork appears to assign each posting one primary
metier) are only fetched once, via an in-run seen_ids set; the pipeline's
own deduplication is the second, authoritative safety net.

Given the one-request-per-job floor, `max_jobs_per_slug` defaults low (3)
so that expanding `domains` to the full ~430-slug taxonomy stays a ~30
minute run (measured: 53 slugs / 147 jobs / 0 errors in 214.5s at
max_jobs_per_slug=3, rate_limit_delay=1.0s -- linear scaling puts the full
taxonomy at roughly 29 minutes) rather than the ~2.5 hours it would take
at the previously-tried max_jobs_per_slug=8 / rate_limit_delay=2.0s.
Breadth (roles touched) matters more than depth (jobs per role) for
discovery, and depth can be raised later if the runtime budget allows.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

from src.collectors._jsonld import (
    extract_itemlist_urls,
    extract_jobposting_ld,
    map_jobposting_ld_to_raw,
)
from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.hellowork.com"
_METIER_URL = _BASE_URL + "/fr-fr/emploi/metier_{slug}.html"
_DOMAIN_INDEX_URL = _BASE_URL + "/fr-fr/emploi/index-domaine_{domain}.html"
_HEADERS = {
    "Accept": "text/html",
    "User-Agent": "Mozilla/5.0 (compatible; job-aggregator/1.0)",
}

_JOB_ID_RE = re.compile(r"/emplois/([A-Za-z0-9]+)\.html")
# Matches metier role-listing links (not the paired "index-metier_..." pages,
# which describe/paginate a single metier rather than listing jobs).
_METIER_SLUG_RE = re.compile(r'href="/fr-fr/emploi/metier_([a-z0-9-]+)\.html"')


class HelloWorkCollector(BaseCollector):
    """Collects jobs from HelloWork domain/metier listing pages + JSON-LD job pages.

    Configuration (sources.yaml -> config sub-key):
        domains               List of HelloWork domain slugs (e.g. "informatique",
                               "data-et-ia") whose full metier taxonomy is discovered
                               at runtime via index-domaine_{domain}.html.
        metier_slugs           Optional explicit list of additional/override metier
                               slugs, merged with anything discovered via `domains`.
        max_jobs_per_slug      Max job-detail pages to fetch per metier slug (default: 3 --
                               kept low by default since `domains` can expand to 400+
                               slugs; breadth of roles touched matters more than depth
                               per role for discovery, and depth is cheap to raise later).
        max_jobs               Optional total unique jobs across all slugs (default: unlimited).
        rate_limit_delay       Seconds between requests (default: 1.0 -- robots.txt sets no
                               crawl-delay; still well within polite-scraping norms).
        timeout                Per-request timeout in seconds (default: 20).
    """

    def __init__(self, config: dict[str, Any] | None = None, **_: Any) -> None:
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 20)),
            retries=3,
            rate_limit_delay=float(cfg.get("rate_limit_delay", 1.0)),
        )
        self.domains: list[str] = list(cfg.get("domains") or [])
        self.explicit_metier_slugs: list[str] = list(cfg.get("metier_slugs") or [])
        self.max_jobs_per_slug: int = int(cfg.get("max_jobs_per_slug", 3))
        self.max_jobs: int | None = cfg.get("max_jobs")

    def collect(
        self,
        on_slug_done: Callable[[str, list[dict[str, Any]]], None] | None = None,
        skip_slugs: set[str] | None = None,
        shutdown_event: threading.Event | None = None,
    ) -> CollectorResult:
        """Collect jobs across every resolved metier slug.

        Args:
            on_slug_done: optional callback invoked after a metier slug's
                listing + detail pages have all been attempted (whether or
                not any jobs were found for it), with the slug name and the
                jobs collected for just that slug. Lets a caller persist
                results incrementally instead of waiting ~30 minutes for
                this whole method to return — see src/hellowork/scanner.py,
                which uses this to stage jobs and checkpoint scan progress
                as it goes, so an interruption doesn't lose everything
                collected up to that point.
            skip_slugs: metier slugs to skip entirely — the resumable
                checkpoint's already-completed slugs from a previous,
                interrupted attempt at this same scan. Not called back for
                skipped slugs (they were already reported done previously).
            shutdown_event: checked before starting the next slug and before
                each job-detail request within a slug. When set, collection
                stops promptly *without* calling on_slug_done for whatever
                slug was in progress — the checkpoint granularity here is
                per-slug (see hellowork_scan_checkpoint), so a slug is only
                ever reported "done" once every one of its job pages has
                actually been attempted, exactly mirroring how a caught
                mid-slug stop already behaves on any other early exit in
                this loop. The next scan attempt simply re-fetches that slug
                in full (safe/idempotent — insert_job() dedups), never
                skipping the jobs that were in flight.
        """
        start_time = time.time()
        errors: list[str] = []
        jobs: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        skip_slugs = skip_slugs or set()

        metier_slugs, discovery_errors = self._resolve_metier_slugs()
        errors.extend(discovery_errors)

        if not metier_slugs:
            errors.append("No metier slugs available (no domains/metier_slugs configured or resolved)")
            return CollectorResult(
                source_name="hellowork",
                jobs=[],
                errors=errors,
                duration_seconds=time.time() - start_time,
            )

        for slug in metier_slugs:
            if shutdown_event is not None and shutdown_event.is_set():
                logger.info(
                    "HelloWork: shutdown requested — stopping before metier slug "
                    "%r (checkpoint already reflects the last completed slug).",
                    slug,
                )
                break
            if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                break
            if slug in skip_slugs:
                continue

            listing_url = _METIER_URL.format(slug=slug)
            try:
                self._enforce_rate_limit()
                response = self._fetch_with_retry(listing_url, headers=_HEADERS)
                job_urls = extract_itemlist_urls(response.text)
            except (httpx.HTTPError, RuntimeError) as exc:
                errors.append(f"HelloWork: failed to list jobs for metier '{slug}': {exc}")
                continue

            logger.info("HelloWork: %d job URLs found for metier '%s'", len(job_urls), slug)

            slug_jobs: list[dict[str, Any]] = []
            fetched_for_slug = 0
            slug_interrupted = False
            for job_url in job_urls:
                if shutdown_event is not None and shutdown_event.is_set():
                    logger.info(
                        "HelloWork: shutdown requested — stopping mid metier slug "
                        "%r after %d/%d job(s); slug will be retried in full on the "
                        "next scan (not marked complete).",
                        slug, fetched_for_slug, len(job_urls),
                    )
                    slug_interrupted = True
                    break
                if fetched_for_slug >= self.max_jobs_per_slug:
                    break
                if self.max_jobs is not None and len(jobs) >= self.max_jobs:
                    break

                job_id_match = _JOB_ID_RE.search(job_url)
                job_id = job_id_match.group(1) if job_id_match else None
                if job_id and job_id in seen_ids:
                    continue

                try:
                    self._enforce_rate_limit()
                    detail_response = self._fetch_with_retry(job_url, headers=_HEADERS)
                except (httpx.HTTPError, RuntimeError) as exc:
                    errors.append(f"HelloWork: failed to load {job_url}: {exc}")
                    continue

                ld = extract_jobposting_ld(detail_response.text)
                if not ld:
                    logger.warning("HelloWork: no JobPosting JSON-LD found at %s", job_url)
                    continue

                if job_id:
                    seen_ids.add(job_id)
                raw_job = map_jobposting_ld_to_raw(ld, job_url, job_id)
                jobs.append(raw_job)
                slug_jobs.append(raw_job)
                fetched_for_slug += 1

            if slug_interrupted:
                # Do NOT call on_slug_done — this slug did not actually
                # finish, so it must not be checked off in the checkpoint
                # (that would make the next resume skip it and permanently
                # lose whatever job pages were never reached).
                break

            # This slug's listing + all its detail pages have been attempted
            # (successfully or not) — safe to check off, even with 0 jobs.
            if on_slug_done is not None:
                on_slug_done(slug, slug_jobs)

        duration = time.time() - start_time
        self._log_collection("hellowork", len(jobs), errors, duration)
        return CollectorResult(
            source_name="hellowork",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
            metadata={"metier_slugs_queried": len(metier_slugs)},
        )

    def _resolve_metier_slugs(self) -> tuple[list[str], list[str]]:
        """Return the deduplicated metier slug list (domain taxonomy + explicit), and any errors."""
        errors: list[str] = []
        seen: set[str] = set()
        resolved: list[str] = []

        for domain in self.domains:
            index_url = _DOMAIN_INDEX_URL.format(domain=domain)
            try:
                self._enforce_rate_limit()
                response = self._fetch_with_retry(index_url, headers=_HEADERS)
            except (httpx.HTTPError, RuntimeError) as exc:
                errors.append(f"HelloWork: failed to load domain taxonomy for '{domain}': {exc}")
                continue

            slugs = _METIER_SLUG_RE.findall(response.text)
            new_count = 0
            for slug in slugs:
                if slug not in seen:
                    seen.add(slug)
                    resolved.append(slug)
                    new_count += 1
            logger.info("HelloWork: domain '%s' contributed %d metier slugs (%d new)",
                        domain, len(slugs), new_count)

        for slug in self.explicit_metier_slugs:
            if slug not in seen:
                seen.add(slug)
                resolved.append(slug)

        return resolved, errors
