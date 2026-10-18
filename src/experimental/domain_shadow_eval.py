"""I17 — EXPERIMENTAL offline evaluation harness for the shadow domain classifier.

Pure, read-only comparison logic: given a list of samples (each carrying the
job's title/description, production's already-computed domain_relevance
score, and optionally a hand-labeled ground_truth), measures how often the
shadow classifier agrees with production and — where ground truth exists —
its precision/recall/false-positive/false-negative counts.

Never touches any database and never influences anything: this module only
consumes data callers already have and returns a metrics summary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.experimental.domain_classifier import (
    ExperimentalDomainClassifier,
    production_label_from_score,
)


@dataclass
class ShadowEvaluationMetrics:
    n_samples: int
    n_labeled: int
    disagreement_count: int
    disagreement_rate: float
    true_positives: int = 0
    false_positives: int = 0
    true_negatives: int = 0
    false_negatives: int = 0
    precision: float | None = None
    recall: float | None = None


def evaluate_shadow_classifier(
    samples: list[dict[str, Any]],
    classifier: ExperimentalDomainClassifier,
) -> ShadowEvaluationMetrics:
    """Evaluate the shadow classifier against a batch of samples.

    Each sample is a dict with keys:
      - "title", "description": job text
      - "production_domain_relevance": float, production's own score for this job
      - "ground_truth" (optional): "on_domain" | "off_domain" — only jobs
        carrying this key contribute to precision/recall.

    Deterministic: the same samples + classifier always produce the same
    metrics (the classifier itself is deterministic given a deterministic
    embedding service).
    """
    n = len(samples)
    disagreements = 0
    tp = fp = tn = fn = 0
    n_labeled = 0

    for sample in samples:
        result = classifier.classify(sample.get("title"), sample.get("description"))
        prod_label = production_label_from_score(sample["production_domain_relevance"])

        if result.label != "uncertain" and prod_label != "uncertain" and result.label != prod_label:
            disagreements += 1

        ground_truth = sample.get("ground_truth")
        if ground_truth in ("on_domain", "off_domain"):
            n_labeled += 1
            predicted_positive = result.label == "on_domain"
            actual_positive = ground_truth == "on_domain"
            if predicted_positive and actual_positive:
                tp += 1
            elif predicted_positive and not actual_positive:
                fp += 1
            elif not predicted_positive and actual_positive:
                fn += 1
            else:
                tn += 1

    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None

    return ShadowEvaluationMetrics(
        n_samples=n,
        n_labeled=n_labeled,
        disagreement_count=disagreements,
        disagreement_rate=(disagreements / n) if n else 0.0,
        true_positives=tp,
        false_positives=fp,
        true_negatives=tn,
        false_negatives=fn,
        precision=precision,
        recall=recall,
    )
