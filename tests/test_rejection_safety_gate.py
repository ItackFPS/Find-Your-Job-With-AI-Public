"""CI safety gate: no accept-labelled job is rejected, end to end.

Runs the whole deterministic decision path (hard filter, requirements gate,
scoring) through the evaluation harness on the synthetic benchmark and fails
on any unsafe rejection — an accept-labelled job whose verdict is ``reject``.

Scope, deliberately narrow (ADR 0007: the harness is not a tuning target):

- it asserts the safety invariant only, never a precision/recall target;
- the golden file (tests/test_evaluation_runner.py) is a change detector, not
  this gate: updating the golden cannot hide an unsafe rejection here;
- the accept-labelled set is pinned below, so making the gate pass by
  relabelling a job needs an explicit, reviewed edit of this file (the
  dataset manifest's labels_sha256 already refuses a silent label edit);
- passing it says nothing about real postings: the dataset is author-written
  (``kind: synthetic``, 22 accept labels). Real-world evidence comes from
  labelled exports and the rejection audit sample, not from this test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"

# Pinned on purpose: shrinking this set is how a gate gets weakened. Change it
# only together with a justified label change in the dataset (new manifest).
ACCEPT_LABELLED = frozenset({
    "bm01", "bm02", "bm03", "bm04", "bm05", "bm06", "bm07", "bm08", "bm09",
    "bm10", "bm11", "bm13", "bm14", "bm16", "bm17", "bm18", "bm19", "bm20",
    "bm46", "bm47", "bm48", "bm49",
})


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DS)


@pytest.fixture(scope="module")
def result(dataset):
    return run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))


def test_accept_labelled_set_is_pinned(dataset):
    accept = {k for k, v in dataset.labels.items() if v.expected == "accept"}
    assert accept == ACCEPT_LABELLED, (
        "the accept-labelled set changed: justify the label change in the PR and "
        f"update ACCEPT_LABELLED. added={sorted(accept - ACCEPT_LABELLED)} "
        f"removed={sorted(ACCEPT_LABELLED - accept)}"
    )


def test_no_unsafe_rejection_end_to_end(dataset, result):
    unsafe = [
        d for d in result.decisions
        if d.job_id in ACCEPT_LABELLED and d.verdict == "reject"
    ]
    assert not unsafe, "UNSAFE REJECTION (release blocker):\n" + "\n".join(
        f"  {d.job_id} [{d.stage}] {'; '.join(d.reasons)}" for d in unsafe
    )
    assert result.metrics["overall"]["unsafe_rejection_rate"] == 0.0


def test_every_rejection_states_its_reason(result):
    """A rejection with no recorded reason cannot be audited or reviewed."""
    silent = [d.job_id for d in result.decisions if d.verdict == "reject" and not d.reasons]
    assert not silent, f"rejections without a recorded reason: {silent}"
