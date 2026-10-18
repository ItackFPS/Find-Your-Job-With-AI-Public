"""Regression tests for ranking, pagination, scoring, and domain/skill matching.

Covers:
- Global ranking order preserved across pagination pages
- France on-site / hybrid / remote scoring (no remote penalty for French jobs)
- Non-France non-remote hard filtering
- Word-boundary matching in description fallback (git ≠ agit)
- Skill abbreviation expansion (AI → artificial intelligence)
- Domain alias lookup (informatique / réseaux in filters.yaml)
- Pagination correctness test via _get_jobs_filtered ordering

No Internet access. No collectors called. No real database touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src.models.job import Job
from src.pipeline.deduplicate import (
    _norm_title_simple,
    cross_source_deduplicate,
)
from src.pipeline.filter import (
    _build_description_pattern,
    _expand_skill_for_description,
    _normalize,
    build_filter_config,
    load_filters,
    load_profile,
    matches_location,
    matches_title_keywords,
)
from src.pipeline.scoring import (
    _score_domain_relevance,
    _score_experience_fit,
    _score_location_fit,
    _score_skills_match,
    score_job,
)
from src.requirements.models import GateResult
from tests.fixtures.profiles import TEST_PROFILE_PATH

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(**kwargs: Any) -> Job:
    defaults: dict[str, Any] = {
        "source": "test",
        "title": "Software Engineer",
        "company": "Acme",
        "remote": False,
        "location": "Paris, France",
        "countries": ["France"],
        "discovered_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return Job(**defaults)


def _france_scoring_config(preferred_countries: list[str] | None = None) -> dict[str, Any]:
    """Minimal scoring config with France as preferred country."""
    return {
        "scoring": {
            "location_fit": {
                "weight": 0.30,
                "target_department": "83",
                "preferred_countries": preferred_countries or ["france"],
            },
            "domain_relevance": {"weight": 0.25},
            "seniority_match": {"weight": 0.15},
            "experience_fit": {"weight": 0.10},
            "skills_match": {"weight": 0.20},
        }
    }


# ---------------------------------------------------------------------------
# France work-mode scoring — no penalty for French on-site/hybrid jobs
# ---------------------------------------------------------------------------

class TestFranceWorkModeScoring:
    """French jobs must not be penalised by the location_fit work-mode tiers
    (remote preference is folded into location_fit, see
    src.pipeline.scoring._score_location_fit)."""

    def test_france_remote_job_gets_full_score(self):
        score = _score_location_fit(
            location="Paris, France", countries=["France"], work_mode="remote",
            remote=True, department=None, config=_france_scoring_config(),
        )
        assert score == 0.75

    def test_france_onsite_job_gets_no_penalty(self):
        """On-site French jobs are geographically valid — they should not be
        ranked below comparable French remote jobs by a significant margin."""
        score = _score_location_fit(
            location="Lyon, France", countries=["France"], work_mode="on_site",
            remote=False, department=None, config=_france_scoring_config(),
        )
        assert score >= 0.6, f"French on-site score {score} is too low"

    def test_france_hybrid_job_gets_no_penalty(self):
        score = _score_location_fit(
            location="Marseille, France", countries=["France"], work_mode="hybrid",
            remote=False, department=None, config=_france_scoring_config(),
        )
        assert score >= 0.6

    def test_non_france_remote_job_gets_bonus(self):
        """A remote job outside France still scores above the unknown-location floor
        (already passed the location hard filter's remote-scope check)."""
        score = _score_location_fit(
            location="Berlin, Germany", countries=["Germany"], work_mode="remote",
            remote=True, department=None, config=_france_scoring_config(),
        )
        assert score == 0.55

    def test_non_france_onsite_gets_penalty(self):
        """A non-remote job outside France scores lower than an equivalent France job."""
        score = _score_location_fit(
            location="Munich, Germany", countries=["Germany"], work_mode="on_site",
            remote=False, department=None, config=_france_scoring_config(),
        )
        france_onsite_score = _score_location_fit(
            location="Lyon, France", countries=["France"], work_mode="on_site",
            remote=False, department=None, config=_france_scoring_config(),
        )
        assert score < france_onsite_score

    def test_france_remote_and_onsite_score_difference_is_small(self):
        """The gap between French remote and French on-site should be ≤ 0.15
        to avoid unfairly penalising valid French on-site jobs."""
        remote_score = _score_location_fit(
            location="Paris, France", countries=["France"], work_mode="remote",
            remote=True, department=None, config=_france_scoring_config(),
        )
        onsite_score = _score_location_fit(
            location="Paris, France", countries=["France"], work_mode="on_site",
            remote=False, department=None, config=_france_scoring_config(),
        )
        assert remote_score - onsite_score <= 0.15 + 1e-9, (
            f"Remote ({remote_score}) vs on-site ({onsite_score}) gap "
            f"{remote_score - onsite_score:.2f} is too large for a French job"
        )

    def test_unknown_location_gets_conservative_floor(self):
        """No location/country data at all → the conservative unknown floor (0.40)."""
        score = _score_location_fit(
            location="", countries=[], work_mode="remote",
            remote=True, department=None, config=_france_scoring_config(),
        )
        assert score == 0.40

    def test_var_department_gets_top_tier(self):
        """A job in the target department (Var, "83") scores 1.0 regardless of work mode."""
        score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department=None, config=_france_scoring_config(),
        )
        assert score == 1.0


# ---------------------------------------------------------------------------
# Phase 3 (2026-09-10): experience_fit unknown-value semantics + location_fit
# baseline calibration
# ---------------------------------------------------------------------------

class TestPhase3LocationCalibration:
    """(5)(6)(7)(8)(9) Var/PACA/remote/baseline calibration + no-hard-filter guarantee."""

    def test_var_gets_intended_advantage_over_equivalent_non_var(self):
        """(5) A Var job must clearly outrank an otherwise-identical non-Var,
        non-PACA France on-site job."""
        var_score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=_france_scoring_config(),
        )
        baseline_score = _score_location_fit(
            location="Lyon, France", countries=["France"], work_mode="on_site",
            remote=False, department="69", config=_france_scoring_config(),
        )
        assert var_score == 1.0
        assert baseline_score == 0.60
        assert var_score - baseline_score == pytest.approx(0.40, abs=1e-9), (
            "Phase 3 widened the Var-vs-baseline gap from 0.35 to 0.40 by "
            "lowering the baseline, not by raising Var's own score"
        )

    def test_non_var_france_uses_calibrated_baseline(self):
        """(6) Preferred-country on-site/hybrid, outside Var/PACA, is exactly
        the new calibrated 0.60 (was 0.65) — not a hard rejection, still a
        healthy score."""
        score = _score_location_fit(
            location="Lyon, France", countries=["France"], work_mode="on_site",
            remote=False, department="69", config=_france_scoring_config(),
        )
        assert score == 0.60

    def test_paca_neighbor_still_favored_between_var_and_baseline(self):
        """(7) A PACA-neighbor department (e.g. 13 = Bouches-du-Rhône) must
        still score strictly between Var (1.0) and the calibrated non-PACA
        baseline (0.60) — unaffected by the Phase 3 baseline change."""
        paca_score = _score_location_fit(
            location="Marseille (13)", countries=["France"], work_mode="on_site",
            remote=False, department="13", config=_france_scoring_config(),
        )
        assert paca_score == 0.85
        assert 0.60 < paca_score < 1.0

    def test_full_remote_still_second_highest_tier(self):
        """(8) France remote (0.75) is unaffected by the on-site baseline
        change and still clearly outranks the (now-lower) on-site baseline."""
        remote_score = _score_location_fit(
            location="Paris, France", countries=["France"], work_mode="remote",
            remote=True, department=None, config=_france_scoring_config(),
        )
        onsite_baseline = _score_location_fit(
            location="Lyon, France", countries=["France"], work_mode="on_site",
            remote=False, department="69", config=_france_scoring_config(),
        )
        assert remote_score == 0.75
        assert remote_score > onsite_baseline

    def test_non_france_full_remote_rule_unaffected(self):
        """(8) A non-preferred-country remote job (already passed the hard
        filter's EU/worldwide-remote-scope check) is unaffected by the
        France on-site baseline change."""
        score = _score_location_fit(
            location="Berlin, Germany", countries=["Germany"], work_mode="remote",
            remote=True, department=None, config=_france_scoring_config(),
        )
        assert score == 0.55

    def test_location_never_returns_zero_or_rejects(self):
        """(9) location_fit is a ranking factor, never an eligibility gate —
        every combination, including totally unknown location, must return a
        valid positive float, never 0.0 and never raise."""
        cases = [
            dict(location="Toulon (83)", countries=["France"], work_mode="on_site", remote=False, department="83"),
            dict(location="Marseille (13)", countries=["France"], work_mode="on_site", remote=False, department="13"),
            dict(location="Paris, France", countries=["France"], work_mode="remote", remote=True, department=None),
            dict(location="Lyon, France", countries=["France"], work_mode="on_site", remote=False, department="69"),
            dict(location="Berlin, Germany", countries=["Germany"], work_mode="remote", remote=True, department=None),
            dict(location="Munich, Germany", countries=["Germany"], work_mode="on_site", remote=False, department=None),
            dict(location="", countries=[], work_mode=None, remote=False, department=None),
        ]
        for kwargs in cases:
            score = _score_location_fit(**kwargs, config=_france_scoring_config())
            assert isinstance(score, float)
            assert score > 0.0, f"location_fit must never be 0 (would act as a rejection): {kwargs}"
            assert score <= 1.0

    def test_location_does_not_dominate_domain_or_seniority(self):
        """Location's calibration change must not let it outweigh a
        confirmed off-domain or wrong-seniority signal in the full composite:
        a Var job with a confirmed domain/seniority mismatch must still lose
        to a non-Var job that matches on domain and seniority."""
        cfg = {
            "scoring": {
                "location_fit": {"target_department": "83", "preferred_countries": ["france"]},
                "domain_relevance": {
                    "domain_title_keywords": ["développeur"],
                    "off_domain_keywords": ["commercial"],
                },
                "skills_match": {"target_skills": ["python"]},
                "seniority_match": {"preferred_level_keywords": ["junior"]},
            }
        }
        var_off_domain = _job(
            title="Commercial Sédentaire H/F", description="CRM, prospection.",
            location="Toulon (83)", countries=["France"], department="83",
        )
        non_var_on_domain = _job(
            title="Développeur Junior Python", description="Python requis.",
            location="Lyon, France", countries=["France"], department="69",
        )
        assert score_job(var_off_domain, cfg) < score_job(non_var_on_domain, cfg), (
            "A confirmed off-domain Var job must not outrank a domain/seniority-matched "
            "non-Var job purely from the location calibration change"
        )


class TestPhase3ExperienceUnknownSemantics:
    """(1)(2)(3)(4) Unknown experience is neutral, not positive; no hard rejects at scoring."""

    def test_unknown_experience_is_neutral_not_positive(self):
        """(1) Unknown (no raw_payload years key) returns the new neutral
        0.50, not the old proxy-inflated 0.80."""
        job = _job(raw_payload={})
        score = _score_experience_fit(job, _skills_config([]))
        assert score == 0.50

    def test_explicit_acceptable_experience_unaffected(self):
        """(2) An explicit, low (acceptable) years requirement is completely
        unaffected by the Phase 3 unknown-value change — still 1.0 for <=1yr."""
        job = _job()
        gate_result = GateResult(passes=True, min_experience_years=1)
        score = _score_experience_fit(job, _skills_config([]), gate_result)
        assert score == 1.0

    def test_explicit_experience_above_max_still_scores_via_existing_curve(self):
        """(3) A job whose explicit requirement exceeds the user's configured
        max (already handled upstream by the requirements gate — MANDATORY
        excess is a hard reject before scoring, PREFERRED excess gets its own
        separate _experience_penalty) still flows through the SAME decay
        curve at the scoring layer, unaffected by Phase 3 — scoring never
        re-implements the gate's eligibility logic."""
        job = _job()
        gate_result = GateResult(passes=True, min_experience_years=5)
        score = _score_experience_fit(job, _skills_config([]), gate_result)
        assert score == 0.40  # unchanged decay curve value for 5 years

    def test_missing_experience_does_not_hard_reject_at_scoring_layer(self):
        """(4) Scoring a job with zero experience information must return a
        valid, usable composite score via the real score_job() entry point —
        never None, never an exception, never treated as ineligible. Scoring
        answers 'how strong a match', never 'is this eligible' — that
        distinction belongs entirely to the (untouched) requirements gate."""
        job = _job(
            title="Développeur Python Junior", description="Python requis.",
            raw_payload={}, location="Toulon (83)", countries=["France"], department="83",
        )
        cfg = {
            "scoring": {
                "location_fit": {"target_department": "83", "preferred_countries": ["france"]},
                "domain_relevance": {"domain_title_keywords": ["développeur"]},
                "skills_match": {"target_skills": ["python"]},
                "seniority_match": {"preferred_level_keywords": ["junior"]},
            }
        }
        score = score_job(job, cfg)
        assert isinstance(score, float)
        assert 0.0 < score <= 1.0


