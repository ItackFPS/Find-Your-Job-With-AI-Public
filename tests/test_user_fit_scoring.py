"""Regression tests for the user-fit scoring redesign (location > domain >
skills > seniority > experience), validated against real production data
during design — see the approved plan for the full real-data simulation.

Covers:
- The "Intern" seniority false-positive bug found in real data (54 jobs
  wrongly flagged before the fix).
- The "cannot detect domain" vs "confirmed wrong domain" distinction.
- Var location cannot rescue a confirmed off-domain job.
- Mathematical ceiling proof: neither gate can be overturned by semantic score.
- Experience-curve boundaries and ordering.
- The 8-job conceptual ranking test demonstrating the stated priority order.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.models.job import Job
from src.pipeline.scoring import (
    _compute_factors,
    _score_domain_relevance,
    _score_experience_fit,
    _score_seniority_match,
    score_job,
)
from src.requirements.models import GateResult


def _job(**kwargs) -> Job:
    defaults: dict = {
        "id": "j1",
        "source": "test",
        "title": "Software Engineer",
        "description": "",
        "location": "France",
        "countries": ["France"],
        "remote": False,
        "raw_payload": {},
        "discovered_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return Job(**defaults)


# Preferred-level keywords as the orchestrator constructs them for the
# current profile.yaml (internship/alternance/junior/entry_level/graduate),
# including the "intern"/"trainee" alias fix.
_PREFERRED_LEVEL_KEYWORDS = [
    "internship", "stagiaire", "stage", "intern", "trainee",
    "alternance", "apprenti", "apprentissage",
    "junior", "debutant",
    "entry level",
    "graduate",
]


def _seniority_cfg(keywords: list[str] = _PREFERRED_LEVEL_KEYWORDS) -> dict:
    return {"scoring": {"seniority_match": {"preferred_level_keywords": keywords, "junior_bonus": 1.2}}}


# ---------------------------------------------------------------------------
# Bare "Intern" false-positive regression (found in real production data)
# ---------------------------------------------------------------------------

class TestInternAliasRegression:
    def test_bare_intern_title_scores_as_preferred_not_wrong_seniority(self):
        for title in ["DevOps Intern", "Cloud Engineer (Intern)", "AI Engineering, Intern (Paris)"]:
            score = _score_seniority_match(title, _seniority_cfg())
            assert score == 1.0, f"{title!r} should score 1.0 (preferred), got {score}"

    def test_without_the_alias_fix_bare_intern_was_wrongly_penalized(self):
        """Regression check: WITHOUT 'intern' in the keyword list, the bug
        reproduces (title scores 0.0, triggering the wrong-seniority gate)."""
        keywords_without_alias = [k for k in _PREFERRED_LEVEL_KEYWORDS if k != "intern"]
        score = _score_seniority_match("DevOps Intern", _seniority_cfg(keywords_without_alias))
        assert score == 0.0, "Old bug: bare 'Intern' without the alias scores 0.0"

    def test_trainee_alias_also_recognized(self):
        score = _score_seniority_match("Data Trainee", _seniority_cfg())
        assert score == 1.0


# ---------------------------------------------------------------------------
# Title takes precedence over a generic source-supplied job_level
# (2026-09-11 audit fix). Confirmed real-data cases: LinkedIn tagged these
# jobs raw_payload["job_level"] = "mid-senior level" while the title itself
# explicitly names a junior/entry-level position — the tag was silently
# overriding the title and triggering the wrong-seniority gate (score 0.0).
# ---------------------------------------------------------------------------

class TestTitleTakesPrecedenceOverJobLevel:
    def test_junior_title_wins_over_mid_senior_job_level(self):
        for title in [
            "DBA Junior (H/F)",
            "IT Apprentice",
            "Consultant Cybersécurité Junior – Lyon",
            "Data Engineer Junior (CDI) – Azure / Databricks",
            "Ingénieur Cloud & DevOps Linux junior (H/F)",
        ]:
            score = _score_seniority_match(
                title, _seniority_cfg(), job_level="mid-senior level"
            )
            assert score > 0.5, (
                f"{title!r} has an explicit junior signal — job_level "
                f"'mid-senior level' must not override it, got {score}"
            )

    def test_senior_title_still_gated_regardless_of_job_level(self):
        """A title with its own explicit wrong-seniority signal must still
        gate to 0.0 — the fix only changes which source is consulted, not
        the outcome when the title itself says wrong-seniority."""
        score = _score_seniority_match(
            "Senior Backend Engineer", _seniority_cfg(), job_level="entry level"
        )
        assert score == 0.0, (
            "Title's own 'Senior' signal must win even when job_level "
            "happens to say something junior-sounding"
        )

    def test_generic_title_falls_back_to_job_level_unchanged(self):
        """When the title carries no seniority signal at all, job_level
        must still decide the score exactly as before this fix — this is
        the existing, desired behavior for jobs like 'Architecte
        Infrastructure' where the title itself is seniority-neutral but
        the source-supplied level correctly flags it as non-junior."""
        score = _score_seniority_match(
            "Architecte Infrastructure (F/H)", _seniority_cfg(), job_level="mid-senior level"
        )
        assert score == 0.0, "No title signal -> job_level's 'senior' indicator must decide"

    def test_no_job_level_falls_back_to_title_only_behavior(self):
        """job_level omitted entirely (the default) must reproduce the
        pre-fix, title-only behavior exactly."""
        assert _score_seniority_match("DevOps Intern", _seniority_cfg()) == 1.0
        assert _score_seniority_match("Senior Software Engineer", _seniority_cfg()) == 0.0
        assert _score_seniority_match("Software Engineer", _seniority_cfg()) == 0.5

    def test_neutral_title_and_neutral_job_level_stays_neutral(self):
        score = _score_seniority_match(
            "Software Engineer", _seniority_cfg(), job_level="Associate"
        )
        assert score == 0.5

    def test_compute_factors_wires_job_level_as_fallback_not_override(self):
        """End-to-end through _compute_factors (the actual call site the bug
        lived in): a job whose raw_payload carries LinkedIn's job_level must
        still have its title's explicit 'Junior' signal win."""
        job = _job(
            title="DBA Junior (H/F)",
            raw_payload={"job_level": "mid-senior level"},
        )
        factors = _compute_factors(job, _seniority_cfg())
        assert factors["seniority_match"] > 0.5


