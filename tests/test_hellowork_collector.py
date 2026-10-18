"""Tests for the HelloWork collector (role-listing pages + JSON-LD job pages)."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.hellowork import HelloWorkCollector
from src.collectors.registry import CollectorRegistry


def _html_response(html: str, status_code: int = 200) -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.text = html
    mock.raise_for_status = MagicMock()
    return mock


def _listing_html(job_urls: list[str]) -> str:
    itemlist = {
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "url": url, "position": i + 1}
            for i, url in enumerate(job_urls)
        ],
    }
    return f'<html><script type="application/ld+json">{json.dumps(itemlist)}</script></html>'


def _domain_index_html(metier_slugs: list[str]) -> str:
    links = "".join(
        f'<a href="/fr-fr/emploi/index-metier_{s}.html">x</a>'
        f'<a href="/fr-fr/emploi/metier_{s}.html">y</a>'
        for s in metier_slugs
    )
    return f"<html><body>{links}</body></html>"


def _detail_html(title: str = "Développeur Python", company: str = "Acme") -> str:
    ld = {
        "@type": "JobPosting",
        "title": title,
        "description": "<p>Job description</p>",
        "datePosted": "2026-09-01T00:00:00Z",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"@type": "Organization", "name": company},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Paris",
                "addressCountry": "FR",
                "postalCode": "75000",
            },
        },
    }
    return f'<html><script type="application/ld+json">{json.dumps(ld)}</script></html>'


class TestRegistry:
    def test_hellowork_registered(self) -> None:
        assert "hellowork" in CollectorRegistry().list_collectors()


class TestHelloWorkCollector:
    def test_no_metier_slugs_configured(self) -> None:
        result = HelloWorkCollector(config={}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/83072305.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[_html_response(_listing_html([job_url])), _html_response(_detail_html())],
        ):
            result = HelloWorkCollector(config={"metier_slugs": ["developpeur"]}).collect()

        assert result.source_name == "hellowork"
        assert result.errors == []
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Développeur Python"
        assert job["company"] == "Acme"
        assert job["source_job_id"] == "83072305"
        assert job["canonical_url"] == job_url

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_empty_listing(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            return_value=_html_response(_listing_html([])),
        ):
            result = HelloWorkCollector(config={"metier_slugs": ["devops"]}).collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_listing_failure_recorded_others_continue(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/1.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[
                RuntimeError("HTTP 500"),
                _html_response(_listing_html([job_url])),
                _html_response(_detail_html()),
            ],
        ):
            result = HelloWorkCollector(
                config={"metier_slugs": ["broken-slug", "developpeur"]}
            ).collect()
        assert len(result.jobs) == 1
        assert len(result.errors) == 1
        assert "broken-slug" in result.errors[0]

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_detail_page_failure_recorded(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/2.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[_html_response(_listing_html([job_url])), RuntimeError("timeout")],
        ):
            result = HelloWorkCollector(config={"metier_slugs": ["developpeur"]}).collect()
        assert result.jobs == []
        assert any("timeout" in e for e in result.errors)

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_no_jobposting_ld_on_detail_page_skipped(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/3.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[_html_response(_listing_html([job_url])), _html_response("<html></html>")],
        ):
            result = HelloWorkCollector(config={"metier_slugs": ["developpeur"]}).collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_max_jobs_per_slug_bounds_detail_fetches(self, _mock_rl: MagicMock) -> None:
        urls = [f"https://www.hellowork.com/fr-fr/emplois/{i}.html" for i in range(5)]
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[_html_response(_listing_html(urls))] + [_html_response(_detail_html())] * 2,
        ) as mock_fetch:
            result = HelloWorkCollector(
                config={"metier_slugs": ["developpeur"], "max_jobs_per_slug": 2}
            ).collect()
        assert len(result.jobs) == 2
        assert mock_fetch.call_count == 3  # 1 listing + 2 detail

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_duplicate_job_ids_across_slugs_deduplicated(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/42.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[
                _html_response(_listing_html([job_url])),
                _html_response(_detail_html()),
                _html_response(_listing_html([job_url])),
            ],
        ):
            result = HelloWorkCollector(
                config={"metier_slugs": ["developpeur", "devops"]}
            ).collect()
        assert len(result.jobs) == 1


class TestDomainTaxonomyDiscovery:
    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_domain_expands_to_metier_slugs(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            return_value=_html_response(_domain_index_html(["developpeur", "devops"])),
        ) as mock_fetch:
            slugs, errors = HelloWorkCollector(config={"domains": ["informatique"]})._resolve_metier_slugs()
        assert slugs == ["developpeur", "devops"]
        assert errors == []
        mock_fetch.assert_called_once()
        assert "index-domaine_informatique.html" in mock_fetch.call_args[0][0]

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_index_metier_links_excluded(self, _mock_rl: MagicMock) -> None:
        # _domain_index_html emits both index-metier_x and metier_x links per slug;
        # only the latter (job listing pages) should be picked up.
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            return_value=_html_response(_domain_index_html(["developpeur"])),
        ):
            slugs, _ = HelloWorkCollector(config={"domains": ["informatique"]})._resolve_metier_slugs()
        assert slugs == ["developpeur"]

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_multiple_domains_deduplicated_and_merged(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[
                _html_response(_domain_index_html(["developpeur", "devops"])),
                _html_response(_domain_index_html(["data-analyst", "devops"])),  # devops overlaps
            ],
        ):
            slugs, errors = HelloWorkCollector(
                config={"domains": ["informatique", "data-et-ia"], "metier_slugs": ["scrum-master"]}
            )._resolve_metier_slugs()
        assert slugs == ["developpeur", "devops", "data-analyst", "scrum-master"]
        assert errors == []

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_domain_fetch_failure_recorded_other_domains_continue(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[RuntimeError("HTTP 500"), _html_response(_domain_index_html(["data-analyst"]))],
        ):
            slugs, errors = HelloWorkCollector(
                config={"domains": ["broken-domain", "data-et-ia"]}
            )._resolve_metier_slugs()
        assert slugs == ["data-analyst"]
        assert len(errors) == 1
        assert "broken-domain" in errors[0]

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_full_collection_using_domains(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/999.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[
                _html_response(_domain_index_html(["developpeur"])),
                _html_response(_listing_html([job_url])),
                _html_response(_detail_html()),
            ],
        ):
            result = HelloWorkCollector(config={"domains": ["informatique"]}).collect()
        assert result.errors == []
        assert len(result.jobs) == 1
        assert result.jobs[0]["source_job_id"] == "999"

    def test_no_domains_and_no_metier_slugs_is_an_error(self) -> None:
        result = HelloWorkCollector(config={}).collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestResumableCheckpointHooks:
    """Tests for collect()'s skip_slugs/on_slug_done params — the mechanism
    src/hellowork/scanner.py uses to persist a per-slug checkpoint and skip
    already-completed slugs on a resumed scan (see that module's docstring).
    """

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_skip_slugs_are_never_fetched(self, _mock_rl: MagicMock) -> None:
        job_url = "https://www.hellowork.com/fr-fr/emplois/1.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[_html_response(_listing_html([job_url])), _html_response(_detail_html())],
        ) as mock_fetch:
            result = HelloWorkCollector(
                config={"metier_slugs": ["already-done", "developpeur"]}
            ).collect(skip_slugs={"already-done"})

        # Only "developpeur"'s listing + 1 detail page were fetched — the
        # skipped slug's listing page was never requested at all.
        assert mock_fetch.call_count == 2
        fetched_urls = [call.args[0] for call in mock_fetch.call_args_list]
        assert not any("already-done" in u for u in fetched_urls)
        assert len(result.jobs) == 1

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_on_slug_done_called_once_per_processed_slug_with_its_jobs(self, _mock_rl: MagicMock) -> None:
        url_a = "https://www.hellowork.com/fr-fr/emplois/1.html"
        url_b = "https://www.hellowork.com/fr-fr/emplois/2.html"
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[
                _html_response(_listing_html([url_a])),
                _html_response(_detail_html(title="A")),
                _html_response(_listing_html([url_b])),
                _html_response(_detail_html(title="B")),
            ],
        ):
            calls: list[tuple[str, list]] = []
            HelloWorkCollector(config={"metier_slugs": ["dev-a", "dev-b"]}).collect(
                on_slug_done=lambda slug, jobs: calls.append((slug, jobs))
            )

        assert [c[0] for c in calls] == ["dev-a", "dev-b"]
        assert len(calls[0][1]) == 1 and calls[0][1][0]["title"] == "A"
        assert len(calls[1][1]) == 1 and calls[1][1][0]["title"] == "B"

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_on_slug_done_called_even_when_slug_yields_zero_jobs(self, _mock_rl: MagicMock) -> None:
        """A slug whose listing succeeds but whose jobs are all
        skipped/failed (e.g. no JobPosting JSON-LD) is still fully attempted
        and must be checked off — otherwise a resume would retry it forever
        even though there was nothing wrong with processing it."""
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            return_value=_html_response(_listing_html([])),
        ):
            calls: list[tuple[str, list]] = []
            HelloWorkCollector(config={"metier_slugs": ["empty-slug"]}).collect(
                on_slug_done=lambda slug, jobs: calls.append((slug, jobs))
            )

        assert calls == [("empty-slug", [])]

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_on_slug_done_not_called_for_a_skipped_slug(self, _mock_rl: MagicMock) -> None:
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            return_value=_html_response(_listing_html([])),
        ):
            calls: list[str] = []
            HelloWorkCollector(config={"metier_slugs": ["done-already", "todo"]}).collect(
                on_slug_done=lambda slug, jobs: calls.append(slug),
                skip_slugs={"done-already"},
            )

        assert calls == ["todo"]

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_listing_failure_does_not_trigger_on_slug_done(self, _mock_rl: MagicMock) -> None:
        """A slug whose listing page fails to fetch was NOT fully attempted
        — it must not be checked off, so a resume retries it."""
        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=RuntimeError("HTTP 500"),
        ):
            calls: list[str] = []
            HelloWorkCollector(config={"metier_slugs": ["broken"]}).collect(
                on_slug_done=lambda slug, jobs: calls.append(slug)
            )

        assert calls == []


class TestGracefulShutdown:
    """Regression coverage for shutdown_event, mirroring the LinkedIn scanner
    solution (see TestGracefulShutdown in tests/test_linkedin_scanner.py):
    same lifecycle semantics -- checked between units of work, never marks a
    partially-fetched unit as done, never touches checkpoint save/load
    itself. Here the checkpointed unit is a metier slug, not a page, so a
    mid-slug stop cannot report partial credit for that slug (on_slug_done
    is simply not called) -- the next scan re-fetches it in full, which is
    safe/idempotent (insert_job() dedups) and exactly matches how a caught
    per-slug error already behaves (see test_listing_failure_does_not_
    trigger_on_slug_done above)."""

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_shutdown_before_first_slug_stops_without_fetching(self, _mock_rl: MagicMock) -> None:
        event = threading.Event()
        event.set()

        with patch("src.collectors.hellowork.HelloWorkCollector._fetch_with_retry") as mock_fetch:
            calls: list[str] = []
            result = HelloWorkCollector(config={"metier_slugs": ["dev-a", "dev-b"]}).collect(
                on_slug_done=lambda slug, jobs: calls.append(slug),
                shutdown_event=event,
            )

        mock_fetch.assert_not_called()
        assert calls == []
        assert result.jobs == []

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_shutdown_between_slugs_stops_before_next_slug_completed_one_preserved(
        self, _mock_rl: MagicMock
    ) -> None:
        event = threading.Event()
        url_a = "https://www.hellowork.com/fr-fr/emplois/1.html"

        calls: list[str] = []

        def on_slug_done(slug, jobs):
            calls.append(slug)
            # Simulate the backend shutdown handler firing right after
            # slug-a's checkpoint was saved (the real save happens in
            # HelloWorkScanner._stage_slug, called from this same callback).
            event.set()

        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry",
            side_effect=[
                _html_response(_listing_html([url_a])),
                _html_response(_detail_html(title="A")),
            ],
        ) as mock_fetch:
            HelloWorkCollector(config={"metier_slugs": ["dev-a", "dev-b"]}).collect(
                on_slug_done=on_slug_done, shutdown_event=event,
            )

        assert calls == ["dev-a"]  # dev-b never started
        assert mock_fetch.call_count == 2  # dev-a's listing + one detail page only

    @patch("src.collectors.hellowork.HelloWorkCollector._enforce_rate_limit")
    def test_shutdown_mid_slug_stops_before_next_job_and_slug_not_marked_done(
        self, _mock_rl: MagicMock
    ) -> None:
        event = threading.Event()
        url_a = "https://www.hellowork.com/fr-fr/emplois/1.html"
        url_b = "https://www.hellowork.com/fr-fr/emplois/2.html"

        fetch_count = 0

        def fake_fetch(url, headers=None):
            nonlocal fetch_count
            fetch_count += 1
            if url.endswith("emploi/metier_dev-a.html"):
                return _html_response(_listing_html([url_a, url_b]))
            if url == url_a:
                event.set()  # shutdown requested right after the 1st job fetch
                return _html_response(_detail_html(title="A"))
            raise AssertionError(f"must not fetch {url} after shutdown was requested")

        with patch(
            "src.collectors.hellowork.HelloWorkCollector._fetch_with_retry", side_effect=fake_fetch,
        ):
            calls: list[str] = []
            HelloWorkCollector(config={"metier_slugs": ["dev-a", "dev-b"]}).collect(
                on_slug_done=lambda slug, jobs: calls.append(slug), shutdown_event=event,
            )

        # dev-a was interrupted mid-slug -- must NOT be checked off (or its
        # partial jobs staged), and dev-b must never have started.
        assert calls == []
        assert fetch_count == 2  # listing + job A's detail page only, not job B
