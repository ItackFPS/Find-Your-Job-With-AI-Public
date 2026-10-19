"""APEC (apec.fr) API-based job collector.

Uses the public JSON search API — the same endpoint called by the APEC Angular SPA —
to collect job listings without a browser or Crawl4AI.

Robots.txt compliance (verified 2026-08-27):
  Directive: "All robots will spider the domain"
  No Disallow rules — fully permissive.
  Sitemaps: sitemap_offres_search_engine.xml.gz (job offers)

API endpoint (verified 2026-08-27):
  POST https://www.apec.fr/cms/webservices/rechercheOffre
  Content-Type: application/json;charset=UTF-8
  No authentication required — same public endpoint used by the APEC SPA.

Pagination:
  Controlled by the 'pagination.startIndex' field in the POST body.
  Page N uses startIndex = (N-1) * page_size.

Work mode (idNomTeletravail field):
  20765 → on_site  (présentiel)
  20766 → hybrid   (télétravail partiel)
  20949 → None     (code observed but label unconfirmed; left as unknown)
  absent → None    (unknown)

Employment type:
  The typeContrat field contains numeric codes that do not reliably decode
  to canonical values (CDI and CDD share code 101888 in observed data).
  employment_type is left as None rather than guessing.

Canonical URL:
  Constructed from numeroOffre: detail-offre/{numeroOffre}
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from src.collectors.base import BaseCollector, CollectorResult

logger = logging.getLogger(__name__)

_API_URL = "https://www.apec.fr/cms/webservices/rechercheOffre"
_JOB_URL_TEMPLATE = (
    "https://www.apec.fr/candidat/recherche-emploi.html/emploi/detail-offre/{numero}"
)
_HEADERS: dict[str, str] = {
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json",
}

# Confirmed telework codes (verified 2026-08-27 via browser network inspection).
# 20949 has ~5,600 jobs but its label is unconfirmed — kept as None (unknown).
_TELETRAVAIL_MAP: dict[int, str] = {
    20765: "on_site",
    20766: "hybrid",
}


BLOCKED_MESSAGE = (
    "refused automated access (HTTP 403: its bot protection asks for a CAPTCHA). "
    "This collector does not try to get round it; to stop trying, set active: false "
    "on apec in config/sources.yaml"
)


class AccessBlocked(RuntimeError):
    """The site's bot protection refused the request: no retry, page or keyword can help."""


def _is_bot_challenge(resp: httpx.Response) -> bool:
    """A 403 from APEC's bot protection (DataDome), which answers with a CAPTCHA URL."""
    if resp.status_code != 403:
        return False
    cookies = " ".join(resp.headers.get_list("set-cookie")).lower()
    return "captcha-delivery.com" in resp.text or "datadome=" in cookies


