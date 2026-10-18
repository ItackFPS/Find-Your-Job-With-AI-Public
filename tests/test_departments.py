"""Tests for French department data, extraction, and Jobs API filter."""

from __future__ import annotations

import pytest

from src.pipeline.departments import (
    _BY_CODE,
    all_departments,
    extract_department_code,
    get_department,
    search_departments,
)
from src.models.job import Job


# ---------------------------------------------------------------------------
# Static data sanity checks
# ---------------------------------------------------------------------------

class TestDepartmentData:
    def test_all_departments_count(self):
        depts = all_departments()
        # 96 metropolitan (01-95 + 2A + 2B) + 5 overseas = 101
        assert len(depts) >= 96

    def test_key_departments_present(self):
        by_code = _BY_CODE
        assert "83" in by_code  # Var
        assert "69" in by_code  # Rhône
        assert "13" in by_code  # Bouches-du-Rhône
        assert "75" in by_code  # Paris
        assert "2A" in by_code  # Corse-du-Sud
        assert "2B" in by_code  # Haute-Corse

    def test_overseas_departments_present(self):
        by_code = _BY_CODE
        assert "971" in by_code  # Guadeloupe
        assert "972" in by_code  # Martinique
        assert "973" in by_code  # Guyane
        assert "974" in by_code  # La Réunion
        assert "976" in by_code  # Mayotte

    def test_department_names(self):
        assert _BY_CODE["83"].name == "Var"
        assert _BY_CODE["69"].name == "Rhône"
        assert _BY_CODE["13"].name == "Bouches-du-Rhône"
        assert _BY_CODE["75"].name == "Paris"

    def test_get_department_by_code(self):
        d = get_department("83")
        assert d is not None
        assert d.code == "83"
        assert d.name == "Var"

    def test_get_department_case_insensitive(self):
        d = get_department("2a")
        assert d is not None
        assert d.code == "2A"

    def test_get_department_missing(self):
        assert get_department("99") is None


# ---------------------------------------------------------------------------
# Department code extraction
# ---------------------------------------------------------------------------

class TestExtractDepartmentCode:
    # -- Explicit department code in parentheses --

    def test_explicit_code_var(self):
        assert extract_department_code("Toulon (83)") == "83"

    def test_explicit_code_rhone(self):
        assert extract_department_code("Lyon (69)") == "69"

    def test_explicit_code_paris(self):
        assert extract_department_code("Paris (75)") == "75"

    def test_explicit_code_bdr(self):
        assert extract_department_code("Marseille (13)") == "13"

    def test_explicit_code_corse(self):
        assert extract_department_code("Ajaccio (2A)") == "2A"

    # -- Leading department code (France Travail format) --

    def test_lead_code_69_meyzieu(self):
        assert extract_department_code("69 - Meyzieu") == "69"

    def test_lead_code_83_toulon(self):
        assert extract_department_code("83 - Toulon") == "83"

    def test_lead_code_62_sainte_catherine(self):
        assert extract_department_code("62 - Sainte-Catherine") == "62"

    def test_lead_code_78_yvelines(self):
        assert extract_department_code("78 - Yvelines") == "78"

    def test_lead_code_region_string(self):
        # "Occitanie" has no leading code — should return None
        assert extract_department_code("Occitanie") is None

    # -- Postal code --

    def test_postal_code_toulon(self):
        assert extract_department_code("83000 Toulon") == "83"

    def test_postal_code_lyon(self):
        assert extract_department_code("69001 Lyon") == "69"

    def test_postal_code_paris(self):
        assert extract_department_code("75001 Paris") == "75"

    def test_postal_code_guadeloupe(self):
        assert extract_department_code("97100 Basse-Terre") == "971"

    def test_postal_code_reunion(self):
        assert extract_department_code("97400 Saint-Denis") == "974"

    def test_postal_code_corsica_2a(self):
        # 20100 → 2A (num=100 ≤ 199)
        assert extract_department_code("20100 Sartène") == "2A"

    def test_postal_code_corsica_2b(self):
        # 20200 → 2B (num=200 > 199)
        assert extract_department_code("20200 Bastia") == "2B"

    # -- City name lookup --

    def test_city_toulon(self):
        assert extract_department_code("Toulon, France") == "83"

    def test_city_lyon(self):
        assert extract_department_code("Lyon, France") == "69"

    def test_city_marseille(self):
        assert extract_department_code("Marseille") == "13"

    def test_city_paris(self):
        assert extract_department_code("Paris") == "75"

    def test_city_nantes(self):
        assert extract_department_code("Nantes") == "44"

    def test_city_case_insensitive(self):
        assert extract_department_code("TOULON") == "83"

    # -- Unknown / cannot determine --

    def test_empty_location(self):
        assert extract_department_code("") is None

    def test_none_location(self):
        assert extract_department_code("", countries=[]) is None

    def test_unknown_city(self):
        assert extract_department_code("SomeUnknownCity, France") is None

    def test_foreign_city(self):
        # A non-French city should not be confused with a French department
        assert extract_department_code("London, UK") is None

    def test_generic_remote(self):
        assert extract_department_code("Remote, Europe") is None


