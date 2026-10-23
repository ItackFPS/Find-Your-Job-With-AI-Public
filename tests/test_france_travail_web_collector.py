"""France Travail public listing pages (src/collectors/france_travail_web.py, plan F6.2).

The listing HTML below is written for the tests, with the structure France Travail
served on 2026-10-23; the network test checks that the live pages still match it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from src.collectors import base as base_module
from src.collectors import france_travail_web as ftw
from src.collectors.france_travail_web import (
    OCCUPATIONS,
    FranceTravailWebCollector,
    parse_listing,
    parse_robots,
    robots_allows,
)
from src.pipeline.filter import matches_title_keywords
from src.pipeline.normalize import normalize_jobs

ROBOTS = """User-agent: *
Disallow: /offres/recherchesenregistrees
Disallow: *motsCles=*
Disallow: *t:ac=*
Disallow: *JSESSIONID*
Sitemap: https://candidat.francetravail.fr/rechercheoffre/sitemap/croisements-metiers-departements.xml
"""

SITEMAP = """<urlset>
<url><loc>https://candidat.francetravail.fr/offres/emploi/developpeur-web/var/s28m7d83</loc></url>
<url><loc>https://candidat.francetravail.fr/offres/emploi/data-analyst/rhone/s28m18d69</loc></url>
<url><loc>https://candidat.francetravail.fr/offres/emploi/developpeur-web/corse-du-sud/s28m7d2a</loc></url>
<url><loc>https://candidat.francetravail.fr/offres/emploi/webmaster/var/s28m8d83</loc></url>
<url><loc>https://candidat.francetravail.fr/offres/emploi/developpeur-web/paris/s28m7v1</loc></url>
</urlset>"""

LISTING = """<ul>
<li class="result" data-id-offre="215TEST">
 <a class="media with-fav" href="/offres/recherche/detail/215TEST">
  <div class="media-left media-middle"><span class="media-object">
   <img alt="Origine de l'offre : France Travail" src="x.svg"/></span></div>
  <div class="media-body">
   <h2 class="t4 media-heading"><span class="media-heading-title">Chargé de projet (H/F)</span></h2>
   <p class="subtext">ACME SAS - <span>83 - TOULON</span></p>
   <p class="description">Vous rejoignez une équipe de six personnes pour faire évoluer nos
   applications internes et accompagner les utilisateurs...</p>
   <p class="contrat visible-xs">CDI - Temps plein</p>
   <p class="date">Publié il y a 4 jours</p>
  </div>
  <div class="media-right media-middle hidden-xs"><p class="contrat">CDI <span class="type-contrat">Temps plein</span></p></div>
 </a>
</li>
<li class="result" data-id-offre="7712345">
 <a class="media with-fav" href="/offres/recherche/detail/7712345">
  <div class="media-left media-middle"><span class="media-object">
   <img alt="Origine de l'offre : HELLOWORK" src="y.svg"/></span></div>
  <div class="media-body">
   <h2 class="t4 media-heading"><span class="media-heading-title">Développeur Java H/F</span></h2>
   <p class="subtext">Beta Corp - <span>83 - LA SEYNE SUR MER</span></p>
   <p class="description">Mission en Java...</p>
   <p class="date">Publié aujourd'hui</p>
  </div>
 </a>