class TestPhase3PriorPhasesUnchanged:
    """(10)(11) Phase 1 semantic normalization and Phase 2 skills fallback
    must be completely unaffected by the Phase 3 experience/location changes
    — these are independent, orthogonal factors in the same additive model."""

    def test_phase1_semantic_config_unchanged(self):
        from src.pipeline.semantic_matching import load_semantic_config

        config = load_semantic_config("config/semantic_config.yaml")
        assert config["similarity"]["weight"] == 0.06
        assert config["similarity"]["reference_floor"] == 0.20
        assert config["similarity"]["reference_ceiling"] == 0.65

    def test_phase1_semantic_normalization_formula_unchanged(self):
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Some Job")
        result = integrate_semantic_into_ranking(
            [(job, 0.7)], [(job, 0.425)],
            {"similarity": {"weight": 0.06, "min_threshold": 0.0,
                             "reference_floor": 0.20, "reference_ceiling": 0.65}},
        )
        _, final_score = result[0]
        expected = 0.94 * 0.7 + 0.06 * 0.5  # 0.425 is the floor/ceiling midpoint -> normalizes to 0.5
        assert abs(final_score - expected) < 1e-6

    def test_phase2_explicit_skills_match_unchanged(self):
        job = _job(title="Python Developer", description="Build backend services.")
        cfg = _skills_config(["python", "javascript", "sql"])
        score = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score > 0.4  # unaffected by domain_relevance when explicit match exists

    def test_phase2_fallback_unchanged(self):
        job = _job(title="Administrateur Systèmes", description="Supervision du parc informatique.")
        cfg = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        score = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score == pytest.approx(0.40, abs=1e-6)


# ---------------------------------------------------------------------------
# Word-boundary matching — description fallback
# ---------------------------------------------------------------------------

class TestWordBoundaryMatching:
    """The description fallback must not match keywords as embedded substrings."""

    def test_git_does_not_match_agit(self):
        """'git' must not match 'agit' (French: it acts/about)."""
        pattern = _build_description_pattern(["git"])
        assert pattern is not None
        # 'agit' should not match — 'a' precedes 'git' without a boundary
        assert not pattern.search(_normalize("il s'agit d'un poste technique"))

    def test_git_does_not_match_logit(self):
        pattern = _build_description_pattern(["git"])
        assert pattern is not None
        assert not pattern.search(_normalize("using logit regression"))

    def test_git_matches_standalone_git(self):
        pattern = _build_description_pattern(["git"])
        assert pattern is not None
        assert pattern.search(_normalize("experience with git version control"))
        assert pattern.search(_normalize("git, github, ci/cd"))

    def test_git_matches_at_string_boundaries(self):
        pattern = _build_description_pattern(["git"])
        assert pattern is not None
        # At start of string
        assert pattern.search("git repository management")
        # At end of string
        assert pattern.search("you must know git")

    def test_php_does_not_match_inside_longer_word(self):
        """'php' must not create a spurious match inside random words."""
        pattern = _build_description_pattern(["php"])
        assert pattern is not None
        # These are contrived, but verify the boundary works
        assert not pattern.search("xphpy language")

    def test_php_matches_standalone_php(self):
        pattern = _build_description_pattern(["php"])
        assert pattern is not None
        assert pattern.search("développement en php et mysql")
        assert pattern.search("php, laravel, symfony")

    def test_mysql_does_not_match_as_substring(self):
        pattern = _build_description_pattern(["mysql"])
        assert pattern is not None
        assert not pattern.search("xmysqlx connector")

    def test_mysql_matches_standalone(self):
        pattern = _build_description_pattern(["mysql"])
        assert pattern is not None
        assert pattern.search("bases de données: mysql, postgresql")

    def test_multiple_keywords_any_match(self):
        """When the pattern covers multiple keywords, any one match suffices."""
        pattern = _build_description_pattern(["git", "python", "mysql"])
        assert pattern is not None
        assert pattern.search("strong python skills required")
        assert pattern.search("database experience with mysql")
        assert not pattern.search("il agit dans un contexte...")

    def test_empty_keywords_returns_none(self):
        assert _build_description_pattern([]) is None

    def test_node_js_matches(self):
        """node.js (special chars) must still match correctly."""
        pattern = _build_description_pattern(["node.js"])
        assert pattern is not None
        assert pattern.search("backend in node.js")

    def test_python_in_description_matches(self):
        """Common skill 'python' should match normally."""
        pattern = _build_description_pattern(["python"])
        assert pattern is not None
        assert pattern.search("scripting in python is required")

    def test_no_false_positive_for_french_common_words(self):
        """A realistic French manual-worker job description must not match IT skills."""
        french_text = (
            "Il s'agit d'un poste d'ouvrier polyvalent. "
            "Le candidat devra s'agiter rapidement et travailler en equipe. "
            "Connaissance des reseaux electriques souhaitee."
        )
        pattern = _build_description_pattern(["git", "python", "javascript", "php", "mysql"])
        assert pattern is not None
        assert not pattern.search(_normalize(french_text)), (
            "A manual-worker French job description should not match IT skills"
        )


# ---------------------------------------------------------------------------
# Skill alias expansion
# ---------------------------------------------------------------------------

class TestSkillAliasExpansion:
    """Known abbreviations must expand to their canonical long forms."""

    def test_ai_expands_to_artificial_intelligence(self):
        expanded = _expand_skill_for_description("AI")
        assert any("artificial intelligence" in e for e in expanded), (
            f"'AI' should expand to include 'artificial intelligence', got {expanded}"
        )

    def test_ai_lowercase_expands(self):
        expanded = _expand_skill_for_description("ai")
        assert any("artificial intelligence" in e for e in expanded)

    def test_ml_expands_to_machine_learning(self):
        expanded = _expand_skill_for_description("ML")
        assert any("machine learning" in e for e in expanded)

    def test_js_expands_to_javascript(self):
        expanded = _expand_skill_for_description("JS")
        assert any("javascript" in e for e in expanded)

    def test_non_alias_returns_self(self):
        expanded = _expand_skill_for_description("Python")
        assert expanded == ["python"]

    def test_node_js_not_in_aliases(self):
        # node.js has a dot but is NOT a short abbreviation — stays as-is
        expanded = _expand_skill_for_description("Node.js")
        assert expanded == ["node.js"]

    def test_ai_description_pattern_does_not_match_jai(self):
        """After expanding 'AI' → 'artificial intelligence', the pattern
        must not match 'j'ai' or 'ai' standalone in French text."""
        expanded = _expand_skill_for_description("AI")
        pattern = _build_description_pattern(expanded)
        assert pattern is not None
        # "j'ai" is "I have" in French — must not match
        french_text = _normalize("j'ai de l'experience dans le batiment")
        assert not pattern.search(french_text), (
            "Expanded 'AI' pattern must not match French 'j'ai'"
        )

    def test_ai_description_pattern_matches_ai_job(self):
        """After expansion, the pattern should match a genuine AI/ML job."""
        expanded = _expand_skill_for_description("AI")
        pattern = _build_description_pattern(expanded)
        assert pattern is not None
        ai_text = _normalize(
            "We are looking for an engineer with experience in artificial intelligence "
            "and deep learning models."
        )
        assert pattern.search(ai_text)


# ---------------------------------------------------------------------------
# matches_title_keywords integration — description fallback with real profile
# ---------------------------------------------------------------------------

class TestTitleKeywordsDescriptionFallback:
    """Integration tests for matches_title_keywords using the filter functions."""

    def _make_config(self, desc_include: list[str]) -> dict[str, Any]:
        return {
            "title_keywords": {
                "include": ["software engineer"],
                "description_include": desc_include,
                "exclude": [],
            }
        }

    def test_git_in_agit_does_not_pass_filter(self):
        """A job whose description only contains 'agit' must NOT pass when 'git'
        is in the description_include list."""
        job = _job(
            title="Ouvrier Polyvalent",
            description="Il s'agit d'un poste en logistique. Le candidat agit vite.",
            skills=[],
        )
        config = self._make_config(["git"])
        assert not matches_title_keywords(job, config), (
            "Job with only 'agit' in description must not match 'git'"
        )

    def test_git_alone_passes_filter(self):
        job = _job(
            title="Ouvrier Polyvalent",
            description="Must have experience with git and version control.",
            skills=[],
        )
        config = self._make_config(["git"])
        assert matches_title_keywords(job, config)

    def test_ouvrier_polyvalent_does_not_match_it_skills(self):
        """Realistic manual-worker job should not pass IT description filter."""
        job = _job(
            title="Ouvrier Polyvalent",
            description=(
                "Il s'agit d'un poste d'ouvrier polyvalent. Vous agirez sous la "
                "responsabilite du chef d'equipe. Connaissance des reseaux "
                "electriques souhaitee. Travail en equipe, rigueur."
            ),
            skills=[],
        )
        config = self._make_config(["git", "python", "javascript", "php", "mysql", "node.js"])
        assert not matches_title_keywords(job, config)

    def test_it_job_passes_description_filter(self):
        """A genuine IT job with 'python' in the description must pass."""
        job = _job(
            title="Chargé de Mission",  # Generic title, no IT keywords
            description=(
                "Nous recherchons un développeur maîtrisant python, django, et les "
                "bases de données postgresql. Expérience avec git souhaitée."
            ),
            skills=[],
        )
        config = self._make_config(["git", "python", "javascript", "php", "mysql"])
        assert matches_title_keywords(job, config)

    def test_energie_batiment_job_does_not_match_git(self):
        """'Chargé de Projet Energie et Bâtiment' must not pass IT filter via 'agit'."""
        job = _job(
            title="Chargé de Projet Energie et Bâtiment Durables",
            description=(
                "Ce poste consiste à gérer des projets énergétiques. Il s'agit de "
                "coordonner les équipes de terrain sur les réseaux de chaleur. "
                "Connaissance des réseaux électriques et bâtiment durable requise."
            ),
            skills=[],
        )
        config = self._make_config(["git", "python", "javascript", "php", "mysql"])
        assert not matches_title_keywords(job, config)


# ---------------------------------------------------------------------------
# Location hard filter — France-first policy
# ---------------------------------------------------------------------------

class TestFranceLocationHardFilter:
    """France + any work mode must pass. Non-France non-remote must be rejected."""

    def _location_config(self) -> dict[str, Any]:
        return {
            "locations": {"include": ["France", "Europe"]},
            "remote_scope": {
                "european_keywords": ["Europe", "EU", "EMEA", "Europeenne"],
                "worldwide_keywords": ["worldwide", "global", "anywhere", "international"],
            },
        }

    def test_france_onsite_passes(self):
        job = _job(location="Paris, France", countries=["France"], remote=False)
        assert matches_location(job, self._location_config())

    def test_france_hybrid_passes(self):
        job = _job(location="Lyon, France", countries=["France"], remote=False, work_mode="hybrid")
        assert matches_location(job, self._location_config())

    def test_france_remote_passes(self):
        job = _job(location="Remote, France", countries=["France"], remote=True)
        assert matches_location(job, self._location_config())

    def test_outside_france_remote_europe_scope_passes(self):
        job = _job(location="Europe remote", countries=["Germany"], remote=True)
        assert matches_location(job, self._location_config())

    def test_outside_france_nonremote_known_country_rejected(self):
        """On-site job in a known non-France country must be rejected."""
        job = _job(location="Berlin, Germany", countries=["Germany"], remote=False)
        assert not matches_location(job, self._location_config())

    def test_outside_france_remote_no_scope_rejected(self):
        """Remote job outside France with no EU scope indicator must be rejected."""
        job = _job(location="London", countries=["United Kingdom"], remote=True)
        assert not matches_location(job, self._location_config())


# ---------------------------------------------------------------------------
# Pagination correctness
# ---------------------------------------------------------------------------

class TestPaginationScoreOrder:
    """_get_jobs_filtered must return results in job_ids order (score order),
    not in discovered_at order, so that global ranking is preserved across pages."""

    def test_jobs_returned_in_job_ids_score_order(self, tmp_path):
        """Jobs must be returned in the order of the job_ids list, not
        discovered_at order."""
        from src.storage.sqlite import Storage

        db = Storage(db_path=str(tmp_path / "test.db"))
        db.init_db()

        # Insert 5 jobs discovered in reverse order from their expected scores.
        # The "best" job (id=job1) was discovered LAST; the "worst" (job5) FIRST.
        # If pagination uses discovered_at the order would be [job1, job2, ...].
        # If it uses job_ids order (score order) it should be [job1, job2, ...] too
        # — but for a discriminating test, we use a different score order.
        now = datetime.now(UTC)
        jobs_data = [
            # (id, title, discovered_at offset in seconds)
            ("job5", "Fifth ranked job", 100),   # discovered earliest
            ("job4", "Fourth ranked job", 80),
            ("job3", "Third ranked job", 60),
            ("job2", "Second ranked job", 40),
            ("job1", "First ranked job", 20),    # discovered latest
        ]
        jobs = []
        for jid, title, offset in jobs_data:
            j = _job(
                source="test",
                title=title,
                discovered_at=now - timedelta(seconds=offset),
            )
            # Override id via dict manipulation (Job.id is set from source+title hash)
            j_dict = j.model_dump()
            j_dict["id"] = jid
            j_dict["discovered_at"] = now - timedelta(seconds=offset)
            jobs.append(Job(**j_dict))

        db.upsert_jobs_batch(jobs)

        # Score order: job1 (best) → job5 (worst) — opposite of discovered_at order.
        score_ordered_ids = ["job1", "job2", "job3", "job4", "job5"]

        from src.api.routes.jobs import _get_jobs_filtered

        # Fetch all in score order.
        result = _get_jobs_filtered(
            db,
            limit=None,
            offset=0,
            job_ids=score_ordered_ids,
        )
        result_ids = [j.id for j in result]
        assert result_ids == score_ordered_ids, (
            f"Expected score order {score_ordered_ids}, got {result_ids}"
        )

        # Verify pagination: page 1 gets top 3 by score, not by discovered_at.
        page1 = result[:3]
        page1_ids = [j.id for j in page1]
        assert page1_ids == ["job1", "job2", "job3"], (
            f"Page 1 must contain top 3 by score, got {page1_ids}"
        )

        # Page 2 gets next 2.
        page2 = result[3:5]
        page2_ids = [j.id for j in page2]
        assert page2_ids == ["job4", "job5"], (
            f"Page 2 must contain jobs 4-5 by score, got {page2_ids}"
        )

        db.close()

    def test_score_order_not_discovered_at_order(self, tmp_path):
        """Critical regression: page boundaries must be in SCORE order,
        not discovered_at order.  The best-ranked job must always be on page 1
        even if it was discovered first (oldest timestamp)."""
        from src.api.routes.jobs import _get_jobs_filtered
        from src.storage.sqlite import Storage

        db = Storage(db_path=str(tmp_path / "test2.db"))
        db.init_db()

        now = datetime.now(UTC)
        # Best job discovered oldest (would appear last in discovered_at DESC).
        best_job = _job(title="Best scored job")
        worst_job = _job(title="Worst scored job")

        # Patch IDs and timestamps manually.
        best_dict = best_job.model_dump()
        best_dict["id"] = "best"
        best_dict["discovered_at"] = now - timedelta(days=2)  # older

        worst_dict = worst_job.model_dump()
        worst_dict["id"] = "worst"
        worst_dict["discovered_at"] = now - timedelta(minutes=1)  # newer

        db.upsert_jobs_batch([Job(**best_dict), Job(**worst_dict)])

        # Score order: "best" first, "worst" second.
        score_ids = ["best", "worst"]

        result = _get_jobs_filtered(db, limit=None, offset=0, job_ids=score_ids)
        assert [j.id for j in result] == ["best", "worst"], (
            "Oldest (but best-scored) job must appear first"
        )

        db.close()


# ---------------------------------------------------------------------------
# Domain alias lookup
# ---------------------------------------------------------------------------

class TestDomainAliases:
    """French domain names (informatique, réseaux) must resolve to keywords."""

    def test_informatique_domain_produces_title_keywords(self):
        engine = load_filters("config/filters.yaml")
        domain_kws = engine.get("domain_keywords", {})
        informatique = domain_kws.get("informatique", {})
        assert informatique, "informatique domain must exist in filters.yaml"
        title_kws = informatique.get("title", [])
        assert len(title_kws) > 0, "informatique domain must have title keywords"
        # Must include at least one clear French IT job title term
        combined = " ".join(t.lower() for t in title_kws)
        assert "développeur" in combined or "developpeur" in combined or "informaticien" in combined

    def test_réseaux_domain_produces_title_keywords(self):
        engine = load_filters("config/filters.yaml")
        domain_kws = engine.get("domain_keywords", {})
        reseaux = domain_kws.get("réseaux", {})
        assert reseaux, "réseaux domain must exist in filters.yaml"
        title_kws = reseaux.get("title", [])
        assert len(title_kws) > 0, "réseaux domain must have title keywords"

    def test_informatique_domain_in_build_filter_config(self):
        """build_filter_config must pick up keywords for the 'informatique' domain."""
        engine = load_filters("config/filters.yaml")
        profile = load_profile(TEST_PROFILE_PATH)
        # Ensure informatique is in the profile (it currently is).
        if "informatique" not in profile.target_domains:
            pytest.skip("informatique not in current profile.yaml — test not applicable")
        cfg = build_filter_config(profile, engine)
        title_kws = cfg["title_keywords"]["include"]
        # The informatique domain should contribute title keywords (e.g. développeur).
        normalized_kws = [k.lower() for k in title_kws]
        assert any("developpeur" in k or "développeur" in k or "informaticien" in k
                   for k in normalized_kws), (
            f"build_filter_config should include informatique title keywords, "
            f"got title_kws: {title_kws[:10]}"
        )


# ---------------------------------------------------------------------------
# Real skills_match scoring
# ---------------------------------------------------------------------------

def _skills_config(skills: list[str]) -> dict[str, Any]:
    """Minimal scoring config with a specific target_skills list."""
    return {
        "scoring": {
            "location_fit": {"weight": 0.30},
            "domain_relevance": {"weight": 0.25},
            "seniority_match": {"weight": 0.15},
            "experience_fit": {"weight": 0.10},
            "skills_match": {"weight": 0.20, "target_skills": skills},
        }
    }


class TestRealSkillsMatch:
    """skills_match must reflect real overlap between profile skills and job content."""

    def test_no_target_skills_returns_neutral(self):
        """When no target skills configured, returns 0.5 (neutral)."""
        job = _job(title="Python Developer", description="We use Python and SQL.")
        score = _score_skills_match(job, _skills_config([]))
        assert score == 0.5

    def test_no_matching_skills_returns_low(self):
        """Job with no matching skills gets a low score (0.05), not 0.5 constant."""
        job = _job(
            title="Construction Manager",
            description="Manage building projects. Welding, plumbing, concrete.",
            skills=[],
        )
        score = _score_skills_match(job, _skills_config(["python", "javascript", "sql"]))
        assert score == 0.05, f"No-match job should score 0.05, got {score}"

    def test_exact_title_match(self):
        """A job title containing a target skill scores above neutral."""
        job = _job(title="Python Developer", description="Build backend services.")
        score = _score_skills_match(job, _skills_config(["python", "javascript", "sql"]))
        assert score > 0.4, f"Python in title should score above 0.4, got {score}"

    def test_description_match_with_word_boundary(self):
        """Skill appearing in description (as full word) should match."""
        job = _job(
            title="Backend Engineer",
            description="We require strong python skills and experience with postgresql.",
        )
        score = _score_skills_match(job, _skills_config(["python", "sql"]))
        assert score > 0.4

    def test_more_matches_scores_higher(self):
        """More matching skills → higher score."""
        job_few = _job(
            title="Developer",
            description="We use python for data processing.",
        )
        job_many = _job(
            title="Full-stack Developer",
            description="We use python, javascript, mysql, and git daily.",
        )
        skills = ["python", "javascript", "mysql", "git", "react"]
        score_few = _score_skills_match(job_few, _skills_config(skills))
        score_many = _score_skills_match(job_many, _skills_config(skills))
        assert score_many > score_few, (
            f"Job with more skill matches ({score_many:.2f}) should outscore "
            f"job with fewer ({score_few:.2f})"
        )

    def test_all_skills_matched_gives_max_score(self):
        """Matching all target skills returns 1.0."""
        skills = ["python", "javascript"]
        job = _job(
            title="Python JavaScript Developer",
            description="Work with python and javascript every day.",
        )
        score = _score_skills_match(job, _skills_config(skills))
        assert score == 1.0, f"All skills matched should give 1.0, got {score}"

    def test_ai_abbreviation_expands_correctly(self):
        """'AI' target skill expands to 'artificial intelligence' for description matching.
        Must not match 'j'ai' (French 'I have')."""
        skills = ["AI"]
        job_french_verb = _job(
            title="Ouvrier Polyvalent",
            description="J'ai de l'experience dans le batiment. Il s'agit d'un poste.",
        )
        job_real_ai = _job(
            title="Machine Learning Engineer",
            description="Work on artificial intelligence models and deep learning systems.",
        )
        score_false = _score_skills_match(job_french_verb, _skills_config(skills))
        score_true = _score_skills_match(job_real_ai, _skills_config(skills))
        assert score_false == 0.05, f"'j'ai' must not match 'AI' skill, got {score_false}"
        assert score_true > 0.4, f"Real AI job should match 'AI' skill, got {score_true}"

    def test_git_does_not_match_agit(self):
        """'git' target skill must not match 'agit' in French descriptions."""
        skills = ["git"]
        job = _job(
            title="Ouvrier",
            description="Il s'agit d'un poste. Le candidat agit rapidement.",
        )
        score = _score_skills_match(job, _skills_config(skills))
        assert score == 0.05, f"'agit' must not match 'git' skill, got {score}"

    def test_git_matches_standalone(self):
        """'git' must match when appearing as a standalone word."""
        skills = ["git"]
        job = _job(
            title="Developer",
            description="You must know git and github for version control.",
        )
        score = _score_skills_match(job, _skills_config(skills))
        assert score > 0.4

    def test_skills_list_field_used_for_matching(self):
        """Skills listed in job.skills field should also be matched."""
        job = _job(
            title="Developer",
            description="We build software.",
            skills=["Python", "Django", "PostgreSQL"],
        )
        score = _score_skills_match(job, _skills_config(["python", "javascript"]))
        assert score > 0.4, "Python in job.skills should contribute to match"

    def test_score_is_float_in_range(self):
        """Result must always be a float in [0.0, 1.0]."""
        for desc in ["", "python", "j'ai de l'expérience", "artificial intelligence"]:
            job = _job(description=desc)
            score = _score_skills_match(job, _skills_config(["python", "AI", "git"]))
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0, f"Score {score} out of range for desc={desc!r}"

    def test_real_profile_skills_injected_into_scoring(self):
        """Profile target_skills are passed through build_filter_config → scoring config."""
        profile = load_profile(TEST_PROFILE_PATH)
        # Profile currently has skills: python, javascript, php, MySQL, Node.js, Git
        assert len(profile.target_skills) > 0, "Profile must have target_skills"
        # A Python job should score better than a no-match job
        python_job = _job(title="Python Developer", description="Python and SQL required.")
        no_match_job = _job(title="Carreleur", description="Pose de carrelage et faïence.")
        config_with_skills = _skills_config(profile.target_skills)
        python_score = _score_skills_match(python_job, config_with_skills)
        no_match_score = _score_skills_match(no_match_job, config_with_skills)
        assert python_score > no_match_score, (
            f"Python job ({python_score:.2f}) must outscore no-match job ({no_match_score:.2f})"
        )


# ---------------------------------------------------------------------------
# skills_match role-context fallback (Phase 2, 2026-09-10 scoring redesign)
#
# domain_relevance is optional and defaults to None specifically so every
# pre-existing TestRealSkillsMatch call above (all 2-arg, no domain context)
# keeps its original 0.05-floor behavior — only _compute_factors (production)
# supplies domain_relevance, activating the fallback below.
# ---------------------------------------------------------------------------

class TestSkillsMatchRoleContextFallback:
    def test_explicit_skills_unaffected_by_domain_relevance(self):
        """(1) When explicit skill evidence exists, domain_relevance must not
        change the result at all — the fallback branch never runs."""
        job = _job(title="Python Developer", description="Build backend services.")
        cfg = _skills_config(["python", "javascript", "sql"])
        score_no_domain = _score_skills_match(job, cfg)
        score_with_domain = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score_no_domain == score_with_domain, (
            "Explicit skill match must be identical regardless of domain_relevance"
        )
        assert score_no_domain > 0.4

    def test_confirmed_domain_no_skills_gets_sensible_fallback(self):
        """(2) Strong in-domain role (domain_relevance=1.0, title keyword match)
        with zero explicit skill/tool keywords gets a real but modest fallback
        score — not the old 0.05 floor, not a high score either."""
        job = _job(
            title="Administrateur Systèmes et Réseaux",
            description="Vous assurez la supervision du parc informatique et des serveurs.",
        )
        cfg = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        score = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score == pytest.approx(0.20 + 0.20 * 1.0, abs=1e-6)
        assert 0.05 < score < 0.5, f"Expected a modest fallback, got {score}"

    def test_medium_domain_gets_proportionally_lower_fallback(self):
        """(3) A description-only domain match (domain_relevance=0.7) must
        produce a lower fallback than a title match (domain_relevance=1.0) —
        continuous scaling, not a flat step."""
        job = _job(title="Technicien Support", description="Support informatique de niveau 1.")
        cfg = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        score_medium = _score_skills_match(job, cfg, domain_relevance=0.7)
        score_high = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score_medium == pytest.approx(0.20 + 0.20 * 0.7, abs=1e-6)
        assert score_medium < score_high, "Lower domain confidence must yield a lower fallback"

    def test_weak_or_off_domain_gets_no_fallback_boost(self):
        """(4) A job below the fallback threshold (undetermined domain=0.45,
        broad-tech-title-only=0.35) must NOT receive any fallback boost —
        stays at the plain 0.05 floor. A generic/weakly related job must not
        be rewarded merely for having a nonzero domain score."""
        job = _job(title="Coordinateur de projet", description="Suivi de planning.")
        cfg = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        for weak_domain in (0.0, 0.35, 0.45, 0.69):
            score = _score_skills_match(job, cfg, domain_relevance=weak_domain)
            assert score == 0.05, (
                f"domain_relevance={weak_domain} (below threshold) must not get a fallback boost, "
                f"got {score}"
            )

    def test_fallback_cannot_exceed_weakest_explicit_match(self):
        """(5) The fallback must never produce a score as high as even the
        weakest possible EXPLICIT skill match (1 of 5 skills = 0.52) —
        real evidence must always outrank the no-evidence fallback, and the
        fallback alone must not be able to manufacture a high final score."""
        job = _job(title="Ingénieur Systèmes", description="Administration de serveurs Linux.")
        cfg = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        fallback_score = _score_skills_match(job, cfg, domain_relevance=1.0)
        weakest_explicit_job = _job(title="Developer", description="We use python daily.")
        weakest_explicit_score = _score_skills_match(weakest_explicit_job, cfg)
        assert fallback_score < weakest_explicit_score, (
            f"Fallback ({fallback_score:.2f}) must stay below the weakest explicit match "
            f"({weakest_explicit_score:.2f})"
        )
        assert fallback_score <= 0.40

    def test_full_factor_computation_unaffected_for_other_factors(self):
        """(6) Running the full _compute_factors pipeline (which now threads
        domain_relevance into skills_match) must leave location_fit,
        seniority_match, and experience_fit byte-for-byte identical to
        computing them directly — Phase 2 must not touch any other factor."""
        from src.pipeline.scoring import (
            _compute_factors,
            _score_experience_fit,
            _score_location_fit,
            _score_seniority_match,
        )

        job = _job(
            title="Administrateur Systèmes et Réseaux",
            description="Vous assurez la supervision du parc informatique.",
            department="83",
        )
        cfg = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        cfg["scoring"]["domain_relevance"]["domain_title_keywords"] = ["administrateur systemes"]
        factors = _compute_factors(job, cfg)

        expected_location = _score_location_fit(
            job.location, job.countries, job.work_mode, job.remote, job.department, cfg
        )
        expected_seniority = _score_seniority_match(job.title, cfg)
        expected_experience = _score_experience_fit(job, cfg)

        assert factors["location_fit"] == expected_location
        assert factors["seniority_match"] == expected_seniority
        assert factors["experience_fit"] == expected_experience
        # And skills_match did receive the fallback (domain_relevance=1.0 for this title).
        assert factors["domain_relevance"] == 1.0
        assert factors["skills_match"] == pytest.approx(0.40, abs=1e-6)


# ---------------------------------------------------------------------------
# skills_match domain-vocabulary bonus (2026-09-11 audit follow-up)
#
# Distinct from target_skills: scoring.skills_match.domain_vocabulary is a
# SEPARATE curated list (injected from config/filters.yaml's
# domain_skill_vocabulary) that only nudges the existing fallback tier for
# confirmed in-domain jobs with zero target_skill overlap — it must never
# behave like an extra target_skills entry.
# ---------------------------------------------------------------------------

class TestSkillsMatchDomainVocabularyBonus:
    def _cfg(self, target_skills: list[str], domain_vocab: list[str]) -> dict[str, Any]:
        cfg = _skills_config(target_skills)
        cfg["scoring"]["skills_match"]["domain_vocabulary"] = domain_vocab
        return cfg

    def test_confirmed_domain_with_vocabulary_hit_beats_plain_fallback(self):
        """A confirmed in-domain cybersecurity job mentioning real domain
        vocabulary (SIEM) but none of the candidate's target_skills must
        score higher than the same job without that vocabulary mention."""
        job_with_vocab = _job(
            title="Ingénieur Cybersécurité SOC",
            description="Vous exploitez notre SIEM et menez des analyses de threat intelligence.",
        )
        job_without_vocab = _job(
            title="Ingénieur Cybersécurité SOC",
            description="Vous rejoignez une équipe motivée dans un environnement stimulant.",
        )
        cfg = self._cfg(
            ["python", "javascript", "php", "mysql", "docker"],
            ["siem", "threat intelligence", "pentest"],
        )
        score_with = _score_skills_match(job_with_vocab, cfg, domain_relevance=1.0)
        score_without = _score_skills_match(job_without_vocab, cfg, domain_relevance=1.0)
        assert score_with > score_without
        assert score_without == pytest.approx(0.40, abs=1e-6)  # plain fallback, unchanged

    def test_vocabulary_bonus_stays_below_weakest_explicit_match(self):
        """The domain-vocabulary-boosted fallback must still never reach the
        weakest possible EXPLICIT target_skill match (0.52) — real skill
        evidence from the candidate's own profile must always outrank it."""
        job = _job(
            title="Ingénieur Cybersécurité SOC",
            description="Vous exploitez notre SIEM au quotidien.",
        )
        cfg = self._cfg(["python", "javascript", "php", "mysql", "docker"], ["siem"])
        score = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score < 0.52
        assert score == pytest.approx(0.50, abs=1e-6)  # 0.40 fallback + 0.12 bonus, capped

    def test_vocabulary_hit_ignored_below_domain_threshold(self):
        """A domain-vocabulary mention must not rescue a job whose domain is
        NOT confirmed (undetermined/broad-tech-title-only) — same discipline
        as the plain fallback (test_weak_or_off_domain_gets_no_fallback_boost)."""
        job = _job(title="Coordinateur SOC", description="Notre SIEM est central à l'activité.")
        cfg = self._cfg(["python", "javascript", "php", "mysql", "docker"], ["siem"])
        for weak_domain in (0.0, 0.35, 0.45, 0.69):
            score = _score_skills_match(job, cfg, domain_relevance=weak_domain)
            assert score == 0.05, f"domain_relevance={weak_domain} must not get any bonus"

    def test_no_vocabulary_hit_leaves_plain_fallback_unchanged(self):
        """When domain_vocabulary is configured but no term appears in the
        job, the plain fallback score must be identical to having no
        domain_vocabulary configured at all — no accidental side effect."""
        job = _job(title="Administrateur Systèmes", description="Supervision du parc informatique.")
        cfg_with_vocab = self._cfg(
            ["python", "javascript", "php", "mysql", "docker"], ["siem", "vlan", "pentest"]
        )
        cfg_without_vocab = _skills_config(["python", "javascript", "php", "mysql", "docker"])
        score_with = _score_skills_match(job, cfg_with_vocab, domain_relevance=1.0)
        score_without = _score_skills_match(job, cfg_without_vocab, domain_relevance=1.0)
        assert score_with == score_without == pytest.approx(0.40, abs=1e-6)

    def test_explicit_target_skill_match_unaffected_by_domain_vocabulary(self):
        """When the candidate's own target_skills already match, domain
        vocabulary must have zero effect — the bonus only applies inside the
        zero-match fallback branch."""
        job = _job(
            title="Ingénieur Cybersécurité SOC",
            description="Python, SIEM, threat intelligence au quotidien.",
        )
        cfg = self._cfg(["python", "javascript", "php", "mysql", "docker"], ["siem"])
        score = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score > 0.5  # real explicit match (python), not the fallback path


# ---------------------------------------------------------------------------
# Semantic threshold enforcement
# ---------------------------------------------------------------------------

class TestSemanticThresholdEnforcement:
    """min_threshold must zero out below-threshold semantic scores in ranking."""

    def _sem_config(self, weight: float = 0.2, threshold: float = 0.3) -> dict[str, Any]:
        return {"similarity": {"weight": weight, "min_threshold": threshold}}

    def _make_scored_job(self, title: str, det_score: float) -> tuple[Job, float]:
        return _job(title=title), det_score

    def test_below_threshold_score_zeroed(self):
        """A job with semantic score below min_threshold contributes 0 to final score."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Some Job")
        det_score = 0.7
        sem_score = 0.2  # below threshold 0.3

        result = integrate_semantic_into_ranking(
            [(job, det_score)],
            [(job, sem_score)],
            self._sem_config(weight=0.2, threshold=0.3),
        )
        _, final_score = result[0]
        # Expected: 0.8 * 0.7 + 0.2 * 0.0 = 0.56 (sem zeroed out)
        assert abs(final_score - 0.8 * det_score) < 1e-6, (
            f"Below-threshold sem score should not contribute; expected {0.8 * det_score:.3f}, "
            f"got {final_score:.3f}"
        )

    def test_above_threshold_score_kept(self):
        """A job with semantic score at or above min_threshold keeps its score."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Relevant Job")
        det_score = 0.7
        sem_score = 0.5  # above threshold 0.3

        result = integrate_semantic_into_ranking(
            [(job, det_score)],
            [(job, sem_score)],
            self._sem_config(weight=0.2, threshold=0.3),
        )
        _, final_score = result[0]
        expected = 0.8 * det_score + 0.2 * sem_score
        assert abs(final_score - expected) < 1e-6, (
            f"Above-threshold sem score must be kept; expected {expected:.3f}, got {final_score:.3f}"
        )

    def test_exactly_at_threshold_is_kept(self):
        """A semantic score exactly equal to min_threshold passes."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="At-threshold Job")
        det_score = 0.7
        sem_score = 0.3  # exactly at threshold

        result = integrate_semantic_into_ranking(
            [(job, det_score)],
            [(job, sem_score)],
            self._sem_config(weight=0.2, threshold=0.3),
        )
        _, final_score = result[0]
        expected = 0.8 * det_score + 0.2 * sem_score
        assert abs(final_score - expected) < 1e-6

    def test_threshold_zero_keeps_all_scores(self):
        """When min_threshold=0, even very low sem scores are kept."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Low sem job")
        det_score = 0.7
        sem_score = 0.05

        result = integrate_semantic_into_ranking(
            [(job, det_score)],
            [(job, sem_score)],
            self._sem_config(weight=0.2, threshold=0.0),
        )
        _, final_score = result[0]
        expected = 0.8 * det_score + 0.2 * sem_score
        assert abs(final_score - expected) < 1e-6

    def test_omitted_min_threshold_falls_back_to_0_3_not_0_0(self):
        """If `similarity.min_threshold` is absent from config entirely
        (rather than explicitly set), this function must fall back to the
        module's own canonical default (0.3, matching the debug-log-only
        read elsewhere in semantic_matching.py) — not silently disable
        threshold enforcement by defaulting to 0.0. A below-threshold score
        must still be zeroed out."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Some Job")
        det_score = 0.7
        sem_score = 0.2  # below the expected 0.3 fallback

        config_without_min_threshold = {"similarity": {"weight": 0.2}}
        result = integrate_semantic_into_ranking(
            [(job, det_score)],
            [(job, sem_score)],
            config_without_min_threshold,
        )
        _, final_score = result[0]
        assert abs(final_score - 0.8 * det_score) < 1e-6, (
            "Omitted min_threshold must still enforce the 0.3 default, "
            f"zeroing the semantic contribution; got {final_score:.3f}"
        )

    def test_relevant_job_outranks_irrelevant_with_threshold(self):
        """With threshold enforcement, a semantically relevant job ranks above an
        irrelevant one with the same deterministic score."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        relevant = _job(title="Python ML Engineer")
        irrelevant = _job(title="Electrician")
        det = 0.7

        result = integrate_semantic_into_ranking(
            [(relevant, det), (irrelevant, det)],
            [(relevant, 0.65), (irrelevant, 0.15)],  # irrelevant below threshold
            self._sem_config(weight=0.2, threshold=0.3),
        )
        ids = [j.title for j, _ in result]
        assert ids[0] == "Python ML Engineer", (
            "Semantically relevant job must rank above irrelevant job when threshold zeroes out "
            f"the irrelevant job's semantic score; got order: {ids}"
        )


# ---------------------------------------------------------------------------
# Semantic fixed-anchor normalization (Phase 1, 2026-09-10 scoring redesign)
#
# Deliberately NOT per-run min-max/percentile normalization: the core
# invariant under test is that a job's normalized semantic contribution
# depends only on its OWN raw score and the fixed config anchors — never on
# what other jobs happen to be in the same batch. A weak collection run must
# not be able to turn its best (but still mediocre) job into an artificially
# "excellent" (~1.0) semantic score.
# ---------------------------------------------------------------------------

class TestSemanticFixedAnchorNormalization:
    def _sem_config(
        self, weight: float = 0.06, floor: float = 0.20, ceiling: float = 0.65,
        threshold: float = 0.0,
    ) -> dict[str, Any]:
        return {
            "similarity": {
                "weight": weight,
                "min_threshold": threshold,
                "reference_floor": floor,
                "reference_ceiling": ceiling,
            }
        }

    def test_no_anchors_configured_is_identity(self):
        """Without reference_floor/reference_ceiling in config, behavior is
        unchanged from the plain raw-score weighted blend (backward compatible
        with every pre-existing caller/test that doesn't set anchors)."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Some Job")
        det_score, sem_score = 0.7, 0.45
        result = integrate_semantic_into_ranking(
            [(job, det_score)], [(job, sem_score)],
            {"similarity": {"weight": 0.06, "min_threshold": 0.0}},
        )
        _, final_score = result[0]
        expected = 0.94 * det_score + 0.06 * sem_score
        assert abs(final_score - expected) < 1e-6

    def test_score_at_or_below_floor_normalizes_to_zero_contribution(self):
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Weak semantic job")
        det_score = 0.7
        result = integrate_semantic_into_ranking(
            [(job, det_score)], [(job, 0.20)], self._sem_config(),
        )
        _, final_score = result[0]
        assert abs(final_score - (1 - 0.06) * det_score) < 1e-6, (
            "A raw semantic score at the floor anchor must contribute 0"
        )

    def test_score_at_or_above_ceiling_normalizes_to_full_weight(self):
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Strongest realistic semantic job")
        det_score = 0.7
        # 0.65 = reference_ceiling; 0.90 = hypothetical score above it (must clamp, not exceed)
        for raw_sem in (0.65, 0.90):
            result = integrate_semantic_into_ranking(
                [(job, det_score)], [(job, raw_sem)], self._sem_config(),
            )
            _, final_score = result[0]
            expected = (1 - 0.06) * det_score + 0.06 * 1.0
            assert abs(final_score - expected) < 1e-6, (
                f"raw_sem={raw_sem} at/above ceiling must clamp to full weight contribution"
            )

    def test_midpoint_maps_to_half(self):
        """A raw score halfway between floor (0.20) and ceiling (0.65) — i.e. 0.425 —
        must normalize to 0.5, confirming the mapping is a plain linear interpolation."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        job = _job(title="Midpoint job")
        det_score = 0.7
        result = integrate_semantic_into_ranking(
            [(job, det_score)], [(job, 0.425)], self._sem_config(),
        )
        _, final_score = result[0]
        expected = (1 - 0.06) * det_score + 0.06 * 0.5
        assert abs(final_score - expected) < 1e-4

    def test_normalization_is_batch_independent(self):
        """THE core invariant: a job's normalized semantic contribution must be
        identical whether it is scored alongside strong peers or as the lone
        (best-of-a-weak-batch) job — fixed anchors, not per-run min-max/percentile,
        must not let a weak collection run inflate its best job's semantic score."""
        from src.pipeline.semantic_matching import integrate_semantic_into_ranking

        strong_job = _job(title="Strong peer 1")
        weak_job = _job(title="Best of a weak batch")
        det_score = 0.7
        weak_job_raw_sem = 0.30  # genuinely mediocre semantic overlap

        # Scenario A: weak_job is the best job in a batch full of strong peers.
        result_a = integrate_semantic_into_ranking(
            [(strong_job, det_score), (weak_job, det_score)],
            [(strong_job, 0.65), (weak_job, weak_job_raw_sem)],
            self._sem_config(),
        )
        final_a = {j.title: s for j, s in result_a}["Best of a weak batch"]

        # Scenario B: weak_job is the ONLY (and therefore "best") job collected
        # this run — a thin/weak collection day, exactly the failure mode a
        # per-run min-max/percentile normalization would mis-score as ~1.0.
        result_b = integrate_semantic_into_ranking(
            [(weak_job, det_score)],
            [(weak_job, weak_job_raw_sem)],
            self._sem_config(),
        )
        final_b = result_b[0][1]

        assert abs(final_a - final_b) < 1e-9, (
            f"A job's normalized score must not depend on which other jobs share its batch "
            f"(got {final_a:.4f} vs {final_b:.4f})"
        )
        # And it must stay well below the "excellent" range, not get inflated to ~1.0.
        expected = (1 - 0.06) * det_score + 0.06 * ((weak_job_raw_sem - 0.20) / (0.65 - 0.20))
        assert abs(final_b - expected) < 1e-6

    def test_default_config_weight_is_small(self):
        """The production default weight (config/semantic_config.yaml) must be
        small (~0.06), not the old dominant 0.2 — semantic is a secondary/
        tie-breaking signal, not a primary match-quality component."""
        from src.pipeline.semantic_matching import load_semantic_config

        config = load_semantic_config("config/semantic_config.yaml")
        weight = config["similarity"]["weight"]
        assert 0.0 < weight <= 0.10, f"Expected a small secondary weight, got {weight}"


# ---------------------------------------------------------------------------
# Seniority exclusion — new entries (lead / principal / staff / executive)
# and trailing-space fix (vp, chief)
# ---------------------------------------------------------------------------

