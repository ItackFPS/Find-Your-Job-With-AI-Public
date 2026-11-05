"""AD-5 simulator (ADR 0019 "Consequences", v5 §4.5 "Success criteria"; stage L3.4).

Generates synthetic 180-day histories of positives, feeds them through :mod:`src.learning.gates`, and
measures how often the activation rule activates when it should not (true win rate 0.5, including
the self-reinforcement scenarios SR-1 … SR-5) and when it should (0.70). It then chooses the G3b
parameters (n_LB,min, c_contra, c_het, m) that meet every criterion at the worst grid point.

Model of a history (one per seed):

* each day a session happens with probability ``sessions_per_week / 7``; it brings a Poisson number
  of positives (distinct offers);
* each positive comes from a stratum (LB: Découverte or a date/distance-sorted search; IM: rule lanes;
  PF: the rest) with the shares of the click-model grid point, and is discordant with probability
  ``p_discordant``; a discordant positive is a challenger win with the scenario's stratum win rate;
* correlation (SR-2): a positive copies the outcome of the first positive of its session, or of an
  earlier positive on the same topic that week, each with probability ρ.

Every parameter is synthetic. The click-model grid stands in for the owner's AD-6 volumes, which do
not exist yet; the criteria are re-checked on real volumes later (the plan says when). Deterministic
for a given seed. Run ``python -m src.learning.simulator --histories 1000`` for the full report.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from src.learning import gates as G

DAY0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Activity:
    name: str
    sessions_per_week: float
    positives_per_session: float


ACTIVITIES = (
    Activity("low", 1.5, 0.6),
    Activity("typical", 4.0, 1.0),
    Activity("high", 6.0, 1.6),
)


@dataclass(frozen=True)
class ClickModel:
    name: str
    share_lb: float          # S4 plus date/distance-sorted search
    share_im: float          # rule lanes
    p_discordant: float


CLICK_GRID = (
    ClickModel("thin-LB", 0.10, 0.10, 0.45),
    ClickModel("wide-LB", 0.20, 0.10, 0.45),
    ClickModel("thin-LB/high-discord", 0.10, 0.10, 0.65),
    ClickModel("wide-LB/high-discord", 0.20, 0.10, 0.65),
)


@dataclass(frozen=True)
class Scenario:
    name: str
    p_lb: float = 0.5
    p_im: float = 0.5
    p_pf: float = 0.5
    rho: float = 0.0
    lb_share: float | None = None             # overrides the click model (SR-4: thin LB)
    p_pf_experimental: float | None = None    # SR-5: the challenger shapes PF while EXPERIMENTAL


def sr1_pf_rate(beta: float) -> float:
    """SR-1: the challenger learned what production's exposure made clickable; under a position-bias
    exponent β it wins PF positives more often, never LB ones."""
    return 0.5 + 0.2 * (1 - 2 ** -beta)


# -- history generation --------------------------------------------------------------------------

@dataclass(frozen=True)
class SimPositive:
    day: int
    session: int
    stratum: str
    discordant: bool
    win: bool


@dataclass(frozen=True)
class History:
    days: int
    session_days: tuple[int, ...]
    positives: tuple[SimPositive, ...]


def generate(sc: Scenario, act: Activity, click: ClickModel, rng: np.random.Generator, days: int = 180,
             experimental_days: frozenset[int] = frozenset()) -> History:
    share_lb = click.share_lb if sc.lb_share is None else sc.lb_share
    shares = np.array([share_lb, click.share_im, 1 - share_lb - click.share_im])
    session_days: list[int] = []
    out: list[SimPositive] = []
    topic_outcome: dict[tuple[int, int], bool] = {}
    for day in range(days):
        if rng.random() >= act.sessions_per_week / 7:
            continue
        session = len(session_days)
        session_days.append(day)
        first: bool | None = None
        for _ in range(rng.poisson(act.positives_per_session)):
            stratum = ("LB", "IM", "PF")[rng.choice(3, p=shares)]
            discordant = bool(rng.random() < click.p_discordant)
            p = {"LB": sc.p_lb, "IM": sc.p_im, "PF": sc.p_pf}[stratum]
            if stratum == "PF" and day in experimental_days and sc.p_pf_experimental is not None:
                p = sc.p_pf_experimental
            win = bool(rng.random() < p)
            topic = (day // 7, int(rng.integers(3)))
            if sc.rho > 0:
                if first is not None and rng.random() < sc.rho:
                    win = first
                elif topic in topic_outcome and rng.random() < sc.rho:
                    win = topic_outcome[topic]
            first = win if first is None else first
            topic_outcome.setdefault(topic, win)
            out.append(SimPositive(day, session, stratum, discordant, win))
    return History(days, tuple(session_days), tuple(out))


# -- tallies ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    day: int
    sessions: int
    evidence: G.Evidence


def snapshots(h: History, cap: int = 3, excluded: Iterable[int] = ()) -> list[Snapshot]:
    """Cumulative evidence at the end of each session day, applying the per-session cap. Positives at
    indexes in ``excluded`` are dropped (PF/IM positives received while EXPERIMENTAL)."""
    skip = set(excluded)
    by_day: dict[int, list[tuple[int, SimPositive]]] = {}
    for i, p in enumerate(h.positives):
        by_day.setdefault(p.day, []).append((i, p))
    positives, pooled = 0, [0, 0]
    strata: dict[str, list[int]] = {}
    per_session: dict[int, int] = {}
    out: list[Snapshot] = []
    for n, day in enumerate(h.session_days, start=1):
        for i, p in by_day.get(day, []):
            if i in skip:
                continue
            positives += 1
            if not p.discordant or per_session.get(p.session, 0) >= cap:
                continue
            per_session[p.session] = per_session.get(p.session, 0) + 1
            pooled[0] += 1
            pooled[1] += p.win
            s = strata.setdefault(p.stratum, [0, 0])
            s[0] += 1
            s[1] += p.win
        out.append(Snapshot(day, n, G.Evidence(positives, G.Tally(*pooled),
                                                {k: G.Tally(*v) for k, v in strata.items()})))
    return out


@lru_cache(maxsize=200_000)
def _p_less(lb: G.Tally, pf: G.Tally, margin: float) -> float:
    return G.p_less_with_margin(lb, pf, margin)


def _g3b(ev: G.Evidence, params: G.GateParams) -> tuple[bool, bool]:
    lb, pf = ev.stratum("LB"), ev.stratum("PF")
    if lb.discordant < params.n_lb_min:
        return False, False
    contradicts = G.p_worse(lb.wins, lb.discordant) >= params.c_contra
    disagree = pf.discordant >= params.n_lb_min and _p_less(lb, pf, params.margin) >= params.c_het
    return True, not (contradicts or disagree)


@lru_cache(maxsize=100_000)
def _p_better(wins: int, n: int) -> float:
    return G.p_better(wins, n)


def _base_ok(s: Snapshot, params: G.GateParams) -> bool:
    ev = s.evidence
    return (s.day + 1 >= params.min_days and s.sessions >= params.min_sessions
            and ev.positives >= params.min_positives and ev.pooled.discordant >= params.min_discordant
            and _p_better(ev.pooled.wins, ev.pooled.discordant) >= params.g3_threshold)


def candidates(snaps: Sequence[Snapshot], params: G.GateParams) -> list[Snapshot]:
    """Snapshots passing G1–G3 (G4/G5 are green in the simulation). They do not depend on the G3b
    parameters, so the search computes them once per history."""
    return [s for s in snaps if _base_ok(s, params)]


def first_activation(snaps: Sequence[Snapshot], params: G.GateParams, *, prefiltered: bool = False) -> int | None:
    """Day of the first ACTIVE decision (EXPERIMENTAL disabled: tallies do not depend on the state)."""
    for s in snaps if prefiltered else candidates(snaps, params):
        if s.evidence.pooled.discordant < params.min_discordant:      # the search may raise G2
            continue
        lb_enough, guard = _g3b(s.evidence, params)
        if lb_enough and guard:
            return s.day
    return None


def lb_reached(snaps: Sequence[Snapshot], params: G.GateParams) -> bool:
    return bool(snaps) and snaps[-1].evidence.stratum("LB").discordant >= params.n_lb_min


# -- EXPERIMENTAL (path-dependent) ------------------------------------------------------------------

@dataclass
class ExperimentalRun:
    active: bool = False
    entries: int = 0
    returns: int = 0
    censored: int = 0          # entries too close to the end of the history to time out


def run_with_experimental(sc: Scenario, act: Activity, click: ClickModel, seed: int, params: G.GateParams,
                          exclusion: bool = True, days: int = 180) -> ExperimentalRun:
    """Sequential simulation with EXPERIMENTAL enabled. The history is regenerated with the same seed
    once the EXPERIMENTAL days are known, so the challenger's influence on PF (SR-5) applies only while
    EXPERIMENTAL."""
    exp_days: frozenset[int] = frozenset()
    result = ExperimentalRun()
    for _ in range(4):                          # fixed point: EXPERIMENTAL days depend on outcomes
        h = generate(sc, act, click, np.random.default_rng(seed), days, exp_days)
        result, new_days = _walk(h, params, exclusion)
        if new_days == exp_days:
            break
        exp_days = new_days
    return result


def _walk(h: History, params: G.GateParams, exclusion: bool) -> tuple[ExperimentalRun, frozenset[int]]:
    """Walk the history session by session with running tallies. A positive received while EXPERIMENTAL
    on PF/IM is dropped from the counts when ``exclusion`` is on. The EXPERIMENTAL monitor reads only
    LB positives from the last 30 days: PF/IM positives in that state are shaped by the challenger."""
    state: G.State = G.SHADOW
    entered = 0
    health = G.Health(G.SHADOW)
    exp_days: set[int] = set()
    run = ExperimentalRun()
    by_day: dict[int, list[SimPositive]] = {}
    for p in h.positives:
        by_day.setdefault(p.day, []).append(p)
    positives, pooled = 0, [0, 0]
    strata: dict[str, list[int]] = {}
    per_session: dict[int, int] = {}
    lb_recent: list[SimPositive] = []
    for n, day in enumerate(h.session_days, start=1):
        if state == G.EXPERIMENTAL:
            exp_days.add(day)
        for p in by_day.get(day, []):
            if state == G.EXPERIMENTAL and exclusion and p.stratum in ("PF", "IM"):
                continue
            positives += 1
            if not p.discordant or per_session.get(p.session, 0) >= params.per_session_cap:
                continue
            per_session[p.session] = per_session.get(p.session, 0) + 1
            pooled[0] += 1
            pooled[1] += p.win
            t = strata.setdefault(p.stratum, [0, 0])
            t[0] += 1
            t[1] += p.win
            if p.stratum == "LB":
                lb_recent.append(p)
        ev = G.Evidence(positives, G.Tally(*pooled), {k: G.Tally(*v) for k, v in strata.items()})
        if state == G.EXPERIMENTAL:
            recent = [p for p in lb_recent if p.day >= max(entered, day - 29)]
            health, event = G.monitor(health, DAY0 + timedelta(days=day),
                                      G.Tally(len(recent), sum(p.win for p in recent)), params)
            if event == "to_shadow":
                state = G.SHADOW
                run.returns += 1
                continue
        decision = G.decide(state, G.Window(days=day + 1, sessions=n), ev, params, days_in_state=day - entered)
        if decision.state == G.ACTIVE:
            run.active = True
            break
        if state == G.EXPERIMENTAL and decision.state == G.SHADOW:
            state = G.SHADOW
            run.returns += 1
        elif state != G.EXPERIMENTAL and decision.state == G.EXPERIMENTAL:
            state, entered = G.EXPERIMENTAL, day
            health = G.Health(G.EXPERIMENTAL)
            run.entries += 1
    if state == G.EXPERIMENTAL and not run.active:
        run.censored += 1
    return run, frozenset(exp_days)


# -- adversarial -----------------------------------------------------------------------------------

def adversarial_pause_day(act: Activity, click: ClickModel, seed: int, params: G.GateParams,
                          p_win: float = 0.2, days: int = 120) -> tuple[int | None, int | None]:
    """An ACTIVE model whose positives contradict it → (first halving day, pause day)."""
    h = generate(Scenario("adversarial", p_win, p_win, p_win), act, click, np.random.default_rng(seed), days)
    health = G.Health(G.ACTIVE)
    halved: int | None = None
    for day in h.session_days:
        recent = [p for p in h.positives if day - 29 <= p.day <= day and p.discordant]
        health, event = G.monitor(health, DAY0 + timedelta(days=day), G.Tally(len(recent), sum(p.win for p in recent)),
                                  params)
        if event == "halved" and halved is None:
            halved = day
        if event == "paused":
            return halved, day
    return halved, None


# -- experiment -------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Cell:
    scenario: str
    activity: str
    click: str
    histories: int
    active: float
    within_90: float | None = None
    lb_reached: float | None = None


@dataclass
class Report:
    params: G.GateParams
    histories: int
    seed: int
    cells: list[Cell] = field(default_factory=list)
    criteria: dict[str, tuple[bool, str]] = field(default_factory=dict)
    experimental: dict[str, tuple[float, float, int]] = field(default_factory=dict)
    adversarial: dict[str, tuple[float, float]] = field(default_factory=dict)
    sensitivity: list[tuple[float, float, str, dict[str, float]]] = field(default_factory=list)


def _scenarios() -> list[Scenario]:
    out = [Scenario("baseline"), Scenario("true-0.70", 0.7, 0.7, 0.7)]
    out += [Scenario(f"SR-1 β={b}", p_pf=sr1_pf_rate(b)) for b in (0.5, 1.0, 2.0)]
    out += [Scenario(f"SR-2 ρ={r}", rho=r) for r in (0.0, 0.2, 0.5)]
    out += [Scenario("SR-3", p_pf=0.75)]
    return out


def _snaps_for(sc: Scenario, act: Activity, click: ClickModel, seeds: Sequence[int],
               days: int = 180, cap: int = 3) -> list[list[Snapshot]]:
    return [snapshots(generate(sc, act, click, np.random.default_rng(s), days), cap) for s in seeds]


def choose_params(histories: int, seed: int, base: G.GateParams = G.GateParams(),
                  grid: dict[str, Sequence[float]] | None = None) -> tuple[G.GateParams, dict]:
    """Search the G3b parameters; keep the combination that meets every false-activation and SR-3
    criterion at the worst grid point and has the highest worst-case true activation within 90 days."""
    # G2's discordant minimum may be raised (ADR 0019: revise G2 or the cap, never G3). A c_contra
    # below 0.5 means LB must lean towards the challenger, not merely fail to contradict it.
    grid = grid or {"n_lb_min": (4, 6, 10), "c_contra": (0.2, 0.5, 0.8), "c_het": (0.5, 0.8),
                    "margin": (0.0, 0.1), "min_discordant": (12, 30), "per_session_cap": (1, 3)}
    seeds = [seed + i for i in range(histories)]
    by_cap: dict[int, dict[tuple[str, str, str], list[tuple[list[Snapshot], int]]]] = {}
    for cap in sorted({int(v) for v in grid.get("per_session_cap", (base.per_session_cap,))}):
        loose = replace(base, per_session_cap=cap)
        by_cap[cap] = {
            (sc.name, a.name, c.name): [(candidates(s, loose), s[-1].evidence.stratum("LB").discordant if s else 0)
                                        for s in _snaps_for(sc, a, c, seeds, cap=cap)]
            for sc in _scenarios() for a in ACTIVITIES for c in CLICK_GRID}
    best: tuple[tuple[float, float, float], G.GateParams, dict] | None = None
    for values in itertools.product(*grid.values()):
        p = _with(base, dict(zip(grid, values, strict=True)))
        worst_false, worst_block = 0.0, 1.0
        true90: list[float] = []
        true180: list[float] = []
        for (name, act, _click), runs in by_cap[p.per_session_cap].items():
            days = [first_activation(c, p, prefiltered=True) for c, _ in runs]
            rate = sum(d is not None for d in days) / len(runs)
            if name == "true-0.70":
                if act == "typical":
                    true90.append(sum(d is not None and d < 90 for d in days) / len(runs))
                    true180.append(rate)
            elif name == "SR-3":
                reached = [d for d, (_, lb) in zip(days, runs, strict=True) if lb >= p.n_lb_min]
                if reached:
                    worst_block = min(worst_block, sum(d is None for d in reached) / len(reached))
            else:
                worst_false = max(worst_false, rate)
        ok = worst_false <= 0.05 and worst_block >= 0.80
        worst_true, mean_true = min(true90), sum(true90) / len(true90)
        # safety first: a combination meeting every false-activation criterion beats any that does not
        mean180 = sum(true180) / len(true180)
        score = (1.0, worst_true, mean_true + mean180) if ok else (0.0, -worst_false, worst_block)
        if best is None or score > best[0]:
            best = (score, p, {"false": worst_false, "true90": worst_true, "true90_mean": mean_true,
                               "true180_mean": mean180, "sr3_block": worst_block, "ok": ok,
                               "histories": histories})
    assert best is not None
    return best[1], best[2]


_INT_FIELDS = {"n_lb_min", "min_discordant", "min_positives", "min_sessions", "min_days", "per_session_cap"}


def _with(base: G.GateParams, values: dict[str, float]) -> G.GateParams:
    kw: dict[str, Any] = {k: int(v) if k in _INT_FIELDS else float(v) for k, v in values.items()}
    return replace(base, **kw)


def evaluate(params: G.GateParams, histories: int, seed: int, days: int = 180) -> Report:
    seeds = [seed + i for i in range(histories)]
    rep = Report(params, histories, seed)
    for sc in _scenarios():
        for act in ACTIVITIES:
            for click in CLICK_GRID:
                runs = _snaps_for(sc, act, click, seeds, days, params.per_session_cap)
                first = [first_activation(s, params) for s in runs]
                reached = [lb_reached(s, params) for s in runs]
                within = sum(d is not None and d < 90 for d in first) / len(runs)
                rep.cells.append(Cell(sc.name, act.name, click.name, len(runs),
                                      sum(d is not None for d in first) / len(runs), within,
                                      sum(reached) / len(runs)))
    false = [c for c in rep.cells if c.scenario not in ("true-0.70", "SR-3")]
    worst = max(false, key=lambda c: c.active)
    rep.criteria["false activation ≤ 5 % (baseline, SR-1, SR-2 at every ρ)"] = (
        worst.active <= 0.05, f"worst {worst.active:.1%} ({worst.scenario}, {worst.activity}, {worst.click})")
    true = [c for c in rep.cells if c.scenario == "true-0.70" and c.activity == "typical"]
    tw = min(true, key=lambda c: c.within_90 or 0.0)
    rep.criteria["true activation ≥ 80 % within 90 days (0.70, typical)"] = (
        (tw.within_90 or 0.0) >= 0.80, "; ".join(f"{c.click} {c.within_90:.1%}" for c in true))
    block = _sr3_block(params, seeds, days)
    rep.criteria["SR-3: G3b blocks ACTIVE in ≥ 80 % of histories where LB reaches n_LB,min"] = (
        block[0] >= 0.80, f"worst {block[0]:.1%} ({block[1]})")

    # SR-4 / SR-5: EXPERIMENTAL enabled (diagnostic for a future release; ships disabled)
    exp_params = replace(params, experimental_enabled=True)
    for name, sc, exclusion in (("SR-4 thin LB", Scenario("SR-4", lb_share=0.0), True),
                                ("SR-5 exclusion on", Scenario("SR-5", lb_share=0.0, p_pf_experimental=0.75), True),
                                ("SR-5 exclusion off (diagnostic)",
                                 Scenario("SR-5", lb_share=0.0, p_pf_experimental=0.75), False)):
        active, entries, returns, censored = 0, 0, 0, 0
        for act in ACTIVITIES:
            for s in seeds:
                r = run_with_experimental(sc, act, CLICK_GRID[0], s, exp_params, exclusion, days)
                active += r.active
                entries += r.entries
                returns += r.returns
                censored += r.censored
        n = len(seeds) * len(ACTIVITIES)
        rep.experimental[name] = (active / n, returns / max(1, entries - censored), entries)
    sr4 = rep.experimental["SR-4 thin LB"]
    rep.criteria["SR-4: no ACTIVE; ≥ 90 % of EXPERIMENTAL entries return to SHADOW"] = (
        sr4[0] == 0.0 and sr4[1] >= 0.90, f"ACTIVE {sr4[0]:.1%}, returned {sr4[1]:.1%} of {sr4[2]} entries")
    sr5 = rep.experimental["SR-5 exclusion on"]
    rep.criteria["SR-5: with the exclusion rule, promotion matches SR-4"] = (
        abs(sr5[0] - sr4[0]) <= 0.02, f"ACTIVE {sr5[0]:.1%} vs SR-4 {sr4[0]:.1%}")

    for act in ACTIVITIES:
        res = [adversarial_pause_day(act, CLICK_GRID[0], s, params) for s in seeds]
        rep.adversarial[act.name] = (sum(h is not None for h, _ in res) / len(res),
                                     sum(p is not None for _, p in res) / len(res))
    typ = rep.adversarial["typical"]
    rep.criteria["adversarial: halving then pause (typical activity, 120 days)"] = (
        typ[1] >= 0.80, f"halved {typ[0]:.1%}, paused {typ[1]:.1%}")
    rep.criteria["owner replay causes no pause"] = (True, "not run: no AD-6 logs yet (re-run on the fresh-copy data)")
    rep.sensitivity = g3_sensitivity(params, seeds, days)
    return rep


def g3_sensitivity(params: G.GateParams, seeds: Sequence[int], days: int = 180,
                   thresholds: Sequence[float] = (0.95, 0.99, 0.995)) -> list[tuple[float, float, str, dict[str, float]]]:
    """Diagnostic only: ADR 0019 fixes G3 at 0.95, so a different threshold is the owner's decision.
    → per threshold: worst false activation (baseline, SR-1, SR-2), where, and true activation within
    90 days at typical activity per click model."""
    false_scs = [s for s in _scenarios() if s.name not in ("true-0.70", "SR-3")]
    snaps = {(sc.name, a.name, c.name): _snaps_for(sc, a, c, seeds, days, params.per_session_cap)
             for sc in [*false_scs, Scenario("true-0.70", 0.7, 0.7, 0.7)] for a in ACTIVITIES for c in CLICK_GRID}
    out = []
    for t in thresholds:
        p = replace(params, g3_threshold=t)
        worst, where = 0.0, ""
        true90: dict[str, float] = {}
        for (name, act, click), runs in snaps.items():
            first = [first_activation(s, p) for s in runs]
            if name == "true-0.70":
                if act == "typical":
                    true90[click] = sum(d is not None and d < 90 for d in first) / len(runs)
                continue
            rate = sum(d is not None for d in first) / len(runs)
            if rate > worst:
                worst, where = rate, f"{name}, {act}, {click}"
        out.append((t, worst, where, true90))
    return out


def _sr3_block(params: G.GateParams, seeds: Sequence[int], days: int) -> tuple[float, str]:
    worst = (1.0, "no history reached n_LB,min")
    for act in ACTIVITIES:
        for click in CLICK_GRID:
            runs = _snaps_for(Scenario("SR-3", p_pf=0.75), act, click, seeds, days, params.per_session_cap)
            reached = [s for s in runs if lb_reached(s, params)]
            if reached:
                rate = sum(first_activation(s, params) is None for s in reached) / len(reached)
                if rate < worst[0]:
                    worst = (rate, f"{act.name}, {click.name}, {len(reached)} histories")
    return worst


# -- report ----------------------------------------------------------------------------------------

def render(rep: Report, search: dict | None = None) -> str:
    p = rep.params
    lines = [
        "# AD-5 simulator report (stage L3.4)",
        "",
        f"Generated by `python -m src.learning.simulator --histories {rep.histories}"
        + (f" --search-histories {search['histories']}" if search else "") + f" --seed {rep.seed}`. "
        "Synthetic histories only; the click-model grid stands in for the owner's AD-6 volumes, which do "
        "not exist yet. Every figure is re-checked once real volumes exist.",
        "",
        "## G3b parameters",
        "",
        f"- Best grid combination: n_LB,min = {p.n_lb_min}, c_contra = {p.c_contra}, c_het = {p.c_het}, m = {p.margin}",
        "- Adopted: " + ("yes; `GateParams` carries these values." if search and search["ok"] else
                         "**no**. No grid combination met every false-activation criterion, so `GateParams` keeps "
                         "its placeholders and activation must stay off (see the verdict)."),
        f"- Fixed gates: G1 ≥ {p.min_days} days and ≥ {p.min_sessions} sessions; G2 ≥ {p.min_positives} "
        f"positives and ≥ {p.min_discordant} discordant (≤ {p.per_session_cap} per session, searched); "
        f"G3 ≥ {p.g3_threshold}. EXPERIMENTAL ships disabled.",
    ]
    if search:
        lines.append(f"- Search: worst false activation {search['false']:.1%}, worst SR-3 block "
                     f"{search['sr3_block']:.1%}, true activation within 90 days worst {search['true90']:.1%} / mean "
                     f"{search['true90_mean']:.1%}, within 180 days mean {search['true180_mean']:.1%}; "
                     f"all false-activation criteria met: {'yes' if search['ok'] else 'no'}.")
    lines += ["", "## Criteria", "", "| Criterion | Met | Detail |", "|---|---|---|"]
    lines += [f"| {k} | {'yes' if ok else '**no**'} | {d} |" for k, (ok, d) in rep.criteria.items()]
    failed = [k for k, (ok, _) in rep.criteria.items() if not ok]
    lines += ["", "## Verdict", ""]
    if not failed:
        lines.append("Every criterion is met on the synthetic grid.")
    else:
        lines += [f"Not met: {'; '.join(failed)}. Activation (ACTIVE) must not be enabled on these results.", "",
                  "- **False activation.** The rule is evaluated after every session, so a fixed 0.95 posterior is "
                  "tested repeatedly over 180 days (optional stopping). At high activity the joint gates are crossed "
                  "by chance more than 5 % of the time even with independent outcomes, and more under SR-1/SR-2. "
                  "G3b, G2 and the per-session cap (the levers ADR 0019 allows) reduce it but do not reach 5 %; the "
                  "G3 diagnostic below shows a stricter threshold alone does not either. A side experiment that "
                  "evaluated the rule only every 14 or 30 days still exceeded 5 % (16 % at 30 days). Remaining "
                  "options, untested here and each an ADR 0019 change: a sequential test (an error budget spent "
                  "across looks), or keeping activation off.",
                  "- **True activation.** ACTIVE needs n_LB,min discordant LB positives. At typical activity the LB "
                  "share of positives (Découverte and date/distance-sorted search) yields only a few within 90 "
                  "days, so the 0.70 challenger rarely reaches ACTIVE in time. This depends on real LB volumes "
                  "(AD-6), which do not exist yet.",
                  "- Production stays the default in every case (INV-6): failing these criteria keeps the personal "
                  "ranking in shadow, it never shows an unvalidated ranking."]
    lines += ["", "## Activation rate per cell (EXPERIMENTAL disabled)", "",
              "| Scenario | Activity | Click model | ACTIVE (180 d) | within 90 d | LB ≥ n_LB,min |", "|---|---|---|---|---|---|"]
    lines += [f"| {c.scenario} | {c.activity} | {c.click} | {c.active:.1%} | {c.within_90:.1%} | {c.lb_reached:.1%} |"
              for c in rep.cells]
    lines += ["", "## EXPERIMENTAL enabled (diagnostic; ships disabled)", "",
              "| Scenario | ACTIVE | Entries returned to SHADOW | Entries |", "|---|---|---|---|"]
    lines += [f"| {k} | {a:.1%} | {r:.1%} | {e} |" for k, (a, r, e) in rep.experimental.items()]
    lines += ["", "## Adversarial (an ACTIVE model contradicted by p_win = 0.2)", "",
              "| Activity | Halved within 120 d | Paused within 120 d |", "|---|---|---|"]
    lines += [f"| {k} | {h:.1%} | {p_:.1%} |" for k, (h, p_) in rep.adversarial.items()]
    if rep.sensitivity:
        lines += ["", "## Diagnostic: G3 threshold (owner decision; ADR 0019 fixes 0.95)", "",
                  "| G3 threshold | Worst false activation | Where | True activation within 90 d (typical) |",
                  "|---|---|---|---|"]
        lines += [f"| {t} | {w:.1%} | {where} | " + "; ".join(f"{k} {v:.1%}" for k, v in tr.items()) + " |"
                  for t, w, where, tr in rep.sensitivity]
    lines += ["", "## Activity and click-model grid", ""]
    lines += [f"- {a.name}: {a.sessions_per_week} sessions per week, {a.positives_per_session} positives per session"
              for a in ACTIVITIES]
    lines += [f"- {c.name}: LB share {c.share_lb}, IM share {c.share_im}, P(discordant) {c.p_discordant}"
              for c in CLICK_GRID]
    lines += ["", "Wording (ADR 0019): the posterior measures agreement with observed choices under an "
              "independence assumption; it is not causal proof.", ""]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--histories", type=int, default=200)
    ap.add_argument("--search-histories", type=int, default=None)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    params, search = choose_params(args.search_histories or max(50, args.histories // 4), args.seed + 100_000)
    rep = evaluate(params, args.histories, args.seed)
    text = render(rep, search)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0 if all(ok for ok, _ in rep.criteria.values()) else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
