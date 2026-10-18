"""Unit tests for Indeed collector pure-function parsers.

No network access. All fixtures are hard-coded GraphQL response structures
derived from Phase 0 benchmark observations (2026-08-31).
"""

from __future__ import annotations


from src.collectors.indeed import (
    _build_graphql_query,
    _dept_from_postal,
    _map_job_to_raw,
    _parse_employment_type,
    _parse_published_at,
    _parse_salary,
    _parse_work_mode,
    _JOBS_PER_PAGE,
    _HYBRID_ATTR_KEYS,
    _ONSITE_ATTR_KEY,
    _REMOTE_ATTR_KEY,
)

# ---------------------------------------------------------------------------
# Fixtures — minimal GraphQL job dicts matching real API structure
# ---------------------------------------------------------------------------

JOB_FULL = {
    "key": "abc123def456",  # gitleaks:allow (synthetic job key)
    "title": "Développeur Python",
    "datePublished": 1756000000000,  # 2025-08-23T04:26:40+00:00
    "description": {"html": "<p>We are hiring a Python dev.</p>"},
    "location": {
        "countryName": "France",
        "countryCode": "FR",
        "admin1Code": "CVL",
        "city": "Tours",
        "postalCode": "37200",
        "formatted": {"short": "Tours", "long": "37200 Tours"},
    },
    "compensation": {
        "baseSalary": {
            "unitOfWork": "YEAR",
            "range": {"min": 35000.0, "max": 55000.0},
        },
        "currencyCode": "EUR",
        "estimated": None,
    },
    "attributes": [
        {"key": "CF3CP", "label": "Temps plein"},
    ],
    "employer": {
        "relativeCompanyPageUrl": "/cmp/Acme",
        "name": "Acme Corp",
    },
    "recruit": {
        "viewJobUrl": "https://apply.indeed.com/job/abc123",
        "workSchedule": "Full-time",
    },
}

JOB_REMOTE = {
    **JOB_FULL,
    "key": "remote001",
    "attributes": [
        {"key": _REMOTE_ATTR_KEY, "label": "Télétravail"},
        {"key": "CF3CP", "label": "Temps plein"},
    ],
    "location": {
        **JOB_FULL["location"],
        "city": "",
        "postalCode": "",
        "formatted": {"short": "Télétravail", "long": "Télétravail"},
    },
}

JOB_INTERNSHIP = {
    **JOB_FULL,
    "key": "intern001",
    "title": "Stagiaire développeur",
    "attributes": [{"key": "VDTG7", "label": "Stage"}],
    "compensation": None,
}

JOB_ALTERNANCE = {
    **JOB_FULL,
    "key": "alt001",
    "attributes": [{"key": "ALT001", "label": "Alternance"}],
}

JOB_NO_COMPANY = {
    **JOB_FULL,
    "key": "nocompany001",
    "employer": None,
    "recruit": None,
}

JOB_OVERSEAS = {
    **JOB_FULL,
    "key": "overseas001",
    "location": {
        **JOB_FULL["location"],
        "city": "Fort-de-France",
        "postalCode": "97200",
        "formatted": {"short": "Fort-de-France", "long": "97200 Fort-de-France"},
    },
}

JOB_ESTIMATED_SALARY = {
    **JOB_FULL,
    "key": "est_sal001",
    "compensation": {
        "baseSalary": None,
        "currencyCode": None,
        "estimated": {
            "currencyCode": "EUR",
            "baseSalary": {
                "unitOfWork": "YEAR",
                "range": {"min": 28000.0, "max": 32000.0},
            },
        },
    },
}

JOB_HYBRID_LOC = {
    **JOB_FULL,
    "key": "hybrid001",
    "location": {
        **JOB_FULL["location"],
        "formatted": {"short": "Paris (Hybride)", "long": "75001 Paris (Hybride)"},
    },
    "attributes": [],
}


# ---------------------------------------------------------------------------
# _dept_from_postal
# ---------------------------------------------------------------------------


