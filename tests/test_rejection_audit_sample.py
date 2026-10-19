"""Rejection audit sample: rule-backed rejections the user can review (ADR 0010).

The disagreement engine never flags a solid rule-backed rejection, and rejected jobs are never
shown, so a wrong rule could hide good jobs forever. The decision recorder therefore marks a small,
bounded, stratified sample of rule-backed rejections (``signals.audit``) that then appear in the
existing review queue until answered. It is observability only: outcome, score, ranking and
notifications are unchanged (see also test_decision_journal_orchestrator's on/off equivalence).
"""

from __future__ import annotations

import json
import uuid

import pytest

from src.decisions.audit import AUDIT_SAMPLER, select_audit_sample
from src.decisions.models import DecisionRecord, EvidenceItem, JobMeta, SourceRef, StageTraceEntry
from src.decisions.storage import DecisionStorage
from src.feedback.models import EXPLICIT_TYPES, FeedbackEvent
from src.feedback.storage import FeedbackStorage


def _rejected(job_id: str, stage: str = "filter", code: str = "title_keywords", *,
              run: int = 1, snapshot: bool = True, dim: str = "domain") -> DecisionRecord:
    trace = [StageTraceEntry(stage=stage, result="reject", detail=code if stage == "filter" else None)]  # type: ignore[arg-type]
    evidence = [EvidenceItem(dimension=dim, polarity="negative", state="violated",  # type: ignore[arg-type]
                             source=SourceRef(stage=stage, rule_id=f"{stage}.{code}"), message="rule")]
    return DecisionRecord(
        decision_id=f"{job_id}-r{run}", run_id=f"r{run}", job_id=job_id,
        decided_at=f"2026-10-0{run}T00:00:00+00:00", outcome="rejected", deciding_stage=stage,  # type: ignore[arg-type]
        strategy_version="sv", evidence=evidence, stage_trace=trace, signals={},
        snapshot_hash=f"h-{job_id}" if snapshot else None, job=JobMeta(title=job_id))


def _accepted(job_id: str, run: int = 1) -> DecisionRecord:
    return DecisionRecord(
        decision_id=f"{job_id}-r{run}", run_id=f"r{run}", job_id=job_id,
        decided_at=f"2026-10-0{run}T00:00:00+00:00", outcome="accepted", deciding_stage="ranking",
        strategy_version="sv", fit_score=0.7, signals={}, job=JobMeta(title=job_id))


# -- selection (pure) ------------------------------------------------------------------------------

def test_selection_is_bounded_stratified_and_deterministic():
    records = ([_rejected(f"t{i}") for i in range(10)]
               + [_rejected(f"s{i}", code="seniority", dim="seniority") for i in range(10)]
               + [_rejected(f"g{i}", stage="requirements_gate", code="education", dim="education") for i in range(3)])
    a = select_audit_sample(records, run_id="r1", slots=3, excluded_job_ids=set())
    b = select_audit_sample(list(reversed(records)), run_id="r1", slots=3, excluded_job_ids=set())
    assert [r.job_id for r, _ in a] == [r.job_id for r, _ in b]           # order-independent
    assert len(a) == 3
    assert len({info["stratum"] for _, info in a}) == 3                    # one per stratum first
    sizes = {info["stratum"]: info["stratum_size"] for _, info in a}
    assert sizes == {"filter:title_keywords": 10, "filter:seniority": 10, "requirements_gate:education": 3}
    assert all(info["sampler"] == AUDIT_SAMPLER and info["sampled"] is True for _, info in a)


def test_only_rule_backed_rejections_with_text_are_eligible():
    records = [
        _accepted("acc"),
        _rejected("fresh", code="published_age", dim="freshness"),     # freshness is not a suitability judgement
        _rejected("notext", snapshot=False),                           # nothing to show the user
        _rejected("dup", stage="deduplicate", code="dup"),
        _rejected("excluded"),
        _rejected("ok"),
    ]
    picked = select_audit_sample(records, run_id="r1", slots=10, excluded_job_ids={"excluded"})
    assert [r.job_id for r, _ in picked] == ["ok"]


def test_no_slots_no_sample():
    assert select_audit_sample([_rejected("a")], run_id="r1", slots=0, excluded_job_ids=set()) == []


def test_a_rejection_already_flagged_for_review_is_not_sampled_again():
    r = _rejected("a")
    r.signals["assessment"] = {"state": "review"}
    assert select_audit_sample([r], run_id="r1", slots=3, excluded_job_ids=set()) == []


