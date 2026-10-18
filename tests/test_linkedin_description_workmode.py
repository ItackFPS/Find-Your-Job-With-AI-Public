"""Regression tests for LinkedIn description fetching and work-mode extraction.

Covers:
  - _extract_work_mode_from_text: French/English signal detection
  - _fetch_description: HTTP fetch behavior (mocked)
  - Scanner post-filter enrichment: description + work_mode updated in record
  - LinkedInConfig.description_fetch_delay loaded from YAML
  - Stats fields: jobs_description_attempted, jobs_description_fetched
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

try:
    import pandas  # noqa: F401
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

_requires_pandas = pytest.mark.skipif(
    not _HAS_PANDAS, reason="pandas (optional 'linkedin' extra) not installed"
)


# ---------------------------------------------------------------------------
# Work-mode text extraction
# ---------------------------------------------------------------------------

class TestExtractWorkModeFromText:
    def _extract(self, text: str):
        from src.linkedin.scanner import _extract_work_mode_from_text
        return _extract_work_mode_from_text(text)

    # -- Remote signals -------------------------------------------------------
    def test_100_teletravail(self):
        assert self._extract("Poste en 100% télétravail, possibilité de bureau.") == "remote"

    def test_100_teletravail_no_accent(self):
        assert self._extract("Poste en 100% teletravail.") == "remote"

    def test_fully_remote(self):
        assert self._extract("This is a fully remote position.") == "remote"

    def test_full_remote(self):
        assert self._extract("Full remote – work from anywhere in France.") == "remote"

    def test_100_remote(self):
        assert self._extract("100% remote work.") == "remote"

    def test_remote_first(self):
        assert self._extract("We are a remote first company.") == "remote"

    def test_remote_first_hyphen(self):
        assert self._extract("Remote-first culture.") == "remote"

    # -- Hybrid signals -------------------------------------------------------
    def test_hybride(self):
        assert self._extract("Poste hybride: 3 jours au bureau, 2 jours à domicile.") == "hybrid"

    def test_hybrid_english(self):
        assert self._extract("Hybrid working arrangement (2 days remote per week).") == "hybrid"

    def test_teletravail_partiel(self):
        assert self._extract("Télétravail partiel possible selon les besoins.") == "hybrid"

    def test_teletravail_partiel_no_accent(self):
        assert self._extract("teletravail partiel envisageable.") == "hybrid"

    # -- On-site signals -------------------------------------------------------
    def test_presentiel(self):
        assert self._extract("Poste en présentiel, Paris 8ème.") == "on_site"

    def test_en_presentiel(self):
        assert self._extract("Travail en présentiel obligatoire.") == "on_site"

    def test_sur_site(self):
        assert self._extract("Présence sur site requise 5 jours par semaine.") == "on_site"

    def test_sur_place(self):
        assert self._extract("Intervention sur place chez nos clients.") == "on_site"

    # -- Ambiguous / negative context -----------------------------------------
    def test_bare_teletravail_returns_none(self):
        """'télétravail' alone must NOT trigger remote — could be 'pas de télétravail'."""
        assert self._extract("Pas de télétravail possible pour ce poste.") is None

    def test_empty_returns_none(self):
        assert self._extract("") is None

    def test_none_returns_none(self):
        assert self._extract(None) is None  # type: ignore[arg-type]

    def test_unrelated_text_returns_none(self):
        assert self._extract("Développeur Python senior avec 5 ans d'expérience.") is None

    # -- Priority: most specific wins -----------------------------------------
    def test_100_teletravail_beats_hybride(self):
        """When both signals are present, 100% télétravail wins over hybride."""
        text = "Poste en 100% télétravail, bien que hybride soit discutable."
        result = self._extract(text)
        assert result == "remote"


# ---------------------------------------------------------------------------
# _fetch_description: HTTP fetch (mocked)
# ---------------------------------------------------------------------------

class TestFetchDescription:
    """Tests for the LinkedIn guest-API description fetcher."""

    def _make_scanner(self):
        from src.linkedin.scanner import LinkedInConfig, LinkedInScanner, LinkedInStagingStorage
        cfg = LinkedInConfig(fetch_description=False, description_fetch_delay=0.0)
        staging = MagicMock(spec=LinkedInStagingStorage)
        return LinkedInScanner(config=cfg, staging=staging)

    def test_returns_text_on_200(self):
        scanner = self._make_scanner()
        html = (
            "<html><body>"
            "<div class='description__text'>This is a great Python job. "
            "We offer télétravail partiel.</div>"
            "</body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = scanner._fetch_description("12345")

        assert result is not None
        assert "Python" in result

    def test_returns_none_on_404(self):
        scanner = self._make_scanner()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = scanner._fetch_description("99999")

        assert result is None

    def test_returns_none_on_429(self):
        scanner = self._make_scanner()
        mock_resp = MagicMock()
        mock_resp.status_code = 429

        with patch("requests.get", return_value=mock_resp):
            result = scanner._fetch_description("12345")

        assert result is None

    def test_returns_none_on_request_exception(self):
        import requests
        scanner = self._make_scanner()

        with patch("requests.get", side_effect=requests.ConnectionError("timeout")):
            result = scanner._fetch_description("12345")

        assert result is None

    def test_returns_none_for_trivially_short_response(self):
        """A response shorter than 50 chars is discarded as uninformative."""
        scanner = self._make_scanner()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<html><body>No content</body></html>"
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = scanner._fetch_description("12345")

        assert result is None

    def test_returns_none_for_login_gated_page(self):
        """LinkedIn now returns HTTP 200 with a sign-in form when the guest API
        is no longer accessible unauthenticated. The login-page boilerplate must
        be detected and discarded so it is never stored as a description."""
        scanner = self._make_scanner()
        # Simulate the login-gated response LinkedIn actually returns
        login_page_html = (
            "<html><body>"
            "<h1>Identifiez-vous pour accéder à des conseils</h1>"
            "<p>S'identifier avec un e-mail</p>"
            "<p>Mot de passe</p>"
            "<p>Inscrivez-vous maintenant</p>"
            "<p>Signaler cette offre d'emploi</p>"
            "<p>" + "dummy content " * 20 + "</p>"  # pad past 50-char threshold
            "</body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = login_page_html
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = scanner._fetch_description("12345")

        assert result is None, (
            "Login-gated response must be discarded — "
            "storing the login page as a description corrupts work-mode detection"
        )

    def test_real_description_not_discarded(self):
        """A real description that mentions authentication in passing (e.g. job
        in cybersecurity mentioning 'S'identifier' once) is NOT discarded."""
        scanner = self._make_scanner()
        real_desc_html = (
            "<html><body>"
            "<div>Rejoignez notre équipe DevOps. Ce poste est en 100% télétravail. "
            "Vous travaillerez sur des systèmes d'authentification (S'identifier, SSO). "
            "Compétences: Python, Kubernetes, Docker. CDD 12 mois. "
            "Démarrage dès que possible. Salaire selon profil. "
            "Postuler directement sur notre site.</div>"
            "</body></html>"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = real_desc_html
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            result = scanner._fetch_description("12345")

        assert result is not None, "Real description must not be discarded"
        assert "télétravail" in result


# ---------------------------------------------------------------------------
# Post-filter enrichment: record updated when fetch_description=False
# ---------------------------------------------------------------------------

@_requires_pandas
class TestPostFilterEnrichment:
    """Verify that _run_query enriches records after the hard filter when
    fetch_description=False and description_fetch_delay > 0."""

    def _make_df_row(
        self,
        job_id: str = "111",
        title: str = "AI Engineer",
        company: str = "OpenAI",
        is_remote: Any = None,
        job_type: Any = None,
    ) -> dict[str, Any]:
        return {
            "id": job_id,
            "job_url": f"https://www.linkedin.com/jobs/view/{job_id}",
            "title": title,
            "company": company,
            "location": "Paris, France",
            "job_type": job_type,
            "date_posted": None,
            "interval": None,
            "min_amount": None,
            "max_amount": None,
            "currency": "EUR",
            "is_remote": is_remote,
            "job_level": None,
            "company_url": None,
            "company_logo": None,
            "description": None,
        }

    def _make_scanner(
        self,
        fetch_description: bool = False,
        delay: float = 0.001,
        enrich: bool = True,
    ):
        from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner
        cfg = LinkedInConfig(
            enabled=True,
            fetch_description=fetch_description,
            enrich_descriptions=enrich,
            description_fetch_delay=delay,
            rate_limit_seconds=0.0,
            queries=[LinkedInQueryConfig(search_term="ai engineer", location="France")],
        )
        staging = MagicMock()
        staging.insert_job.return_value = True
        staging.needs_description.return_value = True  # default: new job (not in DB yet)
        staging.record_also_seen_by = MagicMock()
        return LinkedInScanner(config=cfg, staging=staging)

    def _make_df(self, rows: list[dict]) -> Any:
        """Create a minimal pandas DataFrame-like mock."""
        import pandas as pd
        return pd.DataFrame(rows)

    def test_description_fetched_for_survivor(self):
        """When fetch_description=False and a job passes the filter, description is fetched."""
        scanner = self._make_scanner(fetch_description=False, delay=0.001)
        row = self._make_df_row("222", title="AI Engineer", company="Mistral")
        self._make_df([row])

        fetched_desc = "This is a fully remote AI engineer position in Paris."

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"<html><body>{fetched_desc}</body></html>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            from jobspy import scrape_jobs as _real_scrape  # noqa: F401
        # Mock scrape_jobs and filters_config
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = (
                "<html><body>This is a fully remote AI engineer position "
                "in Paris with Python experience required.</body></html>"
            )
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            # Run _run_query with a mocked scrape_jobs function
            staged_records: list[dict] = []
            scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or True

            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))

            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="ai engineer", location="France")
            q_stats = scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,  # no filter for this test
                scan_seen_ids={},
            )

        assert q_stats.jobs_description_attempted == 1
        assert q_stats.jobs_description_fetched == 1
        assert staged_records, "Expected at least one job staged"
        rec = staged_records[0]
        assert rec.get("description") is not None
        assert rec.get("description_fetched") == 1

    def test_work_mode_extracted_from_fetched_description(self):
        """When description says 'fully remote', work_mode is set to 'remote'."""
        scanner = self._make_scanner(fetch_description=False, delay=0.001)
        row = self._make_df_row("333", title="Backend Developer", company="Acme")
        # job has is_remote=False, no work_mode

        staged_records: list[dict] = []
        scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or True

        remote_desc = (
            "Backend Developer position. Fully remote. "
            "Work from anywhere in France. Python required."
        )

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"<html><body>{remote_desc}</body></html>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="backend developer", location="France")
            scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        assert staged_records, "Expected job to be staged"
        rec = staged_records[0]
        assert rec.get("work_mode") == "remote"
        assert rec.get("is_remote") == 1

    def test_no_enrichment_when_fetch_description_true_and_description_present(self):
        """When fetch_description=True and bulk fetch returned a description,
        no additional targeted fetch is made (description already available)."""
        scanner = self._make_scanner(fetch_description=True, delay=0.001)
        row = self._make_df_row("444", title="Python Developer", company="Corp")
        row["description"] = "Python dev job."  # already has description from jobspy

        staged_records: list[dict] = []
        scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or True

        with patch("requests.get") as mock_get:
            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="python developer", location="France")
            scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        # Verify no extra HTTP request was made for description
        mock_get.assert_not_called()

    def test_no_enrichment_when_enrich_descriptions_false(self):
        """enrich_descriptions=False disables post-filter enrichment regardless of delay."""
        scanner = self._make_scanner(fetch_description=False, delay=3.0, enrich=False)

        row = self._make_df_row("555", title="DevOps Engineer", company="Corp")

        staged_records: list[dict] = []
        scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or True

        with patch("requests.get") as mock_get:
            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="devops engineer", location="France")
            q_stats = scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        mock_get.assert_not_called()
        assert q_stats.jobs_description_attempted == 0

    def test_no_enrichment_for_already_known_job_with_description(self):
        """Description is NOT re-fetched for jobs already staged WITH a description.

        Regression test for the bug where already-known jobs received a 3s
        description fetch even though they were already staged from a prior scan.
        """
        scanner = self._make_scanner(fetch_description=False, delay=0.001)
        row = self._make_df_row("777", title="Python Developer", company="OldCorp")

        # Simulate a job already staged with a complete description.
        scanner.staging.needs_description = MagicMock(return_value=False)
        scanner.staging.insert_job.side_effect = lambda r: False  # already known


        with patch("requests.get") as mock_get:
            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="python developer", location="France")
            q_stats = scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        # No HTTP request must be made — job already has a description
        mock_get.assert_not_called()
        assert q_stats.jobs_description_attempted == 0
        assert q_stats.jobs_already_known == 1

    def test_enrichment_for_known_job_with_stale_missing_description(self):
        """Description IS (re-)fetched for a known job that was staged incomplete.

        Old LinkedIn records collected under the previously-broken description
        enrichment path (description_fetched=0) must be eligible for rediscovery
        rather than being permanently skipped just because they are "known".
        """
        scanner = self._make_scanner(fetch_description=False, delay=0.001)
        row = self._make_df_row("778", title="Python Developer", company="OldCorp")

        # Simulate a job already staged, but with no description ever fetched.
        scanner.staging.needs_description = MagicMock(return_value=True)

        staged_records: list[dict] = []
        scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or False

        recovered_desc = (
            "Python Developer role. Fully remote. Django and PostgreSQL required."
        )

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"<html><body>{recovered_desc}</body></html>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q_stats = scanner._run_query(
                scan_id="scan-test",
                query=LinkedInQueryConfig(search_term="python developer", location="France"),
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        mock_get.assert_called_once()  # the stale record's description WAS re-fetched
        assert q_stats.jobs_description_attempted == 1
        assert q_stats.jobs_description_fetched == 1
        assert staged_records, "Expected insert_job to be called with the recovered description"
        assert staged_records[0].get("description") is not None
        assert staged_records[0].get("description_fetched") == 1

    def test_fallback_fetch_when_bulk_returns_empty_description(self):
        """When fetch_description=True but JobSpy returns no description, AND
        enrich_descriptions=True, the targeted fetch is attempted as fallback."""
        scanner = self._make_scanner(fetch_description=True, delay=0.001, enrich=True)
        # Simulate JobSpy returning a row with no description (bulk fetch failed)
        row = self._make_df_row("888", title="Cloud Engineer", company="Corp")
        # description is None (default in _make_df_row)

        staged_records: list[dict] = []
        scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or True

        fallback_desc = (
            "Cloud Engineer position. Fully remote. "
            "Python and Kubernetes required. Work from anywhere in France."
        )

        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = f"<html><body>{fallback_desc}</body></html>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="cloud engineer", location="France")
            q_stats = scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        mock_get.assert_called_once()  # targeted fallback fetch was made
        assert q_stats.jobs_description_attempted == 1
        assert q_stats.jobs_description_fetched == 1
        assert staged_records, "Expected job to be staged"
        rec = staged_records[0]
        assert rec.get("description") is not None
        assert rec.get("description_fetched") == 1

    def test_no_fallback_when_bulk_empty_and_enrich_false(self):
        """When fetch_description=True, description is empty, BUT enrich_descriptions=False,
        no targeted fetch is made — job is staged without description."""
        scanner = self._make_scanner(fetch_description=True, delay=0.001, enrich=False)
        row = self._make_df_row("889", title="DevOps Engineer", company="Corp")
        # description is None (default)

        staged_records: list[dict] = []
        scanner.staging.insert_job.side_effect = lambda r: staged_records.append(r) or True

        with patch("requests.get") as mock_get:
            import pandas as pd
            scrape_mock = MagicMock(return_value=pd.DataFrame([row]))
            from src.linkedin.scanner import LinkedInQueryConfig
            q = LinkedInQueryConfig(search_term="devops engineer", location="France")
            q_stats = scanner._run_query(
                scan_id="scan-test",
                query=q,
                scrape_jobs=scrape_mock,
                filters_config=None,
                scan_seen_ids={},
            )

        mock_get.assert_not_called()  # no targeted fetch attempted
        assert q_stats.jobs_description_attempted == 0
        assert staged_records, "Job should still be staged (without description)"
        assert staged_records[0].get("description") is None


