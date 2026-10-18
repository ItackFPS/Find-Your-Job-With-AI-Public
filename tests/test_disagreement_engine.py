"""Disagreement / confidence / abstention engine."""

from __future__ import annotations

import random

import pytest
from pydantic import ValidationError

from src.decisions.disagreement import (
    DisagreementConfig, Signals, assess, evidence_stats, load_disagreement_config,
)
from src.decisions.models import EvidenceItem, SourceRef

CFG = DisagreementConfig()


def _acc(**kw) -> Signals:
    return Signals(outcome="accepted", **kw)


def test_agreeing_signals_are_accepted_with_high_confidence():
    a = assess(_acc(rules_score=0.82, semantic=0.80, phase2=0.85, completeness=1.0), CFG)
    assert a.state == "accept" and a.reasons == [] and a.confidence > 0.9 and a.calibrated is False


def test_the_issue_example_rules_embedding_llm_disagree_goes_to_review():
    """rules 82 / embedding 91 / LLM 87 agree; adding a clearly lower signal (61) must trigger review."""
    ok = assess(_acc(rules_score=0.82, semantic=0.91, phase2=0.87, completeness=1.0), CFG)
    assert ok.state == "accept"
    bad = assess(_acc(rules_score=0.82, semantic=0.91, phase2=0.55, completeness=1.0), CFG)
    assert bad.state == "review" and "signals disagree" in bad.reasons[0]
    assert bad.spread == pytest.approx(0.36) and bad.conflicts and bad.confidence < ok.confidence


def test_single_signal_cannot_show_agreement_so_confidence_is_capped():
    only = assess(_acc(rules_score=0.9, completeness=1.0), CFG)
    assert only.state == "accept" and only.spread is None and only.confidence == pytest.approx(0.7)


@pytest.mark.parametrize("kw,fragment", [
    (dict(unknown_density=0.6), "UNKNOWN"),
    (dict(completeness=0.3), "relevant dimensions"),
    (dict(conflicts=["remote vs on-site"]), "contradiction"),
    (dict(eligibility="REJECT", eligibility_reject_overridden=True), "LLM eligibility check said REJECT"),
])
def test_each_uncertainty_source_triggers_review(kw, fragment):
    a = assess(_acc(rules_score=0.8, semantic=0.8, **kw), CFG)
    assert a.state == "review" and any(fragment in r for r in a.reasons)


def test_unknown_dimensions_are_reported_not_hidden():
    a = assess(_acc(rules_score=0.8, unknown_density=0.6, unknown_dimensions=("salary", "experience")), CFG)
    assert a.unknowns == ["salary", "experience"]


def test_solid_rejection_stands_and_llm_only_rejection_goes_to_review():
    assert assess(Signals(outcome="rejected", reject_sources=("heuristic",)), CFG).state == "reject"
    assert assess(Signals(outcome="rejected", reject_sources=("rule",)), CFG).state == "reject"
    llm = assess(Signals(outcome="rejected", reject_sources=("llm",)), CFG)
    assert llm.state == "review" and "only on LLM evidence" in llm.reasons[0]
    assert assess(Signals(outcome="rejected", reject_sources=("llm",)),
                  DisagreementConfig(review_llm_only_rejection=False)).state == "reject"


def test_uncertainty_is_never_turned_into_a_rejection_and_a_rejection_is_never_turned_into_an_accept():
    """Property test over random signals."""
    rng = random.Random(7)
    for _ in range(2000):
        s = _acc(
            rules_score=rng.choice([None, rng.random()]), semantic=rng.choice([None, rng.random()]),
            phase2=rng.choice([None, rng.random()]), unknown_density=rng.random(), completeness=rng.random(),
            conflicts=["c"] * rng.randint(0, 3), eligibility_reject_overridden=rng.random() < 0.3)
        a = assess(s, CFG)
        assert a.state in ("accept", "review") and 0.0 <= a.confidence <= 1.0
        r = assess(Signals(outcome="rejected", conflicts=["c"] * rng.randint(0, 3),
                           reject_sources=tuple(rng.sample(["heuristic", "rule", "llm", "metadata"], rng.randint(0, 3)))), CFG)
        assert r.state in ("reject", "review") and 0.0 <= r.confidence <= 1.0


