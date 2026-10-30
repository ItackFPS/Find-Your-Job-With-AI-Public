"""Source plug-in contract (plan F6.1): access class, robots.txt, rate policy, parser
canary. Deterministic: HTTP goes through httpx.MockTransport, nothing waits."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.collectors import robots
from src.collectors.base import (
    ACCESS_CLASSES,
    BaseCollector,
    CollectorResult,
    RatePolicy,
    RecordedFixture,
    RequestBudgetExceeded,
    RobotsDisallowed,
)
from src.collectors.canary import PARSER_CHANGED, has_canary, parse_fixture, parser_canary
from src.collectors.registry import CollectorRegistry
from src.collectors.weworkremotely import WeWorkRemotelyCollector
from src.pipeline.normalize import normalize_jobs

REGISTRY = CollectorRegistry()
NAMES = REGISTRY.list_collectors()


def _cls(name: str) -> type[BaseCollector]:
    return REGISTRY.get_collector_class(name)


def _make(name: str, cfg: dict[str, Any] | None = None) -> BaseCollector:
    factory: Callable[..., BaseCollector] = _cls(name)
    return factory(config=cfg or {})


# -- access class -----------------------------------------------------------------------


@pytest.mark.parametrize("name", NAMES)
def test_every_registered_collector_declares_a_valid_access_class(name: str) -> None:
    cls = _cls(name)
    assert cls.access_class in ACCESS_CLASSES, f"{name}: access_class={cls.access_class!r}"
    assert any("access_class" in vars(k) for k in cls.__mro__ if k is not BaseCollector), \
        f"{name} inherits access_class instead of declaring it"
    assert isinstance(cls.rate_policy, RatePolicy)
    if cls.access_class == "public_page":
        assert cls.robots_policy is True, f"{name}: a public_page collector must honour robots.txt"
    if cls.access_class != "official":
        assert has_canary(_make(name)), f"{name} scrapes pages or app endpoints but ships no fixture"


def test_blocked_is_never_a_shipped_class() -> None:
    assert "blocked" not in ACCESS_CLASSES


@pytest.mark.parametrize("name", NAMES)
def test_configured_delay_never_goes_below_the_policy_floor(name: str) -> None:
    collector = _make(name, {"rate_limit_delay": 0})
    assert collector.rate_limit_delay >= _cls(name).rate_policy.min_delay_seconds


# -- recorded fixtures (parser canary, CI half) ------------------------------------------

_WITH_FIXTURE = [n for n in NAMES if has_canary(_make(n))]


@pytest.mark.parametrize("name", _WITH_FIXTURE)
def test_recorded_fixture_still_parses_to_at_least_one_job(name: str) -> None:
    raw = parse_fixture(_make(name))
    assert raw and all(job.get("title") for job in raw)
    assert normalize_jobs(name, raw), f"{name}: fixture parsed but no job survived normalization"


@pytest.mark.parametrize("name", _WITH_FIXTURE)
def test_a_changed_page_format_parses_to_nothing(name: str) -> None:
    """What the canary relies on: a redesigned page yields 0 offers, not garbage."""
    fixture = _make(name).fixture()
    assert fixture is not None
    changed = "{}" if fixture.name.endswith(".json") else \
        "<html><body><div class='new-layout'>Nouvelle mise en page</div></body></html>"
    assert fixture.parse(changed) == []


# -- parser canary (run-time half) -------------------------------------------------------


class _Scraper(BaseCollector):
    access_class = "public_page"
    robots_policy = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def fixture(self) -> RecordedFixture:
        return RecordedFixture("x.html", "<li>job</li>", lambda text: [{"title": text}])

    def collect(self) -> CollectorResult:  # pragma: no cover - not collected here
        return CollectorResult(source_name="scraper", jobs=[])


class _Api(_Scraper):
    access_class = "official"

    def fixture(self) -> None:  # type: ignore[override]
        return None


def _served(collector: BaseCollector, size: int, status: int = 200) -> BaseCollector:
    collector._observe_response(httpx.Response(status, text="x" * size))
    return collector


def test_canary_reports_a_served_page_that_yielded_nothing() -> None:
    assert parser_canary(_served(_Scraper(), 50_000), 0) == PARSER_CHANGED


@pytest.mark.parametrize("collector,jobs", [
    (_served(_Scraper(), 50_000), 3),             # offers were read
    (_served(_Scraper(), 100), 0),                # a tiny answer can be a genuine "no offer"
    (_served(_Scraper(), 50_000, status=503), 0),  # nothing was served: a failure, not a parser change
    (_Scraper(), 0),                              # nothing fetched at all
])
def test_canary_stays_quiet_otherwise(collector: BaseCollector, jobs: int) -> None:
    assert parser_canary(collector, jobs) is None


def test_canary_ignores_collectors_without_a_fixture() -> None:
    api = _served(_Api(), 50_000)
    assert not has_canary(api) and parser_canary(api, 0) is None


# -- shared robots.txt helper ------------------------------------------------------------


@pytest.mark.parametrize("status,text,allowed", [
    (200, "User-agent: *\nDisallow: /private\n", True),   # the file's rules
    (200, "  \n", True),                                   # an empty file allows everything
    (404, "", True),                                       # 4xx: no robots.txt, no restriction
    (403, "User-agent: *\nDisallow: /\n", True),           # a 4xx body is not a robots.txt
    (410, "", True),
    (500, "", False),                                      # 5xx: disallow all
    (503, "User-agent: *\nAllow: /\n", False),
    (302, "", False),                                      # an unfollowed redirect is no answer
])
def test_robots_answer_follows_rfc_9309(status: int, text: str, allowed: bool) -> None:
    rules = robots.rules_from_response(status, text, "x")
    assert (rules is not None and robots.robots_allows(rules, "/jobs")) is allowed


@pytest.mark.parametrize("status,text", [(404, ""), (403, "x"), (200, "  \n"), (500, "")])
def test_strict_reading_needs_a_served_robots_txt(status: int, text: str) -> None:
    assert robots.rules_from_response(status, text, "x", strict=True) is None


def test_robots_cache_reads_404_as_no_restriction_and_5xx_as_disallow_all(monkeypatch) -> None:  # noqa: ANN001
    answers = {"https://none.example/robots.txt": (404, ""), "https://broken.example/robots.txt": (502, "")}
    monkeypatch.setattr(robots, "fetch_robots_txt", lambda url, **_: answers[url])
    cache = robots.RobotsCache("FindYourJobWithAI", "ua")
    assert cache.allows("https://none.example/jobs")
    assert not cache.allows("https://broken.example/jobs")
    strict = robots.RobotsCache("FindYourJobWithAI", "ua", strict=True)
    assert not strict.allows("https://none.example/jobs")


def test_france_travail_web_keeps_its_strict_fail_closed_reading() -> None:
    from src.collectors.france_travail_web import FranceTravailWebCollector

    assert FranceTravailWebCollector.robots_strict is True
    assert all(not _cls(n).robots_strict for n in NAMES if n != "france_travail_web")


def test_agent_lines_end_at_the_first_other_line() -> None:
    text = ("User-agent: FindYourJobWithAI\nCrawl-delay: 5\nUser-agent: OtherBot\nDisallow: /x\n"
            "User-agent: *\nDisallow: /y\n")
    # Crawl-delay closed the first group, so "Disallow: /x" belongs to OtherBot only.
    rules = robots.parse_robots(text, "FindYourJobWithAI")
    assert robots.robots_allows(rules, "/x") and robots.robots_allows(rules, "/y")


def test_groups_match_on_the_product_token() -> None:
    text = "User-agent: FindYourJobWithAI/2.0\nDisallow: /private\n\nUser-agent: *\nDisallow: /\n"
    for agent in ("FindYourJobWithAI", "findyourjobwithai/3.1 (local app)"):
        rules = robots.parse_robots(text, agent)
        assert robots.robots_allows(rules, "/jobs") and not robots.robots_allows(rules, "/private/a")


@pytest.mark.parametrize("rule,path,allowed", [
    ("/caf%C3%A9", "/café/menu", False),      # non-ASCII path compared percent-encoded
    ("/café", "/caf%c3%a9/menu", False),      # either side may be the encoded one
    ("/%7Ejoe", "/~joe/index.html", False),   # escapes of unreserved characters decoded
    ("/a%2Fb", "/a/b", True),                 # an encoded "/" stays distinct from "/"
])
def test_paths_are_compared_percent_encoding_normalised(rule: str, path: str, allowed: bool) -> None:
    rules = robots.parse_robots(f"User-agent: *\nDisallow: {rule}\n")
    assert robots.robots_allows(rules, path) is allowed


def test_robots_cache_fetches_each_site_once_and_fails_closed(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    def fetch(url: str, **_: Any) -> tuple[int, str]:
        calls.append(url)
        if "down.example" in url:
            raise httpx.ConnectError("refused")
        return 200, "User-agent: *\nDisallow: /private\n"

    monkeypatch.setattr(robots, "fetch_robots_txt", fetch)
    cache = robots.RobotsCache("FindYourJobWithAI", "ua")
    assert cache.allows("https://a.example/jobs?page=2")
    assert not cache.allows("https://a.example/private/x")
    assert "disallows /private/x" in cache.refusal("https://a.example/private/x")
    assert not cache.allows("https://down.example/jobs")
    assert "unreachable" in cache.refusal("https://down.example/jobs")
    assert calls == ["https://a.example/robots.txt", "https://down.example/robots.txt"]


def test_france_travail_web_reuses_the_shared_parser() -> None:
    from src.collectors import france_travail_web

    assert france_travail_web.parse_robots is robots.parse_robots
    assert france_travail_web.robots_allows is robots.robots_allows


# -- no public_page collector requests a disallowed URL (fake transport) -----------------

_FEED_URL = "https://jobs.example.org/feed.rss"
PUBLIC_PAGE_CONFIGS: dict[str, dict[str, Any]] = {
    "france_travail_web": {"target_domains": ["web_development"], "departments": ["83"]},
    "hellowork": {"metier_slugs": ["developpeur"]},
    "eures": {"queries": [{"keyword": "python", "location_codes": ["fr"]}], "max_pages_per_query": 1},
    "weworkremotely": {"category_rss": False},
    "remote": {"type": "rss", "careers_url": _FEED_URL},
    "rss": {"type": "rss", "careers_url": _FEED_URL},
    "workingnomads": {},
}
_FT_SITEMAP = ("<urlset><url><loc>https://candidat.francetravail.fr/offres/emploi/developpeur-web/var/s28m7d83"
               "</loc></url></urlset>")


class _FakeWeb:
    """Serves robots.txt and, for every other URL, ``page(url)``; records each URL asked for."""

    def __init__(self, robots_txt: str, page: Callable[[str], str]) -> None:
        self.robots_txt = robots_txt
        self.page = page
        self.urls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=self.robots_txt)
        return httpx.Response(200, text=self.page(url))

    def install(self, monkeypatch) -> None:  # noqa: ANN001
        transport = httpx.MockTransport(self.handler)
        real_client = httpx.Client
        passed = ("headers", "params", "json", "timeout", "follow_redirects")

        def client(*args: Any, **kwargs: Any) -> httpx.Client:
            kwargs.pop("transport", None)
            return real_client(*args, transport=transport, **kwargs)

        def request(method: str) -> Callable[..., httpx.Response]:
            def send(url: str, **kwargs: Any) -> httpx.Response:
                with real_client(transport=transport) as c:
                    return c.request(method, url, **{k: v for k, v in kwargs.items() if k in passed})
            return send

        monkeypatch.setattr(httpx, "Client", client)
        monkeypatch.setattr(httpx, "get", request("GET"))
        monkeypatch.setattr(httpx, "post", request("POST"))
        monkeypatch.setattr(robots, "fetch_robots_txt", robots._fetch_robots_txt_live)


def _fixture_page(name: str) -> Callable[[str], str]:
    fixture = _make(name).fixture()
    assert fixture is not None
    return lambda url: _FT_SITEMAP if url.endswith(".xml") else fixture.content


def test_every_public_page_collector_is_covered_here() -> None:
    public = {n for n in NAMES if _cls(n).access_class == "public_page"}
    assert public == set(PUBLIC_PAGE_CONFIGS)


@pytest.mark.parametrize("name", sorted(PUBLIC_PAGE_CONFIGS))
def test_robots_txt_allowing_everything_lets_the_collector_read(name: str, monkeypatch) -> None:  # noqa: ANN001
    """Control for the test below: the fake transport really serves the collector."""
    web = _FakeWeb("User-agent: *\nAllow: /\n", _fixture_page(name))
    web.install(monkeypatch)
    result = _make(name, PUBLIC_PAGE_CONFIGS[name]).collect()
    assert [u for u in web.urls if not u.endswith("/robots.txt")]
    if name != "hellowork":  # its fixture is a job page; the listing page it asks first lists none
        assert result.jobs, result.errors


@pytest.mark.parametrize("name", sorted(PUBLIC_PAGE_CONFIGS))
def test_no_public_page_collector_requests_a_url_robots_txt_disallows(name: str, monkeypatch) -> None:  # noqa: ANN001
    web = _FakeWeb("User-agent: *\nDisallow: /\n", _fixture_page(name))
    web.install(monkeypatch)
    result = _make(name, PUBLIC_PAGE_CONFIGS[name]).collect()
    assert web.urls and all(u.endswith("/robots.txt") for u in web.urls), web.urls
    assert result.jobs == []
    assert any("robots" in e for e in result.errors), result.errors


def test_a_disallowed_path_is_skipped_while_allowed_pages_are_read(monkeypatch) -> None:  # noqa: ANN001
    job_url = "https://www.hellowork.com/fr-fr/emplois/12345678.html"
    listing = ('<script type="application/ld+json">'
               + json.dumps({"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "url": job_url}]})
               + "</script>")
    web = _FakeWeb("User-agent: *\nDisallow: /fr-fr/emplois/\n", lambda url: listing)
    web.install(monkeypatch)
    result = _make("hellowork", {"metier_slugs": ["developpeur"]}).collect()
    assert "https://www.hellowork.com/fr-fr/emploi/metier_developpeur.html" in web.urls
    assert job_url not in web.urls
    assert any("robots.txt disallows /fr-fr/emplois/12345678.html" in e for e in result.errors)


def test_unreadable_robots_txt_means_nothing_is_requested(monkeypatch) -> None:  # noqa: ANN001
    def fetch(url: str, **_: Any) -> tuple[int, str]:
        return 503, ""

    web = _FakeWeb("", lambda url: "")
    web.install(monkeypatch)
    monkeypatch.setattr(robots, "fetch_robots_txt", fetch)
    result = _make("weworkremotely", {"category_rss": False}).collect()
    assert web.urls == []
    assert result.jobs == [] and "robots.txt unavailable (HTTP 503)" in result.errors[0]


# -- canary on real collectors: only offer pages count, genuine empties are fine ---------

_BIG_SITEMAP = "<urlset>" + "".join(
    f"<url><loc>https://candidat.francetravail.fr/offres/emploi/developpeur-web/x/s28m7d{d:02d}</loc></url>"
    for d in range(1, 96) if d != 13) + "</urlset>"


def test_robots_and_sitemap_answers_are_not_pages_served(monkeypatch) -> None:  # noqa: ANN001
    """Review #58: a run whose plan is legitimately empty (no sitemap page for the
    profile's département) is not a parser change, however large the sitemap."""
    web = _FakeWeb("User-agent: *\nAllow: /\n" + "# padding\n" * 400, lambda url: _BIG_SITEMAP)
    web.install(monkeypatch)
    collector = _make("france_travail_web", {"target_domains": ["web_development"], "departments": ["13"]})
    result = collector.collect()
    assert result.jobs == [] and len(_BIG_SITEMAP) > 4 * collector.canary_min_bytes
    assert parser_canary(collector, 0) is None
    assert collector.health()["responses"] == 2 and collector.health()["ok_responses"] == 0  # robots + sitemap


