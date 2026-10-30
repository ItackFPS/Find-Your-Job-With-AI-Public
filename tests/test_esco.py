"""Tests for the ESCO local SQLite FTS integration.

All tests use a temporary in-memory or temp-file database — no network access,
no pre-existing data/esco.db required.
"""

from __future__ import annotations

import pytest

from src.esco.db import EscoConcept, EscoDb, _sanitize_fts_query
from src.esco.loader import _build_seed_concepts, load_seed_data
from src.models.profile import EscoEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path) -> EscoDb:
    """Temporary, isolated EscoDb for each test."""
    d = EscoDb(db_path=str(tmp_path / "esco_test.db"))
    d.init_db()
    return d


def _python_concept() -> EscoConcept:
    return EscoConcept(
        concept_uri="local:skill/python",
        preferred_label_en="Python (programming language)",
        preferred_label_fr="Python (langage de programmation)",
        alt_labels_en=["Python", "python programming", "python scripting"],
        alt_labels_fr=["Python", "programmation Python"],
        concept_type="skill",
    )


def _docker_concept() -> EscoConcept:
    return EscoConcept(
        concept_uri="local:skill/docker",
        preferred_label_en="Docker (containerisation platform)",
        preferred_label_fr="Docker (plateforme de conteneurisation)",
        alt_labels_en=["Docker", "containerisation"],
        alt_labels_fr=["Docker", "conteneurisation"],
        concept_type="skill",
    )


# ---------------------------------------------------------------------------
# EscoConcept — keyword derivation
# ---------------------------------------------------------------------------

class TestEscoConceptKeyword:
    def test_keyword_strips_parenthetical(self):
        c = _python_concept()
        assert c.keyword == "Python"

    def test_keyword_plain_label(self):
        c = EscoConcept(
            concept_uri="local:skill/sql",
            preferred_label_en="SQL",
            preferred_label_fr="SQL",
        )
        assert c.keyword == "SQL"

    def test_keyword_multi_word(self):
        c = EscoConcept(
            concept_uri="local:skill/ml",
            preferred_label_en="machine learning",
            preferred_label_fr="apprentissage automatique",
        )
        assert c.keyword == "machine learning"


# ---------------------------------------------------------------------------
# EscoDb — init and write
# ---------------------------------------------------------------------------

class TestEscoDbInit:
    def test_count_empty_db(self, db):
        assert db.count() == 0

    def test_is_ready_false_when_empty(self, db):
        assert not db.is_ready()

    def test_upsert_single(self, db):
        db.upsert_concept(_python_concept())
        assert db.count() == 1

    def test_upsert_many(self, db):
        db.upsert_many([_python_concept(), _docker_concept()])
        assert db.count() == 2

    def test_is_ready_after_upsert(self, db):
        db.upsert_concept(_python_concept())
        assert db.is_ready()

    def test_upsert_same_uri_replaces(self, db):
        db.upsert_concept(_python_concept())
        updated = EscoConcept(
            concept_uri="local:skill/python",
            preferred_label_en="Python (updated label)",
            preferred_label_fr="Python (libellé mis à jour)",
        )
        db.upsert_concept(updated)
        assert db.count() == 1
        results = db.search("Python", lang="en")
        assert results[0].preferred_label_en == "Python (updated label)"


# ---------------------------------------------------------------------------
# EscoDb — search / autocomplete
# ---------------------------------------------------------------------------