def test_confidence_decreases_with_each_source_of_doubt():
    base = assess(_acc(rules_score=0.8, semantic=0.8, completeness=1.0), CFG).confidence
    assert assess(_acc(rules_score=0.8, semantic=0.8, completeness=0.5), CFG).confidence < base
    assert assess(_acc(rules_score=0.8, semantic=0.8, completeness=1.0, unknown_density=0.4), CFG).confidence < base
    assert assess(_acc(rules_score=0.8, semantic=0.8, completeness=1.0, conflicts=["x"]), CFG).confidence < base
    assert assess(_acc(rules_score=0.8, semantic=0.5, completeness=1.0), CFG).confidence < base


def _item(dim, state):
    return EvidenceItem(dimension=dim, polarity={"satisfied": "positive", "violated": "negative", "unknown": "unknown"}[state],
                        state=state, source=SourceRef(stage="filter", rule_id="r"))


def test_evidence_stats():
    assert evidence_stats([]) == (0.0, None, [])
    items = [_item("location", "satisfied"), _item("salary", "unknown"), _item("experience", "unknown"),
             _item("experience", "satisfied"), _item("skills", "violated")]
    density, completeness, unknown_dims = evidence_stats(items)
    assert density == pytest.approx(0.4) and completeness == pytest.approx(3 / 4) and unknown_dims == ["salary"]


def test_config_defaults_validation_and_resilience(tmp_path):
    assert load_disagreement_config(tmp_path).mode == "shadow"                 # shadow is the default
    (tmp_path / "disagreement.yaml").write_text("mode: active\nspread_review: 0.2\n")
    c = load_disagreement_config(tmp_path)
    assert c.mode == "active" and c.spread_review == 0.2
    (tmp_path / "disagreement.yaml").write_text("mode: off\n")                # YAML turns bare `off` into False
    assert load_disagreement_config(tmp_path).mode == "off"
    (tmp_path / "disagreement.yaml").write_text("mode: 'off'\n")
    assert load_disagreement_config(tmp_path).mode == "off"
    for bad in ("mode: yolo\n", "mode: true\n", "spread_review: 3\n", "nonsense: 1\n", "mode: [\n"):
        (tmp_path / "disagreement.yaml").write_text(bad)
        assert load_disagreement_config(tmp_path) == DisagreementConfig()
    with pytest.raises(ValidationError):
        DisagreementConfig(mode="x")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.5])
def test_unusable_signals_are_ignored_and_reported_never_crash(bad):
    a = assess(_acc(rules_score=0.8, semantic=bad, completeness=1.0), CFG)
    assert a.invalid_signals == ["semantic"] and a.spread is None
    assert 0.0 <= a.confidence <= 1.0 and "unusable signal" in " ".join(a.reasons)


def test_random_signals_keep_the_invariants():
    rng = random.Random(7)
    for _ in range(300):
        outcome = rng.choice(["accepted", "rejected"])
        s = Signals(
            outcome=outcome,
            rules_score=rng.choice([None, rng.random()]), semantic=rng.choice([None, rng.random()]),
            phase2=rng.choice([None, rng.random()]), completeness=rng.choice([None, rng.random()]),
            unknown_density=rng.random(),
            reject_sources=rng.choice([[], ["hard_filter"], ["llm"]]) if outcome == "rejected" else [],
        )
        a = assess(s, CFG)
        assert 0.0 <= a.confidence <= 1.0
        assert a.state in ({"accept", "review"} if outcome == "accepted" else {"reject", "review"})


def test_rejected_without_any_evidence_goes_to_review_with_that_reason():
    a = assess(Signals(outcome="rejected"), CFG)
    assert a.state == "review" and "no recorded evidence" in a.reasons[0]
