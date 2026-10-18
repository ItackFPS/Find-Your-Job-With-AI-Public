"""I20 — EXPERIMENTAL local personal reranker (OFFLINE / SHADOW EVALUATION ONLY).

Investigates whether a small, local, interpretable reranking layer — built
on top of the SAME per-job factor scores production scoring already
computes (location_fit, domain_relevance, skills_match, seniority_match,
experience_fit, semantic_score) — could improve ordering, using explicit
outcome data (I18's outcome_events, or the existing job_feedback records
personalization already reads) as training signal.

This module is never imported by src.pipeline.orchestrator or any other
production wiring (see src/experimental/__init__.py). By construction it
can only ever operate on the list of jobs a caller already has (which
must already be eligibility-filtered, production-scored jobs — this module
has no way to fetch rejected jobs, so it structurally cannot rescue one).
It never replaces the production score; `apply_reranker_shadow` returns a
*second*, bounded score alongside the untouched baseline for comparison.

Training deliberately avoids any new ML dependency: per-feature weights are
plain Pearson-style correlations between that feature's value and the
binary outcome, clipped to a small bound — the same "clamped-delta on top
of the base score" pattern the (already-shipped) personalization engine
uses, applied to different features. No numpy/scikit-learn is introduced
(see the I20 inspection: neither is currently a project dependency).
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

_DEFAULT_MIN_EXAMPLES = 20
_DEFAULT_MAX_FEATURE_WEIGHT = 0.10
_DEFAULT_MAX_TOTAL_ADJUSTMENT = 0.15

_RERANKER_FEATURES = (
    "location_fit",
    "domain_relevance",
    "skills_match",
    "seniority_match",
    "experience_fit",
    "semantic_score",
)


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Hand-rolled Pearson correlation — no numpy dependency, matching the
    LATER-phase semantic re-evaluation's own approach."""
    n = len(xs)
    if n < 2:
        return 0.0
    mean_x, mean_y = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return float(cov / (var_x * var_y) ** 0.5)


@dataclass(frozen=True)
class RerankerModel:
    feature_weights: dict[str, float]
    n_training_examples: int


def train_simple_reranker(
    labeled_examples: list[dict],
    *,
    min_examples: int = _DEFAULT_MIN_EXAMPLES,
    max_feature_weight: float = _DEFAULT_MAX_FEATURE_WEIGHT,
) -> RerankerModel | None:
    """Train (or refuse to train) a reranker from explicit outcome data.

    ``labeled_examples``: a list of dicts, each ``{"outcome": 0 or 1,
    "factors": {feature_name: value, ...}}`` — "outcome" is 1 for a
    positive signal (interested/applied) and 0 for negative
    (not_interested/rejected/ignored); "factors" is the job's already-
    computed per-job factor scores.

    Returns ``None`` (an explicit NO-GO, never a fabricated model) when
    there are fewer than ``min_examples`` labeled examples — I20's spec is
    explicit that insufficient data must fail safely into DEFERRED, not
    produce output from noise.
    """
    if len(labeled_examples) < min_examples:
        return None

    outcomes = [float(ex["outcome"]) for ex in labeled_examples]
    weights: dict[str, float] = {}
    for feature in _RERANKER_FEATURES:
        values = [float(ex["factors"].get(feature, 0.0)) for ex in labeled_examples]
        correlation = _pearson(values, outcomes)
        weights[feature] = max(-max_feature_weight, min(max_feature_weight, correlation * max_feature_weight))

    return RerankerModel(feature_weights=weights, n_training_examples=len(labeled_examples))


@dataclass(frozen=True)
class RerankedJob:
    job_id: str
    baseline_score: float
    reranked_score: float
    adjustment: float


def apply_reranker_shadow(
    jobs: list[tuple[str, float, dict[str, float]]],
    model: RerankerModel,
    *,
    max_total_adjustment: float = _DEFAULT_MAX_TOTAL_ADJUSTMENT,
) -> list[RerankedJob]:
    """Compute a shadow reranked score for each already-scored job.

    ``jobs``: ``[(job_id, baseline_production_score, factors_dict), ...]``
    — the caller is responsible for this list already being the
    post-eligibility, post-scoring set; this function has no path to any
    other job, so it cannot rescue a job that isn't in it.

    The baseline score is never mutated — each result carries both scores
    plus the (bounded) adjustment applied, so a caller can compare exactly
    what changed. This never becomes the returned/stored production score
    anywhere; it exists purely for offline evaluation.
    """
    results = []
    for job_id, baseline_score, factors in jobs:
        raw_adjustment = sum(
            weight * float(factors.get(feature, 0.0))
            for feature, weight in model.feature_weights.items()
        )
        adjustment = max(-max_total_adjustment, min(max_total_adjustment, raw_adjustment))
        reranked_score = max(0.0, min(1.0, baseline_score + adjustment))
        results.append(RerankedJob(job_id, baseline_score, reranked_score, adjustment))
    return results


@dataclass
class RerankerEvaluation:
    decision: str  # "NO_GO_INSUFFICIENT_DATA" | "EVALUATED"
    n_training_examples: int
    top_k_overlap: int | None = None
    top_k: int = 0
    max_rank_shift: int | None = None
    reranked: list[RerankedJob] = field(default_factory=list)


def evaluate_reranker(
    jobs: list[tuple[str, float, dict[str, float]]],
    labeled_examples: list[dict],
    *,
    min_examples: int = _DEFAULT_MIN_EXAMPLES,
    top_k: int = 20,
) -> RerankerEvaluation:
    """Train (if possible) and evaluate the shadow reranker against the
    existing production (baseline) ordering. Deterministic: the same
    inputs always produce the same evaluation.

    Always returns a decision — either an honest NO-GO (too little
    outcome data to trust a trained model) or a full comparison against
    the untouched baseline ordering.
    """
    model = train_simple_reranker(labeled_examples, min_examples=min_examples)
    if model is None:
        return RerankerEvaluation(
            decision="NO_GO_INSUFFICIENT_DATA",
            n_training_examples=len(labeled_examples),
        )

    reranked = apply_reranker_shadow(jobs, model)

    baseline_order = [r.job_id for r in sorted(reranked, key=lambda r: -r.baseline_score)]
    reranked_order = [r.job_id for r in sorted(reranked, key=lambda r: -r.reranked_score)]

    k = min(top_k, len(baseline_order))
    overlap = len(set(baseline_order[:k]) & set(reranked_order[:k]))

    baseline_rank = {jid: i for i, jid in enumerate(baseline_order)}
    reranked_rank = {jid: i for i, jid in enumerate(reranked_order)}
    max_shift = max(
        (abs(baseline_rank[jid] - reranked_rank[jid]) for jid in baseline_rank), default=0
    )

    return RerankerEvaluation(
        decision="EVALUATED",
        n_training_examples=model.n_training_examples,
        top_k_overlap=overlap,
        top_k=k,
        max_rank_shift=max_shift,
        reranked=reranked,
    )
