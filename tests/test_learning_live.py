"""The personal order the user sees: bounded, evidence-gated, explained, correctable (ADR 0030)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.learning import live
from src.learning.config import PersonalModelConfig
from src.learning.model import fit, offer_terms
from src.learning.shadow import compute_shadow, ensure_schema, current_state
from src.learning.signals import ALL_FEATURES, CONTENT, TERMS, OfferSignal
from src.models.job import Job


def _job(job_id: str, title: str, source: str = "eures") -> Job:
    return Job(id=job_id, source=source, title=title, description="", canonical_url=f"https://x/{job_id}")


def _signal(job_id: str, y: float, features=ALL_FEATURES) -> OfferSignal:
    return OfferSignal(job_id, y, frozenset(features), "2026-11-01T00:00:00+00:00", 1.0, "t")


def test_offer_terms_keep_what_names_the_role() -> None:
    assert offer_terms("Technicien Réseaux H/F - CDI", ["Cisco", "VLAN"]) == ("technicien", "reseau", "cisco", "vlan")


def test_a_single_opening_moves_much_less_than_a_saved_offer() -> None:
    cfg = PersonalModelConfig()
    values = {"a": {TERMS: ("reseau",)}}
    opened = fit([_signal("a", 0.25, CONTENT)], {}, values, None, cfg)
    saved = fit([_signal("a", 1.0)], {}, values, None, cfg)
    assert 0 < opened.adjustment(None, values["a"]) < saved.adjustment(None, values["a"]) / 3
    assert opened.strong == 0 and saved.strong == 1


def test_evidence_scaled_cap_bounds_the_effect() -> None:
    cfg = PersonalModelConfig()
    values = {f"j{i}": {TERMS: ("reseau",)} for i in range(50)}
    model = fit([_signal(f"j{i}", 2.0) for i in range(50)], {}, values, None, cfg)
    assert model.adjustment(None, {TERMS: ("reseau",)}) <= cfg.cap(50) <= cfg.cap_max


def test_forgotten_preference_stops_counting() -> None:
    cfg = PersonalModelConfig()
    values = {"a": {TERMS: ("reseau", "cisco")}}
    model = fit([_signal("a", 1.0)], {}, values, None, cfg, forgotten={(TERMS, "reseau")})
    assert set(model.tables[TERMS]) == {"cisco"}


def test_reasons_name_the_matched_preferences() -> None:
    values = {"a": {TERMS: ("reseau",), "remote": "hybrid"}}
    model = fit([_signal("a", 1.0)], {}, values, None, PersonalModelConfig())
    assert {(r["feature"], r["value"]) for r in model.reasons(None, values["a"])} == {(TERMS, "reseau"), ("remote", "hybrid")}


@pytest.mark.parametrize(("strong", "live_on", "expected"), [(2, 1.0, None), (3, 0.0, None)])
def test_live_order_keeps_production_without_evidence_or_when_off(strong: int, live_on: float, expected: None) -> None:
    cfg = PersonalModelConfig(live_ranking=live_on)
    production = [(SimpleNamespace(id="a"), 0.5)]
    assert live.live_order(production, {"a": 0.1}, strong, cfg, lambda p, c: c) is expected


def test_live_order_reorders_within_the_gates() -> None:
    a, b = SimpleNamespace(id="a"), SimpleNamespace(id="b")
    ranked = live.live_order([(a, 0.6), (b, 0.55)], {"b": 0.1}, 3, PersonalModelConfig(),
                             lambda prod, chal: sorted(chal, key=lambda p: p[1], reverse=True))
    assert [j.id for j, _ in ranked] == ["b", "a"]


def test_live_order_refuses_a_ranking_that_loses_offers() -> None:
    a, b = SimpleNamespace(id="a"), SimpleNamespace(id="b")
    with pytest.raises(ValueError, match="permutation"):
        live.live_order([(a, 0.6), (b, 0.5)], {}, 3, PersonalModelConfig(), lambda prod, chal: chal[:1])


def _db(tmp_path: Path, jobs: list[Job], events: list[tuple[str, str, str]]) -> Path:
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE feedback_events (job_id TEXT, type TEXT, signal_class TEXT, reasons_json TEXT, metadata_json TEXT,
            occurred_at TEXT, superseded INTEGER DEFAULT 0, client_created_at TEXT, client_time_trusted INTEGER);
        CREATE TABLE jobs (id TEXT, source TEXT, work_mode TEXT, remote INTEGER, employment_type TEXT, salary_min REAL,
            title TEXT, skills TEXT, company TEXT);
        CREATE TABLE job_scores (run_id TEXT, job_id TEXT, visible_position INTEGER, score_breakdown_json TEXT);
        CREATE TABLE pipeline_runs (id TEXT, started_at TEXT);
        CREATE TABLE job_embeddings (job_id TEXT, model_name TEXT, vector TEXT);
        CREATE TABLE app_state (id INTEGER, learning_reset_at TEXT);
    """)
    for j in jobs:
        conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                     (j.id, j.source, None, 0, None, None, j.title, "[]", "ACME"))
    for job_id, typ, cls in events:
        conn.execute("INSERT INTO feedback_events (job_id, type, signal_class, reasons_json, metadata_json, occurred_at) "
                     "VALUES (?,?,?,?,?,?)", (job_id, typ, cls, "[]", "{}", "2026-11-01T00:00:00+00:00"))
    conn.commit()
    conn.close()
    return path


