"""Live AD-5 counting and the personal health record (ADR 0019; stage L4.1)."""

from __future__ import annotations

import itertools
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.feedback.exposure import ExposureStorage
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage
from src.learning import gates as G
from src.learning import health as H
from src.learning.shadow import ensure_schema as shadow_schema

T0 = datetime(2026, 9, 1, tzinfo=UTC)
_ids = itertools.count()
BASE_CONFIG = Path("config/personalization.yaml").read_text(encoding="utf-8")


def _config(tmp_path: Path, *, activation: bool = False, extra: str = "") -> Path:
    p = tmp_path / f"personalization-{activation}-{len(extra)}.yaml"
    text = BASE_CONFIG.replace("activation_enabled: false", f"activation_enabled: {str(activation).lower()}")
    p.write_text(text + extra, encoding="utf-8")
    return p


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "jobs.db")
    fb = FeedbackStorage(db_path=path)
    fb.init_db()
    ex = ExposureStorage(db_path=path)
    ex.init_db()
    conn = sqlite3.connect(path)
    shadow_schema(conn)
    conn.executescript("CREATE TABLE IF NOT EXISTS pipeline_runs (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, "
                       "completed_at TEXT, status TEXT NOT NULL DEFAULT 'running', "
                       "stats_json TEXT NOT NULL DEFAULT '{}', error TEXT);")
    conn.commit()
    conn.close()
    yield path, fb
    fb.close()
    ex.close()


def offer(path: str, fb: FeedbackStorage, job: str, at: datetime, *, surface: str = "jobs", win: bool = True,
          discordant: bool = True, session: str | None = None, positive: bool = True) -> None:
    """A run one hour before ``at`` ranks the offer; it is visible at ``at``; saved one hour later."""
    run = f"run-{job}"
    prod, chal = (20, 5) if win else (5, 20)
    if not discordant:
        prod = chal = 10
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO pipeline_runs (id, started_at, status) VALUES (?, ?, 'completed')",
                 (run, (at - timedelta(hours=1)).isoformat()))
    conn.execute("INSERT INTO ranking_shadow (run_id, job_id, production_position, challenger_position, "
                 "personal_adjustment, model_version) VALUES (?, ?, ?, ?, 0.0, 'v')", (run, job, prod, chal))
    conn.execute("INSERT INTO exposure_log (job_id, surface, position, session_id, generation, occurred_at) "
                 "VALUES (?, ?, 1, ?, 0, ?)", (job, surface, session or f"s-{job}", at.isoformat()))
    conn.commit()
    conn.close()
    if positive:
        fb.append(FeedbackEvent(event_id=f"e{next(_ids)}", job_id=job, signal_class="list", type="saved", reasons=[],
                                source="ui", occurred_at=(at + timedelta(hours=1)).isoformat(timespec="seconds"),
                                metadata={}))


def strong_history(path: str, fb: FeedbackStorage, start: datetime = T0, n: int = 24) -> None:
    """24 positives over 24 half-days in distinct sessions: 20 challenger wins, 8 of them on Découverte."""
    for i in range(n):
        offer(path, fb, f"j{i}", start + timedelta(hours=12 * i), surface="decouverte" if i < 8 else "jobs",
              win=i < 20)


def shadow_rows(path: str) -> list[tuple]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT * FROM shadow_positive ORDER BY job_id").fetchall()
    finally:
        conn.close()


def test_surface_mapping():
    assert H.surface_code("jobs") == "S1" and H.surface_code("decouverte") == "S4"
    assert H.surface_code("jobs", "date") == "S2"
    assert H.surface_code("job_detail") == "S0" and H.surface_code("whatever") == "S0" and H.surface_code(None) == "S0"


def test_release_flags_default_off(tmp_path):
    assert H.load_release_flags(Path("config/personalization.yaml")) == H.ReleaseFlags()
    assert H.load_release_flags(tmp_path / "missing.yaml") == H.ReleaseFlags()
    assert H.load_release_flags(_config(tmp_path, activation=True)).activation_enabled is True