class APECCollector(BaseCollector):
    """httpx-based collector for APEC (apec.fr).

    Calls the same public JSON API used by the APEC Angular SPA.
    No browser or Crawl4AI required.

    Multi-query mode (2026-08-28):
      Verified: the POST body accepts a 'motsCles' string field for keyword
      search.  When discovery_queries is provided (a list of French keywords
      injected by the orchestrator), the collector issues one paginated search
      cycle per keyword.  Results are deduplicated by numeroOffre within the
      run.  Falls back to a blank (all-jobs) search when absent.

    Configuration (sources.yaml → config sub-key):
        max_pages        Pages to fetch per keyword (default: 10 → 200 jobs).
        page_size        Jobs per page, max 50 (default: 20).
        rate_limit_delay Seconds between page requests (default: 2.0).
        timeout          Per-request timeout in seconds (default: 30).
        discovery_queries  List of French keywords (injected by orchestrator).
    """

    def __init__(self, config: dict[str, Any] | None = None):
        cfg = config or {}
        super().__init__(
            timeout=int(cfg.get("timeout", 30)),
            retries=int(cfg.get("retries", 3)),
            rate_limit_delay=float(cfg.get("rate_limit_delay", 2.0)),
        )
        self.max_pages: int = int(cfg.get("max_pages", 3))
        self.page_size: int = min(int(cfg.get("page_size", 20)), 50)
        # discovery_queries: French keyword list injected by the orchestrator.
        self.discovery_queries: list[str] = list(cfg.get("discovery_queries") or [])

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def collect(self) -> CollectorResult:
        start_time = time.time()
        jobs: list[dict[str, Any]] = []
        errors: list[str] = []
        seen_ids: set[str] = set()

        # One search cycle per keyword; fall back to blank search when no queries.
        keywords: list[str | None] = list(self.discovery_queries) if self.discovery_queries else [None]

        for keyword in keywords:
            for page in range(self.max_pages):
                self._enforce_rate_limit()
                start_index = page * self.page_size
                try:
                    raw_jobs, total = self._fetch_page(start_index, keyword=keyword)
                except AccessBlocked as exc:
                    errors.append(f"APEC: {exc}")
                    logger.warning("APEC: %s", exc)
                    return self._result(jobs, errors, start_time)
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"APEC: keyword='{keyword}' page {page + 1}: {exc}"
                    )
                    break

                new_on_page = 0
                for raw in raw_jobs:
                    job_id = str(raw.get("numeroOffre") or "").strip()
                    if job_id and job_id in seen_ids:
                        continue
                    if job_id:
                        seen_ids.add(job_id)
                    mapped = _map_job_to_raw(raw)
                    if mapped.get("title"):
                        jobs.append(mapped)
                        new_on_page += 1

                logger.info(
                    "APEC: keyword='%s' page %d/%d fetched=%d new=%d (total=%s)",
                    keyword or "(none)", page + 1, self.max_pages,
                    len(raw_jobs), new_on_page, total,
                )

                if len(raw_jobs) < self.page_size:
                    break  # reached the last page for this keyword

        return self._result(jobs, errors, start_time)

    def _result(self, jobs: list[dict[str, Any]], errors: list[str], start_time: float) -> CollectorResult:
        duration = time.time() - start_time
        self._log_collection("apec", len(jobs), errors, duration)
        return CollectorResult(
            source_name="apec",
            jobs=jobs,
            errors=errors,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_page(
        self,
        start_index: int,
        keyword: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """POST one page to the APEC search API.

        Returns (resultats list, totalCount).
        Raises RuntimeError after all retries are exhausted.
        """
        body: dict[str, Any] = {
            "lieux": [],
            "fonctions": [],
            "statutPoste": [],
            "typesContrat": [],
            "typesConvention": ["143684", "143685", "143686", "143687", "143706"],
            "niveauxExperience": [],
            "idsEtablissement": [],
            "secteursActivite": [],
            "typesTeletravail": [],
            "idNomZonesDeplacement": [],
            "positionNumbersExcluded": [],
            "typeClient": "CADRE",
            "sorts": [{"type": "SCORE", "direction": "DESCENDING"}],
            "pagination": {"range": self.page_size, "startIndex": start_index},
            "activeFiltre": True,
            "pointGeolocDeReference": {"distance": 0},
        }
        # motsCles: verified 2026-08-28 — drives keyword relevance scoring.
        if keyword:
            body["motsCles"] = keyword
        last_error = "unknown error"
        for attempt in range(1, self.retries + 1):
            try:
                logger.debug("APEC: POST page at startIndex=%d (attempt %d)", start_index, attempt)
                resp = httpx.post(_API_URL, json=body, headers=_HEADERS, timeout=self.timeout)

                if _is_bot_challenge(resp):
                    raise AccessBlocked(BLOCKED_MESSAGE)

                if resp.status_code in {408, 429, 500, 502, 503, 504}:
                    last_error = f"HTTP {resp.status_code}"
                    if attempt < self.retries:
                        wait = self._retry_delay(resp, attempt)
                        logger.warning(
                            "APEC: attempt %d got HTTP %d; retrying in %.1fs",
                            attempt, resp.status_code, wait,
                        )
                        time.sleep(wait)
                        continue
                    break

                resp.raise_for_status()
                data = resp.json()
                return data.get("resultats") or [], int(data.get("totalCount") or 0)

            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                last_error = str(exc)
                if attempt < self.retries:
                    backoff = 2 ** (attempt - 1) * 0.5
                    logger.warning("APEC: attempt %d network error: %s; retrying in %.1fs", attempt, exc, backoff)
                    time.sleep(backoff)

        raise RuntimeError(f"APEC API unreachable after {self.retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Pure helpers — testable without network access
# ---------------------------------------------------------------------------

def _parse_location(lieu: str) -> tuple[str, str | None]:
    """Split 'City - DEPT' into (city, department_code).

    Uses rsplit to handle city names that may contain ' - '.
    Returns (original, None) when the suffix is not a valid department code.
    """
    if " - " not in lieu:
        return lieu, None
    parts = lieu.rsplit(" - ", 1)
    dept = parts[-1].strip()
    if dept.isdigit() or dept in ("2A", "2B"):
        return parts[0].strip(), dept
    return lieu, None


def _map_job_to_raw(job: dict[str, Any]) -> dict[str, Any]:
    """Map an APEC API job dict to the pipeline normalizer raw-dict contract."""
    numero = str(job.get("numeroOffre") or "").strip()
    location_clean, department_code = _parse_location((job.get("lieuTexte") or "").strip())
    tt_code = job.get("idNomTeletravail")
    work_mode: str | None = _TELETRAVAIL_MAP.get(int(tt_code)) if tt_code is not None else None
    canonical_url = _JOB_URL_TEMPLATE.format(numero=numero) if numero else None

    return {
        "title": (job.get("intitule") or "").strip(),
        "company": (job.get("nomCommercial") or "").strip() or None,
        "description": (job.get("texteOffre") or "").strip(),
        "location": location_clean,
        "countries": ["France"],
        "department_code": department_code,
        "canonical_url": canonical_url,
        "source_job_id": numero or None,
        "published_at": job.get("datePublication") or None,
        # typeContrat codes don't reliably distinguish CDI/CDD in observed data
        "type": None,
        "remote": work_mode == "remote",
        "work_mode": work_mode,
        "skills": [],
    }