class TestDeptFromPostal:
    def test_metro_dept_paris(self):
        assert _dept_from_postal("75001", "FR") == "75"

    def test_metro_dept_tours(self):
        assert _dept_from_postal("37200", "FR") == "37"

    def test_corsica_2a(self):
        assert _dept_from_postal("20000", "FR") == "2A"

    def test_corsica_2b(self):
        assert _dept_from_postal("20600", "FR") == "2B"

    def test_martinique(self):
        assert _dept_from_postal("97200", "FR") == "972"

    def test_reunion(self):
        assert _dept_from_postal("97400", "FR") == "974"

    def test_non_france(self):
        assert _dept_from_postal("75001", "DE") is None

    def test_empty_postal(self):
        assert _dept_from_postal("", "FR") is None

    def test_short_postal(self):
        assert _dept_from_postal("7500", "FR") is None

    def test_non_numeric(self):
        assert _dept_from_postal("ABCDE", "FR") is None

    def test_invalid_dept_96(self):
        # 96xxx doesn't correspond to a valid French department
        assert _dept_from_postal("96000", "FR") is None

    def test_valid_dept_01(self):
        assert _dept_from_postal("01000", "FR") == "01"

    def test_valid_dept_95(self):
        assert _dept_from_postal("95000", "FR") == "95"


# ---------------------------------------------------------------------------
# _parse_salary
# ---------------------------------------------------------------------------


class TestParseSalary:
    def test_full_salary(self):
        comp = {
            "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 35000.0, "max": 55000.0}},
            "currencyCode": "EUR",
            "estimated": None,
        }
        mn, mx, cur = _parse_salary(comp)
        assert mn == 35000.0
        assert mx == 55000.0
        assert cur == "EUR"

    def test_estimated_salary_fallback(self):
        comp = {
            "baseSalary": None,
            "currencyCode": None,
            "estimated": {
                "currencyCode": "EUR",
                "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 28000.0, "max": 32000.0}},
            },
        }
        mn, mx, cur = _parse_salary(comp)
        assert mn == 28000.0
        assert mx == 32000.0
        assert cur == "EUR"

    def test_no_compensation(self):
        mn, mx, cur = _parse_salary(None)
        assert mn is None
        assert mx is None
        assert cur == "EUR"

    def test_empty_compensation(self):
        mn, mx, cur = _parse_salary({})
        assert mn is None
        assert mx is None

    def test_employer_salary_preferred_over_estimated(self):
        comp = {
            "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 40000.0, "max": 60000.0}},
            "currencyCode": "EUR",
            "estimated": {
                "currencyCode": "EUR",
                "baseSalary": {"unitOfWork": "YEAR", "range": {"min": 28000.0, "max": 32000.0}},
            },
        }
        mn, mx, _cur = _parse_salary(comp)
        assert mn == 40000.0  # employer value, not estimated

    def test_bad_range_values_return_none(self):
        comp = {
            "baseSalary": {"unitOfWork": "YEAR", "range": {"min": "n/a", "max": None}},
            "currencyCode": "EUR",
            "estimated": None,
        }
        mn, mx, _cur = _parse_salary(comp)
        assert mn is None
        assert mx is None


# ---------------------------------------------------------------------------
# _parse_employment_type
# ---------------------------------------------------------------------------


class TestParseEmploymentType:
    def test_full_time_french(self):
        attrs = [{"key": "CF3CP", "label": "Temps plein"}]
        assert _parse_employment_type(attrs) == "full-time"

    def test_internship(self):
        attrs = [{"key": "VDTG7", "label": "Stage"}]
        assert _parse_employment_type(attrs) == "internship"

    def test_alternance(self):
        attrs = [{"key": "ALT", "label": "Alternance"}]
        assert _parse_employment_type(attrs) == "alternance"

    def test_part_time(self):
        attrs = [{"key": "75GKK", "label": "Temps partiel"}]
        assert _parse_employment_type(attrs) == "part-time"

    def test_multiple_attrs_picks_first_match(self):
        attrs = [
            {"key": "OTHER", "label": "Datastage"},  # software product, not emp type
            {"key": "CF3CP", "label": "Temps plein"},
        ]
        assert _parse_employment_type(attrs) == "full-time"

    def test_no_matching_attr(self):
        attrs = [{"key": "X1", "label": "Datastage"}]
        assert _parse_employment_type(attrs) is None

    def test_empty_attrs(self):
        assert _parse_employment_type([]) is None