# -- queue (storage) --------------------------------------------------------------------------------

@pytest.fixture
def stores(tmp_path):
    db = str(tmp_path / "jobs.db")
    ds, fs = DecisionStorage(db), FeedbackStorage(db)
    ds.init_db()
    fs.init_db()
    yield ds, fs
    ds.close()
    fs.close()


def _audited(job_id: str, run: int = 1) -> DecisionRecord:
    r = _rejected(job_id, run=run)
    r.signals["audit"] = {"sampled": True, "sampler": AUDIT_SAMPLER, "stratum": "filter:title_keywords"}
    return r


def _answer(fs: FeedbackStorage, job_id: str, decision_id: str, type_: str, at: str = "2026-10-01T12:00:00+00:00"):
    fs.append(FeedbackEvent(event_id=str(uuid.uuid4()), job_id=job_id, decision_id=decision_id,
                            signal_class="explicit", type=type_, source="ui", occurred_at=at))


def _queued(ds: DecisionStorage) -> set[str]:
    return {r.job_id for r in ds.review_queue()[1]}


def test_an_audited_rejection_is_queued_until_answered_across_runs(stores):
    ds, fs = stores
    ds.write_run([_audited("a"), _rejected("b")], {})
    assert _queued(ds) == {"a"}                      # unsampled solid rejection: not queued
    assert ds.open_audit_count() == 1
    ds.write_run([_rejected("a", run=2)], {})        # rejected again, not re-sampled: same episode
    assert _queued(ds) == {"a"} and ds.open_audit_count() == 1
    _answer(fs, "a", "a-r1", "unsure")               # "not sure" is an answer too
    assert _queued(ds) == set() and ds.open_audit_count() == 0


def test_an_audit_item_leaves_the_queue_when_the_job_is_accepted_later(stores):
    ds, _ = stores
    ds.write_run([_audited("a")], {})
    ds.write_run([_accepted("a", run=2)], {})
    assert _queued(ds) == set()


def test_jobs_already_audited_or_answered_are_not_sampled_again(stores):
    ds, fs = stores
    ds.write_run([_audited("a"), _rejected("b"), _rejected("c")], {})
    _answer(fs, "b", "b-r1", "not_interested")
    assert ds.audit_ineligible_jobs(["a", "b", "c"]) == {"a", "b"}
    ds.write_run([_accepted("a", run=2)], {})        # a new episode starts when the outcome changes
    assert ds.audit_ineligible_jobs(["a"]) == set()


# -- answers ------------------------------------------------------------------------------------

def test_unsure_is_an_explicit_answer_that_creates_no_stance(stores):
    _, fs = stores
    assert "unsure" in EXPLICIT_TYPES
    _answer(fs, "a", "a-r1", "interested", at="2026-10-01T00:00:00+00:00")
    _answer(fs, "a", "a-r1", "unsure", at="2026-10-02T00:00:00+00:00")
    assert fs.user_stances(["a"]) == {}              # the latest answer is "I don't know"


def test_unsure_becomes_a_borderline_label():
    from src.evaluation.feedback_dataset import derive_label
    from src.feedback.config import FeedbackConfig

    ev = [{"signal_class": "explicit", "type": "unsure", "reasons_json": "[]", "metadata_json": "{}"}]
    assert derive_label(ev, FeedbackConfig())[0] == "borderline"


# -- end to end through the real pipeline ------------------------------------------------------------

def test_pipeline_marks_a_bounded_sample_and_changes_no_outcome(tmp_path):
    from tests.test_decision_journal_orchestrator import _normalised, _run

    (tmp_path / "on").mkdir()
    (tmp_path / "off").mkdir()
    on, db, _ = _run(tmp_path / "on","rejection_audit_max_open: 2\n")
    off, _, _ = _run(tmp_path / "off", "rejection_audit_max_open: 0\n")
    assert _normalised(on) == _normalised(off)        # outcome-neutral
    st = DecisionStorage(str(db))
    try:
        recs = st.get_decisions_for_run(on.run_id)
        audited = [r for r in recs if (r.signals.get("audit") or {}).get("sampled")]
        assert len(audited) == 2
        assert all(r.outcome == "rejected" and r.deciding_stage in ("filter", "requirements_gate")
                   and r.snapshot_hash for r in audited)
        assert {r.job_id for r in audited} <= {r.job_id for r in st.review_queue()[1]}
        assert on.stats.decisions.get("audit_sampled") == 2
    finally:
        st.close()
    assert json.dumps(audited[0].signals["audit"])   # JSON-serialisable provenance
