"""Unit tests for the location / remote-scope filter logic.

France-first policy (as of 2026-08-25):
- Jobs in France (any mode: onsite, hybrid, remote) → ACCEPT.
- Non-remote jobs outside France → REJECT (on-site / hybrid requires physical
  presence; the user works from France).
- Remote jobs outside France → ACCEPT only when the job explicitly signals
  EU-wide or worldwide remote eligibility in the location string.
- Remote jobs outside France with unknown scope → REJECT.
- Jobs with completely unknown location (empty location AND empty countries) →
  ACCEPT (benefit of the doubt; cannot establish they are foreign).

No Internet access.  No collectors called.  No real database touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


from src.models.job import Job
from src.pipeline.filter import filter_jobs, matches_location

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(UTC)
_RECENT = _NOW - timedelta(days=1)


def _job(**kwargs) -> Job:
    """Build a minimal Job for filter testing; kwargs override defaults."""
    defaults: dict = dict(
        source="test",
        title="Software Engineer",
        company="Acme",
        published_at=_RECENT,
        remote=False,
        location="",
        countries=[],
    )
    defaults.update(kwargs)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# Shared filter config — mirrors the production config structure.
# remote.allowed is empty (no remote restriction at filter stage).
# ---------------------------------------------------------------------------

_CFG = {
    "locations": {
        "include": ["France", "Europe"],
    },
    "remote_scope": {
        "european_countries": [
            "France",
            "Germany",
            "Netherlands",
            "Belgium",
            "Spain",
            "Italy",
            "United Kingdom",
            "UK",
            "Switzerland",
            "Austria",
            "Sweden",
            "Denmark",
            "Norway",
            "Finland",
            "Poland",
        ],
        "european_keywords": ["Europe", "EU", "EMEA", "European"],
        "worldwide_keywords": ["worldwide", "global", "anywhere", "international"],
    },
    "remote": {
        "allowed": [],
    },
    "seniority": {"include": [], "exclude": []},
    "posted_within_days": 30,
    "salary_requirements": {},
    "title_keywords": {"include": [], "exclude": []},
}

# Config with Germany explicitly in preferred_countries (tests that Germany
# can be accepted when the user explicitly opts in via preferred_countries).
_CFG_WITH_GERMANY = {
    **_CFG,
    "locations": {"include": ["France", "Europe", "Germany"]},
}

# Config without remote_scope (backward-compatibility path).
_CFG_NO_SCOPE = {k: v for k, v in _CFG.items() if k != "remote_scope"}


# ---------------------------------------------------------------------------
# TestMatchesLocation — France-first policy unit tests
# ---------------------------------------------------------------------------

class TestMatchesLocation:
    """matches_location — France-first policy."""

    # ── France: all modes accepted ────────────────────────────────────────

    def test_france_onsite_accepted(self):
        job = _job(location="Paris, France", countries=["France"], remote=False)
        assert matches_location(job, _CFG) is True

    def test_france_hybrid_accepted(self):
        job = _job(location="Lyon, France (hybrid)", countries=["France"], remote=False)
        assert matches_location(job, _CFG) is True

    def test_france_remote_accepted(self):
        job = _job(location="Paris, France", countries=["France"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_france_only_in_countries_accepted(self):
        """Country match via job.countries even when location string is bare city."""
        job = _job(location="Paris", countries=["France"], remote=False)
        assert matches_location(job, _CFG) is True

    # ── Non-remote + outside France → REJECT ─────────────────────────────

    def test_germany_onsite_rejected(self):
        """Germany onsite: confirmed foreign, not remote → REJECT."""
        job = _job(location="Berlin", countries=["Germany"], remote=False)
        assert matches_location(job, _CFG) is False

    def test_germany_hybrid_rejected(self):
        """Germany hybrid (modelled as remote=False): confirmed foreign → REJECT."""
        job = _job(location="Berlin (hybrid)", countries=["Germany"], remote=False)
        assert matches_location(job, _CFG) is False

    def test_uk_onsite_rejected(self):
        job = _job(location="London", countries=["United Kingdom"], remote=False)
        assert matches_location(job, _CFG) is False

    def test_uk_hybrid_rejected(self):
        job = _job(location="London (hybrid)", countries=["United Kingdom"], remote=False)
        assert matches_location(job, _CFG) is False

    # ── Remote + EU member country (no scope keyword needed) → ACCEPT ────────

    def test_germany_remote_eu_country_accepted(self):
        """Germany is in european_countries → EU-restricted remote job → ACCEPT."""
        job = _job(location="Berlin", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_germany_remote_location_string_eu_country_accepted(self):
        """'Remote job' location string with German country → Germany in european_countries → ACCEPT."""
        job = _job(location="Remote job", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_uk_remote_eu_country_accepted(self):
        """UK is in european_countries → EU-restricted remote → ACCEPT."""
        job = _job(location="London", countries=["United Kingdom"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_uk_abbrev_remote_eu_country_accepted(self):
        """'UK' abbreviation is also in european_countries → ACCEPT."""
        job = _job(location="London", countries=["UK"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_netherlands_remote_eu_country_accepted(self):
        """Netherlands is in european_countries → EU-restricted remote → ACCEPT."""
        job = _job(location="Amsterdam", countries=["Netherlands"], remote=True)
        assert matches_location(job, _CFG) is True

    # ── EU-country policy: focused regression tests ───────────────────────

    def test_single_eu_country_remote_accepted(self):
        """Single EU country (Spain) + remote → ACCEPT."""
        job = _job(location="Remote", countries=["Spain"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_multiple_eu_countries_remote_accepted(self):
        """Multiple EU countries (Germany + Spain) + remote → ACCEPT."""
        job = _job(location="Remote", countries=["Germany", "Spain"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_france_plus_eu_country_remote_accepted(self):
        """France + Germany in countries + remote → ACCEPT (France preferred matches first)."""
        job = _job(location="Remote", countries=["Germany", "France"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_eu_plus_non_eu_mixed_remote_rejected(self):
        """Germany + United States in countries → non-EU present → REJECT."""
        job = _job(location="Remote", countries=["Germany", "United States"], remote=True)
        assert matches_location(job, _CFG) is False

    def test_eu_country_onsite_rejected(self):
        """Germany onsite → non-preferred country, physical presence required → REJECT."""
        job = _job(location="Berlin", countries=["Germany"], remote=False)
        assert matches_location(job, _CFG) is False

    def test_eu_country_hybrid_rejected(self):
        """Spain hybrid (remote=False) → non-preferred, physical presence required → REJECT."""
        job = _job(location="Madrid (hybrid)", countries=["Spain"], remote=False)
        assert matches_location(job, _CFG) is False

    def test_non_eu_remote_rejected(self):
        """United States remote → not in european_countries → REJECT."""
        job = _job(location="Remote", countries=["United States"], remote=True)
        assert matches_location(job, _CFG) is False

    # ── Remote + explicit EU/worldwide scope → ACCEPT ─────────────────────

    def test_germany_remote_worldwide_accepted(self):
        """'Worldwide' in location → explicit worldwide scope → ACCEPT."""
        job = _job(location="Remote (Worldwide)", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_germany_remote_global_accepted(self):
        job = _job(location="Global remote", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_germany_remote_anywhere_accepted(self):
        job = _job(location="Remote - work from anywhere", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_germany_remote_eu_scope_accepted(self):
        """'Europe' in location → EU-wide scope → ACCEPT."""
        job = _job(location="Remote, Europe", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_germany_remote_emea_accepted(self):
        job = _job(location="EMEA - Remote", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_germany_remote_eu_keyword_accepted(self):
        job = _job(location="Remote, EU only", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_uk_remote_france_in_location_accepted(self):
        """'France' explicitly in location string → preferred country match → ACCEPT."""
        job = _job(location="Remote - France eligible", countries=["United Kingdom"], remote=True)
        assert matches_location(job, _CFG) is True

    def test_uk_remote_location_string_eu_country_accepted(self):
        """UK is in european_countries → EU-restricted remote job → ACCEPT."""
        job = _job(location="UK Remote", countries=["United Kingdom"], remote=True)
        assert matches_location(job, _CFG) is True

    # ── No country data: remote + unknown location → benefit of the doubt ──

    def test_remote_only_location_no_countries_accepted(self):
        """'Remote' with no country → unknown origin → ACCEPT."""
        job = _job(location="Remote", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_empty_location_and_countries_remote_accepted(self):
        """Completely unknown location, remote → ACCEPT."""
        job = _job(location="", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_empty_location_and_countries_non_remote_accepted(self):
        """Completely unknown location, non-remote → ACCEPT (cannot establish foreign)."""
        job = _job(location="", countries=[], remote=False)
        assert matches_location(job, _CFG) is True

    # ── Global scope without country data ────────────────────────────────

    def test_worldwide_no_country_accepted(self):
        """'Worldwide' with empty countries → ACCEPT."""
        job = _job(location="Worldwide", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_europe_keyword_no_country_accepted(self):
        job = _job(location="Remote, Europe", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_europe_100_percent_remote_accepted(self):
        job = _job(location="Europe, 100% remote", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_emea_keyword_accepted(self):
        job = _job(location="EMEA - Remote", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_eu_keyword_accepted(self):
        job = _job(location="Remote, EU only", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    def test_europe_in_location_remote_accepted(self):
        job = _job(location="Remote - Europe", countries=[], remote=True)
        assert matches_location(job, _CFG) is True

    # ── Non-European locations always rejected ─────────────────────────────

    def test_usa_remote_rejected(self):
        """USA in job.countries → confirmed non-French foreign → REJECT."""
        job = _job(location="New York, USA", countries=["USA"], remote=True)
        assert matches_location(job, _CFG) is False

    def test_canada_remote_rejected(self):
        job = _job(location="Toronto, Canada", countries=["Canada"], remote=True)
        assert matches_location(job, _CFG) is False

    # ── Explicit Germany in preferred_countries (opt-in) ──────────────────

    def test_germany_onsite_accepted_when_germany_in_preferred(self):
        """When Germany is in preferred_countries it is accepted for on-site jobs."""
        job = _job(location="Berlin", countries=["Germany"], remote=False)
        assert matches_location(job, _CFG_WITH_GERMANY) is True

    # ── No location restriction ────────────────────────────────────────────

    def test_no_location_restriction_allows_all(self):
        cfg = {**_CFG, "locations": {"include": []}}
        job = _job(location="Tokyo, Japan", countries=["Japan"], remote=False)
        assert matches_location(job, cfg) is True

    # ── Backward-compatible path (no remote_scope) ─────────────────────────

    def test_no_remote_scope_france_remote_accepted(self):
        job = _job(location="Paris, France", countries=["France"], remote=True)
        assert matches_location(job, _CFG_NO_SCOPE) is True

    def test_no_remote_scope_germany_remote_rejected(self):
        """Without remote_scope, a remote German job (confirmed foreign) → REJECT."""
        job = _job(location="Berlin", countries=["Germany"], remote=True)
        assert matches_location(job, _CFG_NO_SCOPE) is False

    def test_no_remote_scope_non_remote_france_accepted(self):
        job = _job(location="Paris, France", countries=["France"], remote=False)
        assert matches_location(job, _CFG_NO_SCOPE) is True


# ---------------------------------------------------------------------------
# TestFilterJobsLocationRemote — full filter_jobs end-to-end
# ---------------------------------------------------------------------------

class TestFilterJobsLocationRemote:
    """filter_jobs end-to-end — location interacts with the remote check."""

    def test_france_onsite_passes(self):
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Paris, France", countries=["France"], remote=False,
        )
        passed, rejected = filter_jobs([job], _CFG)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_france_remote_passes(self):
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Paris, France", countries=["France"], remote=True,
        )
        passed, rejected = filter_jobs([job], _CFG)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_germany_onsite_rejected(self):
        """Germany onsite: confirmed foreign, not remote → REJECT."""
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Berlin", countries=["Germany"], remote=False,
        )
        _, rejected = filter_jobs([job], _CFG)
        assert "j1" in rejected
        assert rejected["j1"] == "location"

    def test_germany_onsite_with_germany_in_preferred_passes(self):
        """When Germany is explicitly in preferred_countries, onsite is accepted."""
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Berlin", countries=["Germany"], remote=False,
        )
        passed, rejected = filter_jobs([job], _CFG_WITH_GERMANY)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_germany_remote_eu_country_accepted(self):
        """Germany is in european_countries → remote job passes location filter."""
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Berlin", countries=["Germany"], remote=True,
        )
        passed, rejected = filter_jobs([job], _CFG)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_germany_remote_worldwide_passes(self):
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Remote (Worldwide)", countries=["Germany"], remote=True,
        )
        passed, rejected = filter_jobs([job], _CFG)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_usa_remote_rejected_by_location(self):
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="New York", countries=["USA"], remote=True,
        )
        _, rejected = filter_jobs([job], _CFG)
        assert "j1" in rejected
        assert rejected["j1"] == "location"

    def test_europe_keyword_remote_passes(self):
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Remote, Europe", countries=[], remote=True,
        )
        passed, rejected = filter_jobs([job], _CFG)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_worldwide_remote_no_country_passes(self):
        """'Worldwide' remote job with no country info → ACCEPT."""
        job = _job(
            id="j1", title="Software Engineer", company="A",
            location="Worldwide", countries=[], remote=True,
        )
        passed, rejected = filter_jobs([job], _CFG)
        assert "j1" not in rejected
        assert any(j.id == "j1" for j in passed)

    def test_mixed_batch(self):
        """France remote passes; Germany onsite rejected; EU-scope remote passes;
        USA remote rejected; Germany EU-country remote now passes."""
        jobs = [
            _job(id="fr-remote", location="Paris, France", countries=["France"], remote=True),
            _job(id="de-onsite", location="Berlin", countries=["Germany"], remote=False),
            _job(id="de-eu-remote", location="Remote, Europe", countries=["Germany"], remote=True),
            _job(id="us-remote", location="San Francisco", countries=["USA"], remote=True),
            _job(id="de-eu-country-remote", location="Berlin", countries=["Germany"], remote=True),
        ]
        passed, rejected = filter_jobs(jobs, _CFG)
        passed_ids = {j.id for j in passed}
        assert "fr-remote" in passed_ids
        assert "de-eu-remote" in passed_ids
        assert "de-eu-country-remote" in passed_ids   # Germany in european_countries → ACCEPT
        assert "de-onsite" in rejected
        assert "us-remote" in rejected
        assert rejected["de-onsite"] == "location"
        assert rejected["us-remote"] == "location"

    def test_non_european_onsite_rejected_by_location(self):
        job = _job(
            id="jp-onsite", title="Software Engineer", company="A",
            location="Tokyo", countries=["Japan"], remote=False,
        )
        _, rejected = filter_jobs([job], _CFG)
        assert "jp-onsite" in rejected
        assert rejected["jp-onsite"] == "location"


# ---------------------------------------------------------------------------
# TestHardConstraintPrecedence — location must override a perfect role match
# ---------------------------------------------------------------------------

class TestHardConstraintPrecedence:
    """A perfect role/skill/seniority match must not override a location hard constraint."""

    _CFG_FULL = {
        **_CFG,
        "seniority": {
            "exclude": ["senior", "director", "head of", "chief ", "cto"],
        },
        "title_keywords": {
            "include": ["software engineer", "developer", "junior"],
            "description_include": ["software engineer", "developer"],
            "exclude": [],
        },
    }

    def test_perfect_match_foreign_onsite_rejected(self):
        """Excellent role + seniority + English + foreign onsite → REJECTED at location."""
        job = _job(
            id="perfect-london",
            title="Junior Software Engineer",
            company="Prima",
            location="London",
            countries=["United Kingdom"],
            remote=False,
            description=(
                "We are looking for a Junior Software Engineer to join our team. "
                "You will work with Python, JavaScript, and modern web frameworks."
            ),
            skills=["Python", "JavaScript"],
        )
        _, rejected = filter_jobs([job], self._CFG_FULL)
        assert "perfect-london" in rejected, (
            "A perfect role match must not override a location hard constraint"
        )
        assert rejected["perfect-london"] == "location"

    def test_perfect_match_german_eu_country_remote_passes(self):
        """Germany remote: Germany is in european_countries → passes location filter."""
        job = _job(
            id="perfect-de-remote",
            title="Junior Software Engineer",
            company="GoodCorp",
            location="Berlin",
            countries=["Germany"],
            remote=True,
            description="Junior Software Engineer role. Python, Django, PostgreSQL.",
            skills=["Python", "Django"],
        )
        passed, rejected = filter_jobs([job], self._CFG_FULL)
        assert "perfect-de-remote" not in rejected
        assert any(j.id == "perfect-de-remote" for j in passed)

    def test_perfect_match_france_passes_filter(self):
        """Same excellent match but in France → passes location and all other checks."""
        job = _job(
            id="perfect-fr",
            title="Junior Software Engineer",
            company="GoodCorp",
            location="Paris, France",
            countries=["France"],
            remote=False,
            description="Junior Software Engineer role. Python, Django, PostgreSQL.",
            skills=["Python", "Django"],
        )
        passed, _ = filter_jobs([job], self._CFG_FULL)
        assert any(j.id == "perfect-fr" for j in passed), (
            "The same excellent-match job in France must pass the filter"
        )


# ---------------------------------------------------------------------------
# TestDataIntegrity — job location must not be replaced by company location
# ---------------------------------------------------------------------------

class TestDataIntegrity:
    """Job location (where you work) must not be confused with company location."""

    def test_london_job_german_company_uses_job_location(self):
        """A London job from a German company must store the London location,
        not the company headquarters.  The filter should reject it (London,
        non-remote, outside France)."""
        job = _job(
            id="london-prima",
            location="London",
            countries=["United Kingdom"],  # job location, not company HQ
            remote=False,
        )
        _, rejected = filter_jobs([job], _CFG)
        assert "london-prima" in rejected
        assert rejected["london-prima"] == "location"

    def test_paris_job_german_company_uses_job_location(self):
        """A Paris job from a German company must use the Paris location (France) →
        passes the filter."""
        job = _job(
            id="paris-de-company",
            location="Paris, France",
            countries=["France"],  # job location is France
            remote=False,
        )
        passed, _ = filter_jobs([job], _CFG)
        assert any(j.id == "paris-de-company" for j in passed)

    def test_germany_job_countries_does_not_bleed_into_uk_job(self):
        """Two jobs in the same batch: one German, one UK.  Their country fields
        must remain independent."""
        de_job = _job(id="de", location="Berlin", countries=["Germany"], remote=False)
        uk_job = _job(id="uk", location="London", countries=["United Kingdom"], remote=False)
        _, rejected = filter_jobs([de_job, uk_job], _CFG)
        assert "de" in rejected
        assert "uk" in rejected
        assert rejected["de"] == "location"
        assert rejected["uk"] == "location"