# ---------------------------------------------------------------------------
# _parse_work_mode
# ---------------------------------------------------------------------------


class TestParseWorkMode:
    def test_remote_by_dsqf7_key(self):
        attrs = [{"key": _REMOTE_ATTR_KEY, "label": "Télétravail"}]
        mode, remote = _parse_work_mode(attrs, "France")
        assert mode == "remote"
        assert remote is True

    def test_remote_by_location_string(self):
        mode, remote = _parse_work_mode([], "Télétravail")
        assert mode == "remote"
        assert remote is True

    def test_hybrid_by_location_string(self):
        mode, remote = _parse_work_mode([], "75001 Paris (Hybride)")
        assert mode == "hybrid"
        assert remote is False

    def test_on_site_unknown(self):
        mode, remote = _parse_work_mode([{"key": "CF3CP", "label": "Temps plein"}], "37200 Tours")
        assert mode is None
        assert remote is False

    def test_remote_beats_hybrid(self):
        attrs = [{"key": _REMOTE_ATTR_KEY, "label": "Télétravail"}]
        mode, remote = _parse_work_mode(attrs, "75001 Paris (Hybride)")
        assert mode == "remote"
        assert remote is True

    def test_hybrid_by_paxzc_key(self):
        attrs = [{"key": "PAXZC", "label": "Télétravail partiel"}]
        mode, remote = _parse_work_mode(attrs, "75002 Paris")
        assert mode == "hybrid"
        assert remote is False

    def test_hybrid_by_n83eh_key(self):
        attrs = [{"key": "N83EH", "label": "Travail à domicile occasionnel"}]
        mode, remote = _parse_work_mode(attrs, "69000 Lyon")
        assert mode == "hybrid"
        assert remote is False

    def test_onsite_by_swg7t_key(self):
        attrs = [{"key": "SWG7T", "label": "En présentiel"}]
        mode, remote = _parse_work_mode(attrs, "33000 Bordeaux")
        assert mode == "on_site"
        assert remote is False

    def test_remote_beats_onsite_attribute(self):
        # DSQF7 takes priority over SWG7T if both present
        attrs = [
            {"key": _REMOTE_ATTR_KEY, "label": "Télétravail"},
            {"key": _ONSITE_ATTR_KEY, "label": "En présentiel"},
        ]
        mode, remote = _parse_work_mode(attrs, "Paris")
        assert mode == "remote"
        assert remote is True

    def test_hybrid_beats_onsite_attribute(self):
        # PAXZC + SWG7T together → hybrid wins (some remote allowed)
        attrs = [
            {"key": "PAXZC", "label": "Télétravail partiel"},
            {"key": _ONSITE_ATTR_KEY, "label": "En présentiel"},
        ]
        mode, remote = _parse_work_mode(attrs, "32600 Pujaudran")
        assert mode == "hybrid"
        assert remote is False

    def test_remote_by_location_teletravail_only(self):
        # Only location signal, no attribute key
        mode, remote = _parse_work_mode([], "teletravail")
        assert mode == "remote"
        assert remote is True

    def test_no_signals_returns_none(self):
        attrs = [{"key": "CF3CP", "label": "Temps plein"}]
        mode, remote = _parse_work_mode(attrs, "44000 Nantes")
        assert mode is None
        assert remote is False

    def test_empty_attributes_unknown_location(self):
        mode, remote = _parse_work_mode([], "")
        assert mode is None
        assert remote is False

    def test_hybrid_attr_keys_constant_contains_expected(self):
        assert "PAXZC" in _HYBRID_ATTR_KEYS
        assert "N83EH" in _HYBRID_ATTR_KEYS


# ---------------------------------------------------------------------------
# _parse_published_at
# ---------------------------------------------------------------------------


class TestParsePublishedAt:
    def test_valid_timestamp(self):
        result = _parse_published_at(1756000000000)
        assert result is not None
        assert "2025" in result
        assert "+00:00" in result

    def test_zero(self):
        assert _parse_published_at(0) is None

    def test_none(self):
        assert _parse_published_at(None) is None

    def test_negative(self):
        assert _parse_published_at(-1000) is None


# ---------------------------------------------------------------------------
# _build_graphql_query
# ---------------------------------------------------------------------------


