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


@pytest.mark.parametrize("status,text", [(404, "User-agent: *\nDisallow:\n"), (500, ""), (200, "  \n"),
                                         (403, "User-agent: *\nAllow: /\n")])
def test_robots_answer_that_is_not_a_real_robots_txt_fails_closed(status: int, text: str) -> None:
    assert robots.rules_from_response(status, text, "x") is None


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
