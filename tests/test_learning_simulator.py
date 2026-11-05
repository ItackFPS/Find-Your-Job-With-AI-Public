"""The AD-5 simulator (stage L3.4): deterministic, wired to the real gate functions, small N."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import numpy as np
import pytest

from src.learning import gates as G
from src.learning import simulator as sim

HIGH = sim.ACTIVITIES[2]
TYPICAL = sim.ACTIVITIES[1]
WIDE = sim.ClickModel("wide", 0.4, 0.1, 0.6)


def test_generation_is_deterministic_per_seed():
    a = sim.generate(sim.Scenario("x"), TYPICAL, WIDE, np.random.default_rng(3))
    b = sim.generate(sim.Scenario("x"), TYPICAL, WIDE, np.random.default_rng(3))
    c = sim.generate(sim.Scenario("x"), TYPICAL, WIDE, np.random.default_rng(4))
    assert a == b and a != c
    assert all(0 <= p.day < 180 and p.stratum in ("LB", "IM", "PF") for p in a.positives)


def test_sr1_pf_rate_rises_with_position_bias_and_stays_below_0_7():
    rates = [sim.sr1_pf_rate(b) for b in (0.0, 0.5, 1.0, 2.0, 50.0)]
    assert rates[0] == 0.5 and rates == sorted(rates) and rates[-1] < 0.7 + 1e-9


def test_snapshots_apply_the_session_cap_and_exclusions():
    ps = tuple(sim.SimPositive(0, 0, "PF", True, True) for _ in range(5))
    h = sim.History(1, (0,), ps)
    ev = sim.snapshots(h)[-1].evidence
    assert ev.positives == 5 and ev.pooled == G.Tally(3, 3)
    ev = sim.snapshots(h, excluded={0, 1, 2, 3})[-1].evidence
    assert ev.positives == 1 and ev.pooled == G.Tally(1, 1)


def test_snapshot_evidence_matches_the_gate_module():
    h = sim.generate(sim.Scenario("x", 0.7, 0.7, 0.7), HIGH, WIDE, np.random.default_rng(1))
    def at(i: int):
        return sim.DAY0 + timedelta(seconds=i)
    positives = [G.Positive(f"j{i}", f"s{p.session}", at(i), 20, 5 if p.win else 35, p.stratum) if p.discordant
                 else G.Positive(f"j{i}", f"s{p.session}", at(i), 20, 20, p.stratum)
                 for i, p in enumerate(h.positives)]
    assert sim.snapshots(h)[-1].evidence == G.evidence(positives)


def test_a_strong_challenger_activates_and_a_contradicted_one_does_not():
    params = G.GateParams()
    strong = [sim.first_activation(sim.snapshots(
        sim.generate(sim.Scenario("s", 0.9, 0.9, 0.9), HIGH, WIDE, np.random.default_rng(s))), params)
        for s in range(10)]
    assert all(d is not None and d < 90 for d in strong)
    lb_against = [sim.first_activation(sim.snapshots(
        sim.generate(sim.Scenario("c", 0.1, 0.9, 0.9), HIGH, WIDE, np.random.default_rng(s))), params)
        for s in range(10)]
    assert all(d is None for d in lb_against)                      # G3b (a): LB contradicts


def test_prefiltered_search_matches_the_direct_rule():
    params = G.GateParams()
    for s in range(5):
        snaps = sim.snapshots(sim.generate(sim.Scenario("t", 0.7, 0.7, 0.7), HIGH, WIDE, np.random.default_rng(s)))
        assert sim.first_activation(snaps, params) == sim.first_activation(
            sim.candidates(snaps, params), params, prefiltered=True)


def test_thin_lb_never_activates_and_experimental_returns():
    params = replace(G.GateParams(), experimental_enabled=True)
    runs = [sim.run_with_experimental(sim.Scenario("SR-4", lb_share=0.0), HIGH, WIDE, s, params) for s in range(8)]
    assert not any(r.active for r in runs)
    entries = sum(r.entries for r in runs)
    assert sum(r.returns for r in runs) + sum(r.censored for r in runs) == entries


def test_experimental_disabled_never_enters():
    runs = [sim.run_with_experimental(sim.Scenario("SR-4", lb_share=0.0), HIGH, WIDE, s, G.GateParams())
            for s in range(4)]
    assert all(r.entries == 0 and not r.active for r in runs)


def test_adversarial_sequence_halves_then_pauses_at_high_activity():
    res = [sim.adversarial_pause_day(HIGH, WIDE, s, G.GateParams()) for s in range(5)]
    assert all(h is not None and p is not None and h <= p for h, p in res)


@pytest.mark.parametrize("histories", [3])
def test_evaluate_and_render_end_to_end(histories):
    rep = sim.evaluate(G.GateParams(), histories, seed=11, days=120)
    text = sim.render(rep)
    assert "## Criteria" in text and "SR-3" in text and "owner replay" in text
    assert {c.scenario for c in rep.cells} >= {"baseline", "true-0.70", "SR-2 ρ=0.5", "SR-3"}
