"""France Travail offers without API credentials: the public listing pages.

The France Travail API no longer accepts new subscribers (data.gouv.fr notice),
so a fresh install had no France Travail offers at all. This collector reads the
public pages France Travail publishes for search engines, one per occupation x
département (``/offres/emploi/<occupation>/<département>/s<sector>m<occupation>d<code>``,
listed in its own sitemaps), and nothing else:

* **Only published pages**: the occupation x département pages France Travail lists in
  its sitemap (it answers 410 Gone for other combinations). Today that is ten IT-related
  occupations (see ``OCCUPATIONS``).
* **robots.txt is respected**: the rules are fetched at the start of every run and
  every URL is checked against them (``*`` wildcards included). The keyword search
  (``motsCles=``) and the pages that load an offer's full text (``t:ac=``) are
  disallowed, so they are never requested. It fails closed: without a robots.txt
  served with HTTP 200 (any other status, an empty body or a network error), no page
  is requested. Sitemap URLs outside ``BASE`` are never followed.
* **Polite**: one request at a time, ``rate_limit_delay`` (at least 1 s) between
  requests, at most ``max_pages`` listing pages per run (default 60).
* **What a listing gives**: id, title, employer, place, contract, working time, an
  extract of the description (~200 characters) and the publication age. Each offer
  is marked ``description_complete: false`` so later stages know the text is partial,
  and keeps the link to its France Travail page.
* **The occupation is evidence**: every offer carries the France Travail occupation
  of the page it came from, and the profile target domain that page was chosen for
  (``occupation``). The title filter counts it as role evidence, because the extract
  is often too short to name the role. Measured on 3,000 France Travail offers replayed
  with their full text and with an extract: without it, 348 of the 2,113 offers the full
  text accepts were filtered out; with occupation evidence, 67.

Offers that France Travail republishes from partner job boards (HelloWork, Meteojob,
...) say so in ``origin``; their full text lives on the partner's site.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup

from src.collectors.base import BaseCollector, CollectorResult, RatePolicy, RecordedFixture
from src.collectors.robots import RobotsRule, parse_robots, robots_allows, rules_from_response

__all__ = ["FranceTravailWebCollector", "RobotsRule", "parse_listing", "parse_robots", "robots_allows"]

logger = logging.getLogger(__name__)

BASE = "https://candidat.francetravail.fr"
SITEMAP = f"{BASE}/rechercheoffre/sitemap/croisements-metiers-departements.xml"
USER_AGENT = ("FindYourJobWithAI/2.0 (local job-search app; "
              "+https://github.com/ItackFPS/Find-Your-Job-With-AI-Public)")
ROBOTS_AGENT = "FindYourJobWithAI"  # product token of USER_AGENT, matched in robots.txt groups
MIN_RATE_LIMIT_DELAY = 1.0  # seconds between requests; a lower configured value is raised to it

# France Travail occupation codes (sector + occupation, from its sector sitemap:
# "Informatique et télécoms" is sector 28, "Audit, conseil" 6, sector 29 holds web,
# design and business-analysis jobs) per profile target domain. France Travail only
# publishes occupation x département pages for some occupations (on 2026-10-23, of
# these: s28m7/s29m2 développeur web, s28m8/s29m3 webmaster, s28m15 data scientist,
# s28m16 product owner, s28m18 data analyst, s28m28 scrum master, s29m7 business
# analyst, s29m20 CTO, s6m2 consultant informatique, s6m3 consultant SI). Pages are
# taken from its sitemap, so a code listed here without published pages is simply
# not read, and starts being read when France Travail publishes it.
OCCUPATIONS: dict[str, list[str]] = {
    "software_development": ["s28m7", "s29m2", "s28m1", "s28m9", "s28m6", "s28m24", "s28m20",
                             "s28m21", "s29m4", "s28m16", "s28m28", "s29m20", "s6m2"],
    "web_development": ["s28m7", "s29m2", "s28m8", "s29m3", "s28m24", "s28m6"],
    "qa_testing": ["s28m10", "s6m2"],
    "data": ["s28m18", "s28m15", "s29m7", "s28m22", "s6m12"],
    "it_support": ["s28m5", "s6m2"],
    "systems": ["s28m11", "s28m23", "s6m3"],
    "networking": ["s28m11", "s28m19"],
    "devops": ["s28m23", "s28m11", "s6m2"],
    "cloud": ["s28m11", "s28m21", "s6m2"],
    "cybersecurity": ["s28m25", "s6m3"],
    "automation": ["s28m1", "s28m10", "s6m2"],
    "informatique": ["s6m2", "s6m3", "s28m7", "s28m2", "s28m3", "s28m1"],
}

_PAGE_IN_SITEMAP = re.compile(r"/offres/emploi/([^/]+)/([^/]+)/(s\d+m\d+)d(\d{2,3}|2[ab])$", re.I)
_SUBTEXT_SPLIT = re.compile(r"\s+[-–]\s+(?=\d{2,3}\s*[-–]|\(?\d{2,3}\)?\s)")
_AGE_DAYS = re.compile(r"(\d+)\s+jour", re.I)


# -- listing parsing ------------------------------------------------------------------------


def parse_listing(html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Offers on one listing page, as raw dicts for normalize_jobs()."""
    now = now or datetime.now(UTC)
    soup = BeautifulSoup(html, "html.parser")
    offers = []
    for li in soup.select("li.result[data-id-offre]"):
        offer_id = li["data-id-offre"]
        title_el = li.select_one(".media-heading-title")
        if title_el is None:
            continue
        subtext = li.select_one("p.subtext")
        company, location = None, ""
        if subtext is not None:
            place = subtext.find("span")
            location = place.get_text(" ", strip=True) if place else ""
            if place:
                place.extract()
            company = subtext.get_text(" ", strip=True).strip(" -–") or None
        desc_el = li.select_one("p.description")
        contract_el = li.select_one(".media-right p.contrat") or li.select_one("p.contrat")
        contract = ""
        if contract_el is not None:
            kind = contract_el.find("span", class_="type-contrat")
            schedule = kind.get_text(" ", strip=True) if kind else ""
            if kind:
                kind.extract()
            contract = " ".join(filter(None, [contract_el.get_text(" ", strip=True), schedule]))
        date_el = li.select_one("p.date")
        origin_el = li.select_one(".media-left img")
        dept = re.match(r"^\s*(\d{2,3}|2[AB])\s*[-–]", location)
        offers.append({
            "source_job_id": offer_id,
            "title": title_el.get_text(" ", strip=True),
            "company": company,
            "location": location,
            "countries": ["France"],
            "department_code": dept.group(1).upper() if dept else None,
            "description": " ".join((desc_el.get_text(" ", strip=True) if desc_el else "").split()),
            "type": contract,
            "url": f"{BASE}/offres/recherche/detail/{offer_id}",
            "published_at": _published_at(date_el.get_text(" ", strip=True) if date_el else "", now),
            "origin": str(origin_el.get("alt") or "" if origin_el else "").replace("Origine de l'offre :", "").strip(),
            "description_complete": False,
        })
    return offers


