"""Location Priority Calibration fix: top-preferred-location bonus.

Background (2026-09-12 audit + calibration design audit): with the
configured weights (location .30 / domain .25 / skills .20 / seniority .15
/ experience .10) and factor ranges as-is, a maximal skills_match swing
(0.20 weight x ~0.60 range = 0.12) could equal or exceed location_fit's
weighted gap between most adjacent tiers. Live-code proof: a Var job with
zero explicit skill matches (det=0.83) LOST to an otherwise-identical
Remote-France job with a perfect skill match (det=0.905), even though
location is the user's single highest-priority factor.

The fix adds a small (0.08), purely additive bonus inside
src.pipeline.scoring._apply_weights, applied BEFORE the existing
domain_cap/seniority_multiplier gates, to the ONE job that resolves to the
profile's single highest-priority status="preferred" location_preferences
entry -- identified via src.pipeline.scoring._is_top_preferred_location,
which reads the resolved LocationMatch's own .status/.priority fields
(the actual profile data), never inferred from location_fit's numeric
output value and never hardcoded to any department/remote value.

Explicitly NOT changed: the 0.30/0.25/0.20/0.15/0.10 weights,
_score_location_fit's calculation/tiers, _score_skills_match's ranges,
semantic_matching.py, personalization/*, filters.yaml vocabulary.

No live network, no DB mutation outside an isolated tempfile sqlite DB,
no real job data.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.models.job import Job
from src.personalization.config import load_personalization_config
from src.personalization.engine import apply_personalization
from src.personalization.models import FeedbackType
from src.personalization.storage import PersonalizationStorage
from src.pipeline.filter import load_filters, load_profile
from src.pipeline.scoring import (
    SENIORITY_FR_SYNONYMS,
    _apply_weights,
    _is_top_preferred_location,
    _score_location_fit,
    _score_skills_match,
    _top_preferred_location_priority,
    cap_personalization_at_gates,
    load_scoring_config,
    score_jobs_with_breakdown,
)
from src.pipeline.semantic_matching import integrate_semantic_into_ranking
from tests.fixtures.profiles import TEST_PROFILE_PATH


# ---------------------------------------------------------------------------
# Real-profile config builder, mirroring orchestrator.py's SCORE-stage
# injection exactly (same helper pattern as the Pass 5/5.1 test files).
# ---------------------------------------------------------------------------

def _real_scoring_config() -> dict[str, Any]:
    profile = load_profile(TEST_PROFILE_PATH)
    engine_cfg = load_filters("config/filters.yaml")
    scoring_config = load_scoring_config("config/scoring.yaml")

    seniority_cfg = scoring_config.setdefault("scoring", {}).setdefault("seniority_match", {})
    pref_keywords: list[str] = []
    for lvl in profile.experience.preferred_levels:
        pref_keywords.append(lvl.replace("_", " "))
        pref_keywords.extend(SENIORITY_FR_SYNONYMS.get(lvl, []))
        if lvl == "internship":
            pref_keywords.extend(["intern", "trainee"])
    seniority_cfg["preferred_level_keywords"] = pref_keywords

    location_cfg = scoring_config.setdefault("scoring", {}).setdefault("location_fit", {})
    location_cfg["preferred_countries"] = list(profile.geography.preferred_countries)
    location_cfg["location_preferences"] = [
        p.model_dump() for p in profile.geography.location_preferences
    ]

    skills_cfg = scoring_config.setdefault("scoring", {}).setdefault("skills_match", {})
    skills_cfg["target_skills"] = list(profile.target_skills)

    domain_rel_cfg = scoring_config.setdefault("scoring", {}).setdefault("domain_relevance", {})
    domain_title_kws: list[str] = []
    domain_desc_kws: list[str] = []
    for domain in profile.target_domains:
        kws = engine_cfg.get("domain_keywords", {}).get(domain, {})
        domain_title_kws.extend(kws.get("title", []))
        domain_desc_kws.extend(kws.get("description", []))
    domain_rel_cfg["domain_title_keywords"] = list(dict.fromkeys(domain_title_kws))
    domain_rel_cfg["domain_description_keywords"] = list(dict.fromkeys(domain_desc_kws))
    off_domain_kws: list[str] = []
    for group in engine_cfg.get("excluded_title_keywords", {}).values():
        off_domain_kws.extend(group)
    for group in engine_cfg.get("non_it_title_functions", {}).values():
        off_domain_kws.extend(group)
    domain_rel_cfg["off_domain_keywords"] = list(dict.fromkeys(off_domain_kws))

    return scoring_config


SCORING_CFG = _real_scoring_config()
LOCATION_PREFS = SCORING_CFG["scoring"]["location_fit"]["location_preferences"]


def _job(jid: str, **kwargs: Any) -> Job:
    defaults: dict[str, Any] = dict(
        id=jid, source="test", title="Administrateur Systemes Junior", description="",
        company="TestCo", remote=False, location="Toulon (83)", countries=["France"],
        department="83", work_mode="on_site", discovered_at=datetime.now(UTC),
        content_hash=f"hash-{jid}",
    )
    defaults.update(kwargs)
    return Job(**defaults)


ZERO_SKILL_DESC = "Supervision du parc informatique."
FULL_SKILL_DESC = "Python, JavaScript, PHP, MySQL, Node.js au quotidien."


# ---------------------------------------------------------------------------
# 1/2/3. Identifying the top-preferred location: correct, robust, not
# hardcoded to "83"/"Var", and NOT every status="preferred" entry.
# ---------------------------------------------------------------------------

class TestTopPreferredLocationPriorityIsProfileDriven:
    def test_real_profile_top_preferred_priority_is_department_83s_index(self):
        """Sanity: in the real profile, department 83 (Var) is listed first
        among status="preferred" entries -- confirms the fixture, not the
        code, defines what "top" means."""
        top_priority = _top_preferred_location_priority(LOCATION_PREFS)
        assert top_priority == 0
        assert LOCATION_PREFS[top_priority]["type"] == "department"
        assert LOCATION_PREFS[top_priority]["value"] == "83"
        assert LOCATION_PREFS[top_priority]["status"] == "preferred"

    def test_empty_list_has_no_top_preferred(self):
        assert _top_preferred_location_priority([]) is None

    def test_all_acceptable_list_has_no_top_preferred(self):
        prefs = [
            {"type": "department", "value": "01", "status": "acceptable"},
            {"type": "department", "value": "02", "status": "acceptable"},
        ]
        assert _top_preferred_location_priority(prefs) is None

    def test_top_preferred_is_not_hardcoded_to_index_0(self):
        """Robustness proof: when the FIRST list entry is "acceptable" and a
        LATER entry is "preferred", the later one's index is correctly
        identified as top -- nothing here assumes index 0 or any specific
        department/remote value."""
        prefs = [
            {"type": "department", "value": "01", "status": "acceptable"},
            {"type": "department", "value": "75", "status": "acceptable"},
            {"type": "department", "value": "99", "status": "preferred"},
            {"type": "remote", "value": "remote_anywhere", "status": "preferred"},
        ]
        assert _top_preferred_location_priority(prefs) == 2


class TestIsTopPreferredLocationMatchesOnlyVar:
    """Proof point 1: the bonus-trigger predicate fires ONLY for the actual
    priority-0 preferred location (department 83), using the real profile."""

    def test_var_department_matches(self):
        assert _is_top_preferred_location(
            "Toulon (83)", ["France"], "on_site", False, "83", SCORING_CFG
        ) is True

    def test_var_department_matches_regardless_of_work_mode(self):
        for work_mode, remote in [("on_site", False), ("hybrid", False), ("remote", True)]:
            assert _is_top_preferred_location(
                "Toulon (83)", ["France"], work_mode, remote, "83", SCORING_CFG
            ) is True

    def test_var_department_extracted_from_location_string(self):
        """Robustness: works even when `department` isn't pre-populated and
        must be extracted from the location text, exactly like
        _score_location_fit does internally."""
        assert _is_top_preferred_location(
            "Toulon (83)", ["France"], "on_site", False, None, SCORING_CFG
        ) is True

    def test_proof_point_2_remote_france_does_not_match_despite_preferred_status(self):
        """Proof point 2: 'remote_france' is ALSO status="preferred" in the
        real profile (at a lower list priority than department 83) but must
        NOT receive the bonus -- only the single highest-priority preferred
        entry does."""
        remote_france_pref = next(
            p for p in LOCATION_PREFS if p.get("type") == "remote" and p.get("status") == "preferred"
        )
        assert remote_france_pref["value"] == "remote_france"  # sanity on the fixture
        assert _is_top_preferred_location(
            "Remote France", ["France"], "remote", True, None, SCORING_CFG
        ) is False

    @pytest.mark.parametrize(
        "dept,loc,work_mode,remote,countries",
        [
            ("69", "Lyon", "on_site", False, ["France"]),      # acceptable dept
            ("01", "Bourg-en-Bresse", "on_site", False, ["France"]),  # acceptable dept
            ("75", "Paris", "on_site", False, ["France"]),     # non-preferred dept
            (None, "Remote Europe", "remote", True, ["Germany"]),  # outside-France remote
        ],
    )
    def test_proof_point_3_ordinary_tiers_do_not_match(self, dept, loc, work_mode, remote, countries):
        assert _is_top_preferred_location(loc, countries, work_mode, remote, dept, SCORING_CFG) is False


# ---------------------------------------------------------------------------
# 4/5/6. Weights, location_fit values, and skills_match values unchanged.
# ---------------------------------------------------------------------------

class TestWeightsAndUnderlyingRangesUnchanged:
    def test_weights_unchanged(self):
        w = SCORING_CFG["scoring"]
        assert w["location_fit"]["weight"] == 0.30
        assert w["domain_relevance"]["weight"] == 0.25
        assert w["skills_match"]["weight"] == 0.20
        assert w["seniority_match"]["weight"] == 0.15
        assert w["experience_fit"]["weight"] == 0.10

    def test_location_fit_tier_values_unchanged(self):
        assert _score_location_fit(
            "Toulon (83)", ["France"], "on_site", False, "83", SCORING_CFG
        ) == 1.0
        assert _score_location_fit(
            "Remote France", ["France"], "remote", True, None, SCORING_CFG
        ) == 0.85
        assert _score_location_fit(
            "Lyon", ["France"], "on_site", False, "69", SCORING_CFG
        ) == 0.79
        assert _score_location_fit(
            "Paris", ["France"], "on_site", False, "75", SCORING_CFG
        ) == 0.60
        assert _score_location_fit(
            "Remote Europe", ["Germany"], "remote", True, None, SCORING_CFG
        ) == 0.55

    def test_skills_match_values_unchanged(self):
        zero_match_job = _job("z", description=ZERO_SKILL_DESC)
        full_match_job = _job("f", description=FULL_SKILL_DESC)
        # domain_relevance=1.0 for both (confirmed via title match) -> fallback tier for zero-match.
        assert _score_skills_match(zero_match_job, SCORING_CFG, domain_relevance=1.0) == pytest.approx(0.40)
        assert _score_skills_match(full_match_job, SCORING_CFG, domain_relevance=1.0) == 1.0


# ---------------------------------------------------------------------------
# 7/8. Domain/seniority gates remain fully authoritative.
# ---------------------------------------------------------------------------

_GATED_DOMAIN_FACTORS = {
    "location_fit": 1.0, "domain_relevance": 0.05, "skills_match": 1.00,
    "seniority_match": 1.0, "experience_fit": 0.5,
}
_GATED_SENIORITY_FACTORS = {
    "location_fit": 1.0, "domain_relevance": 1.0, "skills_match": 1.00,
    "seniority_match": 0.0, "experience_fit": 0.5,
}


class TestBonusCannotBypassExistingGates:
    def test_domain_gate_fully_absorbs_the_bonus(self):
        """A confirmed off-domain job at the top preferred location must
        still cap at exactly domain_cap (0.40) -- the bonus contributes
        NOTHING, since min(base+bonus, cap) == cap regardless of bonus."""
        without_bonus = _apply_weights(_GATED_DOMAIN_FACTORS, SCORING_CFG, is_top_preferred_location=False)
        with_bonus = _apply_weights(_GATED_DOMAIN_FACTORS, SCORING_CFG, is_top_preferred_location=True)
        assert without_bonus == 0.40
        assert with_bonus == 0.40, (
            f"Domain gate must fully absorb the location bonus, got {with_bonus}"
        )

    def test_seniority_gate_still_suppresses_far_below_any_junior_job(self):
        """A seniority-gated job at the top preferred location leaks only
        bonus*seniority_multiplier (~0.044) through the gate -- confirm this
        never makes it remotely competitive with a suitable junior job in
        even the worst location."""
        senior_at_var = _apply_weights(_GATED_SENIORITY_FACTORS, SCORING_CFG, is_top_preferred_location=True)
        senior_at_var_no_bonus = _apply_weights(_GATED_SENIORITY_FACTORS, SCORING_CFG, is_top_preferred_location=False)
        leak = senior_at_var - senior_at_var_no_bonus
        assert leak == pytest.approx(0.08 * 0.55, abs=1e-9), f"Unexpected leak amount: {leak}"

        junior_worst_location_factors = {
            "location_fit": 0.40, "domain_relevance": 1.0, "skills_match": 0.40,
            "seniority_match": 1.0, "experience_fit": 0.5,
        }
        junior_worst_location = _apply_weights(junior_worst_location_factors, SCORING_CFG)
        assert junior_worst_location > senior_at_var, (
            "A suitable junior job, even in the worst location, must still "
            f"beat a seniority-gated job at the top preferred location "
            f"({junior_worst_location} vs {senior_at_var})"
        )

    def test_gate_interaction_via_real_full_chain(self):
        """Same two proofs, but through score_jobs_with_breakdown (the real
        call path), not a hand-built factors dict."""
        off_domain_var_job = _job(
            "gate1", title="Commercial Junior",
            description="Vente de solutions aux entreprises. Prospection, CRM.",
        )
        scored = score_jobs_with_breakdown([off_domain_var_job], SCORING_CFG)
        _, det_score, factors = scored[0]
        assert factors["domain_relevance"] == 0.05
        assert det_score == pytest.approx(0.40)

        senior_var_job = _job("gate2", title="Senior Software Engineer", description=FULL_SKILL_DESC)
        scored2 = score_jobs_with_breakdown([senior_var_job], SCORING_CFG)
        _, det_score2, factors2 = scored2[0]
        assert factors2["seniority_match"] == 0.0
        junior_paris_job = _job(
            "gate3", title="Developpeur Python Junior", description=ZERO_SKILL_DESC,
            department="75", location="Paris", work_mode="on_site",
        )
        scored3 = score_jobs_with_breakdown([junior_paris_job], SCORING_CFG)
        _, det_score3, _ = scored3[0]
        assert det_score3 > det_score2, (
            "A junior job in a non-preferred location must still beat a "
            "seniority-gated job at the top preferred location"
        )


# ---------------------------------------------------------------------------
# 9. Semantic blending uses the (bonus-inclusive) deterministic result
# exactly as before -- no special-casing inside semantic_matching.py.
# ---------------------------------------------------------------------------

class TestSemanticBlendUnaffected:
    def test_bonus_flows_into_semantic_blend_like_any_deterministic_score(self):
        var_job = _job("sem1", description=ZERO_SKILL_DESC)
        _, det_score, _ = score_jobs_with_breakdown([var_job], SCORING_CFG)[0]

        semantic_config = {"similarity": {"weight": 0.06, "min_threshold": 0.0}}
        final = integrate_semantic_into_ranking(
            [(var_job, det_score)], [(var_job, 0.39)], semantic_config
        )[0][1]
        expected = 0.94 * det_score + 0.06 * 0.39
        assert final == pytest.approx(expected, abs=1e-9), (
            "Semantic blend formula must be applied to the bonus-inclusive "
            "deterministic score exactly like any other job -- no separate "
            "code path for top-preferred-location jobs"
        )


# ---------------------------------------------------------------------------
# 10. Personalization does not double-count or re-apply the bonus.
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_pers_storage():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    storage = PersonalizationStorage(db_path=str(db_path))
    storage.init_db()
    yield storage
    storage.close()
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


class TestPersonalizationNotDoubleCounted:
    def test_personalization_operates_once_on_the_final_score_only(self, temp_pers_storage: PersonalizationStorage):
        """Personalization must add its own adjustment to the (bonus-
        inclusive) final score exactly once -- it has no knowledge of the
        location bonus and must not re-derive or re-apply it."""
        var_job = _job("pers1", description=ZERO_SKILL_DESC, company="Acme")
        _, det_score, factors = score_jobs_with_breakdown([var_job], SCORING_CFG)[0]
        semantic_config = {"similarity": {"weight": 0.06, "min_threshold": 0.0}}
        final_pre_pers = integrate_semantic_into_ranking(
            [(var_job, det_score)], [(var_job, 0.39)], semantic_config
        )

        temp_pers_storage.add_feedback(
            _job("liked", title="Administrateur Systemes Junior", company="Acme"),
            FeedbackType.APPLIED,
        )
        pers_config = load_personalization_config("config/does-not-exist.yaml")
        personalized, explanations = apply_personalization(final_pre_pers, temp_pers_storage, pers_config)

        pre_score = final_pre_pers[0][1]
        post_score = dict((j.id, s) for j, s in personalized)[var_job.id]
        adjustment = explanations[var_job.id].clamped_adjustment

        assert post_score == pytest.approx(min(max(pre_score + adjustment, 0.0), 1.0), abs=1e-9), (
            "Personalization must add exactly its own computed adjustment "
            "to the bonus-inclusive pre-personalization score once -- no "
            "double application of the location bonus"
        )

    def test_cap_personalization_at_gates_still_functions_for_top_preferred_location(self):
        """The location bonus must not interfere with the existing
        gate-capping mechanism for a job that ALSO happens to be gated."""
        off_domain_var_job = _job(
            "pers2", title="Commercial Junior",
            description="Vente de solutions aux entreprises.",
        )
        scored = score_jobs_with_breakdown([off_domain_var_job], SCORING_CFG)
        _, det_score, factors = scored[0]
        semantic_config = {"similarity": {"weight": 0.06, "min_threshold": 0.0}}
        final_pre_pers = integrate_semantic_into_ranking(
            [(off_domain_var_job, det_score)], [(off_domain_var_job, 0.39)], semantic_config
        )
        pre_score = final_pre_pers[0][1]
        # Simulate a +0.30 personalization boost (as demonstrated in Pass 5).
        boosted = [(off_domain_var_job, min(pre_score + 0.30, 1.0))]
        capped = cap_personalization_at_gates(
            final_pre_pers, boosted, {off_domain_var_job.id: factors}, SCORING_CFG
        )
        assert capped[0][1] == pytest.approx(pre_score, abs=1e-9), (
            "A domain-gated job at the top preferred location must still be "
            "capped back to its pre-personalization value"
        )


# ---------------------------------------------------------------------------
# 11. Jobs NOT at the top preferred location are completely unaffected.
# ---------------------------------------------------------------------------

class TestNonTopPreferredLocationJobsUnaffected:
    @pytest.mark.parametrize(
        "dept,loc,work_mode,remote,countries",
        [
            (None, "Remote France", "remote", True, ["France"]),
            ("69", "Lyon", "on_site", False, ["France"]),
            ("75", "Paris", "on_site", False, ["France"]),
            (None, "Remote Europe", "remote", True, ["Germany"]),
        ],
    )
    def test_score_identical_with_and_without_bonus_logic_present(
        self, dept, loc, work_mode, remote, countries
    ):
        """For any job that does not match the top preferred location, the
        score computed by the current code must equal the score computed by
        explicitly forcing is_top_preferred_location=False -- proving the
        new logic is a true no-op for these jobs."""
        job = _job("nb", department=dept, location=loc, work_mode=work_mode, remote=remote,
                   countries=countries, description=FULL_SKILL_DESC)
        real_score = score_jobs_with_breakdown([job], SCORING_CFG)[0][1]

        from src.pipeline.scoring import _compute_factors
        factors = _compute_factors(job, SCORING_CFG)
        forced_false_score = _apply_weights(factors, SCORING_CFG, is_top_preferred_location=False)
        assert real_score == pytest.approx(forced_false_score, abs=1e-9)


# ---------------------------------------------------------------------------
# 12. The four previously-demonstrated failures, reproduced and fixed.
# ---------------------------------------------------------------------------

class TestFourRegressionCasesNowResolveInVarsFavor:
    def _score(self, dept, loc, work_mode, remote, countries, description):
        job = _job("r", department=dept, location=loc, work_mode=work_mode, remote=remote,
                   countries=countries, description=description)
        return score_jobs_with_breakdown([job], SCORING_CFG)[0][1]

    def test_var_weak_skills_vs_remote_france_perfect_skills(self):
        var = self._score("83", "Toulon (83)", "on_site", False, ["France"], ZERO_SKILL_DESC)
        remote_france = self._score(None, "Remote France", "remote", True, ["France"], FULL_SKILL_DESC)
        assert var > remote_france, f"Var ({var}) must beat Remote-France ({remote_france})"

    def test_var_weak_skills_vs_acceptable_department_perfect_skills(self):
        var = self._score("83", "Toulon (83)", "on_site", False, ["France"], ZERO_SKILL_DESC)
        acceptable = self._score("69", "Lyon", "on_site", False, ["France"], FULL_SKILL_DESC)
        assert var > acceptable, f"Var ({var}) must beat Acceptable dept ({acceptable})"

    def test_var_weak_skills_vs_paris_perfect_skills(self):
        var = self._score("83", "Toulon (83)", "on_site", False, ["France"], ZERO_SKILL_DESC)
        paris = self._score("75", "Paris", "on_site", False, ["France"], FULL_SKILL_DESC)
        assert var > paris, f"Var ({var}) must beat Paris ({paris})"

    def test_var_weak_skills_vs_outside_france_remote_perfect_skills(self):
        var = self._score("83", "Toulon (83)", "on_site", False, ["France"], ZERO_SKILL_DESC)
        outside_france = self._score(None, "Remote Europe", "remote", True, ["Germany"], FULL_SKILL_DESC)
        assert var > outside_france, f"Var ({var}) must beat Outside-France-remote ({outside_france})"


# ---------------------------------------------------------------------------
# Boundary tests around the 0.08 bonus and final clamping.
# ---------------------------------------------------------------------------

class TestBonusBoundaryAndClamping:
    def test_bonus_never_pushes_a_perfect_job_above_1_0(self):
        perfect_factors = {
            "location_fit": 1.0, "domain_relevance": 1.0, "skills_match": 1.0,
            "seniority_match": 1.0, "experience_fit": 1.0,
        }
        score = _apply_weights(perfect_factors, SCORING_CFG, is_top_preferred_location=True)
        assert score == 1.0

    def test_bonus_never_produces_a_score_below_the_no_bonus_score(self):
        """The bonus must only ever raise the score, never lower it."""
        for factors in (
            {"location_fit": 1.0, "domain_relevance": 0.7, "skills_match": 0.6,
             "seniority_match": 0.5, "experience_fit": 0.5},
            _GATED_DOMAIN_FACTORS,
            _GATED_SENIORITY_FACTORS,
        ):
            without = _apply_weights(dict(factors), SCORING_CFG, is_top_preferred_location=False)
            with_bonus = _apply_weights(dict(factors), SCORING_CFG, is_top_preferred_location=True)
            assert with_bonus >= without, (
                f"Bonus must never reduce the score: without={without}, with={with_bonus}"
            )

    def test_bonus_defaults_to_0_08_when_config_key_absent(self):
        cfg_without_key = {
            "scoring": {
                "location_fit": {"weight": 0.30}, "domain_relevance": {"weight": 0.25},
                "skills_match": {"weight": 0.20}, "seniority_match": {"weight": 0.15},
                "experience_fit": {"weight": 0.10},
                # no "gates" key at all
            }
        }
        factors = {
            "location_fit": 0.70, "domain_relevance": 0.70, "skills_match": 0.70,
            "seniority_match": 0.70, "experience_fit": 0.70,
        }
        without = _apply_weights(dict(factors), cfg_without_key, is_top_preferred_location=False)
        with_bonus = _apply_weights(dict(factors), cfg_without_key, is_top_preferred_location=True)
        assert with_bonus - without == pytest.approx(0.08, abs=1e-9)

    def test_bonus_reads_configured_value_not_a_hardcoded_constant(self):
        """The bonus size must come from scoring.gates.top_location_bonus,
        not a hardcoded 0.08 baked into the code."""
        custom_cfg = {
            "scoring": {
                "location_fit": {"weight": 0.30}, "domain_relevance": {"weight": 0.25},
                "skills_match": {"weight": 0.20}, "seniority_match": {"weight": 0.15},
                "experience_fit": {"weight": 0.10},
                "gates": {"top_location_bonus": 0.15},
            }
        }
        factors = {
            "location_fit": 0.70, "domain_relevance": 0.70, "skills_match": 0.70,
            "seniority_match": 0.70, "experience_fit": 0.70,
        }
        without = _apply_weights(dict(factors), custom_cfg, is_top_preferred_location=False)
        with_bonus = _apply_weights(dict(factors), custom_cfg, is_top_preferred_location=True)
        assert with_bonus - without == pytest.approx(0.15, abs=1e-9)

    def test_real_config_top_location_bonus_is_0_08(self):
        assert SCORING_CFG["scoring"]["gates"]["top_location_bonus"] == 0.08

    def test_final_score_job_output_still_clamped_to_0_1_range(self):
        """End-to-end via score_jobs_with_breakdown: even with the bonus,
        every job's final score stays within [0, 1]."""
        jobs = [
            _job("clamp1", description=FULL_SKILL_DESC),  # top preferred + everything maxed
            _job("clamp2", title="Commercial Junior", description="Vente."),  # gated + top preferred
        ]
        for job, score, _factors in score_jobs_with_breakdown(jobs, SCORING_CFG):
            assert 0.0 <= score <= 1.0
