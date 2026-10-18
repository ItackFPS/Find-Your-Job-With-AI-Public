"""Regression tests for the complete Profile → Pipeline field audit.

Covers every profile field's effect (or deliberate non-effect) on:
  - Hard filtering (filter.py)
  - Deterministic scoring (scoring.py)
  - Phase 2 prompt (ai/prompt.py)

All tests use deterministic, in-memory data — no network, no database,
no LLM required.
"""

from __future__ import annotations

from datetime import UTC, datetime


from src.models.job import Job
from src.models.profile import ExperiencePreferences, GeographyPreferences, JobSearchProfile
from src.pipeline.filter import (
    _extract_experience_years_min,
    build_filter_config,
    filter_jobs,
    matches_experience_years,
    matches_work_mode,
)
from src.pipeline.scoring import _score_location_fit, score_job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job(**kwargs) -> Job:
    """Create a minimal valid Job with sensible defaults."""
    defaults = dict(
        source="test",
        title="Software Developer",
        description="A Python backend role.",
        location="Paris, France",
        countries=["France"],
        remote=False,
        discovered_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Job(**defaults)


def _profile(**kwargs) -> JobSearchProfile:
    """Create a minimal profile."""
    defaults = dict(
        target_domains=["software_development"],
        target_skills=["python"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        work_modes=["remote", "hybrid", "on_site"],
        contract_types=[],
        experience=ExperiencePreferences(
            preferred_levels=["junior"],
            max_years=None,
            excluded_seniority=[],
        ),
        languages=["French", "English"],
        freshness_days=30,
    )
    defaults.update(kwargs)
    return JobSearchProfile(**defaults)


def _minimal_engine_cfg() -> dict:
    """Minimal engine configuration for build_filter_config."""
    return {
        "domain_keywords": {
            "software_development": {
                "title": ["software developer", "python developer"],
                "description": ["python", "developer"],
            }
        },
        "contract_keywords": {},
        "seniority_exclusions": {
            "senior": ["senior"],
            "director": ["director"],
        },
        "excluded_title_keywords": [],
        "remote_scope": {
            "european_keywords": ["Europe", "EU"],
            "worldwide_keywords": ["worldwide", "global"],
        },
    }


# ===========================================================================
# SECTION 1 — Work mode filtering
# ===========================================================================

class TestWorkModeFilter:
    """Verify matches_work_mode semantics."""

    def _config(self, modes: list[str]) -> dict:
        return {"work_modes": {"allowed": modes}}

    # Positive cases — job should pass ----------------------------------------

    def test_remote_job_passes_remote_only_profile(self):
        job = _job(remote=True, countries=["Germany"])
        assert matches_work_mode(job, self._config(["remote"]))

    def test_remote_job_passes_mixed_profile(self):
        job = _job(remote=True, countries=["France"])
        assert matches_work_mode(job, self._config(["remote", "hybrid"]))

    def test_onsite_job_passes_when_onsite_allowed(self):
        job = _job(remote=False, countries=["France"])
        assert matches_work_mode(job, self._config(["remote", "hybrid", "on_site"]))

    def test_onsite_job_passes_when_hybrid_allowed(self):
        """When hybrid is in allowed, we cannot safely reject — accept all."""
        job = _job(remote=False, countries=["Germany"])
        assert matches_work_mode(job, self._config(["remote", "hybrid"]))

    def test_unknown_workmode_job_passes_remote_only(self):
        """remote=False + no countries → unknown mode → benefit of the doubt."""
        job = _job(remote=False, countries=[], location=None)
        assert matches_work_mode(job, self._config(["remote"]))

    def test_no_modes_configured_accepts_all(self):
        job = _job(remote=False, countries=["Germany"])
        assert matches_work_mode(job, self._config([]))

    def test_no_workmode_key_accepts_all(self):
        job = _job(remote=False, countries=["Germany"])
        assert matches_work_mode(job, {})

    # Rejection cases — job should fail ----------------------------------------

    def test_onsite_known_country_rejected_when_only_remote(self):
        """remote=False with known country → rejected when user wants only remote."""
        job = _job(remote=False, countries=["Germany"])
        assert not matches_work_mode(job, self._config(["remote"]))

    def test_onsite_france_rejected_when_only_remote(self):
        """Even a France on-site job fails when user wants only remote."""
        job = _job(remote=False, countries=["France"])
        assert not matches_work_mode(job, self._config(["remote"]))

    # Case-insensitive normalisation -------------------------------------------

    def test_mode_names_normalised(self):
        """'on-site' and 'on_site' are equivalent."""
        job = _job(remote=False, countries=["France"])
        # With on-site in the allowed set, should pass.
        assert matches_work_mode(job, self._config(["remote", "on-site"]))

    # Integration with build_filter_config + filter_jobs -----------------------

    def test_build_filter_config_passes_work_modes(self):
        profile = _profile(work_modes=["remote"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert cfg["work_modes"]["allowed"] == ["remote"]

    def test_filter_jobs_rejects_onsite_when_remote_only(self):
        profile = _profile(
            work_modes=["remote"],
            experience=ExperiencePreferences(excluded_seniority=[]),
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        # On-site Germany job → rejected by work_mode filter
        on_site = _job(remote=False, countries=["Germany"], location="Berlin, Germany")
        # Remote France job → passes both location and work_mode filters
        remote_job = _job(remote=True, countries=["France"], location="Paris (Remote)")
        valid, reasons = filter_jobs([on_site, remote_job], cfg)
        assert on_site.id in reasons
        assert any(j.id == remote_job.id for j in valid)

    def test_filter_jobs_unknown_country_passes_remote_only(self):
        """No country data → unknown work mode → passes even in remote-only profile."""
        profile = _profile(work_modes=["remote"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        unknown_job = _job(remote=False, countries=[], location=None)
        valid, _ = filter_jobs([unknown_job], cfg)
        assert any(j.id == unknown_job.id for j in valid)


# ===========================================================================
# SECTION 2 — Experience years filtering
# ===========================================================================

class TestExperienceExtraction:
    """Verify _extract_experience_years_min regex patterns."""

    def test_english_plus_notation(self):
        assert _extract_experience_years_min("5+ years experience") == 5

    def test_english_years_of_experience(self):
        assert _extract_experience_years_min("3 years of experience required") == 3

    def test_english_minimum(self):
        assert _extract_experience_years_min("minimum 4 years of experience") == 4

    def test_english_at_least(self):
        assert _extract_experience_years_min("at least 2 years experience") == 2

    def test_english_range_lower_bound(self):
        assert _extract_experience_years_min("3-5 years of experience") == 3

    def test_french_ans_experience(self):
        assert _extract_experience_years_min("5 ans d'expérience requis") == 5

    def test_french_plus_notation(self):
        assert _extract_experience_years_min("3+ ans d'expérience") == 3

    def test_french_range(self):
        assert _extract_experience_years_min("2 à 4 ans d'expérience") == 2

    def test_french_minimum(self):
        assert _extract_experience_years_min("minimum 3 ans d'expérience") == 3

    def test_no_experience_requirement(self):
        assert _extract_experience_years_min("Join our team as a Python developer") is None

    def test_unrelated_number(self):
        assert _extract_experience_years_min("We were founded 10 years ago") is None

    def test_zero_not_returned(self):
        # "0 years" is nonsensical and should not be matched by sanity guard.
        result = _extract_experience_years_min("0 years of experience required")
        assert result is None or result > 0

    # P1 regression — French patterns without "d'expérience" suffix
    def test_french_au_moins_no_suffix(self):
        assert _extract_experience_years_min("au moins 4 ans sur un poste DevOps") == 4

    def test_french_au_minimum_no_suffix(self):
        assert _extract_experience_years_min("au minimum 5 ans en tant que développeur") == 5

    def test_french_minimum_no_suffix(self):
        assert _extract_experience_years_min("minimum 5 ans requis") == 5

    def test_french_trailing_minimum(self):
        assert _extract_experience_years_min("3 ans minimum requis pour ce poste") == 3

    def test_french_plus_no_suffix(self):
        assert _extract_experience_years_min("4+ ans d'expérience ou plus") == 4

    def test_french_ou_plus(self):
        assert _extract_experience_years_min("6 ans ou plus d'expérience") == 6

    def test_french_markdown_bold(self):
        assert _extract_experience_years_min("d'au moins 4\n  **ans**\n  sur un poste DevOps") == 4

    def test_french_with_context_randstad(self):
        assert _extract_experience_years_min(
            "vous justifiez d'une expérience significative d'au moins 4 ans sur un poste DevOps"
        ) == 4

    def test_french_garantie_no_match(self):
        assert _extract_experience_years_min("garantie de 3 ans sur les pièces") is None

    def test_french_emploi_depuis_no_match(self):
        assert _extract_experience_years_min("emploi depuis 2 ans dans notre équipe") is None


class TestExperienceYearsFilter:
    """Verify matches_experience_years filter function."""

    def _config(self, max_years: int | None) -> dict:
        return {"experience_years": {"max_years": max_years}}

    def test_no_max_configured_always_passes(self):
        job = _job(description="Requires 10+ years experience")
        assert matches_experience_years(job, self._config(None))

    def test_unknown_requirement_passes(self):
        job = _job(description="Come work with us, Python skills needed")
        assert matches_experience_years(job, self._config(3))

    def test_within_limit_passes(self):
        job = _job(description="2 years of experience preferred")
        assert matches_experience_years(job, self._config(3))

    def test_exactly_at_limit_passes(self):
        job = _job(description="3 years of experience required")
        assert matches_experience_years(job, self._config(3))

    def test_exceeds_limit_rejected(self):
        job = _job(description="Requires 5+ years experience in Python")
        assert not matches_experience_years(job, self._config(3))

    def test_range_lower_bound_exceeds_limit(self):
        job = _job(description="5-8 years of experience expected")
        assert not matches_experience_years(job, self._config(3))

    def test_french_exceeds_limit(self):
        job = _job(description="Minimum 5 ans d'expérience requis")
        assert not matches_experience_years(job, self._config(3))

    def test_french_within_limit(self):
        job = _job(description="2 ans d'expérience souhaitée")
        assert matches_experience_years(job, self._config(3))

    # P1 regression — French patterns without "d'expérience" suffix
    def test_french_au_moins_no_suffix_exceeds(self):
        job = _job(description="au moins 4 ans sur un poste similaire")
        assert not matches_experience_years(job, self._config(3))

    def test_french_minimum_no_suffix_exceeds(self):
        job = _job(description="minimum 5 ans requis pour ce poste")
        assert not matches_experience_years(job, self._config(3))

    def test_french_trailing_minimum_exceeds(self):
        job = _job(description="4 ans minimum exigés")
        assert not matches_experience_years(job, self._config(3))

    def test_french_randstad_case(self):
        """Regression: the Randstad Ollioules job must be caught by the hard filter."""
        desc = (
            "vous justifiez d'une expérience significative d'au moins 4 ans "
            "sur un poste DevOps ou d'Administrateur Systèmes."
        )
        job = _job(description=desc)
        assert not matches_experience_years(job, self._config(3))

    def test_title_scanned_too(self):
        """Experience requirement in the job title should also be caught."""
        job = _job(
            title="Senior Dev (10+ years experience)",
            description="No experience mentioned here",
        )
        assert not matches_experience_years(job, self._config(3))

    def test_integration_with_build_filter_config(self):
        profile = _profile(
            experience=ExperiencePreferences(
                preferred_levels=["junior"],
                max_years=3,
                excluded_seniority=[],
            )
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert cfg["experience_years"]["max_years"] == 3

    def test_filter_jobs_passes_experience_to_gate(self):
        """filter_jobs no longer rejects on experience years — that is the gate's job.

        Experience hard-rejection was moved from filter.py (_FILTER_CHECKS) to
        gate.py (_check_experience). This test verifies filter_jobs passes all three
        jobs and that experience-exceed jobs are not silently dropped at this stage.
        """
        profile = _profile(
            experience=ExperiencePreferences(
                preferred_levels=["junior"],
                max_years=3,
                excluded_seniority=[],
            )
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        senior_job = _job(description="Requires 8+ years experience. Python, Django.")
        junior_job = _job(description="2 years experience. Entry level Python developer.")
        unknown_job = _job(description="Python developer, no experience specified.")
        valid, reasons = filter_jobs([senior_job, junior_job, unknown_job], cfg)
        # filter_jobs must NOT reject on experience — gate handles that
        assert senior_job.id not in reasons, (
            "filter_jobs must not reject based on experience years; "
            "HardConstraintGate._check_experience() is the single authority"
        )
        assert any(j.id == junior_job.id for j in valid)
        assert any(j.id == unknown_job.id for j in valid)
        assert any(j.id == senior_job.id for j in valid)


# ===========================================================================
# SECTION 3 — Location scoring differentiation
# ===========================================================================

class TestLocationScoring:
    """Verify preferred vs non-preferred country differentiation in scoring."""

    def _config(self, preferred_countries: list[str] | None = None) -> dict:
        cfg: dict = {"scoring": {"location_fit": {"target_department": "83"}}}
        if preferred_countries is not None:
            cfg["scoring"]["location_fit"]["preferred_countries"] = preferred_countries
        return cfg

    def _score(self, location, countries, config, work_mode=None, remote=False, department=None):
        return _score_location_fit(location, countries, work_mode, remote, department, config)

    def test_preferred_country_gets_full_bonus(self):
        score = self._score("Paris, France", ["France"], self._config(["france"]))
        assert score >= 0.6

    def test_non_preferred_eu_country_gets_reduced_score(self):
        score = self._score("Berlin, Germany", ["Germany"], self._config(["france"]))
        assert score == 0.55

    def test_preferred_beats_non_preferred(self):
        france_score = self._score("Paris", ["France"], self._config(["france"]))
        germany_score = self._score("Berlin", ["Germany"], self._config(["france"]))
        assert france_score > germany_score

    def test_no_location_conservative_floor(self):
        score = self._score(None, [], self._config(["france"]))
        assert score == 0.40

    def test_no_preferred_config_any_location_gets_bonus(self):
        """When no preferred_countries is configured, backward-compatible."""
        score_fr = self._score("Paris", ["France"], self._config(None))
        score_de = self._score("Berlin", ["Germany"], self._config(None))
        assert score_fr == score_de  # Both get the bonus

    def test_country_in_countries_list_matches(self):
        """Preferred country found in job.countries (not only location string)."""
        score = self._score("Remote", ["France"], self._config(["france"]))
        assert score >= 0.6

    def test_location_string_substring_match(self):
        """'Paris, France' → France is preferred."""
        score = self._score("Paris, France", [], self._config(["france"]))
        assert score >= 0.6

    def test_var_department_beats_preferred_country_alone(self):
        """The target department (Var) outranks a generic preferred-country match."""
        var_score = self._score("Toulon (83)", ["France"], self._config(["france"]))
        paris_score = self._score("Paris, France", ["France"], self._config(["france"]))
        assert var_score > paris_score

    def test_scoring_integrated_with_profile_preferred_countries(self):
        """End-to-end: France job should outscore Germany job when France is preferred."""
        cfg = {
            "scoring": {
                "location_fit": {
                    "weight": 1.0,  # 100% weight to isolate the location factor
                    "target_department": "83",
                    "preferred_countries": ["france"],
                },
                "domain_relevance": {"weight": 0.0},
                "seniority_match": {"weight": 0.0, "preferred_level_keywords": [], "junior_bonus": 1.0},
                "experience_fit": {"weight": 0.0},
                "skills_match": {"weight": 0.0},
            }
        }
        france_job = _job(location="Paris, France", countries=["France"])
        germany_job = _job(location="Berlin, Germany", countries=["Germany"])
        france_score = score_job(france_job, cfg)
        germany_score = score_job(germany_job, cfg)
        assert france_score > germany_score


# ===========================================================================
# SECTION 4 — Phase 2 prompt contains profile
# ===========================================================================

class TestPhase2PromptContainsProfile:
    """Verify build_personalized_matching_prompt structure."""

    def test_prompt_contains_user_profile_section(self):
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {
            "target_skills": ["python", "django"],
            "geography": {"preferred_countries": ["France"], "allowed_regions": ["Europe"]},
            "work_modes": ["remote"],
            "experience": {"preferred_levels": ["junior"], "max_years": 3, "excluded_seniority": []},
            "languages": ["French", "English"],
            "target_domains": ["software_development"],
            "contract_types": [],
        }
        prompt = build_personalized_matching_prompt(
            job_description="Backend Python role in Paris",
            profile=profile,
            job_title="Python Developer",
            job_company="Acme",
        )
        assert "USER PROFILE:" in prompt

    def test_profile_appears_before_job(self):
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {"target_skills": ["python"], "geography": {}, "experience": {}}
        prompt = build_personalized_matching_prompt(
            job_description="Python role",
            profile=profile,
        )
        profile_pos = prompt.index("USER PROFILE:")
        job_pos = prompt.index("JOB:")
        assert profile_pos < job_pos, "USER PROFILE section must come before JOB section"

    def test_profile_skills_in_prompt(self):
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {"target_skills": ["python", "fastapi"], "geography": {}, "experience": {}}
        prompt = build_personalized_matching_prompt("A FastAPI backend role", profile)
        assert "python" in prompt.lower()
        assert "fastapi" in prompt.lower()

    def test_profile_geography_in_prompt(self):
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {
            "target_skills": [],
            "geography": {"preferred_countries": ["France"], "allowed_regions": ["Europe"]},
            "experience": {},
        }
        prompt = build_personalized_matching_prompt("A Paris-based role", profile)
        assert "France" in prompt
        assert "Europe" in prompt

    def test_profile_max_years_in_prompt(self):
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {
            "target_skills": [],
            "geography": {},
            "experience": {"max_years": 3},
        }
        prompt = build_personalized_matching_prompt("Role requiring experience", profile)
        assert "3" in prompt

    def test_prompt_schema_omits_profile_match_score(self):
        """profile_match_score/reasons/mismatch_reasons were deliberately
        removed from LLM generation in the 2026-09-04 personalization audit
        (see build_personalized_matching_prompt's docstring and Issue 6 of
        the 2026-09-05 benchmark fix pass): fit is already computed twice
        downstream (deterministic scorer + Phase 3 semantic embedding), and
        explain.py already generates deterministic "why this job" reasons.
        This test used to assert the opposite (pre-audit) and was stale —
        see the equivalent, currently-correct assertion in
        test_phase2_profile_injection.py::test_prompt_omits_match_score_from_schema.
        """
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {"target_skills": [], "geography": {}, "experience": {}}
        prompt = build_personalized_matching_prompt("Some role", profile)
        assert "profile_match_score" not in prompt

    def test_prompt_warns_not_to_override_hard_constraints(self):
        from src.ai.prompt import build_personalized_matching_prompt
        profile = {"target_skills": [], "geography": {}, "experience": {}}
        prompt = build_personalized_matching_prompt("Some role", profile)
        prompt_lower = prompt.lower()
        assert "hard constraint" in prompt_lower or "deterministic" in prompt_lower

    def test_standard_prompt_no_profile_section(self):
        from src.ai.prompt import build_job_understanding_prompt
        prompt = build_job_understanding_prompt("Python role in Paris")
        assert "USER PROFILE:" not in prompt


# ===========================================================================
# SECTION 5 — Geography: filter vs scoring
# ===========================================================================

class TestGeographyFilterAndScoring:
    """Verify the full geography semantics: France preferred, Europe allowed."""

    def _cfg_with_europe(self) -> dict:
        return {
            "locations": {"include": ["France", "Europe"]},
            "remote_scope": {
                "european_keywords": ["Europe", "EU", "EMEA"],
                "worldwide_keywords": ["worldwide", "global"],
            },
        }

    def test_france_job_passes(self):
        from src.pipeline.filter import matches_location
        job = _job(location="Paris, France", countries=["France"], remote=False)
        assert matches_location(job, self._cfg_with_europe())

    def test_eu_remote_passes(self):
        from src.pipeline.filter import matches_location
        job = _job(location="Remote - Europe", countries=["Germany"], remote=True)
        assert matches_location(job, self._cfg_with_europe())

    def test_us_remote_rejected(self):
        from src.pipeline.filter import matches_location
        job = _job(location="New York, US", countries=["United States"], remote=False)
        assert not matches_location(job, self._cfg_with_europe())

    def test_us_remote_with_no_scope_rejected(self):
        from src.pipeline.filter import matches_location
        job = _job(location="Remote, US", countries=["United States"], remote=True)
        assert not matches_location(job, self._cfg_with_europe())

    def test_unknown_country_non_remote_passes(self):
        from src.pipeline.filter import matches_location
        job = _job(location="Unknown City", countries=[], remote=False)
        assert matches_location(job, self._cfg_with_europe())

    def test_france_scores_higher_than_germany(self):
        """France job should outscore Germany job when France is preferred."""
        cfg = {
            "scoring": {
                "location_fit": {
                    "target_department": "83",
                    "preferred_countries": ["france"],
                }
            }
        }
        france_score = _score_location_fit("Paris, France", ["France"], None, False, None, cfg)
        germany_score = _score_location_fit("Berlin, Germany", ["Germany"], None, False, None, cfg)
        assert france_score > germany_score

    def test_valid_eu_job_not_rejected_for_being_non_france(self):
        """An EU job in Germany should still pass the location hard filter."""
        from src.pipeline.filter import matches_location
        job = _job(location="Remote - Europe", countries=["Germany"], remote=True)
        assert matches_location(job, self._cfg_with_europe())


# ===========================================================================
# SECTION 6 — Company validation (regression from previous fix)
# ===========================================================================

class TestCompanyValidation:
    """Verify company validation semantics are still intact."""

    def test_company_none_passes_validation(self):
        from src.pipeline.validate import validate_job
        job = _job(company=None, countries=["France"])
        result = validate_job(job)
        assert result.is_valid

    def test_company_present_passes_validation(self):
        from src.pipeline.validate import validate_job
        job = _job(company="Acme Corp", countries=["France"])
        result = validate_job(job)
        assert result.is_valid

    def test_company_explicit_empty_fails_validation(self):
        from src.pipeline.validate import validate_job
        job = _job(company="", countries=["France"])
        result = validate_job(job)
        assert not result.is_valid
        assert any("Company" in e for e in result.errors)

    def test_normalize_missing_company_becomes_none(self):
        from src.pipeline.normalize import normalize_job
        raw = {"title": "Dev", "description": "A role", "location": "Paris"}
        job = normalize_job("test_source", raw)
        assert job.company is None

    def test_normalize_whitespace_company_becomes_none(self):
        from src.pipeline.normalize import normalize_job
        raw = {"title": "Dev", "description": "A role", "location": "Paris", "company": "   "}
        job = normalize_job("test_source", raw)
        assert job.company is None


# ===========================================================================
# SECTION 7 — Profile field audit table data assertions
# ===========================================================================

class TestProfileFieldAudit:
    """Assert that each profile field has (or deliberately lacks) a pipeline effect."""

    def test_target_domains_affect_filter(self):
        """Domains expand the title/description keyword include list."""
        profile = _profile(target_domains=["software_development"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert len(cfg["title_keywords"]["include"]) > 0

    def test_target_skills_affect_filter(self):
        """With no domain selected, skills are added verbatim to both include lists.

        (When a domain IS selected, the domain alone gates the hard filter —
        see test_target_skills_do_not_bypass_domain_gate below — so this
        isolates the no-domain-configured branch.)
        """
        profile = _profile(target_domains=[], target_skills=["mypythonkeyword"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert "mypythonkeyword" in cfg["title_keywords"]["include"]
        assert "mypythonkeyword" in cfg["title_keywords"]["description_include"]

    def test_target_skills_do_not_bypass_domain_gate(self):
        """A skill alone must not admit a job outside a selected domain.

        e.g. domain=software_development + skill="python" must not let a
        title match on "python" bypass the domain requirement — the user
        expects picking a domain to mean "only that domain, nothing else".
        """
        profile = _profile(target_domains=["software_development"], target_skills=["mypythonkeyword"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert "mypythonkeyword" not in cfg["title_keywords"]["include"]
        assert "mypythonkeyword" not in cfg["title_keywords"]["description_include"]

    def test_freshness_days_in_filter_config(self):
        profile = _profile(freshness_days=14)
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert cfg["posted_within_days"] == 14

    def test_excluded_seniority_generates_filter_keywords(self):
        profile = _profile(
            experience=ExperiencePreferences(
                preferred_levels=["junior"],
                max_years=None,
                excluded_seniority=["senior", "director"],
            )
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert "senior" in cfg["seniority"]["exclude"]
        assert "director" in cfg["seniority"]["exclude"]

    def test_max_years_in_filter_config(self):
        profile = _profile(
            experience=ExperiencePreferences(preferred_levels=[], max_years=3, excluded_seniority=[])
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert cfg["experience_years"]["max_years"] == 3

    def test_max_years_none_passes_all(self):
        profile = _profile(
            experience=ExperiencePreferences(preferred_levels=[], max_years=None, excluded_seniority=[])
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert cfg["experience_years"]["max_years"] is None

    def test_work_modes_in_filter_config(self):
        profile = _profile(work_modes=["remote"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        assert cfg["work_modes"]["allowed"] == ["remote"]

    def test_target_skills_esco_not_in_filter_config(self):
        """ESCO entries are metadata only — not fed to the filter engine."""
        from src.models.profile import EscoEntry
        profile = _profile(
            target_domains=[],
            target_skills=["python"],
            target_skills_esco=[EscoEntry(label="Python", concept_uri="local:skill/python")],
        )
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        # The EscoEntry label should NOT appear separately in include lists
        # (only the plain target_skills strings go there).
        assert "Python" not in cfg["title_keywords"]["include"]
        assert "python" in cfg["title_keywords"]["include"]

    def test_languages_not_in_filter_config(self):
        """Languages have no hard-filter effect — only Phase 2 context."""
        profile = _profile(languages=["Klingon", "Elvish"])
        cfg = build_filter_config(profile, _minimal_engine_cfg())
        # Languages should not appear in any filter include/exclude list.
        for key in ("include", "description_include", "exclude"):
            kws = cfg.get("title_keywords", {}).get(key, [])
            assert "Klingon" not in kws
            assert "Elvish" not in kws