</li>
</ul>"""

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch) -> list[float]:  # noqa: ANN001
    """The collector waits at least 1 s between requests; tests record, not wait."""
    slept: list[float] = []
    monkeypatch.setattr(base_module.time, "sleep", slept.append)
    return slept


def test_robots_rules_with_wildcards() -> None:
    rules = parse_robots(ROBOTS)
    assert robots_allows(rules, "/offres/emploi/developpeur-web/var/s28m7d83")
    assert robots_allows(rules, "/offres/recherche/detail/215TEST")
    assert not robots_allows(rules, "/offres/recherche?motsCles=python")
    assert not robots_allows(rules, "/offres/recherche/detail.detailstandalone.offressimilaires:x?t:ac=215TEST")
    assert not robots_allows(rules, "/offres/recherchesenregistrees")
    assert robots_allows(parse_robots(""), "/anything")


def test_parse_listing_fields() -> None:
    first, partner = parse_listing(LISTING, now=NOW)
    assert first["source_job_id"] == "215TEST"
    assert first["title"] == "Chargé de projet (H/F)"
    assert first["company"] == "ACME SAS"
    assert first["location"] == "83 - TOULON" and first["department_code"] == "83"
    assert first["type"] == "CDI Temps plein"
    assert first["description"].startswith("Vous rejoignez") and first["description"].endswith("...")
    assert first["description_complete"] is False
    assert first["url"] == "https://candidat.francetravail.fr/offres/recherche/detail/215TEST"
    assert first["published_at"] == "2026-10-19T00:00:00+00:00"
    assert first["origin"] == "France Travail"
    assert partner["origin"] == "HELLOWORK" and partner["published_at"] == "2026-10-23T00:00:00+00:00"


def test_unknown_age_is_not_guessed() -> None:
    html = LISTING.replace("Publié il y a 4 jours", "Publié il y a plus de 30 jours")
    assert parse_listing(html, now=NOW)[0]["published_at"] is None


class _Client:
    """Serves robots.txt, the sitemap and listing pages; records every URL asked for."""

    def __init__(self, *a, **k) -> None:  # noqa: ANN002, ANN003
        self.urls: list[str] = []

    def __enter__(self) -> _Client:
        _Client.last = self
        return self

    def __exit__(self, *a) -> None:  # noqa: ANN002
        pass

    def get(self, url: str) -> httpx.Response:
        self.urls.append(url)
        req = httpx.Request("GET", url)
        if url.endswith("/robots.txt"):
            return httpx.Response(self.robots_status, text=self.robots_text, request=req)
        if url.endswith(".xml"):
            return httpx.Response(200, text=self.sitemap_text, request=req)
        return httpx.Response(200, text=LISTING, request=req)

    robots_status = 200
    robots_text = ROBOTS
    sitemap_text = SITEMAP


def test_collect_reads_only_pages_published_in_the_sitemap(monkeypatch) -> None:
    monkeypatch.setattr(ftw.httpx, "Client", _Client)
    collector = FranceTravailWebCollector({"target_domains": ["web_development", "unknown_domain"],
                                           "departments": ["83", "2a", "99"], "rate_limit_delay": 0})
    result = collector.collect()
    urls = _Client.last.urls
    assert urls[0].endswith("/robots.txt") and urls[1].endswith(".xml")
    listing_urls = urls[2:]
    # web_development pages the sitemap publishes for 83 and 2A (99 has none; the city
    # page and the data-analyst page are not asked for), round-robin by département
    assert [u.rsplit("/", 2)[-2] + "/" + u.rsplit("/", 1)[-1] for u in listing_urls] == [
        "var/s28m7d83", "corse-du-sud/s28m7d2a", "var/s28m8d83"]
    assert all("t:ac" not in u and "motsCles" not in u for u in listing_urls)
    # the same offers are on every fake page: deduplicated by France Travail id
    assert {j["source_job_id"] for j in result.jobs} == {"215TEST", "7712345"}
    occ = result.jobs[0]["occupation"]
    assert occ == {"scheme": "france-travail", "slug": "developpeur-web", "code": "s28m7",
                   "target_domains": ["web_development"]}
    assert result.metadata["pages_planned"] == len(listing_urls)


def test_page_cap_is_shared_across_departements(monkeypatch) -> None:
    monkeypatch.setattr(ftw.httpx, "Client", _Client)
    FranceTravailWebCollector({"target_domains": ["web_development"], "departments": ["83", "2A"],
                               "max_pages": 2, "rate_limit_delay": 0}).collect()
    listing_urls = _Client.last.urls[2:]
    assert len(listing_urls) == 2 and "/var/" in listing_urls[0] and "/corse-du-sud/" in listing_urls[1]


@pytest.mark.parametrize("cfg,needle", [
    ({"target_domains": ["sales"], "departments": ["83"]}, "target domain"),
    ({"target_domains": ["data"], "departments": []}, "département"),
])
def test_nothing_to_search_is_reported_not_raised(cfg, needle) -> None:  # noqa: ANN001
    result = FranceTravailWebCollector(cfg).collect()
    assert result.jobs == [] and needle in result.errors[0]


def test_unreachable_site_is_one_clear_error(monkeypatch) -> None:
    class _Down(_Client):
        def get(self, url: str) -> httpx.Response:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(ftw.httpx, "Client", _Down)
    result = FranceTravailWebCollector({"target_domains": ["data"], "departments": ["83"]}).collect()
    assert result.jobs == [] and "unreachable" in result.errors[0]


def test_occupation_counts_as_role_evidence_for_the_title_filter() -> None:
    raw = parse_listing(LISTING, now=NOW)[0]  # "Chargé de projet": no IT word in title or extract
    config = {"title_keywords": {"include": ["developpeur"], "description_include": ["developpeur"],
                                 "exclude": ["boulanger"], "non_it_functions": ["commercial"]},
              "target_domains": ["web_development"]}
    (job,) = normalize_jobs("france-travail-web", [raw])
    assert not matches_title_keywords(job, config)  # no evidence without the occupation
    raw["occupation"] = {"scheme": "france-travail", "slug": "developpeur-web", "code": "s28m7",
                         "target_domains": ["web_development"]}
    (job,) = normalize_jobs("france-travail-web", [raw])
    assert matches_title_keywords(job, config)
    # an occupation outside the profile's domains is not evidence
    assert not matches_title_keywords(job, {**config, "target_domains": ["data"]})
    # exclusions and non-IT functions in the title still win
    raw["title"] = "Commercial (H/F)"
    (job,) = normalize_jobs("france-travail-web", [raw])
    assert not matches_title_keywords(job, config)


def test_every_occupation_code_is_well_formed() -> None:
    import re

    for domain, codes in OCCUPATIONS.items():
        assert codes, domain
        for code in codes:
            assert re.fullmatch(r"s\d+m\d+", code), (domain, code)


@pytest.mark.parametrize("status,text", [
    (500, ROBOTS), (503, ""), (403, ROBOTS), (404, ""), (301, ""), (200, ""), (200, "  \n"),
])
def test_robots_txt_not_served_means_nothing_is_crawled(monkeypatch, status, text) -> None:  # noqa: ANN001
    """Fail closed: without a robots.txt served with HTTP 200, no other page is asked for."""
    class _NoRobots(_Client):
        robots_status = status
        robots_text = text

    monkeypatch.setattr(ftw.httpx, "Client", _NoRobots)
    result = FranceTravailWebCollector({"target_domains": ["web_development"], "departments": ["83"]}).collect()
    assert _NoRobots.last.urls == [f"{ftw.BASE}/robots.txt"]
    assert result.jobs == [] and len(result.errors) == 1
    assert "robots.txt" in result.errors[0] and f"HTTP {status}" in result.errors[0]


def test_consecutive_user_agent_lines_form_one_group() -> None:
    text = """User-agent: *
