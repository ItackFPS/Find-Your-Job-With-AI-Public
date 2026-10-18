"""An experiment's verdict separates "worth a human's review" from "supported by independent evidence"."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.evaluation.dataset import load_dataset
from src.lab.experiment import MIN_LABELLED, judge
from tests.test_lab import _run, _wrong_and_right

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DS, require_complete_labels=False)


def _sourced(dataset, source_of):
    """The same dataset (same labels and content) with each label's provenance set by ``source_of(job_id)``."""
    labels = {j: lab.model_copy(update={"label_source": source_of(j)}) for j, lab in dataset.labels.items()}
    return dataclasses.replace(dataset, labels=labels)


def _improvement(dataset):
    """A fabricated weak baseline (right rejects flipped to accept) and the real decisions, which fix them."""
    base = _run(dataset, {})
    _, right_reject, _ = _wrong_and_right(dataset, base)
    return _run(dataset, {j: "accept" for j in right_reject}), base, right_reject


def test_all_independent_labels_support_promotion(dataset):
    weak, good, _ = _improvement(dataset)
    out = judge(dataset, weak, good, good)
    assert out["eligible_for_approval"] and out["promotion_evidence"] == "independently_supported"
    assert out["independently_validated"] is True and out["evidence_basis"] == "independent"
    assert out["independent_evidence"]["status"] == "improvement" and "EXPLORATORY" not in out["explanation"]


def test_zero_independent_labels_are_exploratory_only(dataset):
    assisted = _sourced(dataset, lambda _j: "human_assisted")
    weak, good, _ = _improvement(assisted)
    out = judge(assisted, weak, good, good)
    assert out["verdict"] == "improvement" and out["eligible_for_approval"]          # worth a human's look ...
    assert out["promotion_evidence"] == "exploratory_only" and out["independently_validated"] is False  # ... no more
    assert out["evidence_basis"] == "not_independent" and out["independent_evidence"]["n_labelled"] == 0
    assert "EXPLORATORY ONLY, not independently validated" in out["explanation"]


def test_mixed_provenance_counts_only_the_independent_labels(dataset):
    weak, good, fixed_ids = _improvement(dataset)
    # every job the candidate fixes was labelled with assistance; the independent labels see no difference
    mixed = _sourced(dataset, lambda j: "human_assisted" if j in fixed_ids else "human")
    out = judge(mixed, weak, good, good)
    assert out["eligible_for_approval"] and out["evidence_basis"] == "mixed"
    ind = out["independent_evidence"]
    assert ind["fixed"] == ind["broken"] == 0 and ind["status"] == "insufficient_evidence"
    assert out["promotion_evidence"] == "exploratory_only"
    # derived (feedback) labels are not independent human judgements either
    derived = _sourced(dataset, lambda j: "derived" if j in fixed_ids else "human")
    assert judge(derived, weak, good, good)["promotion_evidence"] == "exploratory_only"


def test_too_few_independent_labels_are_insufficient_even_if_they_all_agree(dataset):
    weak, good, fixed_ids = _improvement(dataset)
    keep = set(sorted(fixed_ids)[:MIN_LABELLED - 1])                 # fixes only, but below the evidence floor
    few = _sourced(dataset, lambda j: "human" if j in keep else "human_assisted")
    out = judge(few, weak, good, good)
    ind = out["independent_evidence"]
    assert ind["n_labelled"] < MIN_LABELLED and ind["status"] == "insufficient_evidence" and ind["ci95"] is None
    assert out["promotion_evidence"] == "exploratory_only" and not out["independently_validated"]


def test_an_ineligible_experiment_has_no_promotion_tier(dataset):
    weak, good, _ = _improvement(dataset)
    slow = _run(dataset, {}, slow=3.0, fp="s")
    out = judge(dataset, weak, slow, slow)                         # latency guardrail fails
    assert not out["eligible_for_approval"] and out["promotion_evidence"] == "not_eligible"
    assert out["independently_validated"] is False
