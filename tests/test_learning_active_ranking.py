"""Personal ranking when ACTIVE (ADR 0019; stage L4.2): visible order only, production otherwise."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from src.learning import gates as G
from src.learning import health as H


@dataclass(frozen=True)
class J:
    id: str


PROD = [(J("a"), 0.90), (J("b"), 0.80), (J("c"), 0.70), (J("d"), 0.60)]
ADJ = {"a": -0.15, "b": 0.0, "c": 0.0, "d": 0.15}
ON = {"activation_enabled": True, "experimental_enabled": False}
OFF = {"activation_enabled": False, "experimental_enabled": False}


def no_gates(prod, chal):
    return chal


def ids(ranked):
    return [job.id for job, _ in ranked]


@pytest.mark.parametrize("record", [
    {"state": G.SHADOW, "flags": ON, "cap_factor": 1.0},
    {"state": G.PAUSED, "flags": ON, "cap_factor": 1.0},
    {"state": G.EXPERIMENTAL, "flags": ON, "cap_factor": 1.0},
    {"state": G.ACTIVE, "flags": OFF, "cap_factor": 1.0},     # flag turned off after activation
    {},
])
def test_production_unless_active_with_the_flag_on(record):
    assert H.visible_ranking(PROD, ADJ, record, no_gates) is None


def test_active_reorders_within_the_capped_adjustment():
    visible = H.visible_ranking(PROD, ADJ, {"state": G.ACTIVE, "flags": ON, "cap_factor": 1.0}, no_gates)
    assert ids(visible) == ["b", "a", "d", "c"]
    assert [round(s, 2) for _, s in visible] == [0.80, 0.75, 0.75, 0.70]


def test_cap_factor_scales_the_adjustment():
    halved = H.visible_ranking(PROD, ADJ, {"state": G.ACTIVE, "flags": ON, "cap_factor": 0.5}, no_gates)
    assert ids(halved) == ["a", "b", "c", "d"]                 # 0.825, 0.80, 0.70, 0.675


def test_gates_are_reapplied():
    calls = []

    def gate(prod, chal):
        calls.append((ids(prod), ids(chal)))
        return list(prod)

    assert ids(H.visible_ranking(PROD, ADJ, {"state": G.ACTIVE, "flags": ON}, gate)) == ids(PROD)
    assert calls == [(ids(PROD), ["b", "a", "d", "c"])]


def test_a_non_permutation_is_a_guardrail_failure():
    with pytest.raises(ValueError):
        H.visible_ranking(PROD, ADJ, {"state": G.ACTIVE, "flags": ON}, lambda prod, chal: chal[:-1])


def _health(path: str, state: str) -> None:
    conn = sqlite3.connect(path)
    H.ensure_schema(conn)
    with conn:
        conn.execute("INSERT INTO personal_health (id, state, state_since, cap_factor, halvings_json, release_key, "
                     "decision_json, flags_json, last_event, run_id, updated_at) "
                     "VALUES (1, ?, '2026-09-01T00:00:00+00:00', 1.0, '[]', 'k', '{}', '{}', NULL, 'r', "
                     "'2026-09-01T00:00:00+00:00')", (state,))
    conn.close()


def test_pause_only_from_active_or_experimental(tmp_path):
    path = str(tmp_path / "jobs.db")
    assert not H.pause(path, "guardrail")                         # no record yet
    _health(path, G.ACTIVE)
    assert H.pause(path, "guardrail", now=datetime(2026, 9, 2, tzinfo=UTC))
    rec = H.current_health(path)
    assert rec["state"] == G.PAUSED and rec["last_event"] == "paused:guardrail"
    assert not H.pause(path, "guardrail")                         # already paused


def test_stored_visible_positions_order_the_run(tmp_path):
    from src.storage.runs import RunStorage

    rs = RunStorage(db_path=str(tmp_path / "jobs.db"))
    rs.init_db()
    try:
        rs.create_run("prod")
        rs.store_job_scores("prod", [{"job_id": j, "final_score": s, "score_breakdown": {}}
                                     for j, s in [("a", 0.9), ("b", 0.8), ("c", 0.7)]])
        assert rs.get_job_ids_for_run("prod") == ["a", "b", "c"]
        rs.create_run("active")
        rs.store_job_scores("active", [{"job_id": j, "final_score": s, "score_breakdown": {}, "visible_position": p}
                                       for j, s, p in [("a", 0.9, 3), ("b", 0.8, 1), ("c", 0.7, 2)]])
        assert rs.get_job_ids_for_run("active") == ["b", "c", "a"]
        assert [r["job_id"] for r in rs.get_jobs_for_run("active")] == ["b", "c", "a"]
        assert [r["final_score"] for r in rs.get_jobs_for_run("active")] == [0.8, 0.7, 0.9]   # scores unchanged
    finally:
        rs.close()


# -- pipeline -----------------------------------------------------------------------------------

def _run(tmp_path, record=None, ranking_error=False):
    from unittest.mock import patch

    from tests.test_decision_journal_orchestrator import _config_dir
    from tests.test_learning_model_shadow import _pipeline

    cfg = _config_dir(tmp_path, None)
    db = tmp_path / "jobs.db"
    patches = []
    if record is not None:
        patches.append(patch("src.learning.health.update_health", return_value=record))
    if ranking_error:
        patches.append(patch("src.learning.health.visible_ranking", side_effect=ValueError("not a permutation")))
    for p in patches:
        p.start()
    try:
        return db, _pipeline(db, cfg)
    finally:
        for p in patches:
            p.stop()


def test_pipeline_flag_off_keeps_the_production_order(tmp_path):
    _, result = _run(tmp_path)
    assert {s.visible_position for s in result.scored_jobs} == {None}
    assert H.current_health(str(tmp_path / "jobs.db"))["state"] == G.SHADOW


def test_pipeline_active_records_visible_positions(tmp_path):
    _, result = _run(tmp_path, {"state": G.ACTIVE, "flags": ON, "cap_factor": 1.0})
    positions = [s.visible_position for s in result.scored_jobs]
    assert sorted(positions) == list(range(1, len(positions) + 1))
    # no signal → zero adjustment (INV-7): the visible order is the production order
    assert positions == list(range(1, len(positions) + 1))


def test_pipeline_guardrail_failure_pauses_and_uses_production(tmp_path):
    db = tmp_path / "jobs.db"
    _health(str(db), G.ACTIVE)
    _, result = _run(tmp_path, {"state": G.ACTIVE, "flags": ON, "cap_factor": 1.0}, ranking_error=True)
    assert {s.visible_position for s in result.scored_jobs} == {None}
    assert H.current_health(str(db))["state"] == G.PAUSED