def test_a_served_listing_page_without_offers_is_still_flagged(monkeypatch) -> None:  # noqa: ANN001
    redesigned = "<html><body>" + "<div class='card-v2'>Offre</div>" * 200 + "</body></html>"
    web = _FakeWeb("User-agent: *\nAllow: /\n",
                   lambda url: _FT_SITEMAP if url.endswith(".xml") else redesigned)
    web.install(monkeypatch)
    collector = _make("france_travail_web", {"target_domains": ["web_development"], "departments": ["83"]})
    assert collector.collect().jobs == []
    assert parser_canary(collector, 0) == PARSER_CHANGED


def test_a_listing_that_genuinely_lists_no_offer_is_not_flagged(monkeypatch) -> None:  # noqa: ANN001
    empty_list = ('<html><script type="application/ld+json">{"@type": "ItemList", "itemListElement": []}'
                  "</script>" + "<p>0 offre</p>" * 500 + "</html>")
    web = _FakeWeb("User-agent: *\nAllow: /\n", lambda url: empty_list)
    web.install(monkeypatch)
    collector = _make("hellowork", {"domains": ["informatique"], "metier_slugs": ["developpeur"]})
    result = collector.collect()
    assert result.jobs == [] and collector.health()["empty_results"] == 1
    assert parser_canary(collector, 0) is None


