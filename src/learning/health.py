"""Live AD-5 counting and the personal health record (ADR 0019; stage L4.1).

After the shadow block of each run, :func:`update_health`:

1. turns new positives (offers whose aggregated signal reached y ≥ +1.0) into ``shadow_positive``
   rows, **written once**: anchor run (rule 1: latest visible exposure at or before the positive, the
   exposure mapped to the latest run started at or before it; rule 2: latest run that ranked the
   offer before it), both stored positions of that run, stratum from every surface the offer was
   seen on in the 14 days before, session, and the state at the time;
2. counts the evidence (:func:`src.learning.gates.evidence`), evaluates the activation rule
   (:func:`~src.learning.gates.decide`) and the health monitor (:func:`~src.learning.gates.monitor`);
3. stores the result in ``personal_health`` (one row).

**Release flags.** ``activation_enabled`` and ``experimental_enabled`` (``personal_model.activation``
in ``config/personalization.yaml``) are both off. With activation off the decision is recorded
(``decision.state`` shows what the rule would allow) but the stored state never leaves SHADOW: the
L3 simulator found the rule misses its criteria (``docs/reports/2026-11-05-l3-simulator.md``), and
enabling it is the owner's decision.

Both tables are derived personal data (ADR 0015): Reset learning clears them. A change of the
model's algorithm or configuration returns an ACTIVE or EXPERIMENTAL state to SHADOW (ADR 0019:
every release change re-earns activation). The caller fails open.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from src.learning import gates as G
from src.learning.config import load_personal_model_config
from src.learning.model import ALGORITHM_VERSION
from src.learning.shadow import _reset_at
from src.learning.signals import offer_signals

DDL = """
CREATE TABLE IF NOT EXISTS shadow_positive (
    job_id TEXT PRIMARY KEY,
    effective_at TEXT NOT NULL,
    anchor_run_id TEXT NOT NULL,
    anchor_rule TEXT NOT NULL,
    production_position INTEGER NOT NULL,
    challenger_position INTEGER NOT NULL,
    stratum TEXT,
    session_id TEXT,
    state_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS personal_health (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    state_since TEXT NOT NULL,
    cap_factor REAL NOT NULL DEFAULT 1.0,
    halvings_json TEXT NOT NULL DEFAULT '[]',
    release_key TEXT NOT NULL,
    decision_json TEXT NOT NULL DEFAULT '{}',
    flags_json TEXT NOT NULL DEFAULT '{}',
    last_event TEXT,
    run_id TEXT,
    updated_at TEXT NOT NULL
);
"""

SURFACE_CODES = {"jobs": "S1", "decouverte": "S4", "search": "S2", "lanes": "S3", "external": "S5"}
MONITOR_DAYS = 30


@dataclass(frozen=True)
class ReleaseFlags:
    activation_enabled: bool = False
    experimental_enabled: bool = False


def load_release_flags(path: str | Path = "config/personalization.yaml") -> ReleaseFlags:
    """``personal_model.activation`` flags; a missing file or key keeps both off."""
    try:
        data: Any = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ReleaseFlags()
    section = ((data.get("personal_model") or {}).get("activation") or {}) if isinstance(data, dict) else {}
    if not isinstance(section, dict):
        return ReleaseFlags()
    return ReleaseFlags(activation_enabled=section.get("activation_enabled") is True,
                        experimental_enabled=section.get("experimental_enabled") is True)


def surface_code(surface: str | None, sort_key: str | None = None) -> str:
    """App surface name → ADR 0016 code. ``job_detail`` and unknown names are S0 (never counted): the
    list exposure that led to the detail page already carries the stratum."""
    code = SURFACE_CODES.get((surface or "").lower(), "S0")
    if code == "S1" and (sort_key or "").lower() in G.LB_SORTS:
        return "S2"                          # the list sorted by date/distance, not by production
    return code


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class _Seen:
    at: datetime
    surface: str
    sort_key: str | None
    session_id: str | None


def _exposures(conn: sqlite3.Connection, job_id: str, reset_at: str | None) -> list[_Seen]:
    try:
        rows = conn.execute("SELECT occurred_at, client_created_at, surface, sort_key, session_id FROM exposure_log "
                            "WHERE job_id = ? ORDER BY id", (job_id,)).fetchall()
    except sqlite3.OperationalError:
        return []
    out = [_Seen(_parse(r[1] or r[0]), surface_code(r[2], r[3]), r[3], r[4]) for r in rows]
    return [s for s in out if reset_at is None or s.at >= _parse(reset_at)]


def _runs(conn: sqlite3.Connection) -> list[tuple[str, datetime]]:
    try:
        return sorted(((r, _parse(t)) for r, t in conn.execute("SELECT id, started_at FROM pipeline_runs")),
                      key=lambda x: x[1])
    except sqlite3.OperationalError:
        return []


def _run_at(runs: Sequence[tuple[str, datetime]], at: datetime) -> str | None:
    latest = None
    for run_id, started in runs:
        if started <= at:
            latest = run_id
        else:
            break
    return latest


def _ranked_runs(conn: sqlite3.Connection, job_id: str, runs: dict[str, datetime]) -> list[tuple[str, datetime]]:
    return [(r, runs[r]) for (r,) in conn.execute("SELECT run_id FROM ranking_shadow WHERE job_id = ?", (job_id,))
            if r in runs]


def _positions(conn: sqlite3.Connection, run_id: str, job_id: str) -> tuple[int, int] | None:
    row = conn.execute("SELECT production_position, challenger_position FROM ranking_shadow "
                       "WHERE run_id = ? AND job_id = ?", (run_id, job_id)).fetchone()
    return (int(row[0]), int(row[1])) if row else None


def _health_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    row: sqlite3.Row | None = conn.execute("SELECT * FROM personal_health WHERE id = 1").fetchone()
    return row


def record_positives(conn: sqlite3.Connection, positives: Sequence[tuple[str, str]], reset_at: str | None,
                     state: G.State, now: datetime, window_days: int = 14) -> int:
    """Write a ``shadow_positive`` row for each (job id, effective time) not recorded yet and anchored
    now. Rows are never updated. → number written."""
    runs = _runs(conn)
    started = dict(runs)
    known = {r[0] for r in conn.execute("SELECT job_id FROM shadow_positive")}
    written = 0
    for job_id, effective_at in positives:
        if job_id in known:
            continue
        at = _parse(effective_at)
        seen = [s for s in _exposures(conn, job_id, reset_at) if s.at <= at]
        exposures = [G.Exposure(run_id, s.at, s.surface, s.sort_key) for s in seen
                     if (run_id := _run_at(runs, s.at)) is not None]
        a = G.anchor(at, exposures, _ranked_runs(conn, job_id, started), window_days)
        if a is None:
            continue
        pos = _positions(conn, a.run_id, job_id)
        if pos is None:
            continue
        window = [s for s in seen if at - s.at <= timedelta(days=window_days)]
        stratum = G.stratum_of((s.surface, s.sort_key) for s in window)
        session = window[-1].session_id if window else None
        conn.execute("INSERT INTO shadow_positive (job_id, effective_at, anchor_run_id, anchor_rule, "
                     "production_position, challenger_position, stratum, session_id, state_at, recorded_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (job_id, effective_at, a.run_id, a.rule, pos[0], pos[1], stratum, session, state,
                      now.isoformat(timespec="seconds")))
        written += 1
    return written


def _stored_positives(conn: sqlite3.Connection) -> list[G.Positive]:
    return [G.Positive(r[0], r[1], _parse(r[2]), int(r[3]), int(r[4]), r[5], r[6])
            for r in conn.execute("SELECT job_id, session_id, effective_at, production_position, "
                                  "challenger_position, stratum, state_at FROM shadow_positive")]


def _window(conn: sqlite3.Connection, reset_at: str | None, now: datetime) -> G.Window:
    """G1 exposure: days since the first visible exposure after the reset, and distinct sessions."""
    try:
        rows = conn.execute("SELECT occurred_at, session_id FROM exposure_log").fetchall()
    except sqlite3.OperationalError:
        rows = []
    times = [(_parse(t), s) for t, s in rows if reset_at is None or _parse(t) >= _parse(reset_at)]
    if not times:
        return G.Window(days=0, sessions=0)
    first = min(t for t, _ in times)
    return G.Window(days=(now - first).total_seconds() / 86400, sessions=len({s for _, s in times if s}))


def release_key(config_path: str | Path) -> str:
    return f"{ALGORITHM_VERSION}+{load_personal_model_config(config_path).digest()}"


def update_health(db_path: str | Path, run_id: str | None, *, config_path: str | Path,
                  now: datetime | None = None, guardrails_green: bool = True,
                  params: G.GateParams | None = None) -> dict[str, Any]:
    """Record new positives, evaluate the rule and the monitor, store ``personal_health`` → its row."""
    now = now or datetime.now(UTC)
    flags = load_release_flags(config_path)
    base = params or G.GateParams()
    params = replace(base, experimental_enabled=flags.experimental_enabled)
    cfg = load_personal_model_config(config_path)
    key = release_key(config_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        ensure_schema(conn)
        reset_at = _reset_at(conn)
        row = _health_row(conn)
        state: G.State = row["state"] if row else G.SHADOW
        since = _parse(row["state_since"]) if row else now
        health = G.Health(state, float(row["cap_factor"]) if row else 1.0,
                          tuple(_parse(t) for t in json.loads(row["halvings_json"])) if row else ())
        event: str | None = None
        if row and row["release_key"] != key and state in (G.ACTIVE, G.EXPERIMENTAL):
            state, health, since, event = G.SHADOW, G.Health(G.SHADOW), now, "release_changed"
        with conn:
            signals = offer_signals(conn, cfg, reset_at=reset_at, now=now)
            record_positives(conn, [(s.job_id, s.effective_at) for s in signals if s.is_positive],
                             reset_at, state, now, params.window_days)
        positives = _stored_positives(conn)
        # after a pause, resuming needs a fresh window: only positives received since the pause count
        counted = [p for p in positives if p.effective_at >= since] if state == G.PAUSED else positives
        ev = G.evidence(counted, params)
        window = replace(_window(conn, reset_at, now), guardrails_green=guardrails_green)
        if state in (G.ACTIVE, G.EXPERIMENTAL):
            cutoff = max(since, now - timedelta(days=MONITOR_DAYS))
            recent = [p for p in positives if p.discordant and p.effective_at >= cutoff and p.state_at == state]
            health, mon = G.monitor(health, now, G.Tally(len(recent), sum(p.challenger_wins for p in recent)),
                                    params, guardrails_green=guardrails_green)
            if mon:
                event = mon
            if health.state != state:
                state, since = health.state, now
        decision = G.decide(state, window, ev, params, days_in_state=(now - since).total_seconds() / 86400)
        allowed = decision.state
        if allowed == G.ACTIVE and not flags.activation_enabled and state != G.ACTIVE:
            allowed = G.SHADOW                 # release flag off: the decision is recorded, never applied
        if event == "release_changed":
            allowed = G.SHADOW                 # a new release re-earns activation on a later run
        if state == G.PAUSED and allowed != G.ACTIVE:
            allowed = G.PAUSED                 # a pause ends on a full fresh window or a manual resume
        if allowed != state and event not in ("paused", "to_shadow", "halved"):
            event = f"to_{allowed}"
            state, since = allowed, now
            health = G.Health(state)
        record = {
            "state": state, "state_since": since.isoformat(timespec="seconds"), "cap_factor": health.cap_factor,
            "halvings": [t.isoformat(timespec="seconds") for t in health.halvings], "release_key": key,
            "decision": {"would_be": decision.state, "gates": decision.gates, "lb_enough": decision.lb_enough,
                         "g3b_pass": decision.g3b_pass, "p_pooled": decision.p_pooled,
                         "positives": ev.positives, "pooled": asdict(ev.pooled),
                         "strata": {k: asdict(v) for k, v in sorted(ev.strata.items())},
                         "sessions": window.sessions, "days": round(window.days, 2)},
            "flags": asdict(flags), "last_event": event, "run_id": run_id,
            "updated_at": now.isoformat(timespec="seconds"),
        }
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO personal_health (id, state, state_since, cap_factor, halvings_json, "
                "release_key, decision_json, flags_json, last_event, run_id, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (state, record["state_since"], health.cap_factor, json.dumps(record["halvings"]), key,
                 json.dumps(record["decision"], sort_keys=True), json.dumps(record["flags"]), event, run_id,
                 record["updated_at"]))
        return record
    finally:
        conn.close()


def current_health(db_path: str | Path) -> dict[str, Any] | None:
    conn = sqlite3.connect(str(db_path))
    try:
        row = _health_row(conn)
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return {"state": row["state"], "state_since": row["state_since"], "cap_factor": row["cap_factor"],
            "halvings": json.loads(row["halvings_json"]), "decision": json.loads(row["decision_json"]),
            "flags": json.loads(row["flags_json"]), "last_event": row["last_event"], "run_id": row["run_id"],
            "updated_at": row["updated_at"]}


def resume(db_path: str | Path, now: datetime | None = None) -> bool:
    """Manual "Reprendre" after a pause: back to SHADOW, so the full rule must pass again (guarded;
    the monitor stays on). → False when not paused."""
    now = now or datetime.now(UTC)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        with conn:
            cur = conn.execute("UPDATE personal_health SET state = ?, state_since = ?, cap_factor = 1.0, "
                               "halvings_json = '[]', last_event = 'resumed', updated_at = ? "
                               "WHERE id = 1 AND state = ?",
                               (G.SHADOW, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                                G.PAUSED))
        return cur.rowcount == 1
    finally:
        conn.close()


def pause(db_path: str | Path, reason: str, now: datetime | None = None) -> bool:
    """Guardrail failure during a run (fault injection): ACTIVE/EXPERIMENTAL → PAUSED at once, so the
    next run uses production. → False when there was nothing to pause."""
    now = now or datetime.now(UTC)
    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        with conn:
            cur = conn.execute("UPDATE personal_health SET state = ?, state_since = ?, last_event = ?, updated_at = ? "
                               "WHERE id = 1 AND state IN (?, ?)",
                               (G.PAUSED, now.isoformat(timespec="seconds"), f"paused:{reason}",
                                now.isoformat(timespec="seconds"), G.ACTIVE, G.EXPERIMENTAL))
        return cur.rowcount == 1
    finally:
        conn.close()


def visible_ranking(production: Sequence[tuple[Any, float]], adjustments: dict[str, float], record: dict[str, Any],
                    gate_cap: Callable[[list[tuple[Any, float]], list[tuple[Any, float]]], list[tuple[Any, float]]],
                    ) -> list[tuple[Any, float]] | None:
    """The ranking the user sees (stage L4.2) → None means production.

    Only an ACTIVE record with the activation flag on changes it: the challenger adjustment (already
    within ``cap(n)``) is scaled by the monitor's ``cap_factor``, then the scoring gates are re-applied.
    Scores in the result are ordering keys only; stored scores stay the production ones. Raises
    ``ValueError`` when the result is not a permutation of production (a guardrail failure)."""
    if record.get("state") != G.ACTIVE or not record.get("flags", {}).get("activation_enabled"):
        return None
    factor = float(record.get("cap_factor", 1.0))
    ranked = sorted(((job, score + factor * adjustments.get(job.id, 0.0)) for job, score in production),
                    key=lambda p: p[1], reverse=True)
    ranked = gate_cap(list(production), ranked)
    if sorted(job.id for job, _ in ranked) != sorted(job.id for job, _ in production):
        raise ValueError("personal ranking is not a permutation of production")
    return ranked
