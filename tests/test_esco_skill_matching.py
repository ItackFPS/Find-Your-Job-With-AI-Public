"""Phase NEXT / I10 regression tests: ESCO-backed synonym expansion in
``src.pipeline.scoring``.

Scope guard for every test here: this enrichment only ever WIDENS the set
of textual forms ``_score_skills_match`` (a soft ranking factor) accepts as
a match for a target skill. It must never touch the hard deterministic
filter/requirements gate, and it must degrade to exactly the pre-ESCO
behavior (the small hand-maintained ``_SCORING_SKILL_ALIASES`` list) when
the ESCO database is missing, empty, or corrupted.

Architecture under test: ``compute_esco_synonym_map`` is the ONLY thing that
ever touches the ESCO database, and only the orchestrator calls it — ONCE
per pipeline run, never per job. ``_expand_skill`` / ``_score_skills_match``
are pure functions of ``(job, config)``: they read an already-precomputed
``config["scoring"]["skills_match"]["esco_synonyms"]`` dict and never open a
database connection themselves. This is what keeps every pre-existing test
that builds its own ad hoc ``config`` (with no ``esco_synonyms`` key) safe
from ever touching the real production ``data/esco.db``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.esco.db import EscoConcept, EscoDb
from src.models.job import Job
from src.pipeline.scoring import (
    _esco_synonyms_for_skill,
    _expand_skill,
    _score_skills_match,
    compute_esco_synonym_map,
)


def _seed_db(db_path: Path) -> EscoDb:
    db = EscoDb(db_path=str(db_path))
    db.init_db()
    db.upsert_many(
        [
            EscoConcept(
                concept_uri="local:skill/javascript",
                preferred_label_en="JavaScript",
                preferred_label_fr="JavaScript",
                alt_labels_en=["JS", "ECMAScript"],
                alt_labels_fr=["JS"],
            ),
            EscoConcept(
                concept_uri="local:skill/go",
                preferred_label_en="Go (programming language)",
                preferred_label_fr="Go (langage de programmation)",
                alt_labels_en=["Golang"],
                alt_labels_fr=["Golang"],
            ),
            EscoConcept(
                concept_uri="local:skill/web_dev",
                preferred_label_en="Web development",
                preferred_label_fr="développement web",
                alt_labels_en=[],
                alt_labels_fr=["développement de sites web"],
            ),
        ]
    )
    return db


@pytest.fixture(autouse=True)
def _clear_esco_cache():
    """The lookup is lru_cache'd per (skill, db_path) — clear it around every
    test so one test's ESCO db doesn't leak cached results into another's."""
    _esco_synonyms_for_skill.cache_clear()
    yield
    _esco_synonyms_for_skill.cache_clear()


