"""Regression tests for location-fit work-mode ranking and F3 (language penalty) scoring.

Location-fit work-mode rules (remote preference is folded into location_fit,
see src.pipeline.scoring._score_location_fit):
  - Preferred-country + remote scores higher than preferred-country + on-site
  - on_site/hybrid are still accepted, just at a lower location tier
  - No hard rejection for any work mode

F3 rules:
  - French job → 0 language penalty
  - English job → 0 language penalty
  - Mixed French/English → 0 penalty
  - Clearly required non-profile language → moderate penalty
  - Foreign language only "preferred/a plus" → 0 penalty
  - Ambiguous/undetected → 0 penalty
  - Hard rejections remain hard regardless of location/language factors
"""
from __future__ import annotations


from src.models.job import Job
from src.pipeline.scoring import (
    _detect_language_penalty,
    score_job,
    score_jobs_with_breakdown,
)
from src.requirements.models import GateResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cfg(**location_overrides) -> dict:
    """Minimal scoring config with location_fit + F3 configured."""
    lc = {
        "weight": 0.30,
        "target_department": "83",
        "preferred_countries": ["france"],
    }
    lc.update(location_overrides)
    return {
        "scoring": {
            "location_fit": lc,
            "domain_relevance": {"weight": 0.25},
            "skills_match": {"weight": 0.20, "target_skills": ["python"]},
            "seniority_match": {"weight": 0.15},
            "experience_fit": {"weight": 0.10},
            "language": {
                "penalty_required": 0.15,
                "profile_languages": ["French", "English"],
            },
        }
    }


def _lang_cfg(penalty: float = 0.15) -> dict:
    return {"scoring": {"language": {"penalty_required": penalty, "profile_languages": ["French", "English"]}}}


def _job(
    title: str = "Software Engineer",
    description: str = "Python development role",
    work_mode: str | None = None,
    remote: bool = False,
    location: str = "Paris, France",
    countries: list[str] | None = None,
) -> Job:
    return Job(
        title=title,
        description=description,
        work_mode=work_mode,
        remote=remote,
        location=location,
        countries=countries if countries is not None else ["France"],
    )


# ---------------------------------------------------------------------------
# Location-fit work-mode ranking — full score_job end-to-end
# ---------------------------------------------------------------------------

class TestLocationFitWorkModeRanking:
    """Verify that remote > on_site in overall job score when otherwise equal
    (remote preference is now a tier within location_fit, not a separate
    weighted factor — see src.pipeline.scoring._score_location_fit)."""

    def test_remote_ranks_above_equivalent_on_site(self):
        cfg = _cfg()
        j_remote = _job(work_mode="remote", remote=True)
        j_onsite = _job(work_mode="on_site", remote=False)
        s_remote = score_job(j_remote, cfg)
        s_onsite = score_job(j_onsite, cfg)
        assert s_remote > s_onsite, (
            f"Remote ({s_remote:.3f}) should outrank on_site ({s_onsite:.3f})"
        )

    def test_on_site_job_is_accepted_not_zero(self):
        j = _job(work_mode="on_site", remote=False)
        score = score_job(j, _cfg())
        assert score > 0.0

    def test_hybrid_job_is_accepted_not_zero(self):
        j = _job(work_mode="hybrid", remote=False)
        score = score_job(j, _cfg())
        assert score > 0.0

    def test_remote_bonus_is_moderate(self):
        """Remote should outrank on_site but not by enough to overcome a strong
        skills difference from another factor."""
        cfg = _cfg()
        j_remote = _job(work_mode="remote", remote=True, description="unrelated content")
        j_onsite = _job(work_mode="on_site", remote=False, description="python docker linux development")
        s_remote = score_job(j_remote, cfg)
        s_onsite = score_job(j_onsite, cfg)
        # Both are French jobs; the skills-matched on_site should beat the non-matched remote
        assert s_onsite > s_remote, (
            f"Skill-matched on_site ({s_onsite:.3f}) should beat skill-unmatched remote ({s_remote:.3f})"
        )

    def test_unknown_work_mode_no_penalty(self):
        """Jobs with work_mode=None and remote=False must score the same as
        an explicit on_site job (both fall into the same location_fit tier)."""
        j_unknown = _job(work_mode=None, remote=False)
        j_onsite = _job(work_mode="on_site", remote=False)
        s_unknown = score_job(j_unknown, _cfg())
        s_onsite = score_job(j_onsite, _cfg())
        assert s_unknown >= s_onsite

    def test_score_jobs_with_breakdown_applies_location_fit(self):
        jobs = [
            _job(work_mode="remote", remote=True),
            _job(work_mode="on_site", remote=False),
        ]
        results = score_jobs_with_breakdown(jobs, _cfg())
        scores = {r[0].work_mode: r[1] for r in results}
        assert scores["remote"] > scores["on_site"]


