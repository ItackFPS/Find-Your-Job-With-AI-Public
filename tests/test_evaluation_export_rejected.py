"""``export --rejected``: a deterministic sample of jobs the pipeline rejected, rebuilt from the decision
journal's snapshots, so false negatives become measurable on that subsample (survival bias)."""

from __future__ import annotations

import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from src.decisions.storage import DecisionStorage
from src.evaluation.dataset import load_dataset
from src.evaluation.export import export_real_dataset
from tests.test_evaluation_export import LONG, _build_db, _sha


def _reject(conn: sqlite3.Connection, job_id: str, stage: str, *, at: str, outcome: str = "rejected",
            company: str = "Initech", text: bool = True) -> None:
    payload = {"source": "src_r", "title": "Technicien support", "company": company, "location": "Lyon",
               "department": None, "countries": ["France"], "remote": False, "work_mode": None,
               "employment_type": None, "salary_min": None, "salary_max": None, "salary_currency": "EUR",
               "published_at": "2026-09-01T00:00:00+00:00", "skills": ["python"],
               "description": f"{LONG} {company} recrute. Ecrire a jobs@initech.fr"}
    raw = json.dumps(payload, sort_keys=True).encode()
    h = f"h-{job_id}-{at}"
    conn.execute("INSERT OR IGNORE INTO job_snapshots (snapshot_hash, created_at, last_seen_at, job_id_hint, "
                 "text_blob, compression, text_len) VALUES (?,?,?,?,?,?,?)",
                 (h, at, at, job_id, zlib.compress(raw) if text else None, "zlib", len(raw)))
    conn.execute("INSERT INTO job_decisions (decision_id, schema_version, run_id, job_id, decided_at, outcome, "
                 "deciding_stage, strategy_version, evidence_json, conflicts_json, signals_json, stage_trace_json, "
                 "snapshot_hash) VALUES (?,1,?,?,?,?,?,'sv-x','[]','[]','{}','[]',?)",
                 (f"d-{job_id}-{at}", f"run-{at}", job_id, at, outcome, stage, h))


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    path = _build_db(tmp_path / "jobs.db")
    ds = DecisionStorage(str(path))
    ds.init_db()
    ds.close()
    with sqlite3.connect(path) as conn:
        for i in range(10):
            _reject(conn, f"rj{i}", "filter" if i % 2 else "requirements_gate", at="2026-10-01T00:00:00+00:00")
        _reject(conn, "dup", "deduplicate", at="2026-10-01T00:00:00+00:00")       # not an opportunity
        _reject(conn, "flip", "filter", at="2026-09-01T00:00:00+00:00")
        _reject(conn, "flip", "score", at="2026-10-02T00:00:00+00:00", outcome="accepted")  # latest: accepted
        _reject(conn, "purged", "filter", at="2026-10-01T00:00:00+00:00", text=False)
        _reject(conn, "id001", "filter", at="2026-10-01T00:00:00+00:00")         # stored in jobs: a survivor
    return path


def _meta(out: Path) -> dict:
    return json.loads((out / "PRIVATE_export_meta.json").read_text(encoding="utf-8"))


def test_rejected_sample_is_added_from_the_journal_only_for_genuine_rejections(db, tmp_path):
    info = export_real_dataset(db, tmp_path / "o", n=8, seed=1, rejected=6, salt="s",
                               as_of="2026-10-05T00:00:00+00:00")
    meta = _meta(tmp_path / "o")
    rejected = {new: old for new, old in meta["id_map"].items() if meta["strata"][new] == "pipeline_rejected"}
    assert len(rejected) == 6 and info["strata_counts"]["pipeline_rejected"] == 6
    assert set(rejected.values()) <= {f"rj{i}" for i in range(10)}          # never dup/flip/purged/stored
    assert set(meta["rejected_stage"]) == set(rejected)
    assert set(meta["rejected_stage"].values()) <= {"filter", "requirements_gate"}
    assert info["rejected_pool"] == 10
    ds = load_dataset(tmp_path / "o", require_complete_labels=False)
    assert len(ds.job_dicts) == 14
    assert "pipeline-rejected" in ds.manifest.notes and "survival bias" not in ds.manifest.notes.lower()