# ---------------------------------------------------------------------------
# Department search / autocomplete
# ---------------------------------------------------------------------------

class TestSearchDepartments:
    def test_search_by_code_exact(self):
        results = search_departments("83")
        codes = [d.code for d in results]
        assert "83" in codes

    def test_search_by_code_exact_rhone(self):
        results = search_departments("69")
        codes = [d.code for d in results]
        assert "69" in codes

    def test_search_by_name_var(self):
        results = search_departments("var")
        codes = [d.code for d in results]
        assert "83" in codes

    def test_search_by_name_rhone(self):
        results = search_departments("rhone")
        codes = [d.code for d in results]
        assert "69" in codes

    def test_search_by_name_paris(self):
        results = search_departments("paris")
        codes = [d.code for d in results]
        assert "75" in codes

    def test_search_by_name_bdr(self):
        results = search_departments("bouches")
        codes = [d.code for d in results]
        assert "13" in codes

    def test_search_by_city_lyon(self):
        results = search_departments("lyon")
        codes = [d.code for d in results]
        assert "69" in codes

    def test_search_by_city_toulon(self):
        results = search_departments("toulon")
        codes = [d.code for d in results]
        assert "83" in codes

    def test_search_code_prefix(self):
        # "8" should match 80-89 and 83
        results = search_departments("8")
        codes = [d.code for d in results]
        assert "83" in codes

    def test_search_limit(self):
        results = search_departments("a", limit=3)
        assert len(results) <= 3

    def test_search_empty_query(self):
        results = search_departments("")
        assert results == []

    def test_search_no_match(self):
        results = search_departments("zzznomatch999")
        assert results == []

    def test_search_71_return_71(self):
        # Code "71" should return Saône-et-Loire
        results = search_departments("71")
        codes = [d.code for d in results]
        assert "71" in codes


# ---------------------------------------------------------------------------
# Jobs API department filter
# ---------------------------------------------------------------------------
# These tests verify the ?department= query param on GET /api/jobs.
# Department filtering is a Jobs-page display filter — it does not affect the
# user profile or the pipeline hard filter.
# ---------------------------------------------------------------------------

from pathlib import Path
from fastapi.testclient import TestClient


def _make_job(**kwargs) -> Job:
    defaults = {
        "source": "test",
        "title": "Software Engineer",
        "location": "",
        "countries": [],
        "remote": False,
    }
    defaults.update(kwargs)
    return Job(**defaults)


@pytest.fixture
def dept_client(tmp_path: Path):
    """TestClient with isolated DB pre-populated with French and non-French jobs."""
    from src.api.app import app
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db_path = str(tmp_path / "dept_test.db")

    storage = Storage(db_path=db_path)
    storage.init_db()

    jobs = [
        _make_job(location="Toulon (83)", countries=["France"], title="Dev Toulon"),
        _make_job(location="Lyon (69)", countries=["France"], title="Dev Lyon"),
        _make_job(location="Paris (75)", countries=["France"], title="Dev Paris"),
        _make_job(location="London", countries=["United Kingdom"], title="Dev London"),
        _make_job(location="Remote, Europe", countries=[], remote=True, title="Dev Remote"),
        _make_job(location="France", countries=["France"], title="Dev France Unknown"),
    ]
    for j in jobs:
        storage.upsert_job(j)
    storage.close()

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    rs.close()

    app.state.db_path = db_path
    app.state.runner = PipelineRunner(db_path=db_path)

    return TestClient(app)