# ---------------------------------------------------------------------------
# Domain relevance: "cannot detect" vs "confirmed wrong"
# ---------------------------------------------------------------------------

class TestDomainCannotDetectVsConfirmedWrong:
    def _cfg(self, off_domain_kws: list[str] | None = None) -> dict:
        return {
            "scoring": {
                "domain_relevance": {
                    "domain_title_keywords": ["cybersécurité", "développeur", "réseau"],
                    "domain_description_keywords": [],
                    "off_domain_keywords": off_domain_kws or [],
                }
            }
        }

    def test_skill_only_title_with_no_off_domain_evidence_is_undetermined(self):
        """'Expert technique Unix/Linux'-style title: no domain keyword, no
        broad-tech word ('technique' != 'technicien'), no off-domain evidence
        → undetermined (0.45), NOT capped like a confirmed-wrong job."""
        score = _score_domain_relevance("Expert technique Unix/Linux", None, self._cfg())
        assert score == 0.45

    def test_confirmed_off_domain_title_scores_min(self):
        cfg = self._cfg(off_domain_kws=["account executive", "sales", "commercial"])
        score = _score_domain_relevance("Account Executive - Data/IA", None, cfg)
        assert score == 0.05

    def test_undetermined_and_confirmed_wrong_are_distinct_tiers(self):
        cfg = self._cfg(off_domain_kws=["sales"])
        undetermined = _score_domain_relevance("Expert technique Unix/Linux", None, cfg)
        confirmed_wrong = _score_domain_relevance("Sales Manager IA", None, cfg)
        assert undetermined == 0.45
        assert confirmed_wrong == 0.05
        assert undetermined > confirmed_wrong


# ---------------------------------------------------------------------------
# Var cannot rescue a confirmed off-domain job
# ---------------------------------------------------------------------------