User-agent: bingbot
Disallow: /private

User-agent: otherbot
Disallow: /
"""
    rules = parse_robots(text)
    assert not robots_allows(rules, "/private/x")
    assert robots_allows(rules, "/public")
    assert not robots_allows(parse_robots(text, "BingBot"), "/private")
    assert not robots_allows(parse_robots(text, "otherbot"), "/public")


def test_a_group_naming_this_crawler_replaces_the_star_group() -> None:
    text = "User-agent: *\nDisallow: /a\n\nUser-agent: findyourjobwithai\nDisallow: /b\n"
    rules = parse_robots(text, ftw.ROBOTS_AGENT)
    assert robots_allows(rules, "/a") and not robots_allows(rules, "/b")


def test_longest_match_uses_the_path_as_written() -> None:
    # "/a.b.c" is 6 characters as written (its escaped regex, "/a\.b\.c", is 8);
    # "/*/pag" is 6 too (regex 7): a tie, so allow wins.
    rules = parse_robots("User-agent: *\nDisallow: /a.b.c\nAllow: /*/pag\n")
    assert robots_allows(rules, "/a.b.c/page")
    # "/*/page" (7 as written) is longer than "/a.b.c" (6), though both regexes are 8
    rules = parse_robots("User-agent: *\nAllow: /a.b.c\nDisallow: /*/page\n")
    assert not robots_allows(rules, "/a.b.c/page")
    # a longer allow still beats a shorter disallow, and a longer disallow wins
    assert robots_allows(parse_robots("User-agent: *\nDisallow: /a\nAllow: /a.b\n"), "/a.b")
    assert not robots_allows(parse_robots("User-agent: *\nAllow: /a\nDisallow: /a.b\n"), "/a.b")


def test_sitemap_urls_outside_the_site_are_not_followed(monkeypatch) -> None:
    class _Offsite(_Client):
        sitemap_text = SITEMAP.replace("</urlset>", """