def test_positives_are_anchored_stratified_and_written_once(db, tmp_path):
    path, fb = db
    offer(path, fb, "a", T0, surface="decouverte")
    offer(path, fb, "b", T0 + timedelta(days=1), surface="jobs", win=False)
    offer(path, fb, "c", T0 + timedelta(days=2), positive=False)                 # no positive → no row
    cfg = _config(tmp_path)
    H.update_health(path, "r", config_path=cfg, now=T0 + timedelta(days=3))
    rows = shadow_rows(path)
    assert [(r[0], r[2], r[3], r[4], r[5], r[6]) for r in rows] == [
        ("a", "run-a", "visible", 20, 5, "LB"), ("b", "run-b", "visible", 5, 20, "PF")]
    H.update_health(path, "r2", config_path=cfg, now=T0 + timedelta(days=4))
    assert shadow_rows(path) == rows                                              # never rewritten


def test_flag_off_records_the_decision_but_stays_in_shadow(db, tmp_path):
    path, fb = db
    strong_history(path, fb)
    rec = H.update_health(path, "r", config_path=_config(tmp_path), now=T0 + timedelta(days=20))
    assert rec["decision"]["would_be"] == G.ACTIVE and rec["state"] == G.SHADOW
    assert rec["flags"] == {"activation_enabled": False, "experimental_enabled": False}
    assert H.current_health(path)["state"] == G.SHADOW


def test_flag_on_activates_then_fault_injection_pauses(db, tmp_path):
    path, fb = db
    strong_history(path, fb)
    cfg = _config(tmp_path, activation=True)
    rec = H.update_health(path, "r", config_path=cfg, now=T0 + timedelta(days=20))
    assert rec["state"] == G.ACTIVE and rec["last_event"] == "to_active"
    rec = H.update_health(path, "r2", config_path=cfg, now=T0 + timedelta(days=21), guardrails_green=False)
    assert rec["state"] == G.PAUSED and rec["last_event"] == "paused"
    rec = H.update_health(path, "r3", config_path=cfg, now=T0 + timedelta(days=22))
    assert rec["state"] == G.PAUSED                                             # no fresh evidence yet
    assert H.resume(path, now=T0 + timedelta(days=23)) and H.current_health(path)["state"] == G.SHADOW
    assert not H.resume(path)                                                    # only from paused


def test_contradicting_sequence_halves_then_pauses(db, tmp_path):
    path, fb = db
    strong_history(path, fb)
    cfg = _config(tmp_path, activation=True)
    active_at = T0 + timedelta(days=20)
    assert H.update_health(path, "r", config_path=cfg, now=active_at)["state"] == G.ACTIVE
    for i in range(8):
        offer(path, fb, f"x{i}", active_at + timedelta(hours=6 * i + 1), win=False)
    rec = H.update_health(path, "r2", config_path=cfg, now=active_at + timedelta(days=3))
    assert rec["state"] == G.ACTIVE and rec["last_event"] == "halved" and rec["cap_factor"] == 0.5
    for i in range(4):
        offer(path, fb, f"y{i}", active_at + timedelta(days=3, hours=6 * i + 1), win=False)
    rec = H.update_health(path, "r3", config_path=cfg, now=active_at + timedelta(days=5))
    assert rec["state"] == G.PAUSED and rec["last_event"] == "paused"


def test_release_change_returns_to_shadow(db, tmp_path):
    path, fb = db
    strong_history(path, fb)
    assert H.update_health(path, "r", config_path=_config(tmp_path, activation=True),
                           now=T0 + timedelta(days=20))["state"] == G.ACTIVE
    changed = _config(tmp_path, activation=True).read_text(encoding="utf-8").replace("cap_max: 0.15", "cap_max: 0.10")
    cfg2 = tmp_path / "changed.yaml"
    cfg2.write_text(changed, encoding="utf-8")
    rec = H.update_health(path, "r2", config_path=cfg2, now=T0 + timedelta(days=21))
    assert rec["state"] == G.SHADOW and rec["last_event"] == "release_changed"


def test_thin_lb_never_activates_even_with_the_flag_on(db, tmp_path):
    path, fb = db
    for i in range(24):
        offer(path, fb, f"j{i}", T0 + timedelta(hours=12 * i), surface="jobs", win=i < 20)
    rec = H.update_health(path, "r", config_path=_config(tmp_path, activation=True), now=T0 + timedelta(days=20))
    assert rec["state"] == G.SHADOW and rec["decision"]["lb_enough"] is False


def test_reset_clears_health_tables():
    from src.storage.data_classes import JOBS_DB_TABLES, RESET_CLEARED_TABLES

    assert {"shadow_positive", "personal_health"} <= set(RESET_CLEARED_TABLES)
    assert JOBS_DB_TABLES["shadow_positive"] == JOBS_DB_TABLES["personal_health"] == "derived"