class TestEscoDbSearch:
    def test_search_nonexistent_db_returns_empty(self, tmp_path):
        db = EscoDb(db_path=str(tmp_path / "no_such.db"))
        assert db.search("python") == []

    def test_search_empty_db_returns_empty(self, db):
        assert db.search("python") == []

    def test_search_exact_english_label(self, db):
        db.upsert_concept(_python_concept())
        results = db.search("Python", lang="en")
        assert len(results) >= 1
        assert any("Python" in r.preferred_label_en for r in results)

    def test_search_partial_english(self, db):
        db.upsert_concept(_python_concept())
        results = db.search("pyth", lang="en")
        assert len(results) >= 1

    def test_search_english_alt_label(self, db):
        db.upsert_concept(_python_concept())
        results = db.search("python programming", lang="en")
        assert len(results) >= 1

    def test_search_french_label(self, db):
        db.upsert_concept(_python_concept())
        results = db.search("programmation Python", lang="fr")
        assert len(results) >= 1

    def test_search_french_partial(self, db):
        db.upsert_concept(_python_concept())
        results = db.search("programmation", lang="fr")
        assert len(results) >= 1

    def test_search_same_concept_via_en_and_fr(self, db):
        db.upsert_concept(_python_concept())
        en_results = db.search("python", lang="en")
        fr_results = db.search("Python", lang="fr")
        en_uris = {r.concept_uri for r in en_results}
        fr_uris = {r.concept_uri for r in fr_results}
        assert en_uris & fr_uris, "Same concept must be findable in both languages"

    def test_search_limit_respected(self, db):
        concepts = [
            EscoConcept(
                concept_uri=f"local:skill/test-{i}",
                preferred_label_en=f"Python skill {i}",
                preferred_label_fr=f"Compétence Python {i}",
            )
            for i in range(20)
        ]
        db.upsert_many(concepts)
        results = db.search("Python", limit=5)
        assert len(results) <= 5

    def test_search_docker_french(self, db):
        db.upsert_concept(_docker_concept())
        results = db.search("conteneurisation", lang="fr")
        assert len(results) >= 1

    def test_search_concept_type_filter(self, db):
        db.upsert_concept(_python_concept())
        occupation = EscoConcept(
            concept_uri="local:occ/software-dev",
            preferred_label_en="software developer",
            preferred_label_fr="développeur logiciel",
            concept_type="occupation",
        )
        db.upsert_concept(occupation)

        skill_results = db.search("python", concept_type="skill")
        occ_results = db.search("python", concept_type="occupation")
        assert all(r.concept_type == "skill" for r in skill_results)
        assert all(r.concept_type == "occupation" for r in occ_results)

    def test_search_injection_safe(self, db):
        db.upsert_concept(_python_concept())
        # Characters that could break FTS5 query syntax
        dangerous = "python' OR '1'='1"
        results = db.search(dangerous)
        # Must not crash — empty or valid results
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# FTS query sanitization
# ---------------------------------------------------------------------------

class TestSanitizeFtsQuery:
    def test_plain_word(self):
        assert _sanitize_fts_query("python") == "python"

    def test_multi_word(self):
        assert _sanitize_fts_query("web development") == "web development"

    def test_strips_quotes(self):
        result = _sanitize_fts_query("python' OR '1'='1")
        assert "'" not in result

    def test_strips_parentheses(self):
        result = _sanitize_fts_query("python (programming)")
        assert "(" not in result and ")" not in result

    def test_empty_string(self):
        assert _sanitize_fts_query("") == ""

    def test_whitespace_only(self):
        assert _sanitize_fts_query("   ") == ""

    def test_unicode_preserved(self):
        result = _sanitize_fts_query("développement")
        assert "veloppement" in result  # accented chars may survive via \w


# ---------------------------------------------------------------------------
# Seed data loader
# ---------------------------------------------------------------------------

class TestSeedData:
    def test_seed_concepts_not_empty(self):
        concepts = _build_seed_concepts()
        assert len(concepts) > 10

    def test_seed_concepts_have_en_labels(self):
        concepts = _build_seed_concepts()
        for c in concepts:
            assert c.preferred_label_en, f"Missing English label for {c.concept_uri}"

    def test_seed_concepts_have_fr_labels(self):
        concepts = _build_seed_concepts()
        for c in concepts:
            assert c.preferred_label_fr, f"Missing French label for {c.concept_uri}"

    def test_seed_concepts_have_unique_uris(self):
        concepts = _build_seed_concepts()
        uris = [c.concept_uri for c in concepts]
        assert len(uris) == len(set(uris)), "Duplicate concept URIs in seed data"

    def test_seed_python_present(self):
        concepts = _build_seed_concepts()
        labels = {c.concept_uri for c in concepts}
        assert "local:skill/python" in labels

    def test_load_seed_into_db(self, db):
        count = load_seed_data(db)
        assert count > 0
        assert db.count() >= count

    def test_seed_searchable_after_load(self, db):
        load_seed_data(db)
        results = db.search("Python")
        assert len(results) >= 1
        assert any("Python" in r.preferred_label_en for r in results)

    def test_seed_french_searchable_after_load(self, db):
        load_seed_data(db)
        results = db.search("programmation Python", lang="fr")
        assert len(results) >= 1

    def test_seed_docker_french_searchable(self, db):
        load_seed_data(db)
        results = db.search("conteneurisation", lang="fr")
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# EscoEntry model (profile schema)
# ---------------------------------------------------------------------------

