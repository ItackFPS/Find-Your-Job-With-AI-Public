"""Experiment Lab (P5): bundles, verdicts, guardrails, human-approved promotion, isolation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from src.evaluation.dataset import load_dataset
from src.evaluation.models import Decision, RunResult, StrategyRef
from src.evaluation.runner import run_evaluation
from src.lab.bundle import BundleError, bundle_hash, create_bundle, verify_bundle
from src.lab.experiment import judge, paired_bootstrap, run_experiment
from src.lab.promotion import PromotionError, approve, current_pointer, rollback
from src.lab.store import LabStore
from tests.test_evaluation_isolation import SRC, _package_of, imported_names

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


def test_production_never_imports_the_lab():
    bad = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if p.relative_to(SRC).parts[0] != "lab"
           and any(n == "src.lab" or n.startswith("src.lab.")
                   for n in imported_names(p.read_text(encoding="utf-8"), _package_of(p)))]
    assert not bad


# -- bundles ---------------------------------------------------------------------------------

def test_bundle_is_content_addressed_immutable_and_data_only(tmp_path):
    h1, p1 = create_bundle(ROOT / "config", {"scoring.yaml": {"scoring": {"weights": {"skills": 0.31}}}}, tmp_path)
    h2, p2 = create_bundle(ROOT / "config", {"scoring.yaml": {"scoring": {"weights": {"skills": 0.31}}}}, tmp_path)
    h3, _ = create_bundle(ROOT / "config", {}, tmp_path)
    assert (h1, p1) == (h2, p2) and h3 != h1
    assert bundle_hash(p1) == h1                # the directory name IS the content hash
    verify_bundle(p1, h1)
    (p1 / "scoring.yaml").chmod(0o644)
    (p1 / "scoring.yaml").write_text("tampered: true\n")
    with pytest.raises(BundleError, match="modified after creation"):
        verify_bundle(p1, h1)
    with pytest.raises(BundleError, match="only target"):
        create_bundle(ROOT / "config", {"profile.yaml": {"a": 1}}, tmp_path)


def test_overrides_deep_merge_without_dropping_siblings(tmp_path):
    base = yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text())
    key = next(iter(base["scoring"]))
    _, p = create_bundle(ROOT / "config", {"scoring.yaml": {"scoring": {"brand_new_key": 1}}}, tmp_path)
    merged = yaml.safe_load((p / "scoring.yaml").read_text())
    assert merged["scoring"]["brand_new_key"] == 1 and key in merged["scoring"]


# -- verdicts --------------------------------------------------------------------------------

def _run(dataset, verdicts: dict[str, str], slow=1.0, fp="fp") -> RunResult:
    """Fabricated decisions with fixed timings (``slow`` × per stage, same workload): no wall-clock noise."""
    base = run_evaluation(dataset, StrategyRef(config_dir=str(ROOT / "config")))
    decs = [Decision(job_id=d.job_id, verdict=verdicts.get(d.job_id, d.verdict), stage=d.stage) for d in base.decisions]
    timings = {"stage_seconds": {"filter": 0.02 * slow, "requirements_gate": 0.02 * slow, "scoring": 0.02 * slow},
               "stage_jobs": {"filter": 50, "requirements_gate": 30, "scoring": 25}, "per_job_ms_mean": 1.0 * slow}
    return base.model_copy(update={"decisions": decs, "timings": timings,
                                   "strategy": {**base.strategy, "fingerprint": fp}})


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DS, require_complete_labels=False)


def _wrong_and_right(dataset, base: RunResult):
    labels = dataset.labels
    wrong = [d.job_id for d in base.decisions if labels[d.job_id].expected != "borderline"
             and (d.verdict == "accept") != (labels[d.job_id].expected == "accept")]
    right_reject = [d.job_id for d in base.decisions if labels[d.job_id].expected == "reject" and d.verdict == "reject"]
    right_accept = [d.job_id for d in base.decisions if labels[d.job_id].expected == "accept" and d.verdict == "accept"]
    return wrong, right_reject, right_accept


def test_identical_candidate_is_not_an_improvement(dataset):
    base = _run(dataset, {}, fp="a")
    out = judge(dataset, base, _run(dataset, {}, fp="b"), _run(dataset, {}, fp="b"))
    assert out["verdict"] == "insufficient_evidence" and not out["eligible_for_approval"]
    assert out["fixed"] == out["broken"] == 0


def test_new_unsafe_rejection_fails_the_guardrail_and_forces_regression(dataset):
    base = _run(dataset, {})
    _, _, right_accept = _wrong_and_right(dataset, base)
    cand = _run(dataset, {right_accept[0]: "reject"}, fp="c")
    out = judge(dataset, base, cand, cand)
    assert out["guardrails"]["no_new_unsafe_rejection"]["passed"] is False
    assert not out["eligible_for_approval"] and out["verdict"] in ("regression", "insufficient_evidence")


def test_clear_improvement_needs_ci_above_zero_and_green_guardrails(dataset):
    base = _run(dataset, {})
    wrong, right_reject, _ = _wrong_and_right(dataset, base)
    # synthetic wrongness is tiny: fabricate a weak baseline (flip right rejects to accept) so the candidate fixes many
    weak = _run(dataset, {j: "accept" for j in right_reject})
    out = judge(dataset, weak, base, base)
    assert out["fixed"] >= 5 and out["broken"] == 0
    assert out["verdict"] == "improvement" and out["eligible_for_approval"] is True
    lo, hi = out["paired_accuracy_diff_ci95"]
    assert 0 < lo <= hi


def test_irreproducible_or_slow_candidate_is_blocked(dataset):
    weak = _run(dataset, {j.job_id: "accept" for j in _run(dataset, {}).decisions
                          if dataset.labels[j.job_id].expected == "reject"})
    good = _run(dataset, {}, fp="g")
    flaky = _run(dataset, {next(d.job_id for d in good.decisions if d.verdict == "accept"): "reject"}, fp="g")
    assert judge(dataset, weak, good, flaky)["guardrails"]["reproducible"]["passed"] is False
    slow = _run(dataset, {}, slow=3.0, fp="g")
    out = judge(dataset, weak, slow, slow)
    assert out["guardrails"]["latency"]["passed"] is False and not out["eligible_for_approval"]


def test_paired_bootstrap_is_seeded_and_ordered():
    d = [1] * 12 + [0] * 30 + [-1] * 2
    assert paired_bootstrap(d) == paired_bootstrap(d) and paired_bootstrap(d)[0] < paired_bootstrap(d)[1]


def test_baseline_replayed_through_the_lab_reproduces_the_plain_evaluation(dataset, tmp_path):
    """P5 acceptance: an identical-config candidate yields exactly the baseline's decisions."""
    _, path = create_bundle(ROOT / "config", {}, tmp_path)
    res = run_experiment(dataset, StrategyRef(config_dir=str(ROOT / "config")), path)
    assert res["fixed"] == res["broken"] == 0 and res["guardrails"]["reproducible"]["passed"]
    assert res["guardrails"]["no_new_unsafe_rejection"]["passed"]
    assert res["verdict"] == "insufficient_evidence" and res["eligible_for_approval"] is False