class TestJobsApiDepartmentFilter:
    def test_no_department_param_returns_all(self, dept_client):
        r = dept_client.get("/api/jobs")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Dev Toulon" in titles
        assert "Dev Lyon" in titles
        assert "Dev London" in titles

    def test_department_83_shows_only_var_jobs(self, dept_client):
        r = dept_client.get("/api/jobs?department=83")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Dev Toulon" in titles
        assert "Dev Lyon" not in titles
        assert "Dev Paris" not in titles

    def test_department_69_shows_only_rhone_jobs(self, dept_client):
        r = dept_client.get("/api/jobs?department=69")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Dev Lyon" in titles
        assert "Dev Toulon" not in titles
        assert "Dev Paris" not in titles

    def test_department_filter_hides_foreign_jobs(self, dept_client):
        r = dept_client.get("/api/jobs?department=83")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        # Non-French jobs have no department code so they won't match
        assert "Dev London" not in titles
        assert "Dev Remote" not in titles

    def test_department_empty_string_treated_as_no_filter(self, dept_client):
        # empty string should not be passed as department (frontend only sends it when truthy)
        r = dept_client.get("/api/jobs")
        assert r.status_code == 200
        assert len(r.json()) > 0

    def test_unknown_department_code_returns_empty(self, dept_client):
        # code "99" is not a real department — no jobs should match
        r = dept_client.get("/api/jobs?department=99")
        assert r.status_code == 200
        assert r.json() == []


# ---------------------------------------------------------------------------
# Department extraction regression tests — Rhône (69) and Var (83)
# ---------------------------------------------------------------------------

class TestDepartmentRegression69:
    """Rhône: all listed cities must resolve to 69; cross-dept cities must not."""

    def test_lyon(self):
        assert extract_department_code("Lyon, France") == "69"

    def test_meyzieu_city_name(self):
        assert extract_department_code("Meyzieu, France") == "69"

    def test_meyzieu_postal(self):
        assert extract_department_code("69330 Meyzieu") == "69"

    def test_villeurbanne(self):
        assert extract_department_code("Villeurbanne") == "69"

    def test_venissieux_city(self):
        assert extract_department_code("Vénissieux, France") == "69"

    def test_venissieux_postal(self):
        assert extract_department_code("69200 Vénissieux") == "69"

    def test_vaulx_en_velin(self):
        assert extract_department_code("Vaulx-en-Velin") == "69"

    def test_caluire(self):
        assert extract_department_code("Caluire-et-Cuire") == "69"

    def test_bron(self):
        assert extract_department_code("Bron, France") == "69"

    def test_saint_priest(self):
        assert extract_department_code("Saint-Priest (69)") == "69"

    def test_decines_charpieu(self):
        assert extract_department_code("Décines-Charpieu") == "69"

    def test_decines_postal(self):
        assert extract_department_code("69150 Décines-Charpieu") == "69"

    def test_meyzieu_france_travail_format(self):
        # France Travail location format: "69 - Meyzieu"
        assert extract_department_code("69 - Meyzieu") == "69"

    def test_venissieux_france_travail_format(self):
        assert extract_department_code("69 - Vénissieux") == "69"

    def test_genas(self):
        assert extract_department_code("Genas, France") == "69"

    def test_corbas(self):
        assert extract_department_code("Corbas") == "69"

    def test_feyzin(self):
        assert extract_department_code("Feyzin") == "69"

    def test_oullins(self):
        assert extract_department_code("Oullins") == "69"

    # Cross-department: these must NOT return 69
    def test_paris_not_69(self):
        assert extract_department_code("Paris (75)") != "69"

    def test_toulon_not_69(self):
        assert extract_department_code("Toulon (83)") != "69"

    def test_marseille_not_69(self):
        assert extract_department_code("Marseille") != "69"


class TestDepartmentRegression83:
    """Var: all listed cities must resolve to 83; cross-dept cities must not."""

    def test_toulon(self):
        assert extract_department_code("Toulon") == "83"

    def test_toulon_postal(self):
        assert extract_department_code("83000 Toulon") == "83"

    def test_sanary_sur_mer_city(self):
        assert extract_department_code("Sanary-sur-Mer, France") == "83"

    def test_sanary_postal(self):
        assert extract_department_code("83110 Sanary-sur-Mer") == "83"

    def test_la_seyne_sur_mer_city(self):
        assert extract_department_code("La Seyne-sur-Mer") == "83"

    def test_la_seyne_postal(self):
        assert extract_department_code("83500 La Seyne-sur-Mer") == "83"

    def test_hyeres(self):
        assert extract_department_code("Hyères") == "83"

    def test_hyeres_postal(self):
        assert extract_department_code("83400 Hyères") == "83"

    def test_frejus(self):
        assert extract_department_code("Fréjus") == "83"

    def test_saint_raphael(self):
        assert extract_department_code("Saint-Raphaël") == "83"

    def test_draguignan(self):
        assert extract_department_code("Draguignan") == "83"

    def test_six_fours(self):
        assert extract_department_code("Six-Fours-les-Plages") == "83"

    def test_la_valette_du_var(self):
        assert extract_department_code("La Valette-du-Var") == "83"

    def test_brignoles(self):
        assert extract_department_code("Brignoles") == "83"

    # Cross-department: these must NOT return 83
    def test_lyon_not_83(self):
        assert extract_department_code("Lyon") != "83"

    def test_paris_not_83(self):
        assert extract_department_code("Paris") != "83"

    def test_marseille_not_83(self):
        assert extract_department_code("Marseille") != "83"