<url><loc>https://candidat.francetravail.fr.evil.example/offres/emploi/developpeur-web/var/s28m7d83</loc></url>
<url><loc>http://candidat.francetravail.fr/offres/emploi/developpeur-web/var/s28m7d83</loc></url>
<url><loc>https://elsewhere.example/offres/emploi/developpeur-web/var/s28m7d83</loc></url>
</urlset>""")

    monkeypatch.setattr(ftw.httpx, "Client", _Offsite)
    result = FranceTravailWebCollector({"target_domains": ["web_development"], "departments": ["83"]}).collect()
    listing_urls = _Offsite.last.urls[2:]
    assert listing_urls and all(u.startswith(ftw.BASE + "/") for u in listing_urls)
    assert result.metadata["sitemap_offsite_skipped"] == 3


@pytest.mark.parametrize("configured,expected", [(0, 1.0), (0.2, 1.0), (-5, 1.0), (1.0, 1.0), (2.5, 2.5)])
def test_rate_limit_is_never_below_one_second(configured, expected) -> None:  # noqa: ANN001
    assert FranceTravailWebCollector({"rate_limit_delay": configured}).rate_limit_delay == expected
    assert FranceTravailWebCollector({}).rate_limit_delay == 1.0


def test_requests_are_spaced_by_the_rate_limit(monkeypatch, _no_sleep) -> None:  # noqa: ANN001
    monkeypatch.setattr(ftw.httpx, "Client", _Client)
    FranceTravailWebCollector({"target_domains": ["web_development"], "departments": ["83"],
                               "rate_limit_delay": 0}).collect()
    # every request after the first waited (the fake answers instantly), close to 1 s
    assert len(_no_sleep) == len(_Client.last.urls) - 1
    assert all(0.9 < s <= 1.0 for s in _no_sleep)


_OCC = {"scheme": "france-travail", "slug": "developpeur-web", "code": "s28m7",
        "target_domains": ["web_development"]}
_TITLE_CONFIG = {"title_keywords": {"include": ["developpeur"], "description_include": ["developpeur"],
                                    "exclude": [], "non_it_functions": []},
                 "target_domains": ["web_development"]}


def test_occupation_is_only_evidence_from_the_france_travail_web_source() -> None:
    raw = {**parse_listing(LISTING, now=NOW)[0], "occupation": _OCC}
    (job,) = normalize_jobs("some-other-board", [raw])
    assert not matches_title_keywords(job, _TITLE_CONFIG)


@pytest.mark.parametrize("occupation", [
    None, "web_development", ["web_development"], 42,
    {"target_domains": "web_development"}, {"target_domains": None}, {"target_domains": [None, 3]},
    {"target_domains": {"web_development": True}}, {},
])
def test_malformed_occupation_is_ignored_never_raises(occupation) -> None:  # noqa: ANN001
    raw = {**parse_listing(LISTING, now=NOW)[0], "occupation": occupation}
    (job,) = normalize_jobs("france-travail-web", [raw])
    assert not matches_title_keywords(job, _TITLE_CONFIG)


def test_malformed_profile_domains_are_ignored() -> None:
    raw = {**parse_listing(LISTING, now=NOW)[0], "occupation": _OCC}
    (job,) = normalize_jobs("france-travail-web", [raw])
    assert not matches_title_keywords(job, {**_TITLE_CONFIG, "target_domains": "web_development"})


@pytest.mark.network
def test_live_listing_pages_still_parse() -> None:
    result = FranceTravailWebCollector({"target_domains": ["web_development"], "departments": ["69"],
                                        "max_pages": 1}).collect()
    assert result.metadata["pages_planned"] >= 1, "France Travail no longer publishes these pages"
    assert result.jobs, result.errors
    job = result.jobs[0]
    assert job["title"] and job["location"] and job["url"].startswith("https://candidat.francetravail.fr/")