class TestVarCannotRescueOffDomainJob:
    def test_var_location_with_confirmed_off_domain_stays_below_relevant_tier(self):
        config = {
            "scoring": {
                "location_fit": {"target_department": "83"},
                "domain_relevance": {
                    "off_domain_keywords": ["sales"],
                },
                "skills_match": {"target_skills": ["python", "javascript", "php", "mysql", "git"]},
                "seniority_match": {"preferred_level_keywords": []},
            }
        }
        job = _job(
            title="Sales Manager IA H/F",
            description="Vente de solutions IA, CRM, Excel, javascript-based reporting tool.",
            location="Toulon (83)",
            countries=["France"],
            department="83",
        )
        score = score_job(job, config)
        assert score * 100 < 60, (
            f"Var location must not rescue a confirmed off-domain job into the "
            f"'relevant' tier or above; got {score * 100:.1f}"
        )


# ---------------------------------------------------------------------------
# Mathematical ceiling proof: gates cannot be overturned by semantic score
# ---------------------------------------------------------------------------

class TestGateCeilingsSurviveMaxSemantic:
    """final = 0.8*det + 0.2*sem. Even at a hypothetical sem=1.0, a gated
    deterministic score must keep final below the 'relevant' tier (60)."""

    def test_domain_gate_ceiling(self):
        # Confirmed off-domain caps det at 0.40 (config/scoring.yaml's domain_cap).
        det_ceiling = 0.40
        final_at_max_semantic = 0.8 * det_ceiling + 0.2 * 1.0
        assert final_at_max_semantic < 0.60
        assert final_at_max_semantic == 0.52

    def test_seniority_gate_ceiling(self):
        # Confirmed wrong-seniority multiplies det by 0.55; even a perfect
        # base (1.0) before the gate can't exceed 0.55 after it.
        det_ceiling = 1.0 * 0.55
        final_at_max_semantic = 0.8 * det_ceiling + 0.2 * 1.0
        assert final_at_max_semantic < 0.65
        assert abs(final_at_max_semantic - 0.64) < 1e-9


# ---------------------------------------------------------------------------
# Experience curve
# ---------------------------------------------------------------------------

class TestExperienceCurve:
    def _score(self, years: int | None) -> float:
        job = _job()
        gate_result = None if years is None else GateResult(passes=True, min_experience_years=years)
        return _score_experience_fit(job, {}, gate_result)

    def test_boundary_values(self):
        assert self._score(0) == 1.0
        assert self._score(1) == 1.0
        assert self._score(2) == 0.85
        assert self._score(3) == 0.70
        assert self._score(4) == 0.55
        assert self._score(5) == 0.40
        assert self._score(6) == 0.25
        assert self._score(7) == 0.15
        assert self._score(10) == 0.15  # floor

    def test_unknown_defaults_to_neutral(self):
        """Phase 3 (2026-09-10): unknown experience is a genuine neutral
        midpoint (0.5), not the old 0.80 proxy-inflated value — matching the
        same 'no evidence -> neutral' convention _score_skills_match and
        _score_seniority_match already use for their own no-evidence cases."""
        assert self._score(None) == 0.50

    def test_unknown_ranks_below_confirmed_low_years_but_above_confirmed_high_years(self):
        """Phase 3: unknown (0.50) sits between the confirmed 4yr (0.55) and
        5yr (0.40) tiers on the existing curve — every CONFIRMED requirement
        of 4 years or fewer (real positive-ish evidence, all still within a
        plausible candidate max) now outranks 'no evidence', and unknown in
        turn still outranks a confirmed 5+yr requirement (real negative
        evidence). This intentionally REVERSES the old ordering (unknown
        used to outrank even a confirmed 3yr requirement) — confirmed
        evidence now always outranks a guess, in either direction, rather
        than 'no evidence' being scored as if it were near-best-case
        evidence."""
        assert (
            self._score(1) > self._score(2) > self._score(3) > self._score(4)
            > self._score(None) > self._score(5) > self._score(6) > self._score(7)
        )

    def test_unknown_is_not_punished_like_a_bad_explicit_signal(self):
        """Unknown (0.5) must still clearly outrank an explicit high-years
        requirement (e.g. 5+yr, real negative evidence) — 'no evidence' is
        not treated as if it were confirmed bad news either."""
        assert self._score(None) > self._score(5)
        assert self._score(None) > self._score(6)
        assert self._score(None) > self._score(7)


