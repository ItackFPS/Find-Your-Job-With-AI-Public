"""``scoring.explain_score`` must equal the production score it explains; evidence mirrors it."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.decisions import evidence as ev
from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import frozen_clock, run_decisions
from src.models.job import Job
from src.pipeline.filter import load_filters, load_profile
from src.pipeline.scoring import (
    _apply_weights, _compute_factors, _is_top_preferred_location, build_scoring_config, explain_score,
    load_scoring_config, score_job, score_jobs_with_breakdown,
)
from src.requirements.models import GateResult

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


@pytest.fixture(scope="module")
def world():
    ds = load_dataset(DS)
    profile = load_profile(str(DS / "profile.yaml"))
    cfg = build_scoring_config(
        load_scoring_config(str(ROOT / "config" / "scoring.yaml")), profile,
        load_filters(str(ROOT / "config" / "filters.yaml")), esco_synonyms={})
    import datetime as dt

    with frozen_clock(dt.datetime.fromisoformat(ds.manifest.as_of)):
        decisions, _t, _l, extras = run_decisions(ds, StrategyRef(config_dir=str(ROOT / "config")), ds.build_jobs())
    accepted = [j for j in extras["passing_jobs"] if extras["gate_results"][j.id].passes]
    scored = {j.id: (s, f) for j, s, f in score_jobs_with_breakdown(accepted, cfg, gate_results=extras["gate_results"])}
    return {"cfg": cfg, "jobs": accepted, "gate": extras["gate_results"], "scored": scored}


def test_explanation_equals_the_production_score_for_every_accepted_job(world):
    assert len(world["jobs"]) > 10
    for job in world["jobs"]:
        gate = world["gate"][job.id]
        e = explain_score(job, world["cfg"], gate)
        prod_score, prod_factors = world["scored"][job.id]
        assert e.final == prod_score == score_job(job, world["cfg"], gate)
        assert e.factors == prod_factors == _compute_factors(job, world["cfg"], gate)
        is_top = _is_top_preferred_location(job.location, job.countries, job.work_mode, job.remote, job.department, world["cfg"])
        assert e.composite == _apply_weights(e.factors, world["cfg"], is_top_preferred_location=is_top)
        # passing precomputed factors changes nothing
        assert explain_score(job, world["cfg"], gate, factors=prod_factors) == e


def _cfg(**scoring):
    return {"scoring": {
        "location_fit": {"weight": 0.3, "location_preferences": [
            {"type": "department", "value": "83", "status": "preferred", "label": None},
            {"type": "department", "value": "69", "status": "acceptable", "label": None}], "preferred_countries": ["France"]},
        "domain_relevance": {"weight": 0.25, "domain_title_keywords": ["python"], "off_domain_keywords": ["comptable"]},
        "skills_match": {"weight": 0.2, "target_skills": ["python"]},
        "seniority_match": {"weight": 0.15},
        "experience_fit": {"weight": 0.1},
        "gates": {"domain_cap": 0.4, "seniority_multiplier": 0.55, "top_location_bonus": 0.08},
        "language": {"penalty_required": 0.15}, **scoring}}


def test_decomposition_is_internally_consistent_and_matches_the_formula():
    job = Job(id="j", title="Développeur Python junior", description="python " * 30, department="83",
              location="Toulon", countries=["France"])
    e = explain_score(job, _cfg())
    assert e.location_match == {"type": "department", "value": "83", "status": "preferred", "priority": 0, "label": None}
    assert e.top_location_bonus == 0.08 and not e.domain_gate_fired and not e.seniority_gate_fired
    expected = min(max(sum(e.factors[k] * e.weights[k] for k in e.factors) / sum(e.weights.values()) + 0.08, 0.0), 1.0)
    assert e.weighted_base is not None and e.composite == pytest.approx(expected)
    assert e.final == e.composite and all(v == 0 for v in e.penalties.values())


def test_domain_and_seniority_gates_and_penalties_are_reported():
    off = Job(id="o", title="Comptable senior", description="x" * 300, location="Paris", countries=["France"])
    e = explain_score(off, _cfg())
    assert e.domain_gate_fired and e.domain_cap == 0.4 and e.composite <= 0.4
    assert not e.domain_cap_applied                    # the weighted base (0.33) was already under the 0.4 cap
    boosted = _cfg()
    boosted["scoring"]["location_fit"]["weight"] = 5.0   # a dominant, perfect location would otherwise lift it
    top = Job(id="t", title="Comptable senior", description="x" * 300, department="83", location="Toulon", countries=["France"])
    e_top = explain_score(top, boosted)
    assert e_top.domain_gate_fired and e_top.domain_cap_applied and e_top.pre_gate_score > 0.4 and e_top.composite == 0.4
    gate = GateResult(passes=True, education_penalty=0.2, skill_penalty=0.4)
    e2 = explain_score(Job(id="p", title="Python dev", description="python " * 30, countries=["France"]), _cfg(), gate)
    assert e2.penalties["education"] == 0.2 and e2.penalties["skill"] == 0.4
    assert e2.final == pytest.approx(e2.composite * 0.8 * 0.6)


def test_zero_weights_give_the_neutral_composite_without_claiming_gates():
    cfg = _cfg()
    for k in ("location_fit", "domain_relevance", "skills_match", "seniority_match", "experience_fit"):
        cfg["scoring"][k]["weight"] = 0.0
    e = explain_score(Job(id="z", title="Comptable senior", countries=["France"]), cfg)   # would trip the domain gate
    assert e.weighted_base is None and e.composite == 0.5 and e.top_location_bonus == 0.0
    assert not e.domain_gate_fired and not e.seniority_gate_fired and e.pre_gate_score is None
    assert not [i for i in ev.score_items(e) if ".gate." in i.source.rule_id]


def test_a_gate_that_changes_nothing_says_so():
    cfg = _cfg()
    cfg["scoring"]["gates"]["domain_cap"] = 0.99                  # cap above the composite: no effect
    e = explain_score(Job(id="o", title="Comptable", description="x" * 300, countries=["France"]), cfg)
    assert e.domain_gate_fired and not e.domain_cap_applied
    item = next(i for i in ev.score_items(e) if i.source.rule_id == "score.gate.domain")
    assert "no change" in item.message


def test_seniority_gate_and_language_penalty_are_reported_and_equal_production():
    job = Job(id="s", title="Senior Python Engineer", description="python " * 20 + " Anglais courant et allemand courant obligatoire. ",
              countries=["France"])
    cfg = _cfg()
    cfg["scoring"]["seniority_match"]["preferred_level_keywords"] = ["junior"]
    e = explain_score(job, cfg)
    assert e.final == score_job(job, cfg)
    if e.seniority_gate_fired:
        assert e.seniority_multiplier == 0.55
        assert any(i.source.rule_id == "score.gate.seniority" for i in ev.score_items(e))
    lang = Job(id="l", title="Python dev", description="python " * 20 + " Allemand courant obligatoire, exigé. ", countries=["France"])
    el = explain_score(lang, cfg)
    assert el.final == score_job(lang, cfg)
    assert (el.penalties["language"] > 0) == any(i.source.rule_id == "score.penalty.language" for i in ev.score_items(el))


def test_score_items_cover_factors_gates_bonus_and_penalties(world):
    job = Job(id="j", title="Développeur Python junior", description="python " * 30, department="83", countries=["France"])
    items = ev.score_items(explain_score(job, _cfg(), GateResult(passes=True, experience_penalty=0.2)))
    rule_ids = {i.source.rule_id for i in items}
    assert {f"score.{f}" for f in ev.FACTOR_DIMENSION} <= rule_ids
    assert "score.top_location_bonus" in rule_ids and "score.penalty.experience" in rule_ids
    factor_items = [i for i in items if i.source.rule_id in {f"score.{f}" for f in ev.FACTOR_DIMENSION}]
    assert sum(i.weight for i in factor_items) == pytest.approx(1.0, abs=1e-3)
    assert all(i.weight is None for i in items if i not in factor_items)     # weight only ever means "factor share"
    assert next(i for i in factor_items if i.dimension == "location").message.endswith("(preferred, priority 0)")


@pytest.mark.parametrize("value,state", [(1.0, "satisfied"), (0.7, "satisfied"), (0.65, "unknown"), (0.6, "unknown"),
                                         (0.5, "unknown"), (0.26, "unknown"), (0.25, "violated"), (0.05, "violated")])
def test_factor_bands_label_neutral_values_unknown(value, state):
    from src.pipeline.scoring import ScoreExplanation

    expl = ScoreExplanation(
        factors={"experience_fit": value}, weights={"experience_fit": 1.0}, weighted_base=value, top_location_bonus=0.0,
        pre_gate_score=value, domain_gate_fired=False, domain_cap=None, domain_cap_applied=False,
        seniority_gate_fired=False, seniority_multiplier=None,
        composite=value, penalties={}, final=value, location_match=None)
    assert ev.score_items(expl)[0].state == state


def test_domain_and_seniority_gates_become_negative_evidence():
    off = Job(id="o", title="Comptable senior", description="x" * 300, countries=["France"])
    items = ev.score_items(explain_score(off, _cfg()))
    gate = next(i for i in items if i.source.rule_id == "score.gate.domain")
    assert gate.polarity == "negative" and gate.weight is None and gate.observed == "cap=0.4"