def _cap(prod, chal):
    return sorted(chal, key=lambda p: p[1], reverse=True)


def test_behaviour_changes_the_next_ranking_and_is_explained(tmp_path: Path) -> None:
    """The whole loop: actions on past offers move an unseen offer of the same kind."""
    from datetime import UTC, datetime

    seen = [_job(f"s{i}", "Technicien réseaux") for i in range(4)] + [_job("n", "Comptable")]
    fresh = [_job("f-compta", "Comptable fournisseurs"), _job("f-reseau", "Technicien réseaux")]
    events = [(f"s{i}", "interested", "explicit") for i in range(4)] + [("n", "not_interested", "explicit")]
    db = _db(tmp_path, [*seen, *fresh], events)
    production = [(fresh[0], 0.56), (fresh[1], 0.55)]
    cfg_path = tmp_path / "none.yaml"

    adjustments, _ = compute_shadow(db, "r1", production, {}, _cap, embedding_model="", started_at=datetime.now(UTC),
                                    config_path=cfg_path)
    assert adjustments["f-reseau"] > 0 > adjustments["f-compta"]

    state = current_state(db)
    assert state and state["summary"]["strong"] == 5
    ranked = live.live_order(production, adjustments, state["summary"]["strong"], PersonalModelConfig(), _cap)
    assert [j.id for j, _ in ranked] == ["f-reseau", "f-compta"]

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT production_position, challenger_position, explanation_json FROM ranking_shadow "
                       "WHERE job_id = 'f-reseau'").fetchone()
    conn.close()
    assert (row[0], row[1]) == (2, 1)
    assert {"feature": TERMS, "value": "reseau"}.items() <= json.loads(row[2])[0].items() or \
        any(r["value"] == "reseau" for r in json.loads(row[2]))


def test_forget_then_restore_changes_the_next_fit(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    seen = [_job(f"s{i}", "Technicien réseaux") for i in range(3)]
    fresh = _job("f", "Technicien réseaux")
    db = _db(tmp_path, [*seen, fresh], [(f"s{i}", "interested", "explicit") for i in range(3)])
    run = lambda: compute_shadow(db, "r", [(fresh, 0.5)], {}, _cap, embedding_model="",  # noqa: E731
                                 started_at=datetime.now(UTC), config_path=tmp_path / "none.yaml")[0]["f"]
    before = run()
    live.forget(db, TERMS, "reseau")
    live.forget(db, TERMS, "technicien")
    live.forget(db, "remote", "x")
    assert run() == 0.0
    listed = live.preferences(db, PersonalModelConfig())
    assert {"feature": TERMS, "value": "reseau"} in listed["forgotten"]
    live.restore(db, TERMS, "reseau")
    assert 0 < run() < before


def test_preferences_report_evidence_and_comparison(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    seen = [_job(f"s{i}", "Technicien réseaux") for i in range(3)]
    fresh = [_job("a", "Comptable"), _job("b", "Technicien réseaux")]
    db = _db(tmp_path, [*seen, *fresh], [(f"s{i}", "interested", "explicit") for i in range(3)])
    compute_shadow(db, "r", [(fresh[0], 0.5), (fresh[1], 0.495)], {}, _cap, embedding_model="",
                   started_at=datetime.now(UTC), config_path=tmp_path / "none.yaml")
    report = live.preferences(db, PersonalModelConfig())
    assert report["enough_evidence"] and report["strong_signals"] == 3
    assert report["comparison"]["moved"] == 2 and report["comparison"]["movers"][0]["title"]
    assert report["comparison"]["applied"] is False       # nothing was stored as visible in this fixture
    why = live.why(db, "b")
    assert why and why["production_position"] == 2 and why["personal_position"] == 1


def test_unknown_feature_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    sqlite3.connect(db).close()
    with pytest.raises(ValueError, match="unknown feature"):
        live.forget(db, "nope", "x")


def test_ensure_schema_adds_the_explanation_column_to_an_old_database(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("CREATE TABLE ranking_shadow (run_id TEXT, job_id TEXT, production_position INTEGER, "
                 "challenger_position INTEGER, personal_adjustment REAL, model_version TEXT, s4_seed TEXT, "
                 "s4_pool_size INTEGER, s4_pi REAL, PRIMARY KEY (run_id, job_id))")
    ensure_schema(conn)
    assert "explanation_json" in {r[1] for r in conn.execute("PRAGMA table_info(ranking_shadow)")}
    conn.close()
