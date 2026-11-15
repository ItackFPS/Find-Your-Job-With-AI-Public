"""G3 as an anytime-valid sequential test (Phase B B9-B, owner decision D2, ADR 0019 criterion)."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from src.learning import gates as G


def test_the_e_value_starts_at_one_and_grows_with_wins() -> None:
    assert G.e_value(0, 0) == pytest.approx(1.0)
    assert [G.e_value(w, 20) < G.e_value(w + 1, 20) for w in range(20)] == [True] * 20
    assert G.e_value(10, 20) < 1.0 < G.e_value(15, 20)     # a coin-flip record is no evidence


def test_the_activation_bar_is_one_over_alpha() -> None:
    params = G.GateParams()
    assert params.g3_rule == "e_process" and params.g3_alpha == 0.05
    assert not G.g3_pass(15, 20, params) and G.g3_pass(16, 20, params)   # E = 9.6 vs 20.5
    assert G.g3_pass(15, 20, replace(params, g3_rule="posterior"))        # the former rule: P = 0.99


def _ever_passes(rng: np.random.Generator, p_win: float, looks: int, rule: str, histories: int) -> float:
    params = replace(G.GateParams(), g3_rule=rule)
    hits = 0
    for _ in range(histories):
        wins = np.cumsum(rng.random(looks) < p_win)
        # evaluated after every discordant outcome from the 12th on (G2's minimum), like the live rule
        if any(G.g3_pass(int(wins[n - 1]), n, params) for n in range(12, looks + 1)):
            hits += 1
    return hits / histories


def test_a_challenger_that_is_not_better_rarely_ever_passes_however_often_it_is_tested() -> None:
    rng = np.random.default_rng(7)
    e_rate = _ever_passes(rng, 0.5, 200, "e_process", 2000)
    posterior_rate = _ever_passes(np.random.default_rng(7), 0.5, 200, "posterior", 2000)
    assert e_rate <= 0.05                     # Ville's inequality, with sampling margin to spare
    assert posterior_rate > 0.05              # optional stopping: the former rule breaks its own criterion


def test_a_clearly_better_challenger_passes() -> None:
    assert _ever_passes(np.random.default_rng(11), 0.85, 60, "e_process", 300) >= 0.9


def test_decide_reports_the_e_value() -> None:
    ev = G.Evidence(positives=40, pooled=G.Tally(discordant=20, wins=16))
    d = G.decide(G.SHADOW, G.Window(days=30, sessions=20), ev)
    assert d.gates["G3"] and d.e_pooled == pytest.approx(G.e_value(16, 20))
