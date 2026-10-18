"""Tests for profile-driven scoring (P1/P3/P4 config audit fixes).

Covers:
- similarity.weight from config changes final ranking score (P1)
- Invalid similarity.weight raises ValueError (P1)
- preferred_levels from profile drives seniority scoring (P3)
- Two different profiles produce different seniority scores (P3)
- Existing profile behavior preserved (P3)
- Missing profile does not activate hardcoded preferences (P2)
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.pipeline.scoring import _score_seniority_match
from src.pipeline.semantic_matching import integrate_semantic_into_ranking


def _job(jid: str = "j1", title: str = "Test") -> Job:
    return Job(
        id=jid,
        source="test",
        title=title,
        description="desc",
        location="France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
    )


# ---------------------------------------------------------------------------
# P1 — similarity.weight is read from config
# ---------------------------------------------------------------------------

class TestSemanticWeightFromConfig:
    def test_higher_weight_increases_score_when_sem_is_high(self) -> None:
        """Increasing similarity.weight gives more influence to a high semantic score."""
        job = _job()
        det_scored = [(job, 0.4)]
        sem_results = [(job, 1.0)]

        cfg_low = {"similarity": {"weight": 0.2}}
        cfg_high = {"similarity": {"weight": 0.8}}

        final_low = integrate_semantic_into_ranking(det_scored, sem_results, cfg_low)
        final_high = integrate_semantic_into_ranking(det_scored, sem_results, cfg_high)

        assert final_high[0][1] > final_low[0][1]

    def test_exact_formula_respected(self) -> None:
        """Final score = (1 - weight) * det + weight * sem."""
        job = _job()
        det = 0.6
        sem = 0.9
        weight = 0.3
        cfg = {"similarity": {"weight": weight}}

        final = integrate_semantic_into_ranking([(job, det)], [(job, sem)], cfg)
        expected = (1 - weight) * det + weight * sem
        assert abs(final[0][1] - expected) < 1e-9

    def test_default_weight_02_when_key_missing(self) -> None:
        """Omitting similarity.weight falls back to 0.2."""
        job = _job()
        det = 0.6
        sem = 0.9
        cfg_no_weight = {"similarity": {}}
        cfg_explicit = {"similarity": {"weight": 0.2}}

        r1 = integrate_semantic_into_ranking([(job, det)], [(job, sem)], cfg_no_weight)
        r2 = integrate_semantic_into_ranking([(job, det)], [(job, sem)], cfg_explicit)
        assert abs(r1[0][1] - r2[0][1]) < 1e-9

    def test_invalid_weight_raises_value_error(self) -> None:
        """Weight outside [0, 1] must raise ValueError."""
        job = _job()
        with pytest.raises(ValueError, match="similarity.weight"):
            integrate_semantic_into_ranking(
                [(job, 0.5)], [(job, 0.5)], {"similarity": {"weight": 1.5}}
            )

    def test_weight_zero_ignores_semantic_score(self) -> None:
        """Weight 0 means final score equals the deterministic score."""
        job = _job()
        det = 0.7
        cfg = {"similarity": {"weight": 0.0}}
        final = integrate_semantic_into_ranking([(job, det)], [(job, 0.0)], cfg)
        assert abs(final[0][1] - det) < 1e-9


# ---------------------------------------------------------------------------
# P2 — No hardcoded profile fallback
# ---------------------------------------------------------------------------

class TestNoHardcodedProfileFallback:
    def test_seniority_neutral_when_no_preferred_levels(self) -> None:
        """Seniority scoring returns 0.5 (neutral) when profile provides no keywords."""
        config: dict = {"scoring": {"seniority_match": {"preferred_level_keywords": [], "junior_bonus": 1.2}}}
        score = _score_seniority_match("Junior Developer", config)
        assert score == 0.5, "No preferred levels → neutral, not an opinionated guess"

    def test_seniority_neutral_when_key_absent(self) -> None:
        """Seniority scoring returns 0.5 when preferred_level_keywords is not in config."""
        config: dict = {"scoring": {"seniority_match": {"junior_bonus": 1.2}}}
        score = _score_seniority_match("Junior Developer", config)
        assert score == 0.5


# ---------------------------------------------------------------------------
# P3 — preferred_levels drives seniority scoring
# ---------------------------------------------------------------------------

class TestSeniorityProfileDriven:
    def test_junior_keyword_matches_junior_title(self) -> None:
        config = {"scoring": {"seniority_match": {"preferred_level_keywords": ["junior"], "junior_bonus": 1.2}}}
        assert _score_seniority_match("Junior Software Engineer", config) > 0.5

    def test_junior_keyword_misses_senior_title(self) -> None:
        config = {"scoring": {"seniority_match": {"preferred_level_keywords": ["junior"], "junior_bonus": 1.2}}}
        assert _score_seniority_match("Senior Software Engineer", config) == 0.0

    def test_different_profiles_produce_different_scores(self) -> None:
        """A junior-preferring profile scores 'Senior Engineer' lower than a senior-preferring one."""
        junior_cfg = {"scoring": {"seniority_match": {"preferred_level_keywords": ["junior"], "junior_bonus": 1.2}}}
        senior_cfg = {"scoring": {"seniority_match": {"preferred_level_keywords": ["senior"], "junior_bonus": 1.2}}}

        score_for_junior_seeker = _score_seniority_match("Senior Software Engineer", junior_cfg)
        score_for_senior_seeker = _score_seniority_match("Senior Software Engineer", senior_cfg)

        assert score_for_senior_seeker > score_for_junior_seeker

    def test_current_profile_preserved(self) -> None:
        """Current profile (junior/entry level/graduate/internship/alternance) scores junior titles well."""
        levels = ["internship", "alternance", "junior", "entry level", "graduate"]
        config = {"scoring": {"seniority_match": {"preferred_level_keywords": levels, "junior_bonus": 1.2}}}

        assert _score_seniority_match("Junior Python Developer", config) > 0.5
        assert _score_seniority_match("Graduate Software Engineer", config) > 0.5
        assert _score_seniority_match("Alternance Développeur", config) > 0.5

    def test_senior_title_gets_zero_with_junior_profile(self) -> None:
        """A 'Senior' title scores 0 when none of the preferred levels match."""
        levels = ["junior", "entry level", "graduate"]
        config = {"scoring": {"seniority_match": {"preferred_level_keywords": levels, "junior_bonus": 1.2}}}
        assert _score_seniority_match("Senior Backend Engineer", config) == 0.0

    def test_score_clamped_to_one(self) -> None:
        """Multiple keyword matches are clamped to 1.0."""
        levels = ["junior", "entry level", "graduate", "intern"]
        config = {"scoring": {"seniority_match": {"preferred_level_keywords": levels, "junior_bonus": 2.0}}}
        score = _score_seniority_match("Junior Graduate Intern", config)
        assert score <= 1.0


# ---------------------------------------------------------------------------
# French seniority synonyms — regression tests
#
# Bug: _SENIORITY_INDICATORS contains "stagiaire" and "debutant" but the
# orchestrator historically injected only English preferred_level_keywords
# ("internship", "junior", ...).  A title like "Développeur Stagiaire"
# would reach has_seniority_signal=True and score 0.0 instead of earning
# the preferred-level bonus.  The fix adds French synonyms to the injected
# keyword list (orchestrator.py).
# ---------------------------------------------------------------------------

class TestFrenchSeniorityKeywords:
    """Seniority scoring correctly handles French title terms after synonym injection."""

    def _cfg(self, levels: list[str]) -> dict:
        """Build a scoring config with the given preferred_level_keywords."""
        return {
            "scoring": {
                "seniority_match": {
                    "preferred_level_keywords": levels,
                    "junior_bonus": 1.2,
                }
            }
        }

    def _keywords_from_profile(self) -> list[str]:
        """Simulate what the orchestrator now injects for the current profile."""
        _SENIORITY_FR_SYNONYMS: dict[str, list[str]] = {
            "internship": ["stagiaire", "stage"],
            "junior": ["debutant"],
            "alternance": ["apprenti", "apprentissage"],
            "entry_level": [],
            "graduate": [],
        }
        profile_levels = ["internship", "alternance", "junior", "entry_level", "graduate"]
        keywords: list[str] = []
        for lvl in profile_levels:
            keywords.append(lvl.replace("_", " "))
            keywords.extend(_SENIORITY_FR_SYNONYMS.get(lvl, []))
        return keywords

    def test_stagiaire_scores_positive_with_fr_synonyms(self) -> None:
        """'Développeur Stagiaire' earns preferred-level bonus with French synonyms."""
        keywords = self._keywords_from_profile()
        score = _score_seniority_match("Développeur Stagiaire", self._cfg(keywords))
        assert score > 0.5, (
            f"Expected preferred-level bonus (>0.5) for 'Développeur Stagiaire', got {score}"
        )

    def test_stage_scores_positive_with_fr_synonyms(self) -> None:
        """'Stage Développement Web' earns bonus (stage = internship in French)."""
        keywords = self._keywords_from_profile()
        score = _score_seniority_match("Stage Développement Web", self._cfg(keywords))
        assert score > 0.5

    def test_debutant_scores_positive_with_fr_synonyms(self) -> None:
        """'Ingénieur Débutant' earns the junior bonus."""
        keywords = self._keywords_from_profile()
        score = _score_seniority_match("Ingénieur Débutant", self._cfg(keywords))
        assert score > 0.5

    def test_apprenti_scores_positive_with_fr_synonyms(self) -> None:
        """'Développeur Apprenti' earns the alternance bonus."""
        keywords = self._keywords_from_profile()
        score = _score_seniority_match("Développeur Apprenti", self._cfg(keywords))
        assert score > 0.5

    def test_stagiaire_scored_zero_without_fr_synonyms_regression(self) -> None:
        """Regression: WITHOUT French synonyms, stagiaire wrongly scored 0.0 (old bug)."""
        english_only = ["internship", "alternance", "junior", "entry level", "graduate"]
        score = _score_seniority_match("Développeur Stagiaire", self._cfg(english_only))
        # This is the old (buggy) behaviour — score should be 0.0 due to
        # "stagiaire" being in _SENIORITY_INDICATORS but not in preferred keywords.
        assert score == 0.0, (
            f"Regression check: without French synonyms score should be 0.0, got {score}"
        )

    def test_senior_still_zero_after_fix(self) -> None:
        """Adding French synonyms must not change the rejection of senior titles."""
        keywords = self._keywords_from_profile()
        score = _score_seniority_match("Senior Développeur Backend", self._cfg(keywords))
        assert score == 0.0

    def test_neutral_title_still_neutral_after_fix(self) -> None:
        """'Software Engineer' (no seniority signal) stays neutral at 0.5."""
        keywords = self._keywords_from_profile()
        score = _score_seniority_match("Software Engineer", self._cfg(keywords))
        assert score == 0.5
