"""Self-check: current strategy vs the recorded baseline on frozen datasets (src/evaluation/selfcheck.py)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime

import pytest
import yaml

from src.decisions.models import DecisionRecord, JobMeta
from src.decisions.storage import DecisionStorage, snapshot_hash, snapshot_payload
from src.evaluation.__main__ import DEFAULT_SELF_CHECK, main
from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.selfcheck import check_dataset, feedback_coverage, run_self_check, write_report
from src.evaluation.store import golden_to_json, read_golden
from src.feedback.models import FeedbackEvent
from src.feedback.storage import FeedbackStorage
from src.models.job import Job
from tests.test_rejection_safety_gate import ROOT

DS_DIR, GOLDEN = DEFAULT_SELF_CHECK
CONFIG = StrategyRef(config_dir=str(ROOT / "config"))


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DS_DIR)


@pytest.fixture(scope="module")
def committed(dataset):
    return check_dataset(dataset, CONFIG, read_golden(GOLDEN))


def test_committed_benchmark_matches_its_recorded_baseline(committed):
    assert committed["status"] == "pass"
    assert committed["baseline"]["status"] == "identical"
    assert committed["safety"] == {"unsafe_rejections": [], "rejections_without_reason": []}
    assert committed["explanations"]["coverage"] == 1.0


def test_report_states_its_evidence_and_limits(committed):
    ds = committed["dataset"]
    assert ds["kind"] == "synthetic" and ds["n_jobs"] == 50 and ds["label_provenance"] == {"human": 50}
    assert any("synthetic" in x for x in committed["limitations"])
    # eligibility labels are not relevance labels: ranking quality is not claimed
    assert committed["ranking"]["status"] == "not_measured"
    assert committed["ranking"]["score_separation"]["status"] in ("diagnostic", "not_measured")


def test_errors_are_grouped_into_patterns_with_a_lever(committed, dataset):
    fp_ids = {j for p in committed["patterns"] if p["kind"] == "false_positive" for j in p["job_ids"]}
    assert fp_ids and all(dataset.labels[j].expected == "reject" for j in fp_ids)
    assert committed["eligibility"]["fp"] == len(fp_ids)
    assert all(p["lever"]["kind"] in ("lab", "code") for p in committed["patterns"])


def test_the_lab_lever_does_not_promise_a_candidate(committed):
    # The lab agent mines a false-positive candidate only from a title word several such jobs share; on the
    # committed benchmark it finds none, so the report must not say it "can attribute" the pattern.
    fp = next(p for p in committed["patterns"] if p["kind"] == "false_positive")
    assert "can attribute" not in fp["lever"]["detail"]
    assert "human-written hypothesis" in fp["lever"]["detail"]


def _golden_copy(tmp_path, **changes):
    g = read_golden(GOLDEN)
    decisions = [d.model_copy(update=changes[d.job_id]) if d.job_id in changes else d for d in g.decisions]
    path = tmp_path / "golden.json"
    path.write_text(golden_to_json(g.model_copy(update={"decisions": decisions})), encoding="utf-8")
    return path


def test_a_job_the_baseline_got_right_and_now_wrong_is_a_regression(committed, dataset, tmp_path):
    fp = committed["patterns"][0]["job_ids"][0]          # currently accepted, labelled reject
    path = _golden_copy(tmp_path, **{fp: {"verdict": "reject", "stage": "filter", "reasons": ["filter:x"]}})
    entry = check_dataset(dataset, CONFIG, read_golden(path))
    assert entry["status"] == "regression"
    assert entry["baseline"]["flips"]["broken"] == [fp]
    assert entry["baseline"]["delta"]["precision"] < 0


def test_a_fix_alone_is_a_change_for_review_not_a_regression(dataset, tmp_path):
    path = _golden_copy(tmp_path, bm01={"verdict": "reject", "stage": "filter", "reasons": ["filter:x"]})
    entry = check_dataset(dataset, CONFIG, read_golden(path))
    assert entry["status"] == "changed" and entry["baseline"]["flips"]["fixed"] == ["bm01"]


def test_a_baseline_recorded_on_other_labels_is_refused(dataset, tmp_path):
    g = read_golden(GOLDEN)
    path = tmp_path / "g.json"
    path.write_text(golden_to_json(g.model_copy(update={"dataset": {**g.dataset, "labels_sha256": "0" * 64}})),
                    encoding="utf-8")
    entry = check_dataset(dataset, CONFIG, read_golden(path))
    assert entry["status"] == "baseline_mismatch" and "flips" not in entry["baseline"]


def test_a_missing_baseline_never_passes(dataset):
    assert check_dataset(dataset, CONFIG, None)["status"] == "changed"


def test_an_unsafe_rejection_is_a_safety_violation_whatever_the_baseline(dataset, tmp_path):
    cfg = tmp_path / "config"
    shutil.copytree(ROOT / "config", cfg)
    filters = yaml.safe_load((cfg / "filters.yaml").read_text(encoding="utf-8"))
    filters["excluded_title_keywords"]["selfcheck_test"] = ["python"]      # rejects accept-labelled Python jobs
    (cfg / "filters.yaml").write_text(yaml.safe_dump(filters, allow_unicode=True), encoding="utf-8")
    report = run_self_check([(DS_DIR, GOLDEN)], StrategyRef(config_dir=str(cfg)))
    entry = report["datasets"][0]
    assert report["status"] == entry["status"] == "safety_violation"
    assert entry["safety"]["unsafe_rejections"] and entry["baseline"]["flips"]["broken"]
    assert any(p["kind"] == "unsafe_rejection" and p["key"].startswith("filter:") for p in entry["patterns"])
    assert report["recommendations"][0]["kind"] == "action"
    assert "do not relabel" in report["recommendations"][0]["text"]


def test_reports_are_never_overwritten(tmp_path):
    report = {"created_at": "2026-10-18T00:00:00+00:00", "datasets": [{"strategy": {"fingerprint": "abcdef123"}}]}
    a, b = write_report(report, tmp_path), write_report(report, tmp_path)
    assert a != b and json.loads(a.read_text(encoding="utf-8")) == report


def test_cli_exit_code_follows_the_status(tmp_path, capsys):
    assert main(["self-check", "--config-dir", str(ROOT / "config"), "--out", str(tmp_path),
                 "--db", str(tmp_path / "none.db")]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pass"
    written = list(tmp_path.glob("*.json"))
    assert len(written) == 1
    # feedback is reported by default; a missing database is said, not skipped silently
    assert json.loads(written[0].read_text(encoding="utf-8"))["feedback"]["status"] == "unavailable"
    bad = tmp_path / "bad.json"
    bad.write_text(golden_to_json(read_golden(GOLDEN).model_copy(update={"decisions": []})), encoding="utf-8")
    assert main(["self-check", "--dataset", f"{DS_DIR}={bad}", "--no-write", "--no-feedback"]) == 1


# -- feedback coverage (read-only jobs.db) -------------------------------------------------------

@pytest.fixture()
def journal(tmp_path):
    path = str(tmp_path / "jobs.db")
    ds, fs = DecisionStorage(path), FeedbackStorage(path)
    ds.init_db()
    fs.init_db()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    outcomes = {"j0": "rejected", "j1": "accepted", "j2": "accepted", "j3": "rejected"}
    for jid, outcome in outcomes.items():
        job = Job(id=jid, source="s", title="Dev", company="Acme", description="python " * 20)
        payload = snapshot_payload(job)
        h = snapshot_hash(payload)
        meta = JobMeta(title=job.title, company="Acme", source="s")
        ds.write_run([DecisionRecord(decision_id=f"d-{jid}", run_id="r", job_id=jid, strategy_version="sv",
                                     outcome=outcome, deciding_stage="ranking", decided_at=now, snapshot_hash=h,
                                     job=meta)], {h: {"payload": payload, "job_id": jid, "meta": meta}})
    events = [("j0", "not_interested", "2026-01-01"), ("j0", "interested", "2026-01-02"),   # latest wins
              ("j1", "not_interested", "2026-01-01"), ("j2", "unsure", "2026-01-01"),
              ("j2", "viewed", "2026-01-03")]
    for i, (jid, t, at) in enumerate(events):
        fs.append(FeedbackEvent(event_id=f"e{i}", job_id=jid, signal_class="behavior" if t == "viewed" else "explicit",
                                type=t, source="ui", occurred_at=f"{at}T00:00:00+00:00"))
    ds.close()
    fs.close()
    return path


def test_feedback_is_reported_as_signals_never_as_labels(journal):
    before = (os.path.getsize(journal), os.stat(journal).st_mtime_ns)
    fb = feedback_coverage(journal)
    assert (os.path.getsize(journal), os.stat(journal).st_mtime_ns) == before
    assert fb["decided_jobs"] == 4 and fb["jobs_with_explicit_feedback"] == 3
    # one answer per job, the latest; `unsure` is neither positive nor negative; behaviour is not an answer
    assert fb["latest_answers"] == {"interested": 1, "not_interested": 1, "unsure": 1}
    assert fb["contradictions"]["rejected_but_interested"] == {"count": 1, "job_ids": ["j0"]}
    assert fb["contradictions"]["accepted_but_not_interested"] == {"count": 1, "job_ids": ["j1"]}
    assert "not a verified label" in fb["note"]
    with sqlite3.connect(f"file:{journal}?mode=ro", uri=True) as c:
        assert c.execute("SELECT COUNT(*) FROM feedback_events").fetchone()[0] == 5


def test_feedback_coverage_tolerates_a_missing_database(tmp_path):
    assert feedback_coverage(tmp_path / "none.db")["status"] == "unavailable"