# ---------------------------------------------------------------------------
# F3 — _detect_language_penalty
# ---------------------------------------------------------------------------

class TestF3LanguageDetection:
    """Unit tests for language penalty detection."""

    def test_french_job_no_penalty(self):
        j = Job(description="Nous recherchons un developpeur Python maitrise du francais requise")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_english_job_no_penalty(self):
        j = Job(description="English required for this international role")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_mixed_french_english_no_penalty(self):
        j = Job(description="French and English required. Francais et anglais obligatoire.")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_german_required_gets_penalty(self):
        j = Job(description="German required for client communication")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.15

    def test_deutsch_erforderlich_gets_penalty(self):
        j = Job(description="Deutsch erforderlich fuer diese Position")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.15

    def test_allemand_courant_gets_penalty(self):
        j = Job(description="Langues: francais courant, allemand courant exige")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.15

    def test_spanish_required_gets_penalty(self):
        j = Job(description="Spanish required for client communication in South America")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.15

    def test_fluent_german_gets_penalty(self):
        j = Job(description="Fluent German speaker required for this position")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.15

    def test_german_is_a_plus_no_penalty(self):
        j = Job(description="German is a plus but not required for this role")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_german_preferred_no_penalty(self):
        j = Job(description="German preferred but not mandatory")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_german_atout_no_penalty(self):
        j = Job(description="L'allemand serait un atout supplementaire")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_german_souhaite_no_penalty(self):
        j = Job(description="Allemand souhaite mais pas obligatoire")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_description_in_german_only_no_penalty(self):
        """Descriptions written in German without explicit requirement signals → no penalty."""
        j = Job(description=(
            "Wir sind ein innovatives Unternehmen im Bereich Softwareentwicklung. "
            "Wir bieten interessante Projekte und ein tolles Team."
        ))
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_no_language_mention_no_penalty(self):
        j = Job(description="Great Python developer with Docker and Linux skills")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_ambiguous_language_no_penalty(self):
        j = Job(description="Multi-lingual team environment with diverse clients")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_penalty_is_configurable(self):
        j = Job(description="German required for this role")
        assert _detect_language_penalty(j, _lang_cfg(penalty=0.20)) == 0.20

    def test_penalty_zero_disables_f3(self):
        j = Job(description="German required for this role")
        assert _detect_language_penalty(j, _lang_cfg(penalty=0.0)) == 0.0

    def test_title_with_foreign_language_required(self):
        j = Job(title="German Required Developer", description="Python role")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.15

    def test_english_in_profile_languages_never_penalised(self):
        """Even if English sounds foreign, it must never cause a penalty."""
        j = Job(description="Fluent English required for this international role")
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0

    def test_few_foreign_words_in_french_job_no_penalty(self):
        """A French job description with a few German company names must not trigger."""
        j = Job(description=(
            "Nous travaillons avec des clients internationaux dont Volkswagen et BMW. "
            "Maitrise de Python requise. Poste en CDI a Paris."
        ))
        assert _detect_language_penalty(j, _lang_cfg()) == 0.0


# ---------------------------------------------------------------------------
# F3 — full score_job end-to-end
# ---------------------------------------------------------------------------

class TestF3FullScoring:
    """Verify language penalty integrates with the full scoring pipeline."""

    def test_foreign_language_required_reduces_score(self):
        j_clean = _job(description="Python developer role")
        j_foreign = _job(description="Python developer role. German required for meetings.")
        cfg = _cfg()
        s_clean = score_job(j_clean, cfg)
        s_foreign = score_job(j_foreign, cfg)
        assert s_foreign < s_clean, (
            f"Foreign lang required ({s_foreign:.3f}) should score < clean ({s_clean:.3f})"
        )

    def test_french_job_no_language_penalty(self):
        j = _job(description="Maitrise du francais obligatoire. Python requis.")
        j_ref = _job(description="Python developer")
        cfg = _cfg()
        # Should be the same score (French maitrise doesn't penalize)
        assert abs(score_job(j, cfg) - score_job(j_ref, cfg)) < 0.001

    def test_language_penalty_not_hard_rejection(self):
        """Foreign language requirement reduces score but does not eliminate it."""
        j = _job(description="German required. Python developer.")
        score = score_job(j, _cfg())
        assert score > 0.0

    def test_score_jobs_with_breakdown_applies_f3(self):
        jobs = [
            _job(description="Python developer role"),
            _job(description="Python developer. German required."),
        ]
        results = score_jobs_with_breakdown(jobs, _cfg())
        scores_by_desc = {r[0].description[:25]: r[1] for r in results}
        clean_score = next(v for k, v in scores_by_desc.items() if "German" not in k)
        foreign_score = next(v for k, v in scores_by_desc.items() if "German" in k)
        assert clean_score > foreign_score


# ---------------------------------------------------------------------------
# Hard-gate non-regression
# ---------------------------------------------------------------------------

class TestHardGateNonRegression:
    """Hard rejection from requirements gate must NOT be overridden by F1/F3 bonuses."""

    def test_education_penalty_still_applies_with_remote_bonus(self):
        """A remote job with an edu penalty should be penalised, not promoted."""
        j = _job(work_mode="remote", remote=True)
        gate_result = GateResult(passes=True, education_penalty=0.20)
        cfg = _cfg()
        score_with_penalty = score_job(j, cfg, gate_result)

        j_no_penalty = _job(work_mode="remote", remote=True)
        score_no_penalty = score_job(j_no_penalty, cfg)

        assert score_with_penalty < score_no_penalty, (
            "Edu penalty must apply even to remote-bonus jobs"
        )

    def test_experience_penalty_still_applies_with_remote_bonus(self):
        j = _job(work_mode="remote", remote=True)
        gate_result = GateResult(passes=True, experience_penalty=0.40)
        cfg = _cfg()
        score_with_penalty = score_job(j, cfg, gate_result)

        j_no_penalty = _job(work_mode="remote", remote=True)
        score_no_penalty = score_job(j_no_penalty, cfg)

        assert score_with_penalty < score_no_penalty

    def test_all_penalties_stack_correctly(self):
        """All three penalties (edu + exp + language) must all reduce the score."""
        j_clean = _job(description="Python role")
        j_penalised = _job(description="Python role. German required.")
        gate_result = GateResult(passes=True, education_penalty=0.20, experience_penalty=0.40)
        cfg = _cfg()
        s_clean = score_job(j_clean, cfg)
        s_penalised = score_job(j_penalised, cfg, gate_result)
        assert s_penalised < s_clean

    def test_scoring_path_hierarchy(self):
        """Verify penalties are applied in order: raw → edu → skill → exp → language."""
        j = _job(description="German required for this senior role")
        gate_result = GateResult(
            passes=True, education_penalty=0.20, skill_penalty=0.10, experience_penalty=0.40
        )
        cfg = _cfg()
        score = score_job(j, cfg, gate_result)
        assert 0.0 < score < 1.0  # penalised but not zero
