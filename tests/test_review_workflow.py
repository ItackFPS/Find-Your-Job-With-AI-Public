"""End to end: a job the user wants that the pipeline later rejects is reviewed once and feeds evaluation.

Real ``run_pipeline`` (fake embeddings), real decision journal, feedback API, review queue and
feedback-derived dataset. Weak behavioural signals (a view) never put a rejected job up for review.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient

from src.decisions.storage import DecisionStorage
from src.evaluation.dataset import load_dataset
from tests.test_decision_journal_orchestrator import DS, _config_dir, _FakeEmbeddingService


def _run(cfg: Path, db: Path) -> str:
    from src.pipeline.orchestrator import run_pipeline

    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=str(db), config_dir=str(cfg), prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert result.status == "completed", result.error
    time.sleep(1.1)        # decisions are stamped to the second: keep runs and answers strictly ordered
    return result.run_id


def _latest(db: Path, job_id: str):
    store = DecisionStorage(str(db))
    try:
        return store.get_latest_decision(job_id)
    finally:
        store.close()


@pytest.fixture
def client(tmp_path):
    from src.api.app import app

    saved = app.state.db_path
    app.state.db_path = str(tmp_path / "jobs.db")
    yield TestClient(app)
    app.state.db_path = saved


def _queue(client) -> dict[str, dict]:
    return {i["decision"]["job_id"]: i for i in client.get("/api/decisions/review-queue").json()["items"]}


def test_a_wanted_job_that_gets_rejected_is_reviewed_once_and_becomes_a_measurable_miss(tmp_path, client):
    from src.evaluation.feedback_dataset import build_feedback_dataset
    from src.evaluation.models import StrategyRef
    from src.evaluation.runner import run_evaluation
    from src.feedback.service import FeedbackService

    cfg = _config_dir(tmp_path, None, {"disagreement.yaml": "mode: shadow\n"})
    db = tmp_path / "jobs.db"
    run_id = _run(cfg, db)
    store = DecisionStorage(str(db))
    try:
        _, records = store.list_for_run(run_id, limit=1000)
    finally:
        store.close()
    wanted = next(r for r in records if r.outcome == "accepted" and "Symfony" in (r.job.title or ""))
    viewed = next(r for r in records if r.outcome == "rejected" and r.deciding_stage == "filter")

    # explicit intent on the accepted job; only a weak view on a rejected one
    resp = client.post(f"/api/jobs/{wanted.job_id}/feedback",
                       json={"type": "interested", "source": "ui", "decision_id": wanted.decision_id})
    assert resp.status_code == 201, resp.text
    FeedbackService(str(db), str(cfg)).record_behavior(viewed.job_id, "viewed")

    # later the pipeline rejects the wanted job (here: a new title exclusion; ageing out works the same way)
    filters = yaml.safe_load((cfg / "filters.yaml").read_text(encoding="utf-8"))
    filters["excluded_title_keywords"]["test_only"] = ["symfony"]
    (cfg / "filters.yaml").write_text(yaml.safe_dump(filters, allow_unicode=True), encoding="utf-8")
    _run(cfg, db)

    rejected = _latest(db, wanted.job_id)
    assert rejected.outcome == "rejected" and rejected.deciding_stage == "filter"
    assessment = rejected.signals["assessment"]
    assert assessment["state"] == "review"
    assert assessment["signals"]["user"]["basis"] == "explicit:interested"
    assert any("explicit:interested" in r for r in assessment["reasons"])
    assert _latest(db, viewed.job_id).signals["assessment"]["signals"]["user"] is None   # a view is no stance

    queue = _queue(client)
    assert wanted.job_id in queue and viewed.job_id not in queue
    item = queue[wanted.job_id]
    assert item["decision"]["outcome"] == "rejected" and item["has_snapshot"]

    # the user confirms on the rejected decision: answered, and not asked again on the next runs
    before = client.get(f"/api/decisions/{rejected.decision_id}").json()
    resp = client.post(f"/api/jobs/{wanted.job_id}/feedback",
                       json={"type": "interested", "source": "ui", "decision_id": rejected.decision_id})
    assert resp.status_code == 201, resp.text
    assert wanted.job_id not in _queue(client)
    # answering is recorded beside the decision, never into it: the original rejection stays auditable
    assert client.get(f"/api/decisions/{rejected.decision_id}").json() == before

    # the self-check reads the contradiction as a lead to review, not as a label (read-only on jobs.db)
    from src.evaluation.selfcheck import feedback_coverage

    fb = feedback_coverage(db)
    assert wanted.job_id in fb["contradictions"]["rejected_but_interested"]["job_ids"]
    assert fb["status"] == "measured" and "not a verified label" in fb["note"]
    _run(cfg, db)
    again = _latest(db, wanted.job_id)
    assert again.decision_id != rejected.decision_id and again.signals["assessment"]["state"] == "review"
    assert wanted.job_id not in _queue(client)

    # the answer is evaluation ground truth: the rejecting strategy shows it as a false negative
    out = tmp_path / "feedback-ds"
    build_feedback_dataset(db, out, config_dir=cfg, profile_path=cfg / "profile.yaml", anonymize=False,
                           as_of="2099-01-01T00:00:00+00:00", salt="s")
    ds = load_dataset(out, require_complete_labels=False)
    assert "accept" in {lab.expected for lab in ds.labels.values()}
    run = run_evaluation(ds, StrategyRef(config_dir=str(cfg), profile_path=str(cfg / "profile.yaml")))
    assert run.metrics["overall"]["fn"] >= 1

    # the strategy changes back (accepted), then rejects it again: a new episode, so it is asked about again
    del filters["excluded_title_keywords"]["test_only"]
    (cfg / "filters.yaml").write_text(yaml.safe_dump(filters, allow_unicode=True), encoding="utf-8")
    _run(cfg, db)
    assert _latest(db, wanted.job_id).outcome == "accepted" and wanted.job_id not in _queue(client)
    filters["excluded_title_keywords"]["test_only"] = ["symfony"]
    (cfg / "filters.yaml").write_text(yaml.safe_dump(filters, allow_unicode=True), encoding="utf-8")
    _run(cfg, db)
    assert _latest(db, wanted.job_id).outcome == "rejected" and wanted.job_id in _queue(client)
    _run(cfg, db)                                       # unanswered: still there, once
    assert list(_queue(client)).count(wanted.job_id) == 1