def test_rejected_jobs_are_anonymised_like_survivors(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=4, seed=1, rejected=10)
    text = (tmp_path / "o" / "jobs.jsonl").read_text(encoding="utf-8")
    for leak in ("Initech", "initech.fr", "@", "rj0", '"id": "rj'):
        assert leak not in text, leak
    annotations = (tmp_path / "o" / "annotations.csv").read_text(encoding="utf-8-sig")
    assert "pipeline_rejected" not in annotations and "requirements_gate" not in annotations   # blind sheet


def test_rejected_sample_is_deterministic_and_bounded(db, tmp_path):
    kw = dict(n=4, seed=3, rejected=3, salt="s", as_of="2026-10-05T00:00:00+00:00")
    export_real_dataset(db, tmp_path / "a", **kw)
    export_real_dataset(db, tmp_path / "b", **kw)
    assert (tmp_path / "a" / "jobs.jsonl").read_bytes() == (tmp_path / "b" / "jobs.jsonl").read_bytes()
    big = export_real_dataset(db, tmp_path / "c", n=4, seed=3, rejected=500)
    assert big["strata_counts"]["pipeline_rejected"] == 10


def test_default_export_is_unchanged_and_never_reads_the_journal(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=8, seed=1)
    assert "pipeline_rejected" not in set(_meta(tmp_path / "o")["strata"].values())
    assert "survival bias" in load_dataset(tmp_path / "o", require_complete_labels=False).manifest.notes.lower()


def test_rejected_export_without_a_journal_explains_itself(tmp_path):
    path = _build_db(tmp_path / "plain.db")
    info = export_real_dataset(path, tmp_path / "o", n=4, seed=1, rejected=5)
    assert info["rejected_pool"] == 0 and "pipeline_rejected" not in info["strata_counts"]
    assert "survival bias" in load_dataset(tmp_path / "o", require_complete_labels=False).manifest.notes.lower()


def test_rejected_export_never_writes_the_database(db, tmp_path):
    before = _sha(db)
    export_real_dataset(db, tmp_path / "o", n=4, seed=1, rejected=5)
    assert _sha(db) == before


def test_run_warning_reflects_the_rejected_sample(db, tmp_path):
    from src.evaluation.runner import _dataset_warnings

    export_real_dataset(db, tmp_path / "o", n=4, seed=1, rejected=5)
    warns = " ".join(_dataset_warnings(load_dataset(tmp_path / "o", require_complete_labels=False)))
    assert "pipeline-rejected" in warns and "NOT measurable" not in warns


def test_audit_reproduces_and_checks_company_names_of_a_rejected_sample(db, tmp_path):
    from src.evaluation.audit import audit_dataset

    with sqlite3.connect(db) as conn:      # the journal row carries the real company, as production writes it
        conn.execute("UPDATE job_decisions SET company = 'Initech'")
    export_real_dataset(db, tmp_path / "o", n=6, seed=2, rejected=4, salt="s", as_of="2026-10-05T00:00:00+00:00")
    checks = audit_dataset(tmp_path / "o", db=db)["checks"]
    assert checks["reproducible"]["ok"], checks["reproducible"]
    assert checks["company_names"]["ok"] and "checked against the database" in checks["company_names"]["detail"]


def test_the_pool_query_reads_the_journal_in_file_order(db):
    """Through idx_decisions_job every row is a random read: 517 s cold on a 3.3 GB jobs.db. Scan instead."""
    from src.evaluation.export import _rejected_candidates, open_readonly

    conn = open_readonly(db)
    seen: list[str] = []
    conn.set_trace_callback(seen.append)
    try:
        assert len(_rejected_candidates(conn, {"id001"})) == 10
        [sql] = [s for s in seen if "ROW_NUMBER" in s]
        plan = " | ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {sql}"))
    finally:
        conn.close()
    assert "SCAN job_decisions" in plan and "INDEX" not in plan.split("SCAN job_decisions", 1)[1].split("|")[0]


def test_a_journal_predating_content_hash_still_gives_the_pool(db):
    """The fallback query (NULL content_hash) is the same sequential scan and finds the same jobs."""
    from src.evaluation.export import _rejected_candidates, open_readonly

    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE job_decisions DROP COLUMN content_hash")
    conn = open_readonly(db)
    try:
        pool = _rejected_candidates(conn, {"id001"})
    finally:
        conn.close()
    assert sorted(p["id"] for p in pool) == sorted(f"rj{i}" for i in range(10))
