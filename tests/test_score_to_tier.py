"""Boundary tests for the canonical score_to_tier() (src/pipeline/scoring.py).

This is the single Python implementation of the 0-100 score -> tier mapping
that src/api/routes/jobs.py imports — see the "Score -> tier classification"
comment in scoring.py for the consumer list, including the frontend mirror
that cannot import this module and therefore carries a documented copy.
"""

from __future__ import annotations

import pytest

from src.pipeline.scoring import TIER_RANK, score_to_tier, tier_meets_minimum


class TestScoreToTierBoundaries:
    """Phase 4 (2026-09-10): fixed-reference calibration — thresholds
    derived once from a 4-run/30,271-real-job historical sample (see the
    full methodology at scoring.py's _TIER_THRESHOLDS), not live per-run
    percentiles."""

    @pytest.mark.parametrize(
        "score_100,expected",
        [
            # exceptional: >= 80
            (100, "exceptional"),
            (80, "exceptional"),
            # just below exceptional -> excellent
            (79, "excellent"),
            # excellent: >= 72
            (72, "excellent"),
            # just below excellent -> strong
            (71, "strong"),
            # strong: >= 65
            (65, "strong"),
            # just below strong -> relevant
            (64, "relevant"),
            # relevant: >= 60
            (60, "relevant"),
            # just below relevant -> low_priority
            (59, "low_priority"),
            # low end
            (1, "low_priority"),
            (0, "low_priority"),
        ],
    )
    def test_threshold_boundaries(self, score_100, expected):
        assert score_to_tier(score_100) == expected

    def test_negative_score_is_low_priority(self):
        """Scores are expected to be 0-100, but a negative value must not
        crash — it falls through every threshold to the default tier."""
        assert score_to_tier(-1) == "low_priority"

    def test_above_100_is_still_exceptional(self):
        """Not expected in practice (scores are capped at 100), but the
        function must not special-case the upper bound — >=90 is >=90."""
        assert score_to_tier(150) == "exceptional"

    def test_returns_one_of_the_five_known_tiers(self):
        known = {"exceptional", "excellent", "strong", "relevant", "low_priority"}
        for score in range(0, 101):
            assert score_to_tier(score) in known


# ---------------------------------------------------------------------------
# Phase 4: fixed-calibration invariants
# ---------------------------------------------------------------------------

class TestTierOrdering:
    """(4) Tier rank must be strictly monotonic with score across the whole
    0-100 range — no score band can skip backwards to a "worse" tier."""

    _RANK = {"low_priority": 0, "relevant": 1, "strong": 2, "excellent": 3, "exceptional": 4}

    def test_tier_rank_never_decreases_as_score_increases(self):
        prev_rank = -1
        for score in range(0, 101):
            rank = self._RANK[score_to_tier(score)]
            assert rank >= prev_rank, f"Tier rank decreased at score={score}"
            prev_rank = rank

    def test_all_five_tiers_are_reachable(self):
        """A fixed calibration must not define a tier that no score can ever
        reach (distinct from a tier being EMPTY in a given run's data —
        this checks the threshold math itself, not any dataset)."""
        seen = {score_to_tier(s) for s in range(0, 101)}
        assert seen == set(self._RANK)


class TestTierDeterminismAcrossBatches:
    """(5)(6)(7) The fixed calibration must never depend on "the current
    run" in any way — no hidden per-run state, no live percentile
    computation, and calling it never mutates or otherwise affects the
    numeric score it was given."""

    def test_same_score_produces_same_tier_regardless_of_call_context(self):
        """(5) Simulates 'multiple runs' by interleaving calls for a fixed
        score among calls for many other (weak and strong) scores — a
        stateful/percentile-based implementation could let surrounding
        calls influence the result; this one must not."""
        target_score = 66
        expected = score_to_tier(target_score)

        # A "weak batch": mostly low scores surrounding the target.
        for _ in range(50):
            score_to_tier(5)
        assert score_to_tier(target_score) == expected

        # A "strong batch": mostly high scores surrounding the target.
        for _ in range(50):
            score_to_tier(99)
        assert score_to_tier(target_score) == expected

    def test_function_signature_takes_only_a_single_score_no_batch_context(self):
        """(6) Architectural guarantee against a live per-run percentile
        calculation creeping back in: score_to_tier must be a pure function
        of ONE score_100 value — it cannot accept a list of peer scores,
        a run_id, or any other batch-dependent context."""
        import inspect

        sig = inspect.signature(score_to_tier)
        params = list(sig.parameters.values())
        assert len(params) == 1, (
            f"score_to_tier must take exactly one argument (the score), got {params}"
        )

    def test_tier_lookup_does_not_modify_the_score(self):
        """(7) Tier is a derived label, never fed back into the score."""
        score_100 = 73
        for _ in range(5):
            tier = score_to_tier(score_100)
            assert isinstance(tier, str)
            assert score_100 == 73  # the input value itself is never mutated


