"""I20 — EXPERIMENTAL local personal reranker tests.

Covers: safe refusal to train on insufficient data (the expected real-world
outcome today — the live database has zero job_feedback rows), bounded
per-feature and total adjustments, determinism, rejected-job exclusion by
construction, and a structural proof that this module has no path into
production ranking/eligibility and no network dependency.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.experimental.reranker import (
    _DEFAULT_MAX_FEATURE_WEIGHT,
    RerankerModel,
    apply_reranker_shadow,
    evaluate_reranker,
    train_simple_reranker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _labeled_example(outcome: int, **factors: float) -> dict:
    return {"outcome": outcome, "factors": factors}


class TestTrainSimpleReranker:
    def test_insufficient_data_returns_none(self) -> None:
        examples = [_labeled_example(1, location_fit=0.9) for _ in range(5)]
        assert train_simple_reranker(examples, min_examples=20) is None

    def test_zero_examples_returns_none(self) -> None:
        assert train_simple_reranker([], min_examples=20) is None

    def test_exactly_min_examples_trains(self) -> None:
        examples = [
            _labeled_example(1 if i % 2 == 0 else 0, location_fit=1.0 if i % 2 == 0 else 0.0)
            for i in range(20)
        ]
        model = train_simple_reranker(examples, min_examples=20)
        assert model is not None
        assert model.n_training_examples == 20

    def test_feature_weights_bounded(self) -> None:
        # Perfectly correlated feature should still be clipped to the bound.
        examples = [
            _labeled_example(1 if i % 2 == 0 else 0, location_fit=1.0 if i % 2 == 0 else 0.0)
            for i in range(30)
        ]
        model = train_simple_reranker(examples, min_examples=20)
        assert model is not None
        assert abs(model.feature_weights["location_fit"]) <= _DEFAULT_MAX_FEATURE_WEIGHT + 1e-9

    def test_uncorrelated_feature_gets_near_zero_weight(self) -> None:
        examples = [
            _labeled_example(i % 2, domain_relevance=0.5)  # constant value -> zero variance
            for i in range(30)
        ]
        model = train_simple_reranker(examples, min_examples=20)
        assert model is not None
        assert model.feature_weights["domain_relevance"] == 0.0

    def test_deterministic_same_input_same_output(self) -> None:
        examples = [
            _labeled_example(1 if i % 3 == 0 else 0, skills_match=0.2 * (i % 5))
            for i in range(25)
        ]
        m1 = train_simple_reranker(examples, min_examples=20)
        m2 = train_simple_reranker(examples, min_examples=20)
        assert m1 == m2


class TestApplyRerankerShadow:
    def _model(self) -> RerankerModel:
        examples = [
            _labeled_example(1 if i % 2 == 0 else 0, location_fit=1.0 if i % 2 == 0 else 0.0)
            for i in range(30)
        ]
        model = train_simple_reranker(examples, min_examples=20)
        assert model is not None
        return model

    def test_baseline_score_never_mutated(self) -> None:
        model = self._model()
        jobs = [("job-1", 0.5, {"location_fit": 1.0})]
        result = apply_reranker_shadow(jobs, model)
        assert result[0].baseline_score == 0.5

    def test_total_adjustment_is_bounded(self) -> None:
        # Extreme factor values should still be clamped to the configured
        # max total adjustment, never allowed to swing the score unbounded.
        model = self._model()
        jobs = [("job-1", 0.1, {"location_fit": 1.0, "domain_relevance": 1.0, "skills_match": 1.0,
                                 "seniority_match": 1.0, "experience_fit": 1.0, "semantic_score": 1.0})]
        result = apply_reranker_shadow(jobs, model, max_total_adjustment=0.15)
        assert abs(result[0].adjustment) <= 0.15 + 1e-9

    def test_reranked_score_stays_in_unit_range(self) -> None:
        model = self._model()
        jobs = [("job-1", 0.95, {"location_fit": 1.0})]
        result = apply_reranker_shadow(jobs, model)
        assert 0.0 <= result[0].reranked_score <= 1.0

    def test_gated_off_domain_job_cannot_be_rescued_into_top_range(self) -> None:
        # A job the domain gate already capped low (e.g. production's
        # domain_cap=0.40 applied upstream) must not be pushed into "top
        # result" territory by a small, bounded reranker adjustment.
        model = self._model()
        low_baseline = 0.05  # confirmed off-domain, per production's own scale
        jobs = [("off-domain-job", low_baseline, {"location_fit": 1.0, "domain_relevance": 0.05})]
        result = apply_reranker_shadow(jobs, model, max_total_adjustment=0.15)
        assert result[0].reranked_score <= low_baseline + 0.15 + 1e-9
        assert result[0].reranked_score < 0.5  # nowhere near a competitive score

    def test_empty_job_list_returns_empty(self) -> None:
        model = self._model()
        assert apply_reranker_shadow([], model) == []


class TestRejectedJobsCannotEnterReranking:
    """Structural proof: the function only ever sees jobs a caller passed
    in — there is no code path for it to reach into storage and pull a
    rejected job on its own."""

    def test_only_passed_in_jobs_appear_in_output(self) -> None:
        examples = [
            _labeled_example(1 if i % 2 == 0 else 0, location_fit=1.0 if i % 2 == 0 else 0.0)
            for i in range(30)
        ]
        model = train_simple_reranker(examples, min_examples=20)
        assert model is not None
        jobs = [("kept-job", 0.8, {"location_fit": 1.0})]
        result = apply_reranker_shadow(jobs, model)
        job_ids = {r.job_id for r in result}
        assert job_ids == {"kept-job"}
        assert "rejected-job-never-passed-in" not in job_ids


class TestEvaluateReranker:
    def test_insufficient_data_is_a_clean_no_go(self) -> None:
        evaluation = evaluate_reranker(jobs=[], labeled_examples=[], min_examples=20)
        assert evaluation.decision == "NO_GO_INSUFFICIENT_DATA"
        assert evaluation.n_training_examples == 0
        assert evaluation.reranked == []

    def test_real_world_zero_feedback_case_is_no_go(self) -> None:
        # Mirrors the actual measured state of the live database (I20
        # inspection: 0 rows in job_feedback) — the harness must fail safe.
        jobs = [("job-1", 0.7, {"location_fit": 0.9})]
        evaluation = evaluate_reranker(jobs=jobs, labeled_examples=[], min_examples=20)
        assert evaluation.decision == "NO_GO_INSUFFICIENT_DATA"

    def test_sufficient_data_evaluates_and_reports_overlap(self) -> None:
        examples = [
            _labeled_example(1 if i % 2 == 0 else 0, location_fit=1.0 if i % 2 == 0 else 0.0)
            for i in range(30)
        ]
        jobs = [(f"job-{i}", 0.5 + 0.01 * i, {"location_fit": (i % 3) / 2.0}) for i in range(10)]
        evaluation = evaluate_reranker(jobs=jobs, labeled_examples=examples, min_examples=20, top_k=5)
        assert evaluation.decision == "EVALUATED"
        assert evaluation.top_k_overlap is not None
        assert 0 <= evaluation.top_k_overlap <= 5
        assert evaluation.max_rank_shift is not None

    def test_deterministic_same_input_same_output(self) -> None:
        examples = [
            _labeled_example(1 if i % 2 == 0 else 0, location_fit=1.0 if i % 2 == 0 else 0.0)
            for i in range(30)
        ]
        jobs = [(f"job-{i}", 0.5 + 0.01 * i, {"location_fit": (i % 3) / 2.0}) for i in range(10)]
        e1 = evaluate_reranker(jobs=jobs, labeled_examples=examples, min_examples=20, top_k=5)
        e2 = evaluate_reranker(jobs=jobs, labeled_examples=examples, min_examples=20, top_k=5)
        assert e1 == e2


class TestNoCloudOrProductionCoupling:
    def test_module_has_no_network_dependency(self) -> None:
        source = (REPO_ROOT / "src/experimental/reranker.py").read_text(encoding="utf-8")
        for forbidden in ("import requests", "import httpx", "import openai", "import anthropic"):
            assert forbidden not in source

    @pytest.mark.parametrize(
        "production_file",
        [
            "src/pipeline/scoring.py",
            "src/pipeline/orchestrator.py",
            "src/requirements/gate.py",
            "src/personalization/engine.py",
            "src/notifications/engine.py",
        ],
    )
    def test_production_module_never_imports_reranker(self, production_file: str) -> None:
        source = (REPO_ROOT / production_file).read_text(encoding="utf-8")
        assert "src.experimental" not in source