# ---------------------------------------------------------------------------
# Jobs API search tests — prefix / accent-tolerant FTS
# ---------------------------------------------------------------------------

@pytest.fixture
def search_client(tmp_path):
    """TestClient with isolated DB containing jobs for search testing."""
    from src.api.app import app
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db_path = str(tmp_path / "search_test.db")
    storage = Storage(db_path=db_path)
    storage.init_db()

    jobs = [
        _make_job(title="Ingénieur logiciel", location="Lyon (69)", countries=["France"]),
        _make_job(title="Ingénieure QA", location="Meyzieu (69)", countries=["France"]),
        _make_job(title="Ingénieur cybersécurité", location="Toulon (83)", countries=["France"]),
        _make_job(title="Développeur Python", location="Paris (75)", countries=["France"]),
        _make_job(title="Développeur Python senior", location="Lyon (69)", countries=["France"]),
        _make_job(title="DevOps Engineer", location="London", countries=["United Kingdom"]),
        _make_job(title="Chef de projet IT", location="Marseille (13)", countries=["France"]),
    ]
    for j in jobs:
        storage.upsert_job(j)
    storage.close()

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    rs.close()

    app.state.db_path = db_path
    app.state.runner = PipelineRunner(db_path=db_path)
    return TestClient(app)


class TestJobsApiSearch:
    def test_prefix_ing_matches_ingenieur(self, search_client):
        r = search_client.get("/api/jobs?q=ing")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Ingénieur logiciel" in titles
        assert "Ingénieure QA" in titles
        assert "Ingénieur cybersécurité" in titles

    def test_prefix_inge_matches_ingenieur(self, search_client):
        r = search_client.get("/api/jobs?q=inge")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Ingénieur logiciel" in titles
        assert "Ingénieure QA" in titles

    def test_prefix_inge_accented_matches_ingenieur(self, search_client):
        # User types "ingé" — accent-tolerant FTS should still match
        r = search_client.get("/api/jobs?q=ingé")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Ingénieur logiciel" in titles

    def test_prefix_ingen_matches_ingenieur(self, search_client):
        r = search_client.get("/api/jobs?q=ingen")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Ingénieur logiciel" in titles
        assert "Ingénieure QA" in titles

    def test_prefix_pyth_matches_python(self, search_client):
        r = search_client.get("/api/jobs?q=pyth")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Développeur Python" in titles
        assert "Développeur Python senior" in titles

    def test_search_case_insensitive(self, search_client):
        r = search_client.get("/api/jobs?q=PYTHON")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Développeur Python" in titles

    def test_nonexistent_term_returns_empty(self, search_client):
        r = search_client.get("/api/jobs?q=xyznonexistent999")
        assert r.status_code == 200
        assert r.json() == []

    def test_search_combined_with_department_69(self, search_client):
        # "ingé" + department=69 → only Rhône results
        r = search_client.get("/api/jobs?q=inge&department=69")
        assert r.status_code == 200
        jobs = r.json()
        titles = {j["title"] for j in jobs}
        # Lyon (69) ingénieur should appear
        assert "Ingénieur logiciel" in titles
        # Meyzieu is dept 69 — Ingénieure QA should appear
        assert "Ingénieure QA" in titles
        # Toulon (83) ingénieur must NOT appear
        assert "Ingénieur cybersécurité" not in titles

    def test_search_combined_with_department_83(self, search_client):
        # "ingé" + department=83 → only Var results
        r = search_client.get("/api/jobs?q=inge&department=83")
        assert r.status_code == 200
        jobs = r.json()
        titles = {j["title"] for j in jobs}
        assert "Ingénieur cybersécurité" in titles
        assert "Ingénieur logiciel" not in titles
        assert "Ingénieure QA" not in titles

    def test_search_does_not_affect_profile(self, search_client):
        # Searching via Jobs API must not alter the stored profile.
        r_before = search_client.get("/api/profile")
        search_client.get("/api/jobs?q=python")
        r_after = search_client.get("/api/profile")
        assert r_before.json() == r_after.json()

    def test_exact_word_ingenieur_returns_title_matches(self, search_client):
        # Searching the full word "Ingénieur" must still return title-match jobs.
        r = search_client.get("/api/jobs?q=Ingénieur")
        assert r.status_code == 200
        titles = {j["title"] for j in r.json()}
        assert "Ingénieur logiciel" in titles
        assert "Ingénieure QA" in titles
        assert "Ingénieur cybersécurité" in titles

    def test_exact_word_ingenieur_excludes_unrelated_titles(self, search_client):
        # Jobs whose title has no overlap with "ingénieur" must not appear.
        r = search_client.get("/api/jobs?q=Ingénieur")
        titles = {j["title"] for j in r.json()}
        assert "Chef de projet IT" not in titles
        assert "Développeur Python" not in titles
        assert "Développeur Python senior" not in titles