class TestPhase4PriorPhasesUnchanged:
    """(8)(9)(10) Phase 4 touches only score_to_tier's classification
    thresholds — the underlying 0-100 score computed by Phases 1-3 must be
    completely unaffected. Tier is presentation-only and must never feed
    back into ranking or the numeric score."""

    def test_phase1_semantic_config_unchanged(self):
        from src.pipeline.semantic_matching import load_semantic_config

        config = load_semantic_config("config/semantic_config.yaml")
        assert config["similarity"]["weight"] == 0.06
        assert config["similarity"]["reference_floor"] == 0.20
        assert config["similarity"]["reference_ceiling"] == 0.65

    def test_phase2_skills_fallback_unchanged(self):
        from datetime import UTC, datetime

        from src.models.job import Job
        from src.pipeline.scoring import _score_skills_match

        job = Job(
            id="j1", source="test", title="Administrateur Systèmes",
            description="Supervision du parc informatique.",
            location="France", countries=["France"], remote=False,
            raw_payload={}, discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        cfg = {"scoring": {"skills_match": {
            "target_skills": ["python", "javascript", "php", "mysql", "docker"]
        }}}
        score = _score_skills_match(job, cfg, domain_relevance=1.0)
        assert score == pytest.approx(0.40, abs=1e-6)

    def test_phase3_experience_and_location_unchanged(self):
        from datetime import UTC, datetime

        from src.models.job import Job
        from src.pipeline.scoring import _score_experience_fit, _score_location_fit

        job = Job(
            id="j1", source="test", title="Software Engineer", description="",
            location="France", countries=["France"], remote=False,
            raw_payload={}, discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert _score_experience_fit(job, {}) == 0.50  # unknown -> neutral

        loc_cfg = {"scoring": {"location_fit": {
            "target_department": "83", "preferred_countries": ["france"]
        }}}
        baseline = _score_location_fit(
            location="Lyon, France", countries=["France"], work_mode="on_site",
            remote=False, department="69", config=loc_cfg,
        )
        assert baseline == 0.60  # Phase 3 calibrated baseline, unchanged by Phase 4

    def test_tier_calculation_does_not_alter_score_computation_path(self):
        """score_to_tier operates purely on an already-computed score_100 —
        it never calls into score_job / score_jobs_with_breakdown or any
        other scoring-pipeline function, confirming tier is a downstream
        presentation step, not part of ranking itself."""
        import inspect

        from src.pipeline import scoring

        source = inspect.getsource(scoring.score_to_tier)
        for forbidden in ("score_job", "_apply_weights", "_compute_factors", "sorted", "statistics"):
            assert forbidden not in source, (
                f"score_to_tier must not reference {forbidden!r} — it is a pure "
                f"label lookup, never a ranking or re-scoring step"
            )


# ---------------------------------------------------------------------------
# Phase 5 (2026-09-10): TIER_RANK / tier_meets_minimum — the dashboard
# visibility-floor helper. Presentation-filter only: never changes a job's
# score or tier, only whether it passes a "show tier X and above" check.
# ---------------------------------------------------------------------------

class TestTierRankOrdering:
    def test_rank_matches_calibrated_order(self):
        assert TIER_RANK == {
            "low_priority": 0, "relevant": 1, "strong": 2, "excellent": 3, "exceptional": 4,
        }

    def test_rank_is_derived_from_thresholds_not_hand_typed(self):
        """TIER_RANK must be built from _TIER_THRESHOLDS itself so it can
        never silently drift out of sync with score_to_tier()."""
        from src.pipeline.scoring import _TIER_THRESHOLDS

        expected_order = [tier for _, tier in reversed(_TIER_THRESHOLDS)]
        by_rank = sorted(TIER_RANK, key=lambda t: TIER_RANK[t])
        assert by_rank[1:] == expected_order  # index 0 is the default (low_priority)


class TestTierMeetsMinimum:
    @pytest.mark.parametrize(
        "tier,min_tier,expected",
        [
            ("exceptional", "relevant", True),
            ("excellent", "relevant", True),
            ("strong", "relevant", True),
            ("relevant", "relevant", True),   # equal tier passes (inclusive)
            ("low_priority", "relevant", False),
            ("relevant", "strong", False),
            ("exceptional", "exceptional", True),
            ("low_priority", "low_priority", True),  # everything passes the loosest floor
        ],
    )
    def test_threshold_behavior(self, tier, min_tier, expected):
        assert tier_meets_minimum(tier, min_tier) is expected

    def test_unknown_min_tier_never_hides_anything(self):
        """An invalid/unrecognized min_tier value must be permissive (show
        everything) rather than silently hiding all jobs."""
        assert tier_meets_minimum("low_priority", "not_a_real_tier") is True
        assert tier_meets_minimum("exceptional", "not_a_real_tier") is True

    def test_never_modifies_the_score(self):
        """Purely a boolean predicate — confirms it takes only tier strings,
        never a score value, so it structurally cannot feed back into
        ranking."""
        import inspect

        from src.pipeline.scoring import tier_meets_minimum as fn

        params = list(inspect.signature(fn).parameters)
        assert params == ["tier", "min_tier"]