def _published_at(text: str, now: datetime) -> str | None:
    lower = text.lower()
    if "plus de" in lower:  # "plus de 30 jours": a lower bound, not a date
        return None
    if "aujourd" in lower:
        days = 0
    elif "hier" in lower:
        days = 1
    else:
        m = _AGE_DAYS.search(lower)
        if not m:
            return None  # e.g. "Publié il y a plus de 30 jours": unknown, never guessed
        days = int(m.group(1))
    return (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


# -- collector -----------------------------------------------------------------------------


class FranceTravailWebCollector(BaseCollector):
    """Reads France Travail's public occupation x département listing pages."""

    # public_page: anonymous listing pages France Travail publishes in its own sitemaps for
    # search engines; robots.txt allows them (only motsCles= and t:ac= are disallowed).
    access_class = "public_page"
    robots_policy = True
    robots_agent = ROBOTS_AGENT
    robots_user_agent = USER_AGENT
    # >= 1 s between requests; 500 requests per run is far above the default max_pages
    # (60 pages + robots.txt + sitemap) and stops a misconfigured run.
    rate_policy = RatePolicy(min_delay_seconds=MIN_RATE_LIMIT_DELAY, max_requests_per_run=500)

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        # Never faster than one request per MIN_RATE_LIMIT_DELAY, whatever the config says.
        super().__init__(timeout=int(cfg.get("timeout", 30)), retries=int(cfg.get("retries", 2)),
                         rate_limit_delay=max(MIN_RATE_LIMIT_DELAY,
                                              float(cfg.get("rate_limit_delay", MIN_RATE_LIMIT_DELAY))))
        self.max_pages = int(cfg.get("max_pages", 60))
        self.target_domains = [d for d in cfg.get("target_domains") or [] if d in OCCUPATIONS]
        self.departments = [str(d).upper().zfill(2) for d in cfg.get("departments") or []]
        self._client: httpx.Client | None = None

    def _get(self, url: str) -> httpx.Response:
        self._before_request(url)  # robots.txt (rules seeded by collect()) and the request cap
        self._enforce_rate_limit()
        assert self._client is not None
        resp = self._client.get(url)
        self._observe_response(resp)
        return resp

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("france_travail_web_listing.html",
                               self._load_fixture("france_travail_web_listing.html"), parse_listing)

    def collect(self) -> CollectorResult:
        if not self.target_domains:
            return CollectorResult(source_name="france-travail-web", jobs=[], errors=[
                "no target domain in the profile matches a France Travail occupation; nothing to search"])
        if not self.departments:
            return CollectorResult(source_name="france-travail-web", jobs=[], errors=[
                "no département in the profile's locations; add a place to search France Travail"])
        errors: list[str] = []
        jobs: dict[str, dict[str, Any]] = {}
        with httpx.Client(timeout=self.timeout, follow_redirects=False,
                          headers={"User-Agent": USER_AGENT, "Accept-Language": "fr-FR,fr;q=0.9"}) as client:
            self._client = client
            try:
                robots = self._get(f"{BASE}/robots.txt")
            except (httpx.HTTPError, RuntimeError) as exc:
                return CollectorResult(source_name="france-travail-web", jobs=[], errors=[
                    f"France Travail is unreachable: {exc}"])
            # Fail closed: crawl only with a robots.txt actually served (HTTP 200, non-empty).
            # RFC 9309 makes 5xx/unreachable "disallow all"; it lets other 4xx mean "no
            # robots.txt, allow all", but this source is read conservatively, so any answer
            # other than a real robots.txt means no page is requested.
            rules = rules_from_response(robots.status_code, robots.text, ROBOTS_AGENT)
            if rules is None:
                return CollectorResult(source_name="france-travail-web", jobs=[], errors=[
                    f"France Travail robots.txt unavailable (HTTP {robots.status_code}"
                    f"{', empty' if robots.status_code == 200 else ''}); not crawling"])
            self._robots_cache().seed(BASE, rules)
            try:
                published, offsite = self._published_pages()
            except (httpx.HTTPError, RuntimeError) as exc:
                return CollectorResult(source_name="france-travail-web", jobs=[], errors=[
                    f"France Travail is unreachable: {exc}"])
            if offsite:
                logger.warning("France Travail web: %d sitemap URLs outside %s skipped", offsite, BASE)
            pages = self._pages(published)
            for url, occupation in pages[: self.max_pages]:
                path = url[len(BASE):]
                if not robots_allows(rules, path):
                    errors.append(f"skipped (robots.txt): {path}")
                    continue
                try:
                    resp = self._get(url)
                except (httpx.HTTPError, RuntimeError) as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                if resp.status_code != 200:
                    errors.append(f"{path}: HTTP {resp.status_code}")
                    continue
                for offer in parse_listing(resp.text):
                    offer["occupation"] = occupation
                    jobs.setdefault(offer["source_job_id"], offer)
            if len(pages) > self.max_pages:
                logger.info("France Travail web: %d pages planned, %d read (max_pages)",
                            len(pages), self.max_pages)
            self._client = None
        return CollectorResult(source_name="france-travail-web", jobs=list(jobs.values()), errors=errors,
                               metadata={"pages_planned": len(pages), "pages_read": min(len(pages), self.max_pages),
                                         "sitemap_offsite_skipped": offsite})

    def _published_pages(self) -> tuple[list[tuple[str, str, str, str]], int]:
        """(url, occupation slug, occupation code, département) of every occupation x
        département page France Travail lists in its sitemap, and how many sitemap URLs
        were skipped for not being on BASE. Only these pages are read: it answers 410
        Gone for combinations it does not publish."""
        resp = self._get(SITEMAP)
        resp.raise_for_status()
        pages = []
        offsite = 0
        for raw_loc in re.findall(r"<loc>([^<]+)</loc>", resp.text):
            loc = raw_loc.strip()
            if not loc.startswith(BASE + "/"):
                offsite += 1
                continue
            m = _PAGE_IN_SITEMAP.search(loc)
            if m:
                pages.append((loc, m.group(1), m.group(3).lower(), m.group(4).upper()))
        return pages, offsite

    def _pages(self, published: list[tuple[str, str, str, str]]) -> list[tuple[str, dict[str, Any]]]:
        """Pages to read, round-robin across départements so a page cap stays fair."""
        wanted: dict[str, list[str]] = {}
        for domain in self.target_domains:
            for code in OCCUPATIONS[domain]:
                wanted.setdefault(code, []).append(domain)
        by_dept: dict[str, list[tuple[str, dict[str, Any]]]] = {d: [] for d in self.departments}
        for url, slug, code, dept in published:
            if code in wanted and dept in by_dept:
                by_dept[dept].append((url, {"scheme": "france-travail", "slug": slug, "code": code,
                                            "target_domains": sorted(set(wanted[code]))}))
        lists = [by_dept[d] for d in self.departments]
        pages: list[tuple[str, dict[str, Any]]] = []
        for i in range(max((len(p) for p in lists), default=0)):
            pages.extend(p[i] for p in lists if i < len(p))
        return pages