# -- promotion -------------------------------------------------------------------------------

@pytest.fixture()
def lab(tmp_path):
    s = LabStore(tmp_path / "lab.db")
    yield s
    s.close()


def _candidate(lab, tmp_path, verdict="improvement", eligible=True):
    hid = lab.add_hypothesis("raise skills weight", "because")
    h, p = create_bundle(ROOT / "config", {"scoring.yaml": {"scoring": {"x": len(verdict)}}}, tmp_path / "b")
    cid = lab.add_candidate(hid, h, str(p), {})
    ds = {"dataset_id": "d", "jobs_sha256": "a", "labels_sha256": "b"}
    eid = lab.add_experiment(cid, ds, {"baseline_fingerprint": "x", "candidate_fingerprint": "y",
                                       "verdict": verdict, "eligible_for_approval": eligible})
    return cid, eid, p


def test_approval_is_refused_unless_named_and_untampered(lab, tmp_path, dataset):
    jobs = tmp_path / "jobs.db"
    sqlite3.connect(jobs).close()
    _, eid, p = _candidate(lab, tmp_path)
    with pytest.raises(PromotionError, match="named approver"):
        approve(lab, jobs, eid, dataset=dataset, approved_by=" ", reason="r")
    (p / "scoring.yaml").chmod(0o644)
    (p / "scoring.yaml").write_text("tampered: 1\n")
    with pytest.raises((BundleError, PromotionError), match="modified|invalid"):
        approve(lab, jobs, eid, dataset=dataset, approved_by="me", reason="r", config_dir=ROOT / "config")
    assert current_pointer(jobs) is None
    # eligibility, approve/rollback history and forged records: tests/test_lab_promotion_authority.py


