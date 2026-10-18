"""Pass 5 regression tests: personalization cannot bypass a dealbreaker gate.

Background (2026-09-12 audit + fix): apply_personalization (Phase 4) runs
AFTER the deterministic domain/seniority gates and the semantic blend, and
has no visibility into either gate — it only sees the post-gate composite
score. Its bound (personalization.max_abs_adjustment, 0.3 by default) is
trivially reachable from a single prior feedback event. Live-code proof
from the audit: a confirmed off-domain "Commercial Junior" job, correctly
gated to a final score of ~0.40 (low_priority), reached ~0.70 (strong)
after one unrelated "applied" feedback event for a job at the same company.

The fix, src.pipeline.scoring.cap_personalization_at_gates, is called from
orchestrator.py right after apply_personalization. It does not change
personalization's own module or contract — it only refuses the POSITIVE
portion of a gated job's adjustment, using the exact same gate-trigger
condition (_gate_fired) that _apply_weights itself uses.

Covers both the pure capping function in isolation (fast, no DB) and the
full real chain: score_jobs_with_breakdown -> integrate_semantic_into_ranking
-> apply_personalization -> cap_personalization_at_gates, using an isolated
temporary sqlite database (never the real data/jobs.db).
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
from src.pipeline.scoring import (
    _gate_fired,
    cap_personalization_at_gates,
    score_jobs_with_breakdown,
)
from src.pipeline.semantic_matching import integrate_semantic_into_ranking


def _job(job_id: str, **kwargs: Any) -> Job:
    defaults: dict[str, Any] = {
        "id": job_id,
        "source": "test",
        "title": "Software Engineer",
        "company": "Acme",
        "remote": False,
        "location": "Toulon (83)",
        "countries": ["France"],
        "department": "83",
        "discovered_at": datetime.now(UTC),
        "content_hash": f"hash-{job_id}",
    }
    defaults.update(kwargs)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# Part A: cap_personalization_at_gates in isolation (pure function, no DB)
# ---------------------------------------------------------------------------

_GATED_DOMAIN_FACTORS = {
    "location_fit": 1.0, "domain_relevance": 0.05, "skills_match": 0.05,
    "seniority_match": 1.0, "experience_fit": 0.5,
}
_GATED_SENIORITY_FACTORS = {
    "location_fit": 1.0, "domain_relevance": 1.0, "skills_match": 0.8,
    "seniority_match": 0.0, "experience_fit": 0.5,
}
_VALID_FACTORS = {
    "location_fit": 1.0, "domain_relevance": 1.0, "skills_match": 0.8,
    "seniority_match": 1.0, "experience_fit": 0.5,
}

_CFG = {"scoring": {"domain_relevance": {"confirmed_wrong_score": 0.05}}}


class TestGateFiredHelper:
    """_gate_fired is the single source of truth shared by _apply_weights
    and cap_personalization_at_gates — must agree on both gate conditions."""

    def test_confirmed_wrong_domain_fires(self):
        assert _gate_fired(_GATED_DOMAIN_FACTORS, _CFG) is True

    def test_confirmed_wrong_seniority_fires(self):
        assert _gate_fired(_GATED_SENIORITY_FACTORS, _CFG) is True

    def test_valid_job_does_not_fire(self):
        assert _gate_fired(_VALID_FACTORS, _CFG) is False


class TestCapPersonalizationAtGates:
    def test_domain_gated_job_cannot_be_promoted_above_its_ceiling(self):
        """+0.30 personalization on a domain-gated job must be discarded."""
        job = _job("bad-domain")
        pre = [(job, 0.40)]
        post = [(job, 0.70)]  # simulates the demonstrated +0.30 adjustment
        result = cap_personalization_at_gates(
            pre, post, {"bad-domain": _GATED_DOMAIN_FACTORS}, _CFG
        )
        assert dict((j.id, s) for j, s in result)["bad-domain"] == 0.40

    def test_seniority_gated_job_penalty_cannot_be_erased(self):
        job = _job("bad-seniority")
        pre = [(job, 0.42)]
        post = [(job, 0.72)]
        result = cap_personalization_at_gates(
            pre, post, {"bad-seniority": _GATED_SENIORITY_FACTORS}, _CFG
        )
        assert dict((j.id, s) for j, s in result)["bad-seniority"] == 0.42

    def test_valid_job_still_receives_positive_personalization(self):
        """A job that never triggered a gate must be completely unaffected —
        the fix must not blunt legitimate positive personalization."""
        job = _job("good-job")
        pre = [(job, 0.75)]
        post = [(job, 0.95)]
        result = cap_personalization_at_gates(
            pre, post, {"good-job": _VALID_FACTORS}, _CFG
        )
        assert dict((j.id, s) for j, s in result)["good-job"] == 0.95

    def test_negative_personalization_still_works_for_gated_job(self):
        """A gated job can still be pushed DOWN further by negative
        feedback — only the upward direction is blocked."""
        job = _job("bad-domain-2")
        pre = [(job, 0.40)]
        post = [(job, 0.20)]
        result = cap_personalization_at_gates(
            pre, post, {"bad-domain-2": _GATED_DOMAIN_FACTORS}, _CFG
        )
        assert dict((j.id, s) for j, s in result)["bad-domain-2"] == 0.20

    def test_negative_personalization_still_works_for_valid_job(self):
        job = _job("good-job-2")
        pre = [(job, 0.75)]
        post = [(job, 0.50)]
        result = cap_personalization_at_gates(
            pre, post, {"good-job-2": _VALID_FACTORS}, _CFG
        )
        assert dict((j.id, s) for j, s in result)["good-job-2"] == 0.50

    def test_no_adjustment_is_not_double_penalized(self):
        """A gated job with zero net personalization adjustment must stay
        exactly at its pre-personalization value -- capping must never
        subtract anything beyond what personalization itself computed."""
        job = _job("bad-domain-3")
        pre = [(job, 0.40)]
        post = [(job, 0.40)]
        result = cap_personalization_at_gates(
            pre, post, {"bad-domain-3": _GATED_DOMAIN_FACTORS}, _CFG
        )
        assert dict((j.id, s) for j, s in result)["bad-domain-3"] == 0.40

    def test_missing_factors_entry_passes_through_unchanged(self):
        """Defensive: a job id absent from factors_by_job_id (should not
        happen in production, since orchestrator.py builds it from the same
        scored_with_breakdown) is left exactly as personalization computed."""
        job = _job("unknown-factors")
        pre = [(job, 0.40)]
        post = [(job, 0.70)]
        result = cap_personalization_at_gates(pre, post, {}, _CFG)
        assert dict((j.id, s) for j, s in result)["unknown-factors"] == 0.70

    def test_result_is_sorted_descending(self):
        job_a = _job("a")
        job_b = _job("b")
        factors = {"a": _GATED_DOMAIN_FACTORS, "b": _VALID_FACTORS}
        pre = [(job_a, 0.40), (job_b, 0.75)]
        post = [(job_a, 0.70), (job_b, 0.60)]  # a tries to jump above b
        result = cap_personalization_at_gates(pre, post, factors, _CFG)
        assert [j.id for j, _ in result] == ["b", "a"], (
            "The gated job must not out-rank the valid job after capping"
        )


# ---------------------------------------------------------------------------
# Part B: the real end-to-end chain
#   score_jobs_with_breakdown -> integrate_semantic_into_ranking
#   -> apply_personalization -> cap_personalization_at_gates
# Isolated temporary sqlite DB only -- never data/jobs.db. No network.
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db() -> str:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield str(db_path)
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


@pytest.fixture
def pers_storage(temp_db: str) -> PersonalizationStorage:
    s = PersonalizationStorage(db_path=temp_db)
    s.init_db()
    yield s
    s.close()


def _scoring_config() -> dict[str, Any]:
    return {
        "scoring": {
            "location_fit": {"weight": 0.30, "target_department": "83", "preferred_countries": ["france"]},
            "domain_relevance": {
                "weight": 0.25,
                "domain_title_keywords": ["technicien support informatique", "développeur"],
                "off_domain_keywords": ["commercial"],
            },
            "skills_match": {"weight": 0.20, "target_skills": ["python", "docker"]},
            "seniority_match": {
                "weight": 0.15,
                "preferred_level_keywords": ["junior", "alternance", "stagiaire"],
                "junior_bonus": 1.2,
            },
            "experience_fit": {"weight": 0.10},
        }
    }


def _semantic_config() -> dict[str, Any]:
    return {"similarity": {"weight": 0.06, "min_threshold": 0.0}}


class TestFullChainPersonalizationCannotUndoGate:
    def test_domain_gated_job_survives_personalization_end_to_end(
        self, pers_storage: PersonalizationStorage
    ):
        bad_job = _job(
            "commercial-junior",
            title="Commercial Junior",
            description="Vente de solutions aux entreprises. Prospection, negociation, CRM.",
        )
        good_job = _job(
            "it-support-junior",
            title="Technicien Support Informatique Junior",
            description="Helpdesk, maintenance parc informatique.",
        )
        jobs = [bad_job, good_job]

        scoring_config = _scoring_config()
        scored = score_jobs_with_breakdown(jobs, scoring_config)
        factors_by_job_id = {job.id: factors for job, _, factors in scored}

        # Sanity: the domain gate actually fired for the bad job pre-fix.
        assert factors_by_job_id["commercial-junior"]["domain_relevance"] == 0.05

        det_pairs = [(job, score) for job, score, _ in scored]
        final_ranked = integrate_semantic_into_ranking(
            det_pairs, [(j, 0.39) for j in jobs], _semantic_config()
        )
        pre_pers_by_id = dict((j.id, s) for j, s in final_ranked)
        assert pre_pers_by_id["commercial-junior"] < 0.45, (
            "Domain gate must keep the confirmed off-domain job's pre-"
            f"personalization score low, got {pre_pers_by_id['commercial-junior']}"
        )

        # One prior "applied" feedback event, unrelated to domain fit, that
        # shares the bad job's company AND a generic title keyword ("junior")
        # -- exactly the mechanism the audit demonstrated reaches the +0.30 cap.
        pers_storage.add_feedback(
            _job("liked-1", title="Developpeur Full Stack Junior", company="Acme"),
            FeedbackType.APPLIED,
        )
        pers_config = load_personalization_config("config/does-not-exist.yaml")

        personalized, _explanations = apply_personalization(
            final_ranked, pers_storage, pers_config
        )
        post_pers_by_id = dict((j.id, s) for j, s in personalized)
        # Confirm personalization DID try to promote the bad job (proves the
        # test actually exercises the failure mode, not a no-op).
        assert post_pers_by_id["commercial-junior"] > pre_pers_by_id["commercial-junior"] + 0.2, (
            "Test setup must reproduce the demonstrated +0.3-ish promotion "
            "before the gate-capping fix is applied"
        )

        capped = cap_personalization_at_gates(
            final_ranked, personalized, factors_by_job_id, scoring_config
        )
        capped_by_id = dict((j.id, s) for j, s in capped)

        assert capped_by_id["commercial-junior"] == pytest.approx(
            pre_pers_by_id["commercial-junior"]
        ), (
            "A confirmed off-domain job must not be promoted above its "
            "pre-personalization (gated) score"
        )
        assert capped_by_id["commercial-junior"] * 100 < 60, (
            "The confirmed off-domain job must stay below the 'relevant' "
            f"tier even after personalization, got {capped_by_id['commercial-junior'] * 100:.1f}"
        )

    def test_valid_job_still_benefits_from_the_same_positive_feedback(
        self, pers_storage: PersonalizationStorage
    ):
        """The fix must not make personalization inert for legitimate jobs
        -- only gated jobs lose their positive delta."""
        good_job = _job(
            "it-support-junior-2",
            title="Technicien Support Informatique Junior",
            description="Helpdesk, maintenance parc informatique.",
            company="OtherCo",
        )
        scoring_config = _scoring_config()
        scored = score_jobs_with_breakdown([good_job], scoring_config)
        factors_by_job_id = {job.id: factors for job, _, factors in scored}
        det_pairs = [(job, score) for job, score, _ in scored]
        final_ranked = integrate_semantic_into_ranking(
            det_pairs, [(good_job, 0.39)], _semantic_config()
        )
        pre_score = dict((j.id, s) for j, s in final_ranked)[good_job.id]

        pers_storage.add_feedback(
            _job("liked-2", title="Technicien Support Junior", company="OtherCo"),
            FeedbackType.APPLIED,
        )
        pers_config = load_personalization_config("config/does-not-exist.yaml")
        personalized, _ = apply_personalization(final_ranked, pers_storage, pers_config)
        capped = cap_personalization_at_gates(
            final_ranked, personalized, factors_by_job_id, scoring_config
        )
        capped_score = dict((j.id, s) for j, s in capped)[good_job.id]

        assert capped_score > pre_score, (
            "A non-gated job must still receive its full positive "
            f"personalization adjustment (pre={pre_score}, post={capped_score})"
        )
