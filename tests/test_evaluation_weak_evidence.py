"""Weak-evidence comparison: the pre-registered AD-2b test, offline (stage L5.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.evaluation import weak_evidence as W
from src.evaluation.heldout import load_split
from src.evaluation.models import Decision, LabelRecord, StrategyRef

SYNTH = "tests/fixtures/evaluation/datasets/synthetic-ai-filter-50"
SCORING = "config/scoring.yaml"


def _label(job_id: str, expected: str) -> LabelRecord:
    return LabelRecord(job_id=job_id, expected=expected, label_source="human", labeler="t",  # type: ignore[arg-type]
                       labeled_at="2026-11-05T00:00:00+00:00")


def _accept(job_id: str, score: float, *, weak: tuple[str, ...] = (), domain: float = 1.0) -> Decision:
    return Decision(job_id=job_id, verdict="accept", stage="passed", score=score,
                    factors={"domain_relevance": domain}, flags=[f"weak:{c}" for c in weak])


def _reject(job_id: str, reason: str, stage: str = "filter") -> Decision:
    return Decision(job_id=job_id, verdict="reject", stage=stage, reasons=[reason])  # type: ignore[arg-type]


def test_candidate_config_flips_only_the_preregistered_checks():
    with W.candidate_config("config") as cand:
        text = (cand / "filters.yaml").read_text(encoding="utf-8")
        section = text[text.index("\nweak_evidence_mode:"):]
        for code in W.CANDIDATE_RANK:
            assert f"  {code}: rank" in section
        for code in ("work_mode", "distance", "contract_type"):
            assert f"  {code}: reject" in section
        assert (cand / "scoring.yaml").read_text(encoding="utf-8") == Path(SCORING).read_text(encoding="utf-8")
    assert not cand.exists()                                    # temporary
    assert "published_age: reject" in Path("config/filters.yaml").read_text(encoding="utf-8")   # source untouched


def test_candidate_config_refuses_a_missing_or_already_ranked_check(tmp_path):
    (tmp_path / "filters.yaml").write_text("weak_evidence_mode:\n  published_age: rank\n", encoding="utf-8")
    with pytest.raises(ValueError), W.candidate_config(tmp_path, ("published_age",)):
        pass
    (tmp_path / "filters.yaml").write_text("filters: {}\n", encoding="utf-8")
    with pytest.raises(ValueError), W.candidate_config(tmp_path, ("published_age",)):
        pass


def test_lanes():
    decisions = [_accept("m", 0.70), _accept("low", 0.40), _reject("r", "filter:location"),
                 _accept("o", 0.70, weak=("published_age",)), _accept("od", 0.70, weak=("seniority",), domain=0.35),
                 _accept("os", 0.50, weak=("seniority",))]
    assert W.lanes(decisions, SCORING) == {"m": W.MAIN, "low": W.BELOW, "r": W.REJECTED, "o": W.OUTSIDE,
                                           "od": W.BELOW, "os": W.BELOW}


def test_compare_counts_relevant_offers_and_precision():
    labels = {j: _label(j, e) for j, e in [("a", "accept"), ("b", "accept"), ("c", "reject"), ("d", "borderline")]}
    base = [_accept("a", 0.70), _reject("b", "filter:published_age"), _accept("c", 0.70), _accept("d", 0.70)]
    cand = [_accept("a", 0.70), _accept("b", 0.70, weak=("published_age",)), _accept("c", 0.70),
            _accept("d", 0.70)]
    out = W.compare(base, cand, labels, SCORING, n_boot=200)
    c1 = out["criterion_1"]
    assert (c1["baseline"], c1["candidate"], c1["candidate_outside"], c1["passes"]) == (1, 2, 1, True)
    assert c1["relevant_by_lane"]["baseline"] == {W.MAIN: 1, W.OUTSIDE: 0, W.BELOW: 0, W.REJECTED: 1}
    c2 = out["criterion_2"]
    assert (c2["baseline"], c2["candidate"]) == (0.5, 0.5)       # borderline left out; main list unchanged
    assert c2["labelled_shown"] == {"baseline": 2, "candidate": 2} and c2["passes"] is True
    assert out["criterion_3"] == {"outside_size": 1, "max": 10, "passes": True}
    assert out["criterion_4"]["passes"] is True and out["criterion_4"]["weak_flagged_acceptances"] == 1


def test_compare_fails_criterion_1_without_a_doubling():
    labels = {"a": _label("a", "accept")}
    out = W.compare([_accept("a", 0.70)], [_accept("a", 0.70)], labels, SCORING, n_boot=50)
    assert out["criterion_1"]["passes"] is False


def test_compare_flags_a_strong_change():
    labels = {"a": _label("a", "accept")}
    accepted_strong = W.compare([_reject("a", "filter:location")], [_accept("a", 0.70)], labels, SCORING,
                                n_boot=50)["criterion_4"]
    assert accepted_strong["passes"] is False and accepted_strong["strong_or_other_changes"] == 1
    moved = W.compare([_reject("a", "filter:location")], [_reject("a", "filter:distance")], labels, SCORING,
                      n_boot=50)["criterion_4"]
    assert moved["passes"] is False and moved["strong_or_other_changes"] == 1


def test_a_weak_first_rejection_caught_by_a_later_check_is_not_a_strong_change():
    labels = {"a": _label("a", "accept")}
    c4 = W.compare([_reject("a", "filter:seniority")],
                   [_reject("a", "Job requires at least 5 years", stage="requirements_gate")], labels, SCORING,
                   n_boot=50)["criterion_4"]
    assert c4 == {"verdict_changes": 0, "weak_flagged_acceptances": 0, "rejected_by_a_later_check": 1,
                  "strong_or_other_changes": 0, "passes": True}


def test_precision_ci_is_none_without_a_main_list():
    labels = {"a": _label("a", "accept")}
    c2 = W.compare([_reject("a", "filter:location")], [_reject("a", "filter:location")], labels, SCORING,
                   n_boot=50)["criterion_2"]
    assert c2["diff_ci95"] is None and c2["passes"] is None


def test_runner_default_config_has_no_weak_flags():
    from src.evaluation.runner import _parse_as_of, frozen_clock, run_decisions

    ds = load_split(SYNTH)
    with frozen_clock(_parse_as_of(ds.manifest.as_of)):
        decisions, _, _, extras = run_decisions(ds, StrategyRef(), ds.build_jobs())
    assert extras["weak_flags"] == {}
    assert not any(f.startswith("weak:") for d in decisions for f in d.flags)


def test_run_on_the_synthetic_benchmark():
    out = W.run(load_split(SYNTH), StrategyRef())
    assert out["preregistration"] == W.PREREGISTRATION and out["split"]["name"] == "all"
    assert out["criterion_3"]["passes"] and out["criterion_4"]["passes"]
    assert out["criterion_4"]["verdict_changes"] == out["criterion_4"]["weak_flagged_acceptances"] > 0
    lanes = out["lanes"]
    assert sum(lanes["baseline"].values()) == sum(lanes["candidate"].values()) == out["jobs"]
    assert lanes["candidate"][W.MAIN] == lanes["baseline"][W.MAIN]        # flagged offers never enter main
    assert out["criterion_2"]["candidate"] == out["criterion_2"]["baseline"]


def test_cli_refuses_the_heldout_split(capsys):
    from src.evaluation.__main__ import main

    with pytest.raises(SystemExit):
        main(["weak-evidence", SYNTH, "--split", "heldout"])
    assert "invalid choice" in capsys.readouterr().err