class TestSeniorityExclusionConfig:
    """Verify that filters.yaml seniority entries match common senior job titles."""

    def _load_cfg(self) -> dict:
        from pathlib import Path

        import yaml
        root = Path(__file__).parent.parent
        with open(root / "config" / "filters.yaml", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def _excl_keywords(self, groups: list[str]) -> list[str]:
        cfg = self._load_cfg()
        excl = cfg.get("seniority_exclusions", {})
        kws: list[str] = []
        for g in groups:
            kws.extend(excl.get(g, []))
        return [k.lower() for k in kws]

    def _matches(self, title: str, groups: list[str]) -> bool:
        title_lower = title.lower()
        for kw in self._excl_keywords(groups):
            if kw in title_lower:
                return True
        return False

    # vp / chief — trailing-space fix
    def test_vp_at_end_of_title(self):
        assert self._matches("Engineering VP", ["vp"]), (
            "'vp' at end of title must match (trailing-space bug was removed)"
        )

    def test_vp_at_start_of_title(self):
        assert self._matches("VP of Engineering", ["vp"])

    def test_chief_at_end_of_title(self):
        assert self._matches("Technology Chief", ["c_suite"]), (
            "'chief' at end of title must match (trailing-space bug was removed)"
        )

    def test_chief_at_start_of_title(self):
        assert self._matches("Chief Technology Officer", ["c_suite"])

    # lead
    def test_tech_lead_excluded(self):
        assert self._matches("Tech Lead", ["lead"])

    def test_team_lead_excluded(self):
        assert self._matches("Team Lead Backend", ["lead"])

    def test_lead_developer_excluded(self):
        assert self._matches("Lead Developer", ["lead"])

    def test_lead_engineer_excluded(self):
        assert self._matches("Lead Software Engineer", ["lead"])

    # principal
    def test_principal_engineer_excluded(self):
        assert self._matches("Principal Engineer", ["principal"])

    def test_principal_developer_excluded(self):
        assert self._matches("Principal Developer", ["principal"])

    # staff
    def test_staff_engineer_excluded(self):
        assert self._matches("Staff Engineer", ["staff"])

    def test_staff_developer_excluded(self):
        assert self._matches("Staff Software Developer", ["staff"])

    # executive
    def test_executive_director_excluded(self):
        assert self._matches("Executive Director", ["executive"])

    # Verify regular junior roles are NOT caught
    def test_junior_developer_not_excluded(self):
        assert not self._matches("Junior Developer", ["lead", "principal", "staff", "executive"])

    def test_software_engineer_not_excluded(self):
        assert not self._matches("Software Engineer", ["lead", "principal", "staff", "executive"])

    # French lead equivalents (real-world audit gap: "LEAD Développeur PHP" slipped through)
    def test_lead_developpeur_prefix_excluded(self):
        assert self._matches("LEAD Développeur / Développeuse PHP", ["lead"]), (
            "'lead développeur' prefix pattern must be excluded"
        )

    def test_lead_ingenieur_prefix_excluded(self):
        assert self._matches("Lead Ingénieur DevOps", ["lead"]), (
            "'lead ingénieur' prefix pattern must be excluded"
        )

    def test_developpeur_lead_suffix_excluded(self):
        assert self._matches("Développeur Lead Python", ["lead"]), (
            "'développeur lead' suffix pattern must be excluded"
        )

    def test_ingenieur_lead_suffix_excluded(self):
        assert self._matches("Ingénieur Lead Systèmes", ["lead"]), (
            "'ingénieur lead' suffix pattern must be excluded"
        )

    def test_junior_developpeur_not_lead_excluded(self):
        assert not self._matches("Junior Développeur PHP", ["lead"]), (
            "Junior developer title must not be caught by lead exclusion"
        )

    # French director equivalents
    def test_directeur_excluded(self):
        assert self._matches("Directeur Technique", ["director"]), (
            "'directeur' is the French equivalent of director and must be excluded"
        )

    def test_directrice_excluded(self):
        assert self._matches("Directrice des Systèmes d'Information", ["director"]), (
            "'directrice' is the French feminine director title and must be excluded"
        )

    # French VP equivalents
    def test_vice_president_hyphen_excluded(self):
        assert self._matches("Vice-Président Technique", ["vp"]), (
            "'vice-président' (hyphenated French) must be excluded"
        )

    def test_vice_president_space_excluded(self):
        assert self._matches("Vice Président Produit", ["vp"]), (
            "'vice président' (space-separated French) must be excluded"
        )


# ---------------------------------------------------------------------------
# explain.py — work_mode rejection detail
# ---------------------------------------------------------------------------

class TestExplainWorkModeRejection:
    """work_mode rejection detail must produce a readable explanation, not a generic fallback."""

    def _make_profile(self):

        import yaml

        from src.models.profile import JobSearchProfile
        with open(TEST_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return JobSearchProfile(**data)

    def test_work_mode_rejection_not_generic(self):
        from src.pipeline.explain import generate_explanation
        profile = self._make_profile()
        job = _job(title="On-site Job Germany", countries=["Germany"], remote=False, work_mode="on_site")
        result = generate_explanation(
            job=job,
            profile=profile,
            filter_result="rejected",
            rejection_detail="work_mode",
            domain_keywords={},
        )
        texts = [r.text for r in result.rejection_reasons]
        # Must not fall through to the generic "Did not pass filter: work_mode" message
        assert not any("Did not pass filter" in t for t in texts), (
            f"work_mode rejection should show a specific message, got: {texts}"
        )
        assert any("work mode" in t.lower() or "remote" in t.lower() for t in texts), (
            f"Expected a work-mode-related explanation, got: {texts}"
        )


# ---------------------------------------------------------------------------
# normalize.py — salary uppercase K
# ---------------------------------------------------------------------------

class TestSalaryParseUppercaseK:
    def _parse(self, v):
        from src.pipeline.normalize import _parse_salary
        return _parse_salary(v)

    def test_uppercase_k(self):
        assert self._parse("50K") == 50000.0

    def test_lowercase_k(self):
        assert self._parse("50k") == 50000.0

    def test_dollar_uppercase_k(self):
        assert self._parse("$80K") == 80000.0

    def test_euro_uppercase_k(self):
        assert self._parse("€60K") == 60000.0

    def test_plain_number(self):
        assert self._parse("75000") == 75000.0

    def test_none_returns_none(self):
        assert self._parse(None) is None


# ---------------------------------------------------------------------------
# profile.py — languages as string in to_semantic_info
# ---------------------------------------------------------------------------

class TestProfileLanguagesString:
    def test_languages_is_string(self):

        import yaml

        from src.models.profile import JobSearchProfile
        with open(TEST_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        profile = JobSearchProfile(**data)
        info = profile.to_semantic_info()
        assert isinstance(info["languages"], str), (
            f"languages must be a string for embedding, got {type(info['languages'])}: {info['languages']!r}"
        )
        assert "[" not in info["languages"], (
            f"languages must not be a Python list repr, got: {info['languages']!r}"
        )


# ---------------------------------------------------------------------------
# Domain relevance scoring (formerly "title relevance" — redesigned to
# distinguish "cannot detect the domain" (0.45, undetermined, not penalized)
# from "confirmed wrong domain" (0.05, matches known off-domain vocabulary,
# triggers the domain gate in _apply_weights). Skill-in-title is no longer a
# domain_relevance tier — it lives exclusively in skills_match to avoid
# double-counting the same signal.
# ---------------------------------------------------------------------------

def _domain_rel_config(
    domain_title_kws: list[str] | None = None,
    domain_desc_kws: list[str] | None = None,
    off_domain_kws: list[str] | None = None,
) -> dict[str, Any]:
    """Minimal scoring config wired for _score_domain_relevance."""
    return {
        "scoring": {
            "domain_relevance": {
                "weight": 0.25,
                "domain_title_keywords": domain_title_kws or [],
                "domain_description_keywords": domain_desc_kws or [],
                "off_domain_keywords": off_domain_kws or [],
            },
        }
    }


class TestDomainRelevanceScoring:
    """_score_domain_relevance must assign tiered scores based on domain/off-domain evidence."""

    def test_no_title_returns_undetermined(self):
        assert _score_domain_relevance(None, None, _domain_rel_config()) == 0.45

    def test_empty_title_returns_undetermined(self):
        assert _score_domain_relevance("", None, _domain_rel_config()) == 0.45

    def test_no_config_falls_back_to_broad_tech_tier(self):
        """When no domain/off-domain keywords are configured, a broad tech
        indicator in the title still earns the mid tier (0.35)."""
        score = _score_domain_relevance("Software Developer", None, _domain_rel_config())
        assert score == 0.35

    def test_domain_keyword_in_title_returns_max(self):
        """Direct domain keyword match in title → 1.0."""
        config = _domain_rel_config(domain_title_kws=["cybersécurité", "développeur"])
        score = _score_domain_relevance("Expert en Cybersécurité", None, config)
        assert score == 1.0

    def test_domain_keyword_accent_insensitive(self):
        """Domain keyword matching must be accent-insensitive."""
        config = _domain_rel_config(domain_title_kws=["cybersecurite"])
        score = _score_domain_relevance("Expert en Cybersécurité", None, config)
        assert score == 1.0

    def test_domain_keyword_developpeur(self):
        config = _domain_rel_config(domain_title_kws=["développeur", "ingénieur réseau"])
        score = _score_domain_relevance("Développeur Junior Python", None, config)
        assert score == 1.0

    def test_domain_keyword_in_description_returns_high(self):
        """Domain keyword found only in the description (not the title) → 0.7."""
        config = _domain_rel_config(
            domain_title_kws=["cybersécurité"], domain_desc_kws=["développement logiciel"],
        )
        score = _score_domain_relevance(
            "Alternance Support Technique", "Poste en développement logiciel au sein de l'équipe.", config,
        )
        assert score == 0.7

    def test_skill_only_signal_does_not_earn_a_domain_bonus(self):
        """A skill name alone in the title (no domain keyword, no broad-tech
        word, no off-domain evidence) must NOT earn a domain_relevance bonus
        — that signal belongs to skills_match only, to avoid double-counting."""
        config = _domain_rel_config(domain_title_kws=["cybersécurité", "développeur"])
        score = _score_domain_relevance("Python Ninja", None, config)
        assert score == 0.45, "Skill-only titles must land at undetermined, not a bonus tier"

    def test_broad_tech_engineer_returns_mid_tier(self):
        """'engineer' in title (no domain match) → 0.35."""
        config = _domain_rel_config(domain_title_kws=["cybersécurité"])
        score = _score_domain_relevance("Data Scientist / IA Engineer", None, config)
        assert score == 0.35

    def test_broad_tech_ingenieur_returns_mid_tier(self):
        """French 'ingénieur' in title → 0.35."""
        config = _domain_rel_config(domain_title_kws=["cybersécurité"])
        score = _score_domain_relevance("Ingénieur Développement IA & Automatisation", None, config)
        assert score == 0.35

    def test_broad_tech_technicien_returns_mid_tier(self):
        config = _domain_rel_config(domain_title_kws=["cybersécurité"])
        score = _score_domain_relevance("Technicien Réseau Senior", None, config)
        assert score == 0.35

    def test_broad_tech_analyste_returns_mid_tier(self):
        config = _domain_rel_config(domain_title_kws=["cybersécurité"])
        score = _score_domain_relevance("Analyste Fonctionnel", None, config)
        assert score == 0.35

    def test_undetected_domain_is_not_penalized(self):
        """A title with no domain/broad-tech/off-domain evidence lands at the
        undetermined score (0.45) — NOT the old low Tier-4 value — because
        failing to detect the domain is not the same as confirming it's wrong."""
        config = _domain_rel_config(domain_title_kws=["développeur", "cybersécurité"])
        score = _score_domain_relevance("Alternance Learning & Development H/F", None, config)
        assert score == 0.45

    def test_confirmed_off_domain_title_returns_min(self):
        """A title matching known off-domain vocabulary (e.g. sales/HR/finance
        role keywords, reused from filters.yaml) → 0.05, the confirmed-wrong
        tier that triggers the domain gate."""
        config = _domain_rel_config(
            domain_title_kws=["développeur", "cybersécurité"],
            off_domain_kws=["commercial", "recruteur"],
        )
        score = _score_domain_relevance("Recruteur H/F", None, config)
        assert score == 0.05

    def test_off_domain_keyword_is_word_boundary_guarded(self):
        """Off-domain keyword matching must not match as an embedded substring."""
        config = _domain_rel_config(off_domain_kws=["vendeur"])
        # "Développeur" must not match "vendeur" as a substring.
        score = _score_domain_relevance("Développeur Python", None, config)
        assert score != 0.05

    def test_domain_keyword_takes_priority_over_broad_tech(self):
        """When both a domain keyword and a broad-tech word match, the domain
        keyword wins (1.0, not 0.35)."""
        config = _domain_rel_config(domain_title_kws=["cybersécurité", "cybersecurity"])
        score = _score_domain_relevance("Python Cybersecurity Engineer", None, config)
        assert score == 1.0

    def test_tech_indicator_does_not_match_development(self):
        """'developer' must NOT match 'development' — different words."""
        config = _domain_rel_config(domain_title_kws=["développeur", "cybersécurité"])
        # "Learning & Development" contains "development" but NOT "developer"
        score = _score_domain_relevance("Alternance Learning & Development H/F", None, config)
        assert score == 0.45  # Undetermined — no off-domain evidence configured here

    def test_relevant_job_outranks_irrelevant_via_domain_relevance(self):
        """A domain-matched job must score substantially higher than an undetermined one."""
        it_config = _domain_rel_config(
            domain_title_kws=["développeur", "cybersécurité", "ingénieur informatique"],
        )
        cybersec_score = _score_domain_relevance("Expert en Cybersécurité", None, it_config)
        learning_score = _score_domain_relevance("Alternance Learning & Development H/F", None, it_config)
        assert cybersec_score > learning_score + 0.5, (
            f"Cybersec job ({cybersec_score}) must beat L&D ({learning_score}) by ≥ 0.5"
        )


# ---------------------------------------------------------------------------
# Domain-relevance keyword-matching precision (2026-09-11 audit fix)
#
# _score_domain_relevance's title-keyword matching was a bare substring
# check, letting "developer" (a real, wanted keyword) bare-match inside
# "Business Developer" — a standard sales-role title, not a software role.
# A live audit found 167 such postings scoring domain_relevance=1.0 in a
# 7,194-job production sample. Fixed via _domain_keyword_matches: a narrow,
# evidence-driven exclusion for "developer"/"developpeur" preceded by a
# business/sales qualifier — NOT a blanket word-boundary rewrite (confirmed
# against the real corpus to break 20+ legitimate matches: French gender
# inflections, plurals, and portmanteau compounds like "NetDevOps"). "rust"
# separately got a plain word-boundary guard (confirmed safe) after it was
# found bare-matching inside "Trust"/"Trusted".
#
# Loads the REAL config/filters.yaml keyword vocabulary for the profile's
# actually-selected target_domains (same injection orchestrator.py performs
# for scoring.domain_relevance.domain_title_keywords), rather than a
# hand-picked list, so these tests exercise the real production vocabulary.
# ---------------------------------------------------------------------------

def _real_domain_config() -> dict[str, Any]:
    engine_cfg = load_filters("config/filters.yaml")
    profile = load_profile(TEST_PROFILE_PATH)
    title_kws: list[str] = []
    for domain in profile.target_domains:
        title_kws.extend(engine_cfg.get("domain_keywords", {}).get(domain, {}).get("title", []))
    return _domain_rel_config(domain_title_kws=list(dict.fromkeys(title_kws)))


class TestDomainRelevanceFalsePositives:
    """Generic keywords must not match when a preceding word changes the
    job's actual function — confirmed real-data false positives."""

    def test_business_developer_not_domain_matched(self):
        score = _score_domain_relevance("Business Developer", None, _real_domain_config())
        assert score < 0.7, f"'Business Developer' is a sales role, got domain_relevance={score}"

    def test_business_developer_with_agence_web_not_domain_matched(self):
        """The confirmed real audit example — 'Web' elsewhere in the title
        must not rescue it either."""
        score = _score_domain_relevance(
            "Stage Business Developer - Agence Web & Marketing Digital (H/F)",
            None, _real_domain_config(),
        )
        assert score < 0.7, f"got domain_relevance={score}"

    def test_business_developer_growth_marketing_saas_not_domain_matched(self):
        """The second confirmed real audit example."""
        score = _score_domain_relevance(
            "Stage – Business Developer & Growth Marketing B2B SaaS (H/F)",
            None, _real_domain_config(),
        )
        assert score < 0.7, f"got domain_relevance={score}"

    def test_growth_marketing_alone_not_domain_matched(self):
        score = _score_domain_relevance("Growth Marketing Manager", None, _real_domain_config())
        assert score < 0.7, f"got domain_relevance={score}"

    def test_business_development_not_domain_matched(self):
        """'Business Development' (noun, no '-er') never matched any
        keyword in the first place — regression guard, not a behavior change."""
        score = _score_domain_relevance("Business Development Manager", None, _real_domain_config())
        assert score < 0.7, f"got domain_relevance={score}"

    def test_sales_developer_not_domain_matched(self):
        score = _score_domain_relevance("Sales Developer", None, _real_domain_config())
        assert score < 0.7, f"got domain_relevance={score}"

    def test_rust_does_not_match_inside_trust(self):
        """'rust' (the language keyword) must not bare-match inside
        'Trust'/'Trusted' — a second confirmed real audit false positive.
        Isolated to a minimal config (just "rust") so an unrelated keyword
        elsewhere in the real vocabulary (e.g. "sre", "engineer") can't
        mask whether THIS specific fix is actually working."""
        config = _domain_rel_config(domain_title_kws=["rust"])
        score = _score_domain_relevance("Trusted Platform Specialist", None, config)
        assert score < 0.7, f"got domain_relevance={score}"

    def test_rust_does_not_match_inside_trust_and_safety(self):
        config = _domain_rel_config(domain_title_kws=["rust"])
        score = _score_domain_relevance("Trust & Safety Specialist", None, config)
        assert score < 0.7, f"got domain_relevance={score}"


class TestDomainRelevanceTruePositivesPreserved:
    """Legitimate technical titles named in the fix's scope must be
    completely unaffected — same score before and after the fix."""

    def test_web_developer(self):
        assert _score_domain_relevance("Web Developer", None, _real_domain_config()) == 1.0

    def test_developpeur_web(self):
        assert _score_domain_relevance("Développeur Web", None, _real_domain_config()) == 1.0

    def test_developpement_web_unaffected_by_fix(self):
        """'Développement Web' (noun form) has no matching keyword in the
        real vocabulary today (0.45, undetermined) — true both before and
        after this fix, since neither 'developer' nor any broad-tech
        indicator appears as a complete word in this exact phrase. This
        guards against the fix accidentally changing it either way; closing
        the underlying vocabulary gap is a separate, out-of-scope change."""
        score = _score_domain_relevance("Développement Web H/F", None, _real_domain_config())
        assert score == 0.45

    def test_developpeur_logiciel(self):
        assert _score_domain_relevance("Développeur logiciel", None, _real_domain_config()) == 1.0

    def test_cybersecurite(self):
        assert _score_domain_relevance("Expert Cybersécurité", None, _real_domain_config()) == 1.0

    def test_reseaux_role(self):
        assert _score_domain_relevance("Administrateur Réseaux", None, _real_domain_config()) == 1.0

    def test_systemes_role(self):
        assert _score_domain_relevance(
            "Administrateur Systèmes et Réseaux (H/F)", None, _real_domain_config()
        ) == 1.0

    def test_systems_administrator_plural_bare_title(self):
        """2026-09-12 Pass 4 fix: "systems" (English, referenced by
        target_domains) was missing the plural "systems administrator"
        keyword its "systèmes" (French alias) counterpart already had —
        this exact title scored the generic 0.35 "engineer/administrator"
        broad-tech tier instead of 1.0 before the fix."""
        assert _score_domain_relevance(
            "Systems Administrator", None, _real_domain_config()
        ) == 1.0

    def test_devops(self):
        assert _score_domain_relevance("DevOps Engineer", None, _real_domain_config()) == 1.0

    def test_infrastructure(self):
        assert _score_domain_relevance(
            "Infrastructure Engineer", None, _real_domain_config()
        ) == 1.0

    def test_automation(self):
        assert _score_domain_relevance(
            "Automation Engineer", None, _real_domain_config()
        ) == 1.0

    def test_automatisation_french(self):
        assert _score_domain_relevance(
            "Ingénieur Automatisation Industrielle", None, _real_domain_config()
        ) == 1.0

    def test_plural_developers_still_matches(self):
        """Regression guard: the fix must not require a strict trailing
        boundary — 'Developers' (plural) must still match, exactly as
        before this fix (verified against the real corpus)."""
        assert _score_domain_relevance(
            "Software Python Developers / Paris", None, _real_domain_config()
        ) == 1.0

    def test_netdevops_compound_still_matches(self):
        """Regression guard: portmanteau compounds with no separator
        ('NetDevOps') must still match 'devops' — confirmed real title."""
        assert _score_domain_relevance("Expert NetDevOps F/H", None, _real_domain_config()) == 1.0

    def test_secdevops_compound_still_matches(self):
        assert _score_domain_relevance(
            "Consultant SecDevOps F/H", None, _real_domain_config()
        ) == 1.0

    def test_rust_language_still_matches(self):
        """Regression guard: a genuine Rust-language role must still match."""
        assert _score_domain_relevance("Rust Backend Engineer", None, _real_domain_config()) == 1.0

    def test_developer_preceded_by_tech_qualifier_still_matches(self):
        """Only 'business'/'sales' exclude the match — any other preceding
        word (a real tech qualifier) must be unaffected."""
        for title in ["Full-Stack Developer", "Python Developer", "AWS Developer", "AI Developer"]:
            score = _score_domain_relevance(title, None, _real_domain_config())
            assert score == 1.0, f"{title!r} must still match, got {score}"


class TestAutomationDomainScoringAfterVocabularyMerge:
    """Regression coverage for the 2026-09-12 fix: config/filters.yaml's
    "automatisation" (French) domain_keywords bucket — which held the real
    cloud/devops/sre/platform vocabulary — was merged into "automation"
    (the key profile.yaml's target_domains actually uses). Before the fix,
    these titles scored domain_relevance=0.35 (the generic "engineer"
    broad-tech-indicator tier) because domain_kws.get("automation", {})
    resolved to a thin, unrelated bucket. Asserts the domain_relevance
    component specifically — not score_job()'s final weighted score,
    which isn't an established contract for these titles — since that's
    the signal this bug actually broke.
    """

    def test_cloud_architect_gets_strong_domain_relevance_not_generic_tier(self):
        score = _score_domain_relevance(
            "Cloud Architect AWS/Azure", None, _real_domain_config()
        )
        assert score == 1.0, (
            f"Expected strong (1.0) domain relevance for a direct vocabulary "
            f"match, got {score} — regressed back to the generic-engineer tier?"
        )

    def test_architecte_cloud_gets_strong_domain_relevance(self):
        assert _score_domain_relevance(
            "Architecte Cloud GCP", None, _real_domain_config()
        ) == 1.0

    def test_cloud_native_engineer_gets_strong_domain_relevance(self):
        assert _score_domain_relevance(
            "Cloud Native Engineer Kubernetes", None, _real_domain_config()
        ) == 1.0

    def test_cloudops_engineer_gets_strong_domain_relevance(self):
        assert _score_domain_relevance(
            "CloudOps Engineer Azure", None, _real_domain_config()
        ) == 1.0

    def test_devsecops_gets_strong_domain_relevance(self):
        assert _score_domain_relevance(
            "DevSecOps Engineer", None, _real_domain_config()
        ) == 1.0

    def test_platform_engineer_gets_strong_domain_relevance(self):
        assert _score_domain_relevance(
            "Platform Engineer", None, _real_domain_config()
        ) == 1.0

    def test_finops_gets_strong_domain_relevance(self):
        assert _score_domain_relevance(
            "FinOps Engineer", None, _real_domain_config()
        ) == 1.0


# ---------------------------------------------------------------------------
# Deduplication improvements — gender marker normalization (2026-08-28, session 4)
# ---------------------------------------------------------------------------

class TestTitleNormalization:
    """_norm_title_simple must strip gender markers and normalize accents/case."""

    def test_removes_euse_marker(self):
        assert _norm_title_simple("Développeur(euse) web junior") == "developpeur web junior"

    def test_removes_e_marker(self):
        assert _norm_title_simple("Alternant(e) en informatique") == "alternant en informatique"

    def test_removes_h_f_slash(self):
        assert _norm_title_simple("Technicien H/F") == "technicien"

    def test_removes_f_h_in_parens(self):
        assert _norm_title_simple("Développeur (H/F)") == "developpeur"

    def test_preserves_junior_senior(self):
        """Junior/Senior markers must NOT be stripped — they differentiate jobs."""
        assert "junior" in _norm_title_simple("Développeur Junior (H/F)")
        assert "senior" in _norm_title_simple("Développeur Senior H/F")

    def test_strips_accents(self):
        assert _norm_title_simple("Ingénieur réseau") == "ingenieur reseau"

    def test_lowercases(self):
        assert _norm_title_simple("TECHNICIEN EXPLOITANT INFORMATIQUE") == "technicien exploitant informatique"

    def test_none_returns_empty(self):
        assert _norm_title_simple(None) == ""


class TestCrossSourceDeduplicationGenderMarkers:
    """Tier 2 with gender-marker normalisation: same company+location+employment_type
    collapses even when titles differ only by H/F markers or accent variants."""

    def test_deduplicates_same_location_same_title(self):
        """Same job on two portals with identical location strings must collapse."""
        job_eures = _job(
            source="eures",
            company="TechCorp",
            title="Développeur full-stack en alternance",
            location="France",
            employment_type="alternance",
            countries=["France"],
        )
        job_ft = _job(
            source="france-travail",
            company="TechCorp",
            title="Développeur full-stack en alternance",
            location="France",
            employment_type="alternance",
            countries=["France"],
        )
        result = cross_source_deduplicate([job_eures, job_ft])
        assert len(result) == 1, (
            "Same job from two sources with identical location must be deduplicated; "
            f"got {len(result)} jobs"
        )

    def test_deduplicates_gender_marker_variant(self):
        """Gender marker variants of the same title must collapse when location matches."""
        job_with_marker = _job(
            source="eures",
            company="AcmeInc",
            title="Développeur(euse) web junior",
            location="France",
            employment_type="CDI",
            countries=["France"],
        )
        job_without_marker = _job(
            source="france-travail",
            company="AcmeInc",
            title="Développeur web junior",
            location="France",
            employment_type="CDI",
            countries=["France"],
        )
        result = cross_source_deduplicate([job_with_marker, job_without_marker])
        assert len(result) == 1

    def test_different_employment_type_not_collapsed(self):
        """Same company + title + location but different contract type → NOT duplicates."""
        job_cdi = _job(
            source="eures",
            company="AcmeInc",
            title="Développeur web",
            location="France",
            employment_type="CDI",
            countries=["France"],
        )
        job_alternance = _job(
            source="france-travail",
            company="AcmeInc",
            title="Développeur web",
            location="France",
            employment_type="alternance",
            countries=["France"],
        )
        result = cross_source_deduplicate([job_cdi, job_alternance])
        assert len(result) == 2, (
            "Jobs with different employment types at same company must NOT collapse"
        )

    def test_no_company_not_collapsed(self):
        """Jobs without a company name must not be collapsed on title alone."""
        job1 = _job(
            source="eures",
            company=None,
            title="Développeur web junior",
            location="France",
            employment_type="CDI",
        )
        job2 = _job(
            source="france-travail",
            company=None,
            title="Développeur web junior",
            location="France",
            employment_type="CDI",
        )
        result = cross_source_deduplicate([job1, job2])
        assert len(result) == 2, "Jobs without company must not collapse (Tier 2 requires company)"


# ---------------------------------------------------------------------------
# Excluded title keywords — ouvrier added (2026-08-28, session 4)
# ---------------------------------------------------------------------------

class TestExcludedTitleKeywordsOuvrier:
    """'ouvrier' and related manual-labor terms must be in the exclusion list."""

    def _load_excluded(self) -> list[str]:
        from pathlib import Path

        import yaml
        root = Path(__file__).parent.parent
        with open(root / "config" / "filters.yaml") as f:
            cfg = yaml.safe_load(f)
        # excluded_title_keywords is grouped by opt-out domain (see filters.yaml) —
        # flatten every group to check membership regardless of grouping.
        groups = cfg.get("excluded_title_keywords", {})
        keywords = groups if isinstance(groups, list) else [kw for group in groups.values() for kw in group]
        return [k.lower() for k in keywords]

    def test_ouvrier_in_exclusion_list(self):
        excluded = self._load_excluded()
        assert "ouvrier" in excluded, (
            "filters.yaml must exclude 'ouvrier' (manual labor, never IT)"
        )

    def test_ouvrier_job_rejected_by_filter(self):
        from src.pipeline.filter import matches_title_keywords
        job = _job(
            title="Ouvrier Polyvalent",
            description="Poste en usine. Manutention et assemblage.",
        )
        config = {"title_keywords": {"include": ["développeur"], "exclude": ["ouvrier"]}}
        assert not matches_title_keywords(job, config), (
            "Ouvrier Polyvalent must be rejected by title keyword filter"
        )


# ---------------------------------------------------------------------------
# End-to-end ranking separation — relevant vs irrelevant jobs (2026-08-28, session 4)
# ---------------------------------------------------------------------------

def _full_relevance_config(
    domain_kws: list[str] | None = None,
    target_skills: list[str] | None = None,
) -> dict[str, Any]:
    """Scoring config that mirrors the priority-ordered production weights
    (location > domain > skills > seniority > experience)."""
    return {
        "scoring": {
            "location_fit": {
                "weight": 0.30,
                "target_department": "83",
                "preferred_countries": ["france"],
            },
            "domain_relevance": {
                "weight": 0.25,
                "domain_title_keywords": domain_kws or ["développeur", "cybersécurité",
                                                         "ingénieur informatique",
                                                         "ingénieur réseau"],
                "off_domain_keywords": [],
            },
            "seniority_match": {
                "weight": 0.15,
                "junior_bonus": 1.2,
                "preferred_level_keywords": ["junior", "alternance", "internship"],
            },
            "experience_fit": {"weight": 0.10},
            "skills_match": {
                "weight": 0.20,
                "target_skills": target_skills or ["python", "javascript", "php"],
            },
        }
    }


class TestRelevanceFirstRanking:
    """Domain/skills relevance must still meaningfully separate relevant from
    irrelevant jobs even though location now carries the top weight — an
    irrelevant-but-convenient job (French, remote, junior-worded) must not
    outrank a genuinely relevant one."""

    def _score(self, job: Job) -> float:
        return score_job(job, _full_relevance_config())

    def test_domain_matched_job_beats_wrapper_only_job(self):
        """A cybersecurity expert must rank above an L&D alternance job."""
        now = datetime.now(UTC)
        cybersec = _job(
            title="Expert en Cybersécurité",
            description="Sécurité des systèmes d'information, pentest, Python.",
            location="Paris, France", countries=["France"], remote=True,
            discovered_at=now - timedelta(days=14),
        )
        learning_dev = _job(
            title="Alternance Learning & Development H/F",
            description="Informatique entreprise alternance technologies.",
            location="Paris, France", countries=["France"], remote=False,
            discovered_at=now,
        )
        cybersec_score = self._score(cybersec)
        ld_score = self._score(learning_dev)
        assert cybersec_score > ld_score, (
            f"Cybersec expert ({cybersec_score:.3f}) must beat L&D alternance "
            f"({ld_score:.3f}) even though L&D is on-site French with a junior/"
            f"alternance seniority match"
        )

    def test_developer_job_beats_management_research_job(self):
        """A developer role must rank above a management/research stage."""
        now = datetime.now(UTC)
        dev = _job(
            title="Développeur PHP Junior",
            description="Développement web avec PHP et MySQL.",
            location="Lyon, France", countries=["France"], remote=False,
            discovered_at=now - timedelta(days=7),
        )
        mgmt = _job(
            title="Stage fin d'études - Management directions Recherche & Innovation",
            description="Informatique innovation alternance entreprise.",
            location="Paris, France", countries=["France"], remote=False,
            discovered_at=now,
        )
        dev_score = self._score(dev)
        mgmt_score = self._score(mgmt)
        assert dev_score > mgmt_score, (
            f"PHP developer ({dev_score:.3f}) must beat management research stage "
            f"({mgmt_score:.3f})"
        )

    def test_irrelevant_job_cannot_reach_top_score(self):
        """Even a perfectly convenient irrelevant job (French, fresh, junior) must
        score below 0.6 with relevance-first weights."""
        now = datetime.now(UTC)
        irrelevant = _job(
            title="Responsable Partenariats Entreprises et Placement Alternance",
            description="Informatique entreprise alternance.",  # minimal IT wrapper
            location="Paris, France", countries=["France"], remote=True,
            discovered_at=now,
        )
        score = self._score(irrelevant)
        assert score < 0.60, (
            f"Irrelevant French/fresh/remote alternance job scored {score:.3f} ≥ 0.60; "
            "convenience factors must not push unrelated jobs to the top"
        )

    def test_relevant_job_can_reach_high_score(self):
        """A domain-matched Python job with skills in description should score ≥ 0.65."""
        now = datetime.now(UTC)
        relevant = _job(
            title="Développeur Python Junior",
            description="Python, JavaScript, PHP et MySQL requis. Poste junior en alternance.",
            location="Paris, France", countries=["France"], remote=True,
            discovered_at=now,
        )
        score = self._score(relevant)
        assert score >= 0.65, (
            f"Domain-matched Python job scored {score:.3f} < 0.65; "
            "domain_relevance + skills_match should push relevant jobs to top"
        )

    def test_tech_role_without_domain_keyword_beats_irrelevant(self):
        """Data Scientist (tech indicator but not in user domains) must beat HR job."""
        now = datetime.now(UTC)
        data_sci = _job(
            title="Alternance Data Scientist / IA Engineer",
            description="Machine learning, Python, modèles IA.",
            location="Paris, France", countries=["France"], remote=False,
            discovered_at=now,
        )
        hr_job = _job(
            title="Alternance Assistant(e) Communication Web",
            description="Informatique entreprise alternance technologies.",
            location="Paris, France", countries=["France"], remote=False,
            discovered_at=now,
        )
        ds_score = self._score(data_sci)
        hr_score = self._score(hr_job)
        assert ds_score > hr_score, (
            f"Data Scientist ({ds_score:.3f}) must beat HR/Communication job ({hr_score:.3f})"
        )


# ---------------------------------------------------------------------------
# Seniority hard filter — word-boundary matching + new lead/manager/head terms
# (2026-08-28, session 5)
# ---------------------------------------------------------------------------

def _make_seniority_config(groups: list[str]) -> dict[str, Any]:
    """Build a filter config with only the requested seniority exclusion groups."""
    from pathlib import Path

    import yaml
    root = Path(__file__).parent.parent
    with open(root / "config" / "filters.yaml", encoding="utf-8") as f:
        engine_cfg = yaml.safe_load(f)
    with open(TEST_PROFILE_PATH, encoding="utf-8") as f:
        profile_data = yaml.safe_load(f)
    # Override profile to use only the specified exclusion groups
    profile_data.setdefault("experience", {})["excluded_seniority"] = groups
    from src.models.profile import JobSearchProfile
    profile = JobSearchProfile(**profile_data)
    return build_filter_config(profile, engine_cfg)


class TestSeniorityWordBoundary:
    """matches_seniority() must use word-boundary matching so standalone keywords
    like 'lead' do not false-positive on 'leads' or 'management'."""

    def _excluded(self, title: str, groups: list[str]) -> bool:
        from src.pipeline.filter import matches_seniority
        cfg = _make_seniority_config(groups)
        return not matches_seniority(_job(title=title), cfg)

    # --- lead ---
    def test_lead_recruiter_excluded_by_lead(self):
        """Standalone 'Lead' at start of title must trigger lead exclusion."""
        assert self._excluded("Lead Recruiter (Commercial & Corporate Hiring)", ["lead"]), (
            "'Lead Recruiter' must be excluded — 'lead' (bare) is now in lead group"
        )

    def test_lead_developer_still_excluded(self):
        assert self._excluded("Lead Developer Backend", ["lead"])

    def test_team_leader_excluded(self):
        """'Team Leader' must be excluded by the 'leader' keyword."""
        assert self._excluded("Team Leader DevOps", ["lead"])

    def test_leads_not_excluded(self):
        """'Sales Leads' (plural noun) must NOT trigger lead exclusion."""
        assert not self._excluded("Sales Leads Specialist", ["lead"]), (
            "'leads' (plural noun) must not be matched by word-boundary 'lead'"
        )

    def test_leadership_not_excluded(self):
        """'Leadership' in title must NOT trigger lead exclusion."""
        assert not self._excluded("Leadership Development Program", ["lead"]), (
            "'leadership' must not be matched by word-boundary 'lead'"
        )

    # --- manager ---
    def test_it_manager_excluded(self):
        assert self._excluded("IT Manager", ["manager"])

    def test_project_manager_excluded(self):
        assert self._excluded("Project Manager", ["manager"])

    def test_community_manager_excluded(self):
        assert self._excluded("Community Manager", ["manager"])

    def test_responsable_excluded(self):
        assert self._excluded("Responsable Informatique", ["manager"])

    def test_chef_de_projet_excluded(self):
        assert self._excluded("Chef de Projet Informatique", ["manager"])

    def test_management_not_excluded(self):
        """'Management' (noun, not the job function 'manager') must NOT trigger exclusion."""
        assert not self._excluded("Project Management Specialist", ["manager"]), (
            "'management' must not be matched by word-boundary 'manager'"
        )

    # --- head ---
    def test_head_of_engineering_excluded(self):
        assert self._excluded("Head of Engineering", ["director"])

    def test_department_head_excluded(self):
        assert self._excluded("IT Department Head", ["director"])

    def test_overhead_not_excluded(self):
        """'overhead' must not trigger head exclusion."""
        assert not self._excluded("Overhead Cost Analyst", ["director"]), (
            "'overhead' must not be matched by word-boundary 'head'"
        )

    # --- senior already worked; guard against regression ---
    def test_senior_recruiter_excluded_by_senior(self):
        assert self._excluded("Senior Recruiter", ["senior"]), (
            "'Senior Recruiter' must be excluded by the senior group"
        )

    def test_junior_developer_not_excluded(self):
        assert not self._excluded("Junior Python Developer", ["lead", "senior", "manager", "director"]), (
            "A standard junior IT job must not be excluded by any seniority group"
        )


# ---------------------------------------------------------------------------
# Non-IT title functions gate — description fallback blocked for non-IT roles
# (2026-08-28, session 5)
# ---------------------------------------------------------------------------

def _make_domain_config(title_include: list[str] | None = None) -> dict[str, Any]:
    """Minimal filter config with IT title keywords and non_it_functions loaded from YAML."""
    from pathlib import Path

    import yaml
    root = Path(__file__).parent.parent
    with open(root / "config" / "filters.yaml", encoding="utf-8") as f:
        engine_cfg = yaml.safe_load(f)
    with open(TEST_PROFILE_PATH, encoding="utf-8") as f:
        profile_data = yaml.safe_load(f)
    from src.models.profile import JobSearchProfile
    profile = JobSearchProfile(**profile_data)
    cfg = build_filter_config(profile, engine_cfg)
    if title_include is not None:
        cfg["title_keywords"]["include"] = title_include
    return cfg


class TestNonItTitleFunctionsGate:
    """Jobs whose title contains a non-IT function word must not pass via
    description fallback, even when the description is full of IT keywords."""

    # A representative IT vocabulary for the "_accepted" cases below, passed
    # via _make_domain_config's title_include override so this class tests
    # the non_it_functions GATE mechanism itself rather than whatever domains
    # happen to be selected in the live config/profile.yaml (target_domains
    # there can be — and has been — narrowed independently of this test).
    _IT_TITLE_INCLUDE = ["it support", "python", "développeur", "developpeur"]

    def _passes(self, title: str, description: str = "", title_include: list[str] | None = None) -> bool:
        cfg = _make_domain_config(title_include=title_include)
        job = _job(title=title, description=description)
        return matches_title_keywords(job, cfg)

    # --- Lead Recruiter (the reported failing case) ---
    def test_lead_recruiter_rejected(self):
        """Lead Recruiter with IT-heavy description must be rejected."""
        assert not self._passes(
            title="Lead Recruiter (Commercial & Corporate Hiring)",
            description=(
                "Recruiting developers: Python, JavaScript, Docker, Kubernetes, Linux."
                " Experience with applicant tracking systems and technical screening."
            ),
        ), "'Lead Recruiter' must be rejected regardless of IT content in description"

    def test_senior_recruiter_rejected(self):
        """Senior Recruiter must be rejected (seniority + non-IT function)."""
        assert not self._passes(
            title="Senior Recruiter",
            description="Hiring software engineers. Python, Java, DevOps, cloud experience.",
        ), "'Senior Recruiter' must be rejected"

    # --- Legitimate IT jobs must still pass ---
    def test_it_support_technician_accepted(self):
        assert self._passes(title="IT Support Technician", title_include=self._IT_TITLE_INCLUDE), (
            "'IT Support Technician' must be accepted"
        )

    def test_junior_python_developer_accepted(self):
        assert self._passes(
            title="Junior Python Developer",
            description="Python, Django, REST API, Git.",
            title_include=self._IT_TITLE_INCLUDE,
        ), "'Junior Python Developer' must be accepted"

    # --- Non-IT job with many IT keywords in description ---
    def test_non_it_job_with_it_description_rejected(self):
        """A recruiter job whose description is full of IT keywords must still be rejected."""
        assert not self._passes(
            title="Recruteur Technique",
            description=(
                "Recherche de développeurs Python, JavaScript, DevOps, Docker, "
                "Kubernetes, AWS, Linux, SQL, Git, React, Node.js pour nos clients."
            ),
        ), "Recruiter job must not pass IT filter via description regardless of IT keywords"

    # --- IT keyword in title, non-IT word only in description → accepted ---
    def test_title_with_it_keyword_wins_over_non_it_function(self):
        """An IT title is accepted even when the description contains non-IT words.

        The non_it_functions gate is applied to the TITLE only.  "Développeur Python"
        has no non-IT function word in its title, so it passes steps 3–4 and is
        accepted by the title keyword match on "développeur".
        """
        assert self._passes(
            title="Développeur Python",
            description="Recrutement en cours.",
            title_include=self._IT_TITLE_INCLUDE,
        ), "IT keyword in title must accept the job when no non-IT function is in the title"
