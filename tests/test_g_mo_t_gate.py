"""G-MO-T: the temporary multi-occupation gate of 2.0 (ADR 0028).

Six non-private occupation profiles run through the real decision path. Blocking:

* no decision moved against the recorded baseline (0 strong-rule regressions);
* no clear-target offer is rejected by a domain or off-domain rule.

On failure, fix the vocabulary or narrow the stated 2.0 scope. Never change a threshold or re-record
a baseline to make this pass. This is not ADR 0023's L6 criterion, which returns with L6.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from src.evaluation.dataset import load_dataset
from src.evaluation.metrics import rejection_reason
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation
from src.evaluation.store import diff_golden, read_golden, to_golden

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("build_g_mo_t", ROOT / "scripts" / "build_g_mo_t_datasets.py")
assert _spec and _spec.loader
_build = importlib.util.module_from_spec(_spec)
import sys  # noqa: E402

sys.modules["build_g_mo_t"] = _build
_spec.loader.exec_module(_build)

OCCUPATIONS = {o.slug: o for o in _build.OCCUPATIONS}
# A rule that decides whether the job is in the user's field.
DOMAIN_RULES = {"filter:title_keywords"}
# Reported, not gated: a title word such as "Head" is the career-stage rule doing its job.
KNOWN_NON_DOMAIN_REJECTIONS = {"chef-12": "filter:seniority"}


@pytest.fixture(scope="module", params=sorted(OCCUPATIONS))
def evaluated(request):
    occ = OCCUPATIONS[request.param]
    dataset = load_dataset(_build.dataset_dir(occ))
    return occ, dataset, run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))


def test_six_adr_0023_profiles_are_covered():
    assert set(OCCUPATIONS) == {"nurse", "accountant", "warehouse", "sales", "sysadmin", "chef"}


def test_no_decision_moved_against_the_baseline(evaluated):
    occ, _, result = evaluated
    assert diff_golden(read_golden(_build.golden_file(occ)), to_golden(result)) == []


def test_no_clear_target_offer_is_rejected_by_a_domain_rule(evaluated):
    _, dataset, result = evaluated
    wrong = [
        (d.job_id, rejection_reason(d)) for d in result.decisions
        if dataset.labels[d.job_id].category == "clear_target" and rejection_reason(d) in DOMAIN_RULES
    ]
    assert wrong == []


def test_any_other_rejection_of_a_clear_target_is_known(evaluated):
    _, dataset, result = evaluated
    other = {
        d.job_id: rejection_reason(d) for d in result.decisions
        if dataset.labels[d.job_id].category == "clear_target" and d.verdict == "reject"
    }
    assert other.items() <= KNOWN_NON_DOMAIN_REJECTIONS.items()


def test_another_occupations_offers_are_not_accepted(evaluated):
    _, dataset, result = evaluated
    accepted = [d.job_id for d in result.decisions
                if dataset.labels[d.job_id].category == "other_occupation" and d.verdict == "accept"]
    assert accepted == []
