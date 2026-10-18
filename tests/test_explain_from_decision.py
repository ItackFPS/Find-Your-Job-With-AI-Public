"""explain.py renders the decision journal (one canonical decision, several representations)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.decisions.models import (
    ConflictItem, DecisionRecord, EvidenceItem, Exposure, JobMeta, SourceRef, SpanRef,
)
from src.decisions.storage import DecisionStorage
from src.models.job import Job
from src.pipeline.explain import explain_from_decision
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

NOW = datetime.now(UTC).isoformat(timespec="seconds")


def _e(rule, polarity, state, msg="m", dim="skills", stage="filter", weight=None, observed=None, span=None):
    return EvidenceItem(dimension=dim, polarity=polarity, state=state, message=msg, weight=weight, observed=observed,
                        source=SourceRef(stage=stage, rule_id=rule), span=span)


def _rec(outcome="accepted", stage="ranking", evidence=(), conflicts=(), run="run1", job="j1"):
    return DecisionRecord(decision_id=f"d-{uuid.uuid4().hex[:8]}", run_id=run, job_id=job, decided_at=NOW,
                          outcome=outcome, deciding_stage=stage, strategy_version="sv-1", fit_score=0.8,
                          evidence=list(evidence), conflicts=list(conflicts), exposure=Exposure(rank=1), job=JobMeta())


def test_accepted_decision_renders_supporting_against_unknown_and_conflicts():
    r = _rec(evidence=[
        _e("filter.location", "positive", "satisfied", dim="location"),
        _e("score.skills_match", "positive", "satisfied", weight=0.2, observed="0.760", stage="score"),
        _e("gate.education.soft", "negative", "violated", "Education above the candidate's level: soft penalty of 20%", stage="requirements_gate"),
        _e("filter.salary", "unknown", "unknown", "No data for salary; hard filter accepts by default"),
    ], conflicts=[ConflictItem(description="Metadata marks job as remote, description says on-site")])
    out = explain_from_decision(r)
    texts = [x.text for x in out.positive_reasons]
    assert texts == ["Passes 1 hard-filter check(s): location", "Skills match: 76% (counts for 20% of the score)"]
    assert all(x.kind == "positive" and x.emoji == "🟢" for x in out.positive_reasons)
    assert [x.text for x in out.concerns] == [
        "Education above the candidate's level: soft penalty of 20%",
        "No data for salary (the hard filter accepts missing data by default)",
        "Conflict: Metadata marks job as remote, description says on-site"]
    assert [x.emoji for x in out.concerns] == ["🔴", "🟡", "🟡"] and not out.rejection_reasons


def test_rejected_decision_renders_the_deciding_violations_with_their_quotes():
    span = SpanRef(start=0, end=5, text="Bac+5 requis")
    r = _rec("rejected", "requirements_gate", [
        _e("gate.education", "negative", "violated", "Job requires Bac+5 (mandatory); candidate maximum is bac+2.",
           dim="education", stage="requirements_gate", span=span),
        _e("filter.location", "positive", "satisfied"),                       # not a reason to reject
        _e("gate.skills", "unknown", "unknown", stage="requirements_gate"),   # unknown never becomes a rejection
    ])
    out = explain_from_decision(r)
    assert [x.text for x in out.rejection_reasons] == ['Job requires Bac+5 (mandatory); candidate maximum is bac+2. (posting says: "Bac+5 requis")']
    assert not out.positive_reasons and not out.concerns


def test_violated_evidence_from_any_stage_is_a_rejection_reason():
    r = _rec("rejected", "llm_eligibility", [_e("llm_eligibility.role_level", "negative", "violated", "Too senior for the profile",
                                              stage="llm_eligibility", dim="seniority")])
    assert [x.text for x in explain_from_decision(r).rejection_reasons] == ["Too senior for the profile"]


def test_review_outcome_is_not_rendered_as_a_plain_pass():
    out = explain_from_decision(_rec("review", evidence=[_e("filter.location", "positive", "satisfied")]))
    assert out.concerns[0].text.startswith("This job is flagged for review")


def test_non_numeric_score_observation_never_crashes_the_rendering():
    odd = _e("score.skills_match", "positive", "satisfied", "fallback text", weight=0.2, observed="n/a", stage="score")
    assert explain_from_decision(_rec(evidence=[odd])).positive_reasons[0].text == "fallback text"


def test_quotes_are_omitted_when_job_text_was_not_stored_and_long_quotes_are_clipped():
    blank = _e("gate.education", "negative", "violated", "Requires Bac+5", span=SpanRef(start=0, end=5, text=""), stage="requirements_gate")
    long_ = _e("gate.experience", "negative", "violated", "Requires 9 years", span=SpanRef(start=0, end=5, text="x" * 400), stage="requirements_gate")
    out = explain_from_decision(_rec("rejected", "requirements_gate", [blank, long_]))
    assert out.rejection_reasons[0].text == "Requires Bac+5"
    assert out.rejection_reasons[1].text.endswith('…")') and len(out.rejection_reasons[1].text) < 220


def test_rejected_without_a_violation_still_says_where_it_was_rejected():
    out = explain_from_decision(_rec("rejected", "deduplicate", []))
    assert [x.text for x in out.rejection_reasons] == ["Rejected at deduplicate"]


def test_every_non_filter_evidence_item_is_rendered_exactly_once_and_filter_lines_are_folded():
    ev = [_e(f"gate.c{i}", p, s, msg=f"item {i}", stage="requirements_gate") for i, (p, s) in enumerate(
        [("positive", "satisfied")] * 3 + [("negative", "violated")] * 2 + [("unknown", "unknown")] * 4)]
    out = explain_from_decision(_rec(evidence=ev))
    assert len(out.positive_reasons) + len(out.concerns) == len(ev)
    assert sorted(x.text.removeprefix("Unknown: ") for x in out.positive_reasons + out.concerns) == sorted(f"item {i}" for i in range(9))
    many_filters = [_e(f"filter.{c}", "positive", "satisfied") for c in ("location", "seniority", "contract_type")] \
        + [_e("filter.salary", "unknown", "unknown"), _e("filter.work_mode", "unknown", "unknown")]
    folded = explain_from_decision(_rec(evidence=many_filters))
    assert [x.text for x in folded.positive_reasons] == ["Passes 3 hard-filter check(s): location, seniority, contract type"]
    assert [x.text for x in folded.concerns] == ["No data for salary, work mode (the hard filter accepts missing data by default)"]


# -- route: the explanation endpoint follows the journal --------------------------------------------------


@pytest.fixture
def api(tmp_path: Path):
    from src.api.app import app

    cfg = tmp_path / "config"
    cfg.mkdir()
    saved = (app.state.db_path, getattr(app.state, "config_dir", None))
    app.state.db_path, app.state.config_dir = str(tmp_path / "t.db"), str(cfg)
    s = Storage(app.state.db_path)
    s.init_db()
    s.upsert_job(Job(id="j1", source="s", title="Python dev", description="python " * 60, location="Paris", countries=["France"]))
    s.close()
    rs = RunStorage(app.state.db_path)
    rs.init_db()
    rs.create_run("run1")
    rs.complete_run("run1", {"collected": 1})
    rs.close()
    yield TestClient(app), app.state.db_path
    app.state.db_path, app.state.config_dir = saved


def _write(db, rec):
    st = DecisionStorage(db)
    st.init_db()
    st.write_run([rec], {})
    st.close()


def test_route_renders_from_the_decision_of_the_latest_run(api):
    client, db = api
    rec = _rec(evidence=[_e("filter.location", "positive", "satisfied", dim="location")])
    _write(db, rec)
    body = client.get("/api/jobs/j1/explanation").json()
    assert body["explanation_source"] == "decision" and body["decision_id"] == rec.decision_id
    assert [r["text"] for r in body["positive_reasons"]] == ["Passes 1 hard-filter check(s): location"]
    assert body["filter_result"] == "passed" and body["rejection_reason"] is None


def test_a_newer_decision_from_another_run_does_not_shadow_the_latest_completed_run(api):
    client, db = api
    mine = _rec(run="run1", evidence=[_e("filter.location", "positive", "satisfied")])
    _write(db, mine)
    newer = _rec(run="run-in-flight")                 # e.g. a running/failed run's decision, decided later
    st = DecisionStorage(db)
    st.write_run([newer.model_copy(update={"decided_at": "2099-01-01T00:00:00+00:00"})], {})
    st.close()
    assert client.get("/api/jobs/j1/explanation").json()["decision_id"] == mine.decision_id


def test_decision_wins_over_a_conflicting_legacy_rejection_row_and_reason_code_follows_it(api):
    client, db = api
    rs = RunStorage(db)
    rs.init_db()
    rs.store_rejections("run1", [{"job_id": "j1", "rejection_stage": "filter", "rejection_detail": "location",
                                  "title": "t", "company": "c", "location": "l", "source": "s"}])
    rs.close()
    _write(db, _rec("rejected", "filter", [_e("filter.seniority", "negative", "violated", "Rejected by hard filter: seniority")]))
    body = client.get("/api/jobs/j1/explanation").json()
    assert body["filter_result"] == "rejected" and body["rejection_reason"] == "seniority"


def test_route_marks_a_rejection_from_the_decision_even_though_the_job_row_exists(api):
    client, db = api
    _write(db, _rec("rejected", "filter", [_e("filter.seniority", "negative", "violated", "Rejected by hard filter: seniority")]))
    body = client.get("/api/jobs/j1/explanation").json()
    assert body["filter_result"] == "rejected" and body["explanation_source"] == "decision"
    assert [r["text"] for r in body["rejection_reasons"]] == ["Rejected by hard filter: seniority"]
    assert body["rejection_reason"] == "seniority"


def test_route_falls_back_to_heuristics_without_a_decision_or_for_an_older_run(api):
    client, db = api
    assert client.get("/api/jobs/j1/explanation").json()["explanation_source"] == "heuristic"
    _write(db, _rec(run="older-run"))                       # a decision from a run the page does not reflect
    body = client.get("/api/jobs/j1/explanation").json()
    assert body["explanation_source"] == "heuristic" and body["decision_id"] is None


def test_route_survives_a_broken_journal(api, monkeypatch):
    client, db = api
    monkeypatch.setattr(DecisionStorage, "get_latest_decision", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    assert client.get("/api/jobs/j1/explanation").json()["explanation_source"] == "heuristic"
