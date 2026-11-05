"""The AD-5 activation rule and health monitor as pure functions (ADR 0019, v5 §4.5; stage L3.3).

Nothing here reads or writes the database. Stage L4 feeds it the live journal; the simulator
(:mod:`src.learning.simulator`) feeds it synthetic histories. Every threshold is an initial,
unapproved value in :class:`GateParams`; n_LB,min, c_contra, c_het and the margin m are set by the
simulator.

Vocabulary:

* **positive**: an offer whose aggregated value reached y ≥ +1.0, counted once, anchored to one run;
* **discordant**: the two stored rankings of the anchor run place it ≥ 3 positions apart (positions
  beyond 100 count as 101); the **winner** is the ranking that placed it higher;
* **strata**: LB (S4, and S2 sorted by date or distance), IM (S3), PF (S1, S5, S2 by relevance or
  unknown sort). A positive is LB only if every surface it was seen on was LB; any PF exposure makes
  it PF; otherwise any S3 exposure makes it IM. S0 (unknown) is never counted.

The posterior is a descriptive measure of agreement with observed choices under an independence
assumption, not causal proof (ADR 0019 wording rule).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import numpy as np

Stratum = Literal["LB", "IM", "PF"]
State = Literal["shadow", "experimental_unconfirmed", "active", "paused"]

SHADOW: State = "shadow"
EXPERIMENTAL: State = "experimental_unconfirmed"
ACTIVE: State = "active"
PAUSED: State = "paused"

LB_SORTS = frozenset({"date", "distance", "published_at", "newest"})
POSITION_CEILING = 101
DISCORDANCE = 3


@dataclass(frozen=True)
class GateParams:
    # G1 exposure
    min_days: int = 14
    min_sessions: int = 10
    # G2 evidence
    min_positives: int = 20
    min_discordant: int = 12
    per_session_cap: int = 3
    # G3 pooled posterior
    g3_threshold: float = 0.95
    # G3b contradiction guard (set by the simulator; see docs/reports/…-l3-simulator.md)
    n_lb_min: int = 6
    c_contra: float = 0.80
    c_het: float = 0.80
    margin: float = 0.10
    # positive window
    window_days: int = 14
    # EXPERIMENTAL ships disabled (owner decision, 2026-11-05)
    experimental_enabled: bool = False
    experimental_timeout_days: int = 60
    # health monitor, ACTIVE
    halve_min_discordant: int = 8
    halve_p_worse: float = 0.80
    rate_drop: float = 0.50
    rate_min_baseline: int = 6
    second_halving_days: int = 30
    # health monitor, EXPERIMENTAL (stricter)
    exp_halve_min_discordant: int = 6
    exp_halve_p_worse: float = 0.70


# -- strata and positives ------------------------------------------------------------------------

def surface_stratum(surface: str, sort_key: str | None = None) -> Stratum | None:
    """Stratum of one exposure; None for S0 (unknown)."""
    if surface == "S4":
        return "LB"
    if surface == "S2":
        return "LB" if (sort_key or "").lower() in LB_SORTS else "PF"
    if surface == "S3":
        return "IM"
    if surface in ("S1", "S5"):
        return "PF"
    return None


def stratum_of(seen: Iterable[tuple[str, str | None]]) -> Stratum | None:
    """Stratum of a positive from every (surface, sort key) it was seen on in the window before it."""
    strata = {s for surface, sort_key in seen if (s := surface_stratum(surface, sort_key)) is not None}
    if not strata:
        return None
    if "PF" in strata:
        return "PF"
    if "IM" in strata:
        return "IM"
    return "LB"


@dataclass(frozen=True)
class Exposure:
    run_id: str
    first_visible_at: datetime
    surface: str
    sort_key: str | None = None


@dataclass(frozen=True)
class Anchor:
    run_id: str
    rule: Literal["visible", "ranked"]
    anchored_at: datetime


def anchor(positive_at: datetime, exposures: Sequence[Exposure],
           ranked_runs: Sequence[tuple[str, datetime]], window_days: int = 14) -> Anchor | None:
    """ADR 0019 run association: the latest visible exposure at or before the positive, else the
    latest run that ranked the offer and started before it; None when neither exists or the positive
    falls outside the window."""
    visible = [e for e in exposures if e.first_visible_at <= positive_at]
    if visible:
        e = max(visible, key=lambda x: x.first_visible_at)
        a = Anchor(e.run_id, "visible", e.first_visible_at)
    else:
        ranked = [(r, t) for r, t in ranked_runs if t < positive_at]
        if not ranked:
            return None
        r, t = max(ranked, key=lambda x: x[1])
        a = Anchor(r, "ranked", t)
    return a if positive_at - a.anchored_at <= timedelta(days=window_days) else None


@dataclass(frozen=True)
class Positive:
    job_id: str
    session_id: str | None
    effective_at: datetime
    production_position: int
    challenger_position: int
    stratum: Stratum | None
    state_at: State = SHADOW

    @property
    def discordant(self) -> bool:
        p, c = min(self.production_position, POSITION_CEILING), min(self.challenger_position, POSITION_CEILING)
        return abs(p - c) >= DISCORDANCE

    @property
    def challenger_wins(self) -> bool:
        return min(self.challenger_position, POSITION_CEILING) < min(self.production_position, POSITION_CEILING)


@dataclass(frozen=True)
class Tally:
    discordant: int = 0
    wins: int = 0


@dataclass(frozen=True)
class Evidence:
    positives: int
    pooled: Tally
    strata: dict[str, Tally] = field(default_factory=dict)

    def stratum(self, name: str) -> Tally:
        return self.strata.get(name, Tally())


def evidence(positives: Sequence[Positive], params: GateParams = GateParams()) -> Evidence:
    """Count positives (one per offer), then discordant positives under the per-session cap, pooled and
    per stratum. S0 positives are excluded; positives received while EXPERIMENTAL on PF or IM surfaces
    are excluded from the counts (they would favour the challenger)."""
    seen: set[str] = set()
    counted: list[Positive] = []
    for p in sorted(positives, key=lambda x: (x.effective_at, x.job_id)):
        if p.job_id in seen or p.stratum is None:
            continue
        seen.add(p.job_id)
        if p.state_at == EXPERIMENTAL and p.stratum in ("PF", "IM"):
            continue
        counted.append(p)
    per_session: dict[str, int] = {}
    pooled = [0, 0]
    strata: dict[str, list[int]] = {}
    for i, p in enumerate(counted):
        if not p.discordant:
            continue
        key = p.session_id or f"__solo{i}"
        if per_session.get(key, 0) >= params.per_session_cap:
            continue
        per_session[key] = per_session.get(key, 0) + 1
        win = int(p.challenger_wins)
        pooled[0] += 1
        pooled[1] += win
        s = strata.setdefault(p.stratum or "", [0, 0])
        s[0] += 1
        s[1] += win
    return Evidence(len(counted), Tally(*pooled), {k: Tally(*v) for k, v in strata.items()})


# -- posteriors ----------------------------------------------------------------------------------

def p_better(wins: int, discordant: int) -> float:
    """Exact P(win rate > 0.5) under Beta(1 + wins, 1 + losses): P(Bin(n + 1, ½) ≤ wins)."""
    n = discordant + 1
    return sum(math.comb(n, k) for k in range(wins + 1)) / 2 ** n


def p_worse(wins: int, discordant: int) -> float:
    return 1.0 - p_better(wins, discordant)


_GRID = np.linspace(0.0, 1.0, 4001)


def _trapezoid(y: np.ndarray, x: np.ndarray) -> float:
    """Trapezoidal integral (``np.trapezoid`` exists only from numpy 2; numpy >= 1.26 is supported)."""
    return float(np.sum((y[1:] + y[:-1]) / 2 * np.diff(x)))


def _beta_pdf(a: float, b: float) -> np.ndarray:
    x = _GRID[1:-1]
    log = (a - 1) * np.log(x) + (b - 1) * np.log1p(-x) + math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    pdf = np.concatenate([[0.0], np.exp(log), [0.0]])
    return pdf / _trapezoid(pdf, _GRID)


def p_less_with_margin(lb: Tally, pf: Tally, margin: float) -> float:
    """P(p_LB < p_PF − m) for independent Beta(1, 1) posteriors, by numerical integration."""
    f_lb = _beta_pdf(1 + lb.wins, 1 + lb.discordant - lb.wins)
    f_pf = _beta_pdf(1 + pf.wins, 1 + pf.discordant - pf.wins)
    cdf_pf = np.concatenate([[0.0], np.cumsum((f_pf[1:] + f_pf[:-1]) / 2 * np.diff(_GRID))])
    survival = 1.0 - np.interp(_GRID + margin, _GRID, cdf_pf, right=1.0)
    return float(np.clip(_trapezoid(f_lb * survival, _GRID), 0.0, 1.0))


# -- gates and states ----------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    days: float
    sessions: int
    guardrails_green: bool = True
    pause_condition: bool = False


@dataclass(frozen=True)
class Decision:
    gates: dict[str, bool]
    lb_enough: bool
    g3b_pass: bool
    state: State
    p_pooled: float


def g3b(ev: Evidence, params: GateParams) -> tuple[bool, bool]:
    """→ (LB reached n_LB,min, guard passes). A vacuous guard (LB too small) never passes."""
    lb, pf = ev.stratum("LB"), ev.stratum("PF")
    if lb.discordant < params.n_lb_min:
        return False, False
    contradicts = p_worse(lb.wins, lb.discordant) >= params.c_contra
    disagree = pf.discordant >= params.n_lb_min and p_less_with_margin(lb, pf, params.margin) >= params.c_het
    return True, not (contradicts or disagree)


def decide(current: State, window: Window, ev: Evidence, params: GateParams = GateParams(),
           days_in_state: float = 0.0) -> Decision:
    """The state the rule allows now. ``paused`` evaluates like SHADOW on a fresh window (the caller
    passes that window's evidence)."""
    p = p_better(ev.pooled.wins, ev.pooled.discordant)
    gates = {
        "G1": window.days >= params.min_days and window.sessions >= params.min_sessions,
        "G2": ev.positives >= params.min_positives and ev.pooled.discordant >= params.min_discordant,
        "G3": p >= params.g3_threshold,
        "G4": window.guardrails_green,
        "G5": not window.pause_condition,
    }
    lb_enough, guard = g3b(ev, params)
    base = all(gates.values())
    if base and lb_enough and guard:
        state: State = ACTIVE
    elif current == EXPERIMENTAL:
        timed_out = days_in_state >= params.experimental_timeout_days
        state = EXPERIMENTAL if base and not lb_enough and not timed_out and params.experimental_enabled else SHADOW
    elif base and not lb_enough and params.experimental_enabled:
        state = EXPERIMENTAL
    else:
        state = SHADOW if current != ACTIVE else ACTIVE
    if current == ACTIVE and state == SHADOW:
        state = ACTIVE          # leaving ACTIVE is the monitor's job, never the activation rule's
    return Decision(gates, lb_enough, guard, state, p)


# -- health monitor -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Health:
    state: State
    cap_factor: float = 1.0
    halvings: tuple[datetime, ...] = ()


def monitor(health: Health, now: datetime, recent: Tally, params: GateParams = GateParams(), *,
            guardrails_green: bool = True, top20_rate: float | None = None,
            trailing_median: float | None = None, baseline_positives: int = 0) -> tuple[Health, str | None]:
    """One monitor step for ACTIVE or EXPERIMENTAL → (new health, event). Events: ``halved``,
    ``paused`` (ACTIVE → paused), ``to_shadow`` (EXPERIMENTAL → shadow). Easier to pause than to
    activate: fewer events and a lower probability."""
    if health.state not in (ACTIVE, EXPERIMENTAL):
        return health, None
    exp = health.state == EXPERIMENTAL
    stop: State = SHADOW if exp else PAUSED
    stop_event = "to_shadow" if exp else "paused"
    if not guardrails_green:
        return Health(stop), stop_event
    min_n, p_min = ((params.exp_halve_min_discordant, params.exp_halve_p_worse) if exp
                    else (params.halve_min_discordant, params.halve_p_worse))
    worse = recent.discordant >= min_n and p_worse(recent.wins, recent.discordant) >= p_min
    dropped = (not exp and top20_rate is not None and trailing_median is not None
               and baseline_positives >= params.rate_min_baseline
               and top20_rate < (1 - params.rate_drop) * trailing_median)
    if not (worse or dropped):
        return health, None
    recent_halvings = [h for h in health.halvings if now - h <= timedelta(days=params.second_halving_days)]
    if recent_halvings:
        return Health(stop), stop_event
    return Health(health.state, health.cap_factor / 2, (*health.halvings, now)), "halved"