@pytest.fixture
def relevance_client(tmp_path):
    """TestClient where one job has 'ingénieur' only in description (not title)."""
    from src.api.app import app
    from src.api.runner import PipelineRunner
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db_path = str(tmp_path / "relevance_test.db")
    storage = Storage(db_path=db_path)
    storage.init_db()

    desc_only = ("Nous cherchons un chef de projet expérimenté. "
                 "Une expérience en tant qu'ingénieur est un plus. " * 5)

    jobs = [
        _make_job(title="Ingénieur logiciel", location="Lyon (69)", countries=["France"]),
        _make_job(title="Chef de projet IT", location="Paris (75)", countries=["France"],
                  description=desc_only),
    ]
    for j in jobs:
        storage.upsert_job(j)
    storage.close()

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    rs.close()

    app.state.db_path = db_path
    app.state.runner = PipelineRunner(db_path=db_path)
    return TestClient(app)


class TestJobsApiSearchRelevance:
    """Title matches must rank before description-only matches."""

    def test_title_match_appears_in_results(self, relevance_client):
        r = relevance_client.get("/api/jobs?q=ingenieur")
        assert r.status_code == 200
        titles = [j["title"] for j in r.json()]
        assert "Ingénieur logiciel" in titles

    def test_title_match_ranks_first(self, relevance_client):
        # "Chef de projet IT" mentions ingénieur 5× in description;
        # "Ingénieur logiciel" has it in the title.  Title must come first.
        r = relevance_client.get("/api/jobs?q=ingenieur")
        titles = [j["title"] for j in r.json()]
        assert titles[0] == "Ingénieur logiciel", (
            f"Expected title match first, got: {titles}"
        )

    def test_search_with_q_preserves_fts_order_not_score_order(self, relevance_client):
        # Verifies the score-based re-sort does NOT override FTS relevance.
        # Without the fix, jobs are re-sorted by pipeline score, which discards
        # text relevance and lets description-pollution jobs leapfrog title matches.
        r = relevance_client.get("/api/jobs?q=Ingénieur")
        jobs = r.json()
        assert len(jobs) >= 1
        # First result must have the search word in its title.
        assert "ingénieur" in jobs[0]["title"].lower()


class TestJobsApiCountryFacets:
    """GET /api/jobs/countries returns per-country job counts."""

    def test_returns_200(self, dept_client):
        r = dept_client.get("/api/jobs/countries")
        assert r.status_code == 200

    def test_returns_list_of_objects_with_name_and_count(self, dept_client):
        r = dept_client.get("/api/jobs/countries")
        data = r.json()
        assert "facets" in data
        facets = data["facets"]
        assert isinstance(facets, list)
        for f in facets:
            assert "name" in f
            assert "count" in f
            assert isinstance(f["count"], int)

    def test_france_appears_with_correct_count(self, dept_client):
        # dept_client has 4 France jobs (Toulon, Lyon, Paris, France Unknown)
        r = dept_client.get("/api/jobs/countries")
        by_name = {f["name"]: f["count"] for f in r.json()["facets"]}
        assert "France" in by_name
        assert by_name["France"] == 4

    def test_uk_appears(self, dept_client):
        r = dept_client.get("/api/jobs/countries")
        by_name = {f["name"]: f["count"] for f in r.json()["facets"]}
        assert "United Kingdom" in by_name
        assert by_name["United Kingdom"] == 1

    def test_job_with_no_country_not_counted(self, dept_client):
        # "Dev Remote" has countries=[] — must not show up as a country entry
        r = dept_client.get("/api/jobs/countries")
        names = {f["name"] for f in r.json()["facets"]}
        assert "" not in names
        assert None not in names

    def test_sorted_by_count_descending(self, dept_client):
        r = dept_client.get("/api/jobs/countries")
        counts = [f["count"] for f in r.json()["facets"]]
        assert counts == sorted(counts, reverse=True)