class TestExactAndSynonymMatches:
    def test_exact_label_match_returns_its_own_synonyms(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        forms = _esco_synonyms_for_skill("javascript", db_path=str(db_path))
        assert "js" in forms
        assert "ecmascript" in forms

    def test_expand_skill_includes_esco_synonym_alongside_hand_list(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        esco_map = compute_esco_synonym_map(["JavaScript"], db_path=str(db_path))
        config = {"scoring": {"skills_match": {"esco_synonyms": esco_map}}}

        expanded = set(_expand_skill("JavaScript", config))
        assert "js" in expanded
        assert "ecmascript" in expanded
        assert "javascript" in expanded  # the skill itself is never dropped

    def test_expand_skill_without_config_never_touches_esco(self) -> None:
        """The pre-ESCO call shape (no config argument) must keep working
        exactly as before — this is what every pre-existing caller does."""
        assert _expand_skill("javascript") == ["javascript"]


class TestFrenchEnglishEquivalents:
    def test_french_alt_label_recognized_for_english_skill(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        forms = _esco_synonyms_for_skill("web development", db_path=str(db_path))
        assert "developpement de sites web" in forms  # accents stripped by _norm_text

    def test_golang_synonym_both_languages(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        forms = _esco_synonyms_for_skill("go (programming language)", db_path=str(db_path))
        assert "golang" in forms


class TestUnknownSkills:
    def test_unrecognized_skill_returns_empty_set(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        assert _esco_synonyms_for_skill("some totally unknown skill", db_path=str(db_path)) == frozenset()

    def test_expand_skill_falls_back_to_itself_when_esco_has_no_match(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        esco_map = compute_esco_synonym_map(["kubernetes"], db_path=str(db_path))
        config = {"scoring": {"skills_match": {"esco_synonyms": esco_map}}}
        assert _expand_skill("kubernetes", config) == ["kubernetes"]


class TestMalformedOrMissingEscoData:
    def test_missing_db_file_returns_empty_set_not_raise(self, tmp_path: Path) -> None:
        forms = _esco_synonyms_for_skill("python", db_path=str(tmp_path / "nope.db"))
        assert forms == frozenset()

    def test_corrupted_db_file_returns_empty_set_not_raise(self, tmp_path: Path) -> None:
        db_path = tmp_path / "corrupt.db"
        db_path.write_bytes(b"this is not a sqlite database")
        forms = _esco_synonyms_for_skill("python", db_path=str(db_path))
        assert forms == frozenset()

    def test_empty_but_initialized_db_returns_empty_set(self, tmp_path: Path) -> None:
        db_path = tmp_path / "empty.db"
        db = EscoDb(db_path=str(db_path))
        db.init_db()  # schema created, zero concepts -> is_ready() is False
        forms = _esco_synonyms_for_skill("python", db_path=str(db_path))
        assert forms == frozenset()


class TestNeverPullsUnrelatedConceptSynonyms:
    def test_prefix_collision_does_not_leak_other_concepts_synonyms(self, tmp_path: Path) -> None:
        """A loose FTS prefix match (e.g. 'go' also prefix-matching some
        unrelated 'Google ...' concept) must not leak that OTHER concept's
        synonyms onto a skill it isn't actually equal to."""
        db_path = tmp_path / "esco.db"
        db = _seed_db(db_path)
        db.upsert_concept(
            EscoConcept(
                concept_uri="local:skill/gospel_singing",
                preferred_label_en="Gospel singing technique",
                preferred_label_fr="chant gospel",
                alt_labels_en=["gospel vocals"],
            )
        )
        forms = _esco_synonyms_for_skill("go (programming language)", db_path=str(db_path))
        assert "gospel vocals" not in forms
        assert "golang" in forms


class TestComputeEscoSynonymMap:
    """Unit tests for the ONE function that is allowed to touch the ESCO
    database — called once per pipeline run by the orchestrator."""

    def test_maps_each_target_skill_to_its_forms(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        result = compute_esco_synonym_map(["JavaScript", "Go"], db_path=str(db_path))
        assert "js" in result["javascript"]
        assert "golang" in result["go"]

    def test_skill_with_no_esco_match_maps_to_empty_list(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        result = compute_esco_synonym_map(["kubernetes"], db_path=str(db_path))
        assert result["kubernetes"] == []

    def test_empty_target_skills_yields_empty_map(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        assert compute_esco_synonym_map([], db_path=str(db_path)) == {}

    def test_missing_database_yields_empty_lists_for_every_skill(self, tmp_path: Path) -> None:
        result = compute_esco_synonym_map(
            ["JavaScript", "Go"], db_path=str(tmp_path / "missing.db")
        )
        assert result == {"javascript": [], "go": []}


class TestOrchestratorWiresEscoSynonyms:
    def test_orchestrator_injects_esco_synonyms_via_compute_esco_synonym_map(self) -> None:
        """Guards the wiring itself: the orchestrator must call
        compute_esco_synonym_map and store the result under
        scoring.skills_match.esco_synonyms — mirroring exactly how it already
        injects scoring.skills_match.domain_vocabulary. A grep-level check is
        deliberately used here (matching this repo's own precedent of
        testing config-injection contracts at the scoring-function level,
        see TestScoringIntegrationIsAdditiveOnly) rather than a full,
        heavyweight orchestrator run."""
        import inspect

        from src.pipeline import orchestrator, scoring

        # The injection lives in scoring.build_scoring_config (extracted verbatim from
        # run_pipeline); the orchestrator must still route its scoring config through it.
        assert "build_scoring_config(" in inspect.getsource(orchestrator)
        source = inspect.getsource(scoring.build_scoring_config)
        assert "compute_esco_synonym_map" in source
        assert 'skills_cfg["esco_synonyms"]' in source


class TestScoringIntegrationIsAdditiveOnly:
    """_score_skills_match is a soft ranking factor — ESCO enrichment must
    only ever be able to RAISE (or leave unchanged) a job's skills_match
    score relative to the pre-ESCO hand-list-only behavior, never lower it,
    and must never affect any hard-filter/gate decision (out of scope for
    this function entirely — it isn't called by the hard filter)."""

    def _job(self, description: str) -> Job:
        return Job(title="Software Engineer", description=description, skills=[])

    def test_synonym_in_description_now_counts_as_a_match(self, tmp_path: Path) -> None:
        db_path = tmp_path / "esco.db"
        _seed_db(db_path)
        target_skills = ["JavaScript", "rust", "elixir", "haskell", "cobol"]
        esco_map = compute_esco_synonym_map(target_skills, db_path=str(db_path))
        config = {
            "scoring": {
                "skills_match": {"target_skills": target_skills, "esco_synonyms": esco_map}
            }
        }
        job = self._job("We use JS extensively across our stack.")
        score = _score_skills_match(job, config)
        # 1 match out of denominator_cap(5) -> 0.4 + 0.6*(1/5) = 0.52
        assert score == pytest.approx(0.52)

    def test_without_esco_synonyms_in_config_same_description_does_not_match(self) -> None:
        """Sanity check establishing the baseline this feature improves on,
        AND proving no hidden ESCO lookup happens without an explicit
        precomputed map in config: 'JS' is not in the hand-maintained alias
        list, so without 'esco_synonyms' present it must NOT match
        'JavaScript' — this is also exactly what every pre-existing test
        calling _score_skills_match with its own ad hoc config still gets."""
        target_skills = ["JavaScript", "rust", "elixir", "haskell", "cobol"]
        config = {"scoring": {"skills_match": {"target_skills": target_skills}}}
        job = self._job("We use JS extensively across our stack.")
        score = _score_skills_match(job, config)
        assert score == pytest.approx(0.05)