# ---------------------------------------------------------------------------
# LinkedInConfig: description_fetch_delay loaded from YAML
# ---------------------------------------------------------------------------

class TestLinkedInConfigDescriptionDelay:
    def test_default_delay_is_3(self):
        from src.linkedin.scanner import LinkedInConfig
        cfg = LinkedInConfig()
        assert cfg.description_fetch_delay == 3.0

    def test_enrich_descriptions_default_is_false(self):
        from src.linkedin.scanner import LinkedInConfig
        cfg = LinkedInConfig()
        assert cfg.enrich_descriptions is False

    def test_enrich_descriptions_loaded_from_yaml(self, tmp_path):
        import yaml
        config = {
            "enabled": True,
            "fetch_description": False,
            "enrich_descriptions": True,
            "description_fetch_delay": 3.0,
            "queries": [{"search_term": "python", "location": "France"}],
        }
        cfg_file = tmp_path / "linkedin.yaml"
        cfg_file.write_text(yaml.dump(config), encoding="utf-8")
        from src.linkedin.scanner import LinkedInConfig
        loaded = LinkedInConfig.load(str(tmp_path))
        assert loaded.enrich_descriptions is True

    def test_enrich_descriptions_false_from_yaml(self, tmp_path):
        import yaml
        config = {
            "enabled": True,
            "fetch_description": False,
            "enrich_descriptions": False,
            "description_fetch_delay": 3.0,
            "queries": [{"search_term": "python"}],
        }
        cfg_file = tmp_path / "linkedin.yaml"
        cfg_file.write_text(yaml.dump(config), encoding="utf-8")
        from src.linkedin.scanner import LinkedInConfig
        loaded = LinkedInConfig.load(str(tmp_path))
        assert loaded.enrich_descriptions is False

    def test_delay_loaded_from_yaml(self, tmp_path):
        import yaml
        config = {
            "enabled": True,
            "fetch_description": False,
            "description_fetch_delay": 5.0,
            "rate_limit_seconds": 10.0,
            "queries": [{"search_term": "python", "location": "France"}],
        }
        cfg_file = tmp_path / "linkedin.yaml"
        cfg_file.write_text(yaml.dump(config), encoding="utf-8")
        from src.linkedin.scanner import LinkedInConfig
        loaded = LinkedInConfig.load(str(tmp_path))
        assert loaded.description_fetch_delay == 5.0

    def test_delay_zero_from_yaml(self, tmp_path):
        import yaml
        config = {
            "enabled": True,
            "fetch_description": False,
            "description_fetch_delay": 0,
            "queries": [{"search_term": "python"}],
        }
        cfg_file = tmp_path / "linkedin.yaml"
        cfg_file.write_text(yaml.dump(config), encoding="utf-8")
        from src.linkedin.scanner import LinkedInConfig
        loaded = LinkedInConfig.load(str(tmp_path))
        assert loaded.description_fetch_delay == 0.0
