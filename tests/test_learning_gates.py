"""The AD-5 activation rule and health monitor as pure functions (ADR 0019; stage L3.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.learning.gates import (
    ACTIVE,
    EXPERIMENTAL,
    PAUSED,
    SHADOW,
    Evidence,
    Exposure,
    GateParams,
    Health,
    Positive,
    Tally,
    Window,
    anchor,
    decide,
    evidence,
    monitor,
    p_better,
    p_less_with_margin,
    p_worse,
    stratum_of,
    surface_stratum,
)

T = datetime(2026, 11, 1, tzinfo=UTC)
OPEN = Window(days=30, sessions=20)


@pytest.mark.parametrize("n,k,p", [(12, 9, 0.954), (15, 11, 0.962), (20, 14, 0.961), (30, 20, 0.965)])
def test_g3_arithmetic_table(n, k, p):
    assert p_better(k, n) == pytest.approx(p, abs=5e-4)
    assert p_better(k - 1, n) < 0.95 <= p_better(k, n)        # k is the minimum to activate


def test_v1_rule_was_one_win_of_one():
    assert p_better(1, 1) == pytest.approx(0.75)


@pytest.mark.parametrize("n,k,p", [(8, 2, 0.91), (10, 3, 0.89), (12, 4, 0.87)])
def test_pause_arithmetic(n, k, p):
    assert p_worse(k, n) == pytest.approx(p, abs=5e-3)
    assert p_worse(k + 1, n) < 0.80


def test_margin_probability_is_sane():
    even = Tally(20, 10)
    assert p_less_with_margin(even, even, 0.0) == pytest.approx(0.5, abs=0.01)
    assert p_less_with_margin(Tally(20, 4), Tally(20, 16), 0.1) > 0.99
    assert p_less_with_margin(Tally(20, 16), Tally(20, 4), 0.1) < 0.01


def test_strata():
    assert surface_stratum("S4") == "LB" and surface_stratum("S2", "date") == "LB"
    assert surface_stratum("S2", "relevance") == "PF" and surface_stratum("S2", None) == "PF"
    assert surface_stratum("S3") == "IM" and surface_stratum("S0") is None
    assert stratum_of([("S4", None), ("S2", "distance")]) == "LB"
    assert stratum_of([("S4", None), ("S1", None)]) == "PF"                 # any PF exposure → PF
    assert stratum_of([("S4", None), ("S3", None)]) == "IM"
    assert stratum_of([("S0", None)]) is None


def test_anchor_rules():
    exp = [Exposure("r1", T, "S1"), Exposure("r2", T + timedelta(days=2), "S4")]
    a = anchor(T + timedelta(days=3), exp, [])
    assert (a.run_id, a.rule) == ("r2", "visible")
    a = anchor(T + timedelta(days=1), exp, [])
    assert a.run_id == "r1"                                               # latest exposure at or before
    a = anchor(T + timedelta(days=1), [], [("r0", T - timedelta(days=1)), ("r9", T + timedelta(days=5))])
    assert (a.run_id, a.rule) == ("r0", "ranked")                          # rule 2: latest run before
    assert anchor(T, [], [("r9", T + timedelta(days=1))]) is None           # rule 3: never ranked before
    assert anchor(T + timedelta(days=20), exp, []) is None                  # outside the 14-day window


def pos(i: int, prod: int, chal: int, stratum="PF", session=None, state=SHADOW, job=None) -> Positive:
    return Positive(job or f"j{i}", session, T + timedelta(hours=i), prod, chal, stratum, state)


def test_discordance_and_ceiling():
    assert pos(0, 10, 7).discordant and pos(0, 10, 7).challenger_wins
    assert not pos(0, 10, 8).discordant
    assert not pos(0, 150, 101).discordant                                  # both beyond 100 → 101
    assert pos(0, 150, 98).discordant


def test_evidence_counts_once_caps_sessions_and_excludes():
    ps = [pos(i, 20, 5, session="s") for i in range(5)]                     # binge: only 3 count
    ps += [pos(9, 20, 5, job="j0")]                                         # same offer again: once
    ps += [pos(10, 20, 5, stratum=None)]                                    # S0: excluded
    ps += [pos(11, 20, 5, stratum="PF", state=EXPERIMENTAL)]                # PF during EXPERIMENTAL
    ps += [pos(12, 20, 5, stratum="LB", state=EXPERIMENTAL)]                # LB during EXPERIMENTAL: kept
    ev = evidence(ps)
    assert ev.positives == 6 and ev.pooled == Tally(4, 4)
    assert ev.stratum("LB") == Tally(1, 1) and ev.stratum("PF") == Tally(3, 3)


def ev(pooled: tuple[int, int], lb: tuple[int, int] = (0, 0), pf: tuple[int, int] | None = None,
       positives: int = 30) -> Evidence:
    pf = pf or (pooled[0] - lb[0], pooled[1] - lb[1])
    return Evidence(positives, Tally(*pooled), {"LB": Tally(*lb), "PF": Tally(*pf)})


def test_activation_needs_every_gate_and_lb():
    strong = ev((20, 16), lb=(8, 6))
    assert decide(SHADOW, OPEN, strong).state == ACTIVE
    assert decide(SHADOW, Window(10, 20), strong).state == SHADOW           # G1 days
    assert decide(SHADOW, Window(30, 5), strong).state == SHADOW            # G1 sessions
    assert decide(SHADOW, OPEN, ev((20, 16), lb=(8, 6), positives=15)).state == SHADOW   # G2
    assert decide(SHADOW, OPEN, ev((20, 12), lb=(8, 6))).state == SHADOW     # G3 (12/20 → 0.81)
    assert decide(SHADOW, Window(30, 20, guardrails_green=False), strong).state == SHADOW
    assert decide(SHADOW, Window(30, 20, pause_condition=True), strong).state == SHADOW


def test_vacuous_guard_never_passes_and_experimental_ships_disabled():
    thin = ev((20, 16), lb=(2, 2))
    d = decide(SHADOW, OPEN, thin)
    assert all(d.gates.values()) and not d.lb_enough and not d.g3b_pass and d.state == SHADOW
    assert decide(SHADOW, OPEN, thin, GateParams(experimental_enabled=True)).state == EXPERIMENTAL


def test_g3b_blocks_contradiction_and_stratum_disagreement():
    lb_contradicts = ev((30, 22), lb=(10, 2))
    assert decide(SHADOW, OPEN, lb_contradicts).state == SHADOW
    disagree = ev((40, 30), lb=(15, 7), pf=(25, 23))                          # PF 0.92 vs LB 0.47
    d = decide(SHADOW, OPEN, disagree)
    assert d.gates["G3"] and d.lb_enough and not d.g3b_pass and d.state == SHADOW


def test_experimental_promotion_and_timeout():
    params = GateParams(experimental_enabled=True)
    assert decide(EXPERIMENTAL, OPEN, ev((20, 16), lb=(8, 6)), params).state == ACTIVE
    assert decide(EXPERIMENTAL, OPEN, ev((20, 16), lb=(2, 2)), params, days_in_state=10).state == EXPERIMENTAL
    assert decide(EXPERIMENTAL, OPEN, ev((20, 16), lb=(2, 2)), params, days_in_state=60).state == SHADOW


def test_paused_resumes_only_on_a_full_fresh_window():
    assert decide(PAUSED, OPEN, ev((20, 16), lb=(8, 6))).state == ACTIVE
    assert decide(PAUSED, OPEN, ev((20, 12), lb=(8, 6))).state == SHADOW


def test_monitor_halves_then_pauses():
    h = Health(ACTIVE)
    h, e = monitor(h, T, Tally(8, 2))
    assert e == "halved" and h.cap_factor == 0.5 and h.state == ACTIVE
    h, e = monitor(h, T + timedelta(days=10), Tally(8, 2))
    assert e == "paused" and h.state == PAUSED
    h2, e = monitor(Health(ACTIVE, 0.5, (T,)), T + timedelta(days=40), Tally(8, 2))
    assert e == "halved"                                                      # first halving expired


def test_monitor_is_easier_to_trip_than_activation():
    assert monitor(Health(ACTIVE), T, Tally(8, 3))[1] is None                 # 3/8: P(worse) 0.75
    assert monitor(Health(ACTIVE), T, Tally(7, 0))[1] is None                 # fewer than 8
    assert monitor(Health(ACTIVE), T, Tally(20, 20), guardrails_green=False)[1] == "paused"


def test_monitor_rate_drop_and_experimental():
    h, e = monitor(Health(ACTIVE), T, Tally(), top20_rate=0.1, trailing_median=0.3, baseline_positives=6)
    assert e == "halved"
    assert monitor(Health(ACTIVE), T, Tally(), top20_rate=0.1, trailing_median=0.3, baseline_positives=5)[1] is None
    h, e = monitor(Health(EXPERIMENTAL), T, Tally(6, 1))                      # stricter: 6 at 0.70
    assert e == "halved"
    assert monitor(h, T + timedelta(days=1), Tally(6, 1))[1] == "to_shadow"
    assert monitor(Health(SHADOW), T, Tally(20, 0)) == (Health(SHADOW), None)