class TestBuildGraphqlQuery:
    def test_keyword_and_location_present(self):
        q = _build_graphql_query("python", "France")
        assert 'what: "python"' in q
        assert 'where: "France"' in q
        assert f"limit: {_JOBS_PER_PAGE}" in q

    def test_cursor_included_when_provided(self):
        q = _build_graphql_query("python", "France", cursor="ABC123")
        assert 'cursor: "ABC123"' in q

    def test_no_cursor_line_when_none(self):
        q = _build_graphql_query("python", "France", cursor=None)
        assert 'cursor: "' not in q

    def test_date_filter_included(self):
        q = _build_graphql_query("python", "France", date_filter_hours=720)
        assert "720h" in q

    def test_no_date_filter_by_default(self):
        q = _build_graphql_query("python", "France")
        assert "dateOnIndeed" not in q

    def test_keyword_quote_escaping(self):
        q = _build_graphql_query('dev "python"', "France")
        assert '\\"python\\"' in q

    def test_query_is_string(self):
        q = _build_graphql_query("python", "France")
        assert isinstance(q, str)
        assert len(q) > 100


# ---------------------------------------------------------------------------
# _map_job_to_raw
# ---------------------------------------------------------------------------


class TestMapJobToRaw:
    def test_full_job_all_fields(self):
        raw = _map_job_to_raw(JOB_FULL)
        assert raw["source_job_id"] == "abc123def456"
        assert raw["title"] == "Développeur Python"
        assert raw["company"] == "Acme Corp"
        assert "hiring a Python dev" in raw["description"]
        assert raw["location"] == "37200 Tours"
        assert raw["countries"] == ["FR"]
        assert raw["department_code"] == "37"
        assert raw["salary_min"] == 35000.0
        assert raw["salary_max"] == 55000.0
        assert raw["currency"] == "EUR"
        assert raw["type"] == "full-time"
        assert raw["work_mode"] is None  # Temps plein, not remote
        assert raw["remote"] is False
        assert raw["canonical_url"] == "https://fr.indeed.com/viewjob?jk=abc123def456"
        assert raw["apply_url"] == "https://apply.indeed.com/job/abc123"
        assert raw["company_url"] == "https://fr.indeed.com/cmp/Acme"
        assert raw["published_at"] is not None
        assert raw["skills"] == []

    def test_remote_job(self):
        raw = _map_job_to_raw(JOB_REMOTE)
        assert raw["work_mode"] == "remote"
        assert raw["remote"] is True
        assert raw["department_code"] is None  # no postal for remote

    def test_internship_no_salary(self):
        raw = _map_job_to_raw(JOB_INTERNSHIP)
        assert raw["type"] == "internship"
        assert raw["salary_min"] is None
        assert raw["salary_max"] is None

    def test_alternance_type(self):
        raw = _map_job_to_raw(JOB_ALTERNANCE)
        assert raw["type"] == "alternance"

    def test_no_company_graceful(self):
        raw = _map_job_to_raw(JOB_NO_COMPANY)
        assert raw["company"] is None
        assert raw["apply_url"] is None
        assert raw["company_url"] is None

    def test_overseas_postal(self):
        raw = _map_job_to_raw(JOB_OVERSEAS)
        assert raw["department_code"] == "972"

    def test_estimated_salary(self):
        raw = _map_job_to_raw(JOB_ESTIMATED_SALARY)
        assert raw["salary_min"] == 28000.0
        assert raw["salary_max"] == 32000.0

    def test_hybrid_work_mode(self):
        raw = _map_job_to_raw(JOB_HYBRID_LOC)
        assert raw["work_mode"] == "hybrid"
        assert raw["remote"] is False

    def test_empty_key_gives_no_canonical_url(self):
        job = {**JOB_FULL, "key": ""}
        raw = _map_job_to_raw(job)
        assert raw["canonical_url"] is None
        assert raw["source_job_id"] is None

    def test_countries_list_when_country_code_present(self):
        raw = _map_job_to_raw(JOB_FULL)
        assert "FR" in raw["countries"]

    def test_countries_empty_when_no_country_code(self):
        job = {**JOB_FULL, "location": {**JOB_FULL["location"], "countryCode": ""}}
        raw = _map_job_to_raw(job)
        assert raw["countries"] == []