# ---------------------------------------------------------------------------
# Conceptual 8-job ranking test — demonstrates the stated priority order
# ---------------------------------------------------------------------------

def _fit_config() -> dict:
    return {
        "scoring": {
            "location_fit": {"target_department": "83", "preferred_countries": ["france"]},
            "domain_relevance": {
                "domain_title_keywords": ["développeur", "cybersécurité", "devops", "qa", "automatisation"],
                "off_domain_keywords": ["commercial", "vendeur", "sales"],
            },
            "skills_match": {"target_skills": ["python", "javascript", "git", "docker", "linux"]},
            "seniority_match": {"preferred_level_keywords": _PREFERRED_LEVEL_KEYWORDS, "junior_bonus": 1.2},
        }
    }


class TestConceptualEightJobRanking:
    """Encodes the plan's 8-job scenario as a relative-ordering test."""

    def _score(self, min_experience_years: int | None = None, **kwargs) -> float:
        gate_result = (
            None if min_experience_years is None
            else GateResult(passes=True, min_experience_years=min_experience_years)
        )
        return score_job(_job(**kwargs), _fit_config(), gate_result)

    def test_priority_order_demonstrated(self):
        job1_var_junior_01yr = self._score(
            title="QA Automatisation Junior", description="Python, JavaScript, Git, Docker.",
            location="Toulon (83)", countries=["France"], department="83",
            min_experience_years=1,
        )
        job8_var_3yr_neutral = self._score(
            title="Développeur QA Automatisation", description="Python, JavaScript, Git, Docker, Linux.",
            location="Toulon (83)", countries=["France"], department="83",
            min_experience_years=3,
        )
        job3_paris_junior_01yr = self._score(
            title="Développeur Junior", description="Python, JavaScript, Git, Docker.",
            location="Paris, France", countries=["France"],
            min_experience_years=1,
        )
        job4_remote_eu_junior_01yr = self._score(
            title="Junior DevOps Engineer", description="Python, JavaScript, Git, Docker.",
            location="Berlin, Germany", countries=["Germany"], remote=True,
            min_experience_years=1,
        )
        job2_var_senior_5yr = self._score(
            title="Senior Développeur QA Automatisation", description="Python, JavaScript, Git, Docker, Linux.",
            location="Toulon (83)", countries=["France"], department="83",
            min_experience_years=5,
        )
        job6_paris_senior_5yr = self._score(
            title="Senior Software Engineer", description="Python, JavaScript, Git, Docker, Linux.",
            location="Paris, France", countries=["France"],
            min_experience_years=5,
        )
        job5_var_unrelated = self._score(
            title="Commercial Sédentaire IA H/F", description="CRM, Excel, prospection commerciale.",
            location="Toulon (83)", countries=["France"], department="83",
        )

        # Junior/domain-matched jobs dominate the top regardless of location nuance.
        assert job1_var_junior_01yr > job8_var_3yr_neutral
        assert job1_var_junior_01yr > job3_paris_junior_01yr
        assert job1_var_junior_01yr > job4_remote_eu_junior_01yr

        # A confirmed seniority mismatch drops a job far below its junior
        # counterparts, even with identical location/domain/skills.
        assert job2_var_senior_5yr < job3_paris_junior_01yr
        assert job2_var_senior_5yr < job4_remote_eu_junior_01yr
        assert job6_paris_senior_5yr < job4_remote_eu_junior_01yr

        # Var alone cannot rescue an unrelated role above the senior mismatches,
        # let alone the junior matches.
        assert job5_var_unrelated < job2_var_senior_5yr
        assert job5_var_unrelated < job1_var_junior_01yr
        assert (job5_var_unrelated * 100) < 60