@pytest.mark.parametrize("name,body", [
    ("eures", json.dumps({"numberRecords": 0, "jvs": [], "facets": {"pad": "x" * 5000}})),
    ("workingnomads", "[]" + " " * 5000),
    ("weworkremotely", '<?xml version="1.0"?><rss version="2.0"><channel><title>WWR</title>'
                       + "<!-- pad -->" * 500 + "</channel></rss>"),
])
def test_well_formed_empty_answers_are_not_parser_changes(name: str, body: str, monkeypatch) -> None:  # noqa: ANN001
    web = _FakeWeb("User-agent: *\nAllow: /\n", lambda url: body)
    web.install(monkeypatch)
    collector = _make(name, PUBLIC_PAGE_CONFIGS[name])
    assert collector.collect().jobs == []
    assert parser_canary(collector, 0) is None


def test_no_http_answer_at_all_is_reported_as_unreachable(monkeypatch) -> None:  # noqa: ANN001
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    web = _FakeWeb("", lambda url: "")
    web.handler = refuse  # type: ignore[method-assign]
    web.install(monkeypatch)
    collector = _make("weworkremotely", {"category_rss": False})
    assert collector.collect().jobs == []
    health = collector.health()
    assert health["network_errors"] >= 1 and health["responses"] == 0


