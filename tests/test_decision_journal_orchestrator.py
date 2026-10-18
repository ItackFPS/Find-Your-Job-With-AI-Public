"""End-to-end proof for the decision journal (P1a).

1. NO BEHAVIOUR CHANGE: the same input through the real ``run_pipeline()`` yields the same
   scored jobs, scores, ranking, rejections and stats with the journal on and off.
2. COMPLETENESS: every collected job (accepted AND rejected) gets one record explaining the
   decision, stage, strategy, score, evidence and exposure.
3. FAIL-OPEN: a journal failure never changes the run.

Embeddings are replaced by a deterministic fake so the whole pipeline (including semantic
matching and personalization) runs offline.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from src.embeddings.embedding import BatchEmbeddingResult, EmbeddingService
from src.models.job import Job
from src.evaluation.dataset import load_dataset
from tests.fixtures.profiles import TEST_PROFILE_PATH

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


class _FakeEmbeddingService(EmbeddingService):
    """Deterministic 8-d hash embeddings; no model download."""

    def __init__(self, *a, **k) -> None:       # noqa: D107 — no model, no cache
        super().__init__(model_name="fake")

    @staticmethod
    def _vec(text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        return [(b + 1) / 256 for b in h[:8]]

    def embed(self, text: str) -> list[float]:       # type: ignore[override]
        return self._vec(text)

    def embed_batch(self, texts, ids=None):          # type: ignore[override]
        return BatchEmbeddingResult(ids=list(ids or range(len(texts))), vectors=[self._vec(t) for t in texts])


def _config_dir(tmp: Path, decisions_yaml: str | None, extra: dict[str, str] | None = None) -> Path:
    cfg = tmp / "config"
    cfg.mkdir()
    shutil.copy(TEST_PROFILE_PATH, cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml", "semantic_config.yaml", "personalization.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    if decisions_yaml is not None:
        (cfg / "decisions.yaml").write_text(decisions_yaml, encoding="utf-8")
    for name, text in (extra or {}).items():
        (cfg / name).write_text(text, encoding="utf-8")
    return cfg


def _run(tmp: Path, decisions_yaml: str | None, extra: dict[str, str] | None = None):
    from src.pipeline.orchestrator import run_pipeline

    cfg = _config_dir(tmp, decisions_yaml, extra)
    db = tmp / "jobs.db"
    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg),
                              prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert result.status == "completed", result.error
    return result, db, cfg


def _normalised(result) -> dict:
    """Everything a consumer can observe, minus volatile/timing fields and the journal summary."""
    stats = result.stats.to_dict()
    for k in ("stage_times", "duration_seconds", "decisions", "stage_progress", "preflight",
              "embedding_cache_hits", "embedding_cache_misses",
              "personalization_cache_hits", "personalization_cache_misses"):
        stats.pop(k, None)
    return {
        "scored": [(r.job_id, r.final_score, r.deterministic_score, r.semantic_score, r.factors,
                    r.personalization_adjustment) for r in result.scored_jobs],
        "rejections": [(r.job_id, r.rejection_stage, r.rejection_detail) for r in result.rejection_records],
        "stats": stats,
    }


@pytest.fixture(scope="module")
def off(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("off"), "enabled: false\n")


@pytest.fixture(scope="module")
def on(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("on"), None)      # defaults: enabled


def test_journal_changes_nothing_observable(off, on):
    assert _normalised(off[0]) == _normalised(on[0])
    assert len(on[0].scored_jobs) > 10 and len(on[0].rejection_records) > 10   # non-trivial run


def test_disabled_journal_writes_no_rows(off):
    import sqlite3

    conn = sqlite3.connect(off[1])
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "job_decisions" not in tables
    assert off[0].stats.decisions["enabled"] is False


def test_every_collected_job_has_exactly_one_record(on):
    from src.decisions.storage import DecisionStorage

    result, db, _ = on
    st = DecisionStorage(str(db))
    recs = st.get_decisions_for_run(result.run_id)
    assert len(recs) == result.stats.collected == 50
    assert len({r.job_id for r in recs}) == 50
    acc = [r for r in recs if r.outcome == "accepted"]
    rej = [r for r in recs if r.outcome == "rejected"]
    assert len(acc) == len(result.scored_jobs)
    assert len(acc) + len(rej) == 50
    assert len(rej) >= len(result.rejection_records)        # + validation/dedup drops (none in this dataset)
    assert {r.job_id for r in acc} == {s.job_id for s in result.scored_jobs}
    # rejected: stage of the journal == stage of the existing RejectionRecord
    by_stage = {r.job_id: r.rejection_stage for r in result.rejection_records}
    for r in rej:
        if r.job_id in by_stage:
            assert r.deciding_stage == by_stage[r.job_id]
    assert result.stats.decisions["recorded"] == 50


def test_accepted_records_carry_score_exposure_trace_and_strategy(on):
    from src.decisions.storage import DecisionStorage

    result, db, _ = on
    st = DecisionStorage(str(db))
    recs = {r.job_id: r for r in st.get_decisions_for_run(result.run_id)}
    for rank, s in enumerate(result.scored_jobs, start=1):
        r = recs[s.job_id]
        assert r.fit_score == s.deterministic_score
        assert r.exposure and r.exposure.rank == rank and r.exposure.final_score == s.final_score
        assert r.exposure.tier in {"exceptional", "excellent", "strong", "relevant", "low_priority"}
        assert r.signals["factors"] == s.factors
        stages = [t.stage for t in r.stage_trace]
        assert stages == ["validate", "deduplicate", "filter", "requirements_gate", "llm_eligibility", "score", "ranking"]
        assert r.strategy_version.startswith("sv-") and r.snapshot_hash is None   # accepted text not stored by default
    strategy = st.get_strategy(next(iter(recs.values())).strategy_version)
    assert strategy and set(strategy["files"]) >= {"profile", "filters", "scoring"}


def test_rejected_records_explain_and_keep_auditable_text(on):
    from src.decisions.storage import DecisionStorage
    from src.models.job import Job

    result, db, _ = on
    st = DecisionStorage(str(db))
    rej = [r for r in st.get_decisions_for_run(result.run_id) if r.outcome == "rejected"]
    stages = {r.deciding_stage for r in rej}
    assert {"filter", "requirements_gate"} <= stages
    for r in rej:
        assert r.fit_score is None and r.exposure is None
        deciding = [e for e in r.evidence if e.state == "violated" and e.source.stage == r.deciding_stage]
        assert deciding, "the deciding stage's violation is part of the evidence"
        assert r.stage_trace[-1].result == "reject"
        assert r.snapshot_hash, "rejected jobs keep a snapshot so false negatives can be audited"
        payload = st.get_snapshot(r.snapshot_hash)
        assert payload and payload["description"]
        assert Job(id="x", **{k: v for k, v in payload.items() if k in Job.model_fields}).title == r.job.title
    gate_items = [e for r in rej if r.deciding_stage == "requirements_gate" for e in r.evidence
                  if e.state == "violated" and e.source.stage == "requirements_gate"]
    assert gate_items and any(e.span is not None for e in gate_items), "text-based gate evidence keeps its span"
    assert all(e.observed and e.source.rule_version for e in gate_items)


def test_snapshots_are_content_addressed_across_runs(on, tmp_path):
    """Re-running over the same jobs adds decisions but no duplicate snapshot rows."""
    import sqlite3

    from src.pipeline.orchestrator import run_pipeline

    result, db, cfg = on
    n_snap = sqlite3.connect(db).execute("SELECT COUNT(*) FROM job_snapshots").fetchone()[0]
    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        r2 = run_pipeline(db_path=str(db), config_dir=str(cfg), prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert r2.status == "completed" and r2.run_id != result.run_id
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM job_snapshots").fetchone()[0] == n_snap
    assert conn.execute("SELECT COUNT(*) FROM job_decisions").fetchone()[0] == 100


def test_journal_failure_is_fail_open(tmp_path, off):
    from src.decisions.storage import DecisionStorage

    with patch.object(DecisionStorage, "write_run", side_effect=RuntimeError("disk full")):
        result, _, _ = _run(tmp_path, None)
    assert _normalised(result) == _normalised(off[0])
    assert "disk full" in result.stats.decisions["error"]


def test_store_rejected_text_false_keeps_audit_metadata_only(tmp_path):
    from src.decisions.storage import DecisionStorage

    result, db, _ = _run(tmp_path, "store_rejected_text: false\n")
    st = DecisionStorage(str(db))
    rej = [r for r in st.get_decisions_for_run(result.run_id) if r.outcome == "rejected"]
    assert rej and all(r.snapshot_hash is None for r in rej)
    assert all(r.job.title for r in rej)             # decision + metadata survive without text


def test_strategy_version_tracks_config_changes(tmp_path):
    from src.decisions.strategy import build_strategy_manifest

    cfg = _config_dir(tmp_path, None)
    a = build_strategy_manifest(cfg, cfg / "profile.yaml")
    assert a == build_strategy_manifest(cfg, cfg / "profile.yaml")
    (cfg / "scoring.yaml").write_text((cfg / "scoring.yaml").read_text() + "\n# x\n")
    b = build_strategy_manifest(cfg, cfg / "profile.yaml")
    assert a["strategy_version"] != b["strategy_version"]
    assert build_strategy_manifest(cfg, cfg / "profile.yaml", use_ai=True)["strategy_version"] != b["strategy_version"]


def test_accepted_jobs_keep_no_job_text_in_quoted_evidence_by_default(on):
    """store_accepted_text is false: span quotes of soft gate penalties on accepted jobs are blanked."""
    from src.decisions.storage import DecisionStorage

    result, db, _ = on
    recs = DecisionStorage(str(db)).get_decisions_for_run(result.run_id)
    acc_spans = [e.span for r in recs if r.outcome == "accepted" for e in r.evidence if e.span]
    assert all(sp.text == "" for sp in acc_spans)
    rej_spans = [e.span for r in recs if r.outcome == "rejected" for e in r.evidence if e.span]
    assert rej_spans and any(sp.text for sp in rej_spans)         # rejected jobs keep their quotes


def test_observer_failure_for_one_job_does_not_lose_the_rest(tmp_path, off):
    from src.decisions import evidence as evidence_mod
    from src.decisions.storage import DecisionStorage

    real = evidence_mod.reason_item

    def flaky(reason, *, soft):
        if reason.requirement == "ruby":
            raise ValueError("boom")
        return real(reason, soft=soft)

    with patch.object(evidence_mod, "reason_item", flaky):
        result, db, _ = _run(tmp_path, None)
    assert _normalised(result) == _normalised(off[0])
    assert result.stats.decisions["skipped_observations"] >= 1
    assert "error" not in result.stats.decisions
    recs = DecisionStorage(str(db)).get_decisions_for_run(result.run_id)
    assert len(recs) >= 45                                         # only the poisoned jobs are missing


def test_recorder_creation_failure_is_fail_open(tmp_path, off):
    with patch("src.decisions.recorder.build_strategy_manifest", side_effect=RuntimeError("no strategy")):
        result, db, _ = _run(tmp_path, None)
    assert _normalised(result) == _normalised(off[0])
    assert result.stats.decisions["enabled"] is False and "no strategy" in result.stats.decisions["error"]


def test_stage_observer_exception_is_fail_open(tmp_path, off):
    from src.decisions.recorder import DecisionRecorder

    with patch.object(DecisionRecorder, "_draft", side_effect=RuntimeError("draft failed")):
        result, _, _ = _run(tmp_path, None)
    assert _normalised(result) == _normalised(off[0])


def test_observers_never_mutate_what_they_observe(tmp_path):
    from src.decisions.recorder import DecisionRecorder

    seen = {}
    real_gate = DecisionRecorder.observe_gate
    real_scores = DecisionRecorder.observe_scores

    def gate(self, jobs_in, gate_results):
        seen["jobs"] = [j.model_dump() for j in jobs_in]
        seen["jobs_obj"] = list(jobs_in)
        seen["gate"] = {k: repr(v) for k, v in gate_results.items()}
        seen["gate_obj_ref"] = gate_results
        seen["gate_obj"] = gate_results
        return real_gate(self, jobs_in, gate_results)

    def scores(self, sw, scoring_config=None):
        import copy

        seen["scored"] = [(j.id, sc, dict(f)) for j, sc, f in sw]
        seen["scored_obj"] = sw
        seen["cfg"] = copy.deepcopy(scoring_config)
        seen["cfg_obj"] = scoring_config
        return real_scores(self, sw, scoring_config=scoring_config)

    with patch.object(DecisionRecorder, "observe_gate", gate), patch.object(DecisionRecorder, "observe_scores", scores):
        _run(tmp_path, None)
    assert [j.model_dump() for j in seen["jobs_obj"]] == seen["jobs"]
    assert {k: repr(v) for k, v in seen["gate_obj"].items()} == seen["gate"]
    assert [(j.id, sc, dict(f)) for j, sc, f in seen["scored_obj"]] == seen["scored"]
    assert seen["cfg_obj"] == seen["cfg"]                    # explaining the score never touches the scoring config


def test_gate_error_fallback_is_journaled_as_the_pipeline_applied_it(tmp_path):
    """If the requirements gate fails and the pipeline proceeds without it, no job is journaled as
    gate-rejected: the journal follows what the pipeline actually did."""
    from src.decisions.storage import DecisionStorage
    from src.requirements import gate as gate_mod

    with patch.object(gate_mod, "gate_jobs", side_effect=RuntimeError("gate exploded")):
        result, db, _ = _run(tmp_path, None)
    assert result.stats.requirements_gate_error
    recs = DecisionStorage(str(db)).get_decisions_for_run(result.run_id)
    assert not [r for r in recs if r.deciding_stage == "requirements_gate"]
    scored_ids = {s.job_id for s in result.scored_jobs}
    assert {r.job_id for r in recs if r.outcome == "accepted"} == scored_ids
    some = next(r for r in recs if r.outcome == "accepted")
    assert [t.result for t in some.stage_trace if t.stage == "requirements_gate"] == ["error"]


def test_accepted_records_explain_why_with_positive_and_unknown_evidence(on):
    from src.decisions.storage import DecisionStorage

    result, db, _ = on
    recs = [r for r in DecisionStorage(str(db)).get_decisions_for_run(result.run_id) if r.outcome == "accepted"]
    assert recs
    for r in recs:
        rule_ids = {e.source.rule_id for e in r.evidence}
        assert {f"filter.{c}" for c in ("seniority", "title_keywords", "contract_type")} <= rule_ids
        assert all(e.state in ("satisfied", "unknown") for e in r.evidence if e.source.stage == "filter")
        assert {"gate.education", "gate.experience", "gate.skills"} <= rule_ids or any(
            e.dimension in ("education", "experience", "skills") for e in r.evidence)
        assert any(e.polarity == "positive" for e in r.evidence)
        assert all(e.observed for e in r.evidence if e.source.stage == "filter")
    assert any(r.unknowns for r in recs), "missing data surfaces as UNKNOWN evidence, not silence"


def test_filter_rejections_carry_the_facts_the_check_looked_at(on):
    from src.decisions.storage import DecisionStorage

    result, db, _ = on
    rej = [r for r in DecisionStorage(str(db)).get_decisions_for_run(result.run_id) if r.deciding_stage == "filter"]
    assert rej
    for r in rej:
        item = next(e for e in r.evidence if e.source.stage == "filter")
        assert item.observed and item.state == "violated"


def test_all_filter_failures_option_exposes_secondary_failing_checks(tmp_path):
    from src.decisions.storage import DecisionStorage

    result, db, _ = _run(tmp_path, "all_filter_failures: true\n")
    recs = [r for r in DecisionStorage(str(db)).get_decisions_for_run(result.run_id) if r.deciding_stage == "filter"]
    multi = [r for r in recs if len([e for e in r.evidence if e.source.stage == "filter"]) > 1]
    assert multi
    for r in multi:                      # the deciding check comes first and is not labelled "also"
        filter_items = [e for e in r.evidence if e.source.stage == "filter"]
        assert filter_items[0].message.startswith("Rejected by hard filter")
        assert all("not the deciding check" in e.message for e in filter_items[1:])


def test_accepted_records_explain_the_score_they_were_given(on):
    from src.decisions.storage import DecisionStorage

    result, db, _ = on
    recs = {r.job_id: r for r in DecisionStorage(str(db)).get_decisions_for_run(result.run_id) if r.outcome == "accepted"}
    assert result.scored_jobs
    for s in result.scored_jobs:
        r = recs[s.job_id]
        expl = r.signals["score_explanation"]
        assert expl["final"] == r.fit_score == s.deterministic_score          # the explanation IS the stored score
        factor_items = [e for e in r.evidence if e.source.stage == "score" and e.source.rule_id.startswith("score.")
                        and e.source.rule_id.split(".")[1].endswith(("_fit", "_relevance", "_match"))]
        assert len(factor_items) == 5 and abs(sum(e.weight for e in factor_items) - 1.0) < 1e-3
        assert {e.dimension for e in factor_items} == {"location", "domain", "skills", "seniority", "experience"}


def test_score_explanation_failure_keeps_the_record_without_score_evidence(tmp_path, off):
    from src.decisions.storage import DecisionStorage
    import src.pipeline.scoring as scoring

    with patch.object(scoring, "explain_score", side_effect=RuntimeError("explain boom")):
        result, db, _ = _run(tmp_path, None)
    assert _normalised(result) == _normalised(off[0])
    recs = [r for r in DecisionStorage(str(db)).get_decisions_for_run(result.run_id) if r.outcome == "accepted"]
    assert recs and all("score_explanation" not in r.signals for r in recs)
    assert all(not [e for e in r.evidence if e.source.stage == "score"] for r in recs)
    assert result.stats.decisions["skipped_observations"] >= len(recs)


def test_a_score_explanation_that_disagrees_with_the_stored_score_is_withheld(tmp_path):
    from src.decisions.storage import DecisionStorage
    import src.pipeline.scoring as scoring

    real = scoring.explain_score

    def drifted(*a, **k):
        e = real(*a, **k)
        import dataclasses
        return dataclasses.replace(e, final=e.final + 0.123)

    with patch.object(scoring, "explain_score", drifted):
        result, db, _ = _run(tmp_path, None)
    recs = [r for r in DecisionStorage(str(db)).get_decisions_for_run(result.run_id) if r.outcome == "accepted"]
    assert recs
    for r in recs:
        assert any("disagrees with the stored score" in c.description for c in r.conflicts)
        assert not [e for e in r.evidence if e.source.stage == "score"]


# -- disagreement / confidence / abstention (P2) -------------------------------------------------------------------


@pytest.fixture(scope="module")
def da_off(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("da_off"), None, {"disagreement.yaml": "mode: off\n"})


@pytest.fixture(scope="module")
def da_shadow(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("da_shadow"), None, {"disagreement.yaml": "mode: shadow\nspread_review: 0.05\n"})


@pytest.fixture(scope="module")
def da_active(tmp_path_factory):
    return _run(tmp_path_factory.mktemp("da_active"), None, {"disagreement.yaml": "mode: active\nspread_review: 0.05\n"})


def _recs(triple):
    from src.decisions.storage import DecisionStorage

    result, db, _ = triple
    return DecisionStorage(str(db)).get_decisions_for_run(result.run_id)


def test_assessment_never_changes_what_the_pipeline_does(da_off, da_shadow, da_active):
    assert _normalised(da_off[0]) == _normalised(da_shadow[0]) == _normalised(da_active[0])


def test_mode_off_records_no_assessment(da_off):
    assert all(r.decision_confidence is None and "assessment" not in r.signals for r in _recs(da_off))


def test_shadow_mode_records_the_assessment_beside_an_unchanged_outcome(da_off, da_shadow):
    off = {r.job_id: r for r in _recs(da_off)}
    shadow = _recs(da_shadow)
    assert shadow
    for r in shadow:
        a = r.signals["assessment"]
        assert a["mode"] == "shadow" and a["calibrated"] is False and a["engine_version"] == "disagreement.v2"
        assert r.decision_confidence == a["confidence"] and r.confidence_calibrated is False
        assert r.outcome == off[r.job_id].outcome and r.deciding_stage == off[r.job_id].deciding_stage
        assert r.outcome in ("accepted", "rejected")                       # shadow never labels `review`
    assert {"accept", "review", "reject"} & {r.signals["assessment"]["state"] for r in shadow}
    # fit_score and decision_confidence are separate quantities
    acc = [r for r in shadow if r.outcome == "accepted"]
    assert acc and all(r.fit_score is not None and r.decision_confidence != r.fit_score for r in acc)


def test_active_mode_labels_review_without_removing_anything(da_shadow, da_active):
    shadow = {r.job_id: r for r in _recs(da_shadow)}
    active = _recs(da_active)
    reviews = [r for r in active if r.outcome == "review"]
    assert reviews, "a tight spread threshold must flag at least one job"
    for r in reviews:
        assert shadow[r.job_id].signals["assessment"]["state"] == "review" == r.signals["assessment"]["state"]
        if shadow[r.job_id].outcome == "accepted":
            assert r.exposure is not None and r.exposure.rank, "a review job stays ranked and exposed"
    # a rejected job is never turned into an accept, and no accepted job is turned into a reject
    for r in active:
        assert not (shadow[r.job_id].outcome == "rejected" and r.outcome == "accepted")
        assert not (shadow[r.job_id].outcome == "accepted" and r.outcome == "rejected")
        if shadow[r.job_id].outcome == "rejected":
            assert r.outcome == "rejected"        # active mode never relabels a rejection
    assert len(active) == len(shadow)


def test_signals_from_semantic_phase2_and_eligibility_reach_the_assessment(tmp_path):
    from types import SimpleNamespace as NS

    from src.decisions.recorder import DecisionRecorder
    from src.decisions.storage import DecisionStorage

    cfg = _config_dir(tmp_path, None, {"disagreement.yaml": "mode: shadow\n"})
    db = str(tmp_path / "x.db")
    rec = DecisionRecorder.create(config_dir=str(cfg), profile_path=cfg / "profile.yaml", db_path=db, run_id="r", use_ai=True)
    job = Job(id="j1", source="s", title="Dev", description="python " * 60)
    rec.observe_validation([job], [job])
    rec.observe_dedup([job], [job])
    rec.observe_filter([job], {}, skipped=True)
    rec.observe_eligibility([job], NS(ran=True, rejection_records=[], decisions={
        "j1": {"decision": "REJECT", "confidence": 0.4, "reason": "low", "status": "success"}}))
    rec.observe_scores([(job, 0.80, {"location_fit": 0.9})])
    rec.observe_semantic({"j1": 0.82})
    rec.observe_phase2({"j1": NS(status="success", analysis=NS(profile_match_score=0.30))})
    rec.finalize([NS(job_id="j1", final_score=0.8, deterministic_score=0.8, semantic_score=0.82,
                     personalization_adjustment=0.0)])
    (r,) = DecisionStorage(db).get_decisions_for_run("r")
    a = r.signals["assessment"]
    assert a["signals"] == {"rules": 0.8, "semantic": 0.82, "phase2": 0.3, "eligibility": "REJECT", "user": None}
    assert a["state"] == "review" and a["spread"] == pytest.approx(0.52)
    assert any("said REJECT" in x for x in a["reasons"]) and any("signals disagree" in x for x in a["reasons"])
    assert {c.description for c in r.conflicts} >= {"LLM eligibility REJECT vs deterministic accept"}
    assert r.signals["eligibility"]["llm_self_reported_confidence"] == 0.4        # kept as a signal only
    assert r.decision_confidence < 0.5 and r.outcome == "accepted"                 # shadow: outcome untouched


def test_disagreement_config_hash_is_recorded_not_part_of_strategy_version(tmp_path):
    from src.decisions.disagreement import load_disagreement_config
    from src.decisions.strategy import build_strategy_manifest

    cfg = _config_dir(tmp_path, None, {"disagreement.yaml": "mode: shadow\n"})
    a = build_strategy_manifest(cfg, cfg / "profile.yaml")
    h = load_disagreement_config(cfg).config_hash()
    (cfg / "disagreement.yaml").write_text("mode: shadow\nspread_review: 0.5\n")
    assert build_strategy_manifest(cfg, cfg / "profile.yaml")["strategy_version"] == a["strategy_version"]
    assert load_disagreement_config(cfg).config_hash() != h