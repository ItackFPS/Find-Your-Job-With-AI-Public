"""``lab.db``: hypotheses, experiments, results, candidate strategies, approvals.

Plain SQLite, separate from ``jobs.db``. Results and approvals are append-only.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, statement TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS candidate_strategies (
    candidate_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, hypothesis_id TEXT NOT NULL,
    bundle_hash TEXT NOT NULL, bundle_path TEXT NOT NULL, overrides_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate');   -- candidate | approved | retired
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, candidate_id TEXT NOT NULL,
    dataset_id TEXT NOT NULL, dataset_jobs_sha256 TEXT NOT NULL, dataset_labels_sha256 TEXT NOT NULL,
    baseline_fingerprint TEXT NOT NULL, candidate_fingerprint TEXT NOT NULL,
    verdict TEXT NOT NULL, eligible_for_approval INTEGER NOT NULL, result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY, approved_at TEXT NOT NULL, candidate_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL, approved_by TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS experiments_no_update BEFORE UPDATE ON experiments
    BEGIN SELECT RAISE(ABORT, 'experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS experiments_no_delete BEFORE DELETE ON experiments
    BEGIN SELECT RAISE(ABORT, 'experiments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_update BEFORE UPDATE ON approvals
    BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS approvals_no_delete BEFORE DELETE ON approvals
    BEGIN SELECT RAISE(ABORT, 'approvals are append-only'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LabStore:
    def __init__(self, path: str | Path = "data/lab.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)

    def close(self) -> None:
        self._conn.close()

    def add_hypothesis(self, statement: str, rationale: str = "") -> str:
        hid = f"hyp-{uuid.uuid4().hex[:10]}"
        with self._conn:
            self._conn.execute("INSERT INTO hypotheses VALUES (?,?,?,?, 'open')", (hid, _now(), statement, rationale))
        return hid

    def add_candidate(self, hypothesis_id: str, bundle_hash: str, bundle_path: str, overrides: dict[str, Any]) -> str:
        if not self._conn.execute("SELECT 1 FROM hypotheses WHERE hypothesis_id = ?", (hypothesis_id,)).fetchone():
            raise KeyError(f"unknown hypothesis {hypothesis_id}")
        cid = f"cand-{bundle_hash[:12]}"
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO candidate_strategies VALUES (?,?,?,?,?,?, 'candidate')",
                (cid, _now(), hypothesis_id, bundle_hash, bundle_path, json.dumps(overrides, sort_keys=True)))
        return cid

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM candidate_strategies WHERE candidate_id = ?", (candidate_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown candidate {candidate_id}")
        return dict(row)

    def add_experiment(self, candidate_id: str, dataset: dict[str, Any], result: dict[str, Any]) -> str:
        eid = f"exp-{uuid.uuid4().hex[:10]}"
        with self._conn:
            self._conn.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (eid, _now(), candidate_id, dataset["dataset_id"], dataset["jobs_sha256"], dataset["labels_sha256"],
                 result["baseline_fingerprint"], result["candidate_fingerprint"], result["verdict"],
                 int(result["eligible_for_approval"]), json.dumps(result, sort_keys=True, default=str)))
        return eid

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment {experiment_id}")
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json"))
        return d

    def record_approval(self, candidate_id: str, experiment_id: str, approved_by: str, reason: str) -> str:
        aid = f"appr-{uuid.uuid4().hex[:10]}"
        with self._conn:
            self._conn.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?)",
                               (aid, _now(), candidate_id, experiment_id, approved_by, reason))
            self._conn.execute("UPDATE candidate_strategies SET status = 'approved' WHERE candidate_id = ?",
                               (candidate_id,))
        return aid