def test_lab_never_touches_jobs_db_outside_promotion(lab, tmp_path):
    jobs = tmp_path / "jobs.db"
    sqlite3.connect(jobs).close()
    before = (jobs.stat().st_size, jobs.stat().st_mtime_ns)
    _candidate(lab, tmp_path)
    assert (jobs.stat().st_size, jobs.stat().st_mtime_ns) == before


# -- review fixes ----------------------------------------------------------------------------

def test_rollback_without_any_approval_is_a_clean_error(tmp_path):
    jobs = tmp_path / "jobs.db"
    sqlite3.connect(jobs).close()
    with pytest.raises(PromotionError, match="no approval"):
        rollback(jobs, "p", approved_by="a", reason="r")


def test_tampered_bundle_is_not_silently_reused_or_replayed(dataset, tmp_path):
    _, p = create_bundle(ROOT / "config", {}, tmp_path)
    (p / "scoring.yaml").chmod(0o644)
    (p / "scoring.yaml").write_text("x: 1\n")
    with pytest.raises(BundleError):
        create_bundle(ROOT / "config", {}, tmp_path)
    with pytest.raises(BundleError):
        run_experiment(dataset, StrategyRef(config_dir=str(ROOT / "config")), p)


def test_experiment_refuses_profile_less_or_insufficient_datasets(dataset, tmp_path):
    from dataclasses import replace

    _, p = create_bundle(ROOT / "config", {}, tmp_path)
    no_profile = replace(dataset, path=tmp_path)      # no profile.yaml there
    with pytest.raises(ValueError, match="pins no profile"):
        run_experiment(no_profile, StrategyRef(config_dir=str(ROOT / "config")), p)
    flagged = replace(dataset, manifest=dataset.manifest.model_copy(update={"notes": "status=insufficient_evidence"}))
    with pytest.raises(ValueError, match="insufficient_evidence"):
        run_experiment(flagged, StrategyRef(config_dir=str(ROOT / "config")), p)


def test_cli_reports_errors_instead_of_tracebacks(tmp_path, capsys, monkeypatch):
    import src.lab.__main__ as cli

    monkeypatch.setattr(cli, "_not_a_present_human", lambda: None)
    monkeypatch.setattr(cli.secrets, "token_hex", lambda _n: "c0de00")
    monkeypatch.setattr("builtins.input", lambda _prompt: "c0de00")
    rc = cli.main(["--lab-db", str(tmp_path / "l.db"), "--jobs-db", str(tmp_path / "none.db"),
                   "rollback", "p", "--by", "a", "--reason", "r"])
    assert rc == 1 and "error:" in capsys.readouterr().err


def test_every_verdict_carries_a_side_by_side_comparison_with_its_limits(dataset):
    base = _run(dataset, {})
    _, _, right_accept = _wrong_and_right(dataset, base)
    cand = _run(dataset, {right_accept[0]: "reject"}, fp="c")
    cmp_ = judge(dataset, base, cand, cand)["comparison"]
    b, c, d = cmp_["baseline"], cmp_["candidate"], cmp_["delta"]
    assert c["fn"] == b["fn"] + 1 and c["tp"] == b["tp"] - 1 and d["fn"] == 1
    assert c["unsafe_rejection_rate"] > b["unsafe_rejection_rate"] and d["unsafe_rejection_rate"] > 0
    assert d["recall"] < 0 and c["accepted"] == b["accepted"] - 1
    assert cmp_["label_provenance"] and cmp_["accept_labels"] == sum(
        1 for lab in dataset.labels.values() if lab.expected == "accept")
    assert any("accept label" in x for x in cmp_["limitations"])          # the synthetic set has few accepts


def test_the_verdict_says_when_discordant_pairs_rest_on_non_independent_labels(dataset):
    import dataclasses

    base = _run(dataset, {})
    _, right_reject, _ = _wrong_and_right(dataset, base)
    weak = _run(dataset, {j: "accept" for j in right_reject})
    assert "not independent" not in judge(dataset, weak, base, base)["explanation"]     # all `human`
    assisted = dataclasses.replace(dataset, labels={
        k: v.model_copy(update={"label_source": "human_assisted"}) for k, v in dataset.labels.items()})
    out = judge(assisted, weak, base, base)
    n = out["fixed"] + out["broken"]
    assert f"{n} of {n} discordant pair(s) rest on labels that are not independent" in out["explanation"]
    assert out["discordant_by_provenance"] == {"human_assisted": n}
    assert "only `human` labels are independent" in " ".join(out["comparison"]["limitations"])