# -- rate policy: floor, injected clock/sleep, per-run cap -------------------------------


class _Paced(BaseCollector):
    access_class = "official"
    rate_policy = RatePolicy(min_delay_seconds=1.0, max_requests_per_run=3)

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(rate_limit_delay=0.2)

    def collect(self) -> CollectorResult:  # pragma: no cover
        return CollectorResult(source_name="paced", jobs=[])


def test_delay_is_clamped_and_waits_use_the_injected_clock(monkeypatch) -> None:  # noqa: ANN001
    now = [1000.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(BaseCollector, "_clock", lambda: now[0])
    monkeypatch.setattr(BaseCollector, "_sleep", sleep)
    collector = _Paced()
    assert collector.rate_limit_delay == 1.0
    for _ in range(3):
        collector._enforce_rate_limit()
    assert slept == [1.0, 1.0]


def test_request_cap_refuses_further_requests_and_is_reported() -> None:
    collector = _Paced()
    for i in range(3):
        collector._before_request(f"https://api.example/{i}")
    with pytest.raises(RequestBudgetExceeded):
        collector._before_request("https://api.example/4")
    health = collector.health()
    assert health["requests"] == 3 and health["request_cap_reached"] is True


def test_robots_refusal_is_not_counted_as_a_request(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(robots, "fetch_robots_txt", lambda url, **_: (200, "User-agent: *\nDisallow: /\n"))
    collector = _Scraper()
    assert collector.robots_check("https://site.example/a") is False
    with pytest.raises(RobotsDisallowed):
        collector._before_request("https://site.example/a")
    assert collector.health()["requests"] == 0 and collector.health()["robots_refused"] == 1


def test_a_collector_reaching_its_cap_still_returns_what_it_read(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(WeWorkRemotelyCollector, "rate_policy", RatePolicy(min_delay_seconds=1.0,
                                                                          max_requests_per_run=2))
    web = _FakeWeb("User-agent: *\nAllow: /\n", _fixture_page("weworkremotely"))
    web.install(monkeypatch)
    collector = WeWorkRemotelyCollector({})  # global feed + 6 category feeds
    result = collector.collect()
    assert len([u for u in web.urls if not u.endswith("/robots.txt")]) == 2
    assert result.jobs
    assert sum("request cap of 2 per run reached" in e for e in result.errors) == 5
    assert collector.health()["request_cap_reached"] is True


# -- robots.txt Crawl-delay (audit D12: EURES asks for 10 s) --------------------------------------

EUROPA_ROBOTS = "User-agent: bingbot\nDisallow: /x/\n\nUser-agent: *\t# any\nDisallow: /cgi-bin/\nCrawl-delay: 10\n"


@pytest.mark.parametrize("text,agent,expected", [
    (EUROPA_ROBOTS, "FindYourJobBot", 10.0),
    (EUROPA_ROBOTS, "bingbot", None),                          # its own group sets none
    ("User-agent: *\nCrawl-delay: soon\n", "x", None),
    ("User-agent: *\nCrawl-delay: -3\n", "x", None),
    ("User-agent: *\nDisallow:\n", "x", None),
])
def test_crawl_delay_is_read_from_the_group_that_applies(text: str, agent: str, expected) -> None:  # noqa: ANN001
    assert robots.parse_crawl_delay(text, agent) == expected
    assert robots.parse_robots(EUROPA_ROBOTS, "x")             # rules unchanged by the delay line


def test_requests_to_a_site_wait_its_crawl_delay(monkeypatch) -> None:  # noqa: ANN001
    clock = {"now": 1000.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(round(seconds, 3))
        clock["now"] += seconds

    monkeypatch.setattr(robots, "fetch_robots_txt", lambda _url, **_kw: (200, EUROPA_ROBOTS))
    monkeypatch.setattr("src.collectors.base.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(BaseCollector, "_sleep", sleep)
    collector = _Scraper()
    collector._before_request("https://europa.eu/eures/api/search")
    clock["now"] += 3
    collector._before_request("https://europa.eu/eures/api/search")
    collector._before_request("https://other.example/page")        # another site: no wait
    assert waits == [7.0]


def test_a_huge_crawl_delay_is_capped(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(robots, "fetch_robots_txt", lambda _url, **_kw: (200, "User-agent: *\nCrawl-delay: 86400\n"))
    assert _Scraper()._robots_cache().crawl_delay("https://slow.example/x") == robots.MAX_CRAWL_DELAY_S