class TestEscoEntry:
    def test_keyword_derived_from_label(self):
        entry = EscoEntry(
            concept_uri="local:skill/python",
            label="Python (programming language)",
        )
        assert entry.keyword == "Python"

    def test_keyword_explicit_overrides_derivation(self):
        entry = EscoEntry(
            concept_uri="local:skill/python",
            label="Python (programming language)",
            keyword="python",
        )
        assert entry.keyword == "python"

    def test_custom_entry_no_uri(self):
        entry = EscoEntry(label="My Custom Skill", custom=True)
        assert entry.concept_uri is None
        assert entry.custom is True

    def test_esco_entry_not_custom_by_default(self):
        entry = EscoEntry(concept_uri="local:skill/python", label="Python")
        assert not entry.custom

    def test_esco_entry_serializes(self):
        entry = EscoEntry(concept_uri="local:skill/python", label="Python")
        d = entry.model_dump()
        assert "concept_uri" in d
        assert "label" in d
        assert "keyword" in d
        assert "custom" in d


# ---------------------------------------------------------------------------
# API endpoint — integration with FastAPI TestClient
# ---------------------------------------------------------------------------

class TestEscoApiEndpoints:
    @pytest.fixture
    def client_with_db(self, tmp_path, monkeypatch, isolated_app):
        """FastAPI test client with a pre-populated ESCO database."""
        from fastapi.testclient import TestClient

        from src.esco.loader import load_seed_data

        # Point the ESCO route to a temporary DB. monkeypatch auto-reverts
        # even if init_db()/load_seed_data()/an assertion below raises —
        # a manual save/restore here previously left the real module-global
        # pointed at a deleted tmp path for the rest of the pytest process
        # on any exception between the assignment and the restore line.
        import src.api.routes.esco as esco_module
        db_path = str(tmp_path / "esco_test.db")
        monkeypatch.setattr(esco_module, "_DB_PATH", db_path)

        db = EscoDb(db_path=db_path)
        db.init_db()
        load_seed_data(db)

        # `with TestClient(app)` runs the REAL app lifespan (startup +
        # shutdown), which otherwise touches real data/jobs.db, real
        # data/esco.db, and — if config/linkedin.yaml or config/sources.yaml
        # have a source enabled — starts a real scheduler that can dispatch
        # a live LinkedIn/HelloWork scan against the real websites. Isolating
        # db_path/config_dir/esco_db_path (all read from app.state by
        # _lifespan) keeps every part of startup local: empty config_dir
        # means LinkedInConfig.load()/SourceRegistry find no config file and
        # gracefully report everything disabled. Confirmed by forensic audit
        # (live HTTP calls + orphaned data/esco.db-wal/-shm files observed
        # from a full test-suite run before this fix).
        # isolated_app (tests/conftest.py) also swaps the notification engine
        # and runner, which are otherwise bound to the real config/ (#46).
        monkeypatch.setitem(isolated_app.state._state, "esco_db_path", db_path)

        with TestClient(isolated_app) as client:
            yield client

    def test_status_ready(self, client_with_db):
        resp = client_with_db.get("/api/esco/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ready"] is True
        assert data["count"] > 0

    def test_status_not_ready_empty_db(self, tmp_path, monkeypatch, isolated_app):
        from fastapi.testclient import TestClient
        import src.api.routes.esco as esco_module

        db_path = str(tmp_path / "empty.db")
        monkeypatch.setattr(esco_module, "_DB_PATH", db_path)
        # See client_with_db above for why all three must be isolated before
        # `with TestClient(app)` triggers the real lifespan. esco_db_path
        # deliberately points at a DIFFERENT tmp file than the route's
        # db_path above: this test wants the route to see an unseeded,
        # not-ready db, but the real lifespan's ESCO auto-seed step (see
        # app.py's `if not esco_db.is_ready(): ... load_seed_data(...)`)
        # would otherwise seed the SAME file before the request runs.
        monkeypatch.setitem(isolated_app.state._state, "esco_db_path", str(tmp_path / "lifespan_seed.db"))

        db = EscoDb(db_path=db_path)
        db.init_db()  # schema created but no data

        with TestClient(isolated_app) as client:
            resp = client.get("/api/esco/status")
            assert resp.status_code == 200
            assert resp.json()["ready"] is False

    def test_search_skills_returns_results(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "python"})
        assert resp.status_code == 200
        results = resp.json()
        assert isinstance(results, list)
        assert len(results) >= 1
        result = results[0]
        assert "concept_uri" in result
        assert "label" in result
        assert "keyword" in result

    def test_search_skills_english(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "python", "lang": "en"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1

    def test_search_skills_french(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "programmation Python", "lang": "fr"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1

    def test_search_skills_no_results(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "zzznomatch999"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_search_skills_limit_param(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "python", "limit": 1})
        assert resp.status_code == 200
        assert len(resp.json()) <= 1

    def test_search_skills_missing_q(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills")
        assert resp.status_code == 422  # missing required parameter

    def test_search_skills_concept_type_field(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "python"})
        assert resp.status_code == 200
        for item in resp.json():
            assert "concept_type" in item

    def test_search_mysql(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "mysql"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        keywords = [r["keyword"].lower() for r in results]
        assert any("mysql" in kw for kw in keywords)

    def test_search_javascript(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "javascript"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1

    def test_search_docker(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "docker"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1

    def test_search_no_results_returns_empty(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "xyzquerty999abc"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_response_has_is_esco_field(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "python"})
        assert resp.status_code == 200
        for item in resp.json():
            assert "is_esco" in item
            assert isinstance(item["is_esco"], bool)

    def test_search_french_developpement(self, client_with_db):
        resp = client_with_db.get("/api/esco/skills", params={"q": "développement", "lang": "fr"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Department API endpoint
# ---------------------------------------------------------------------------

class TestDepartmentApiEndpoints:
    @pytest.fixture
    def client(self, isolated_app):
        from fastapi.testclient import TestClient
        # `with TestClient(...)` runs the real lifespan: isolated_app
        # (tests/conftest.py) points everything it uses at tmp paths.
        with TestClient(isolated_app) as c:
            yield c

    def test_search_by_code(self, client):
        resp = client.get("/api/departments", params={"q": "83"})
        assert resp.status_code == 200
        data = resp.json()
        codes = [d["code"] for d in data]
        assert "83" in codes

    def test_search_by_name_var(self, client):
        resp = client.get("/api/departments", params={"q": "var"})
        assert resp.status_code == 200
        codes = [d["code"] for d in resp.json()]
        assert "83" in codes

    def test_search_by_city_lyon(self, client):
        resp = client.get("/api/departments", params={"q": "lyon"})
        assert resp.status_code == 200
        codes = [d["code"] for d in resp.json()]
        assert "69" in codes

    def test_search_rhone(self, client):
        resp = client.get("/api/departments", params={"q": "69"})
        assert resp.status_code == 200
        codes = [d["code"] for d in resp.json()]
        assert "69" in codes

    def test_search_paris(self, client):
        resp = client.get("/api/departments", params={"q": "paris"})
        assert resp.status_code == 200
        codes = [d["code"] for d in resp.json()]
        assert "75" in codes

    def test_no_results(self, client):
        resp = client.get("/api/departments", params={"q": "zzzmatch999"})
        assert resp.status_code == 200
        assert resp.json() == []

    def test_missing_q(self, client):
        resp = client.get("/api/departments")
        assert resp.status_code == 422

    def test_response_shape(self, client):
        resp = client.get("/api/departments", params={"q": "83"})
        assert resp.status_code == 200
        for item in resp.json():
            assert "code" in item
            assert "name" in item
