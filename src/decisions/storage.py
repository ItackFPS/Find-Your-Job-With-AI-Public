"""SQLite persistence of the decision journal (tables live in ``jobs.db``).

Tables (additive, idempotent ``CREATE TABLE IF NOT EXISTS`` like the rest of the repo):

* ``strategy_versions``  — manifest of every strategy a decision was made under.
* ``job_snapshots``      — content-addressed, optionally compressed job text (so a rejected
  job can be audited later). Text can be purged while the row (metadata) stays.
* ``job_decisions``      — one row per (run, job), accepted and rejected.
"""

from __future__ import annotations

import json
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.decisions.models import DecisionRecord

_SNAPSHOT_FIELDS = (
    "source", "title", "company", "location", "department", "countries", "remote", "work_mode",
    "employment_type", "salary_min", "salary_max", "salary_currency", "published_at",
    "skills", "description",
)


def snapshot_payload(job: Any) -> dict[str, Any]:
    """The job fields needed to rebuild a ``Job`` for audit/evaluation (no URLs, no raw payload)."""
    d = job.to_dict()
    return {k: d.get(k) for k in _SNAPSHOT_FIELDS}


def snapshot_hash(payload: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DecisionStorage:
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def init_db(self) -> None:
        key = str(self._db_path.resolve())
        if key in self._initialized_paths and self._db_path.exists():
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS strategy_versions (
                strategy_version TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                manifest_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_snapshots (
                snapshot_hash TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,        -- first seen
                last_seen_at TEXT NOT NULL,      -- retention clock: refreshed whenever re-referenced
                job_id_hint TEXT,
                title TEXT, company TEXT, source TEXT, location TEXT,
                text_blob BLOB,
                compression TEXT,            -- 'zlib' | 'none'
                text_len INTEGER,
                text_purged_at TEXT
            );
            CREATE TABLE IF NOT EXISTS job_decisions (
                decision_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                outcome TEXT NOT NULL,
                deciding_stage TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                fit_score REAL,
                decision_confidence REAL,
                confidence_calibrated INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL,
                conflicts_json TEXT NOT NULL,
                signals_json TEXT NOT NULL,
                stage_trace_json TEXT NOT NULL,
                exposure_json TEXT,
                snapshot_hash TEXT,
                evidence_text_purged INTEGER NOT NULL DEFAULT 0,   -- span quotes removed by retention
                title TEXT, company TEXT, source TEXT, location TEXT, content_hash TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_run_job ON job_decisions(run_id, job_id);
            CREATE INDEX IF NOT EXISTS idx_decisions_job ON job_decisions(job_id, decided_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_strategy ON job_decisions(strategy_version);
            CREATE INDEX IF NOT EXISTS idx_decisions_outcome ON job_decisions(outcome, deciding_stage);
            CREATE INDEX IF NOT EXISTS idx_decisions_decided_at ON job_decisions(decided_at);
            CREATE INDEX IF NOT EXISTS idx_decisions_snapshot ON job_decisions(snapshot_hash);
            CREATE INDEX IF NOT EXISTS idx_snapshots_last_seen ON job_snapshots(last_seen_at);
        """)
        conn.commit()
        self._initialized_paths.add(key)

    # -- writes -------------------------------------------------------------------------------

    def register_strategy(self, manifest: dict[str, Any]) -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO strategy_versions (strategy_version, created_at, manifest_json) VALUES (?, ?, ?)",
            (manifest["strategy_version"], _now(), json.dumps(manifest, sort_keys=True, default=str)),
        )
        conn.commit()

    def write_run(
        self,
        records: list[DecisionRecord],
        snapshots: dict[str, dict[str, Any]],
        *,
        compress: bool = True,
    ) -> tuple[int, int]:
        """Persist records + their snapshots in one transaction → ``(records, new_snapshots)``.

        ``snapshots`` maps ``snapshot_hash`` → ``{"payload": dict, "job_id": str, "meta": JobMeta-like}``.
        Existing snapshots are kept untouched (content-addressed, so identical content is stored once).
        """
        conn = self._get_conn()
        new_snaps = 0
        with conn:
            for h, snap in snapshots.items():
                raw = json.dumps(snap["payload"], ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                blob = zlib.compress(raw, 6) if compress else raw
                meta = snap["meta"]
                now = _now()
                existed = conn.execute("SELECT 1 FROM job_snapshots WHERE snapshot_hash = ?", (h,)).fetchone()
                # Identical content is stored once; seeing it again refreshes the retention clock and
                # RESTORES the text if a previous retention pass already purged it.
                conn.execute(
                    """INSERT INTO job_snapshots
                       (snapshot_hash, created_at, last_seen_at, job_id_hint, title, company, source, location,
                        text_blob, compression, text_len)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(snapshot_hash) DO UPDATE SET
                         last_seen_at = excluded.last_seen_at,
                         text_blob = COALESCE(job_snapshots.text_blob, excluded.text_blob),
                         compression = CASE WHEN job_snapshots.text_blob IS NULL
                                            THEN excluded.compression ELSE job_snapshots.compression END,
                         text_len = CASE WHEN job_snapshots.text_blob IS NULL
                                         THEN excluded.text_len ELSE job_snapshots.text_len END,
                         text_purged_at = CASE WHEN job_snapshots.text_blob IS NULL
                                               THEN NULL ELSE job_snapshots.text_purged_at END""",
                    (h, now, now, snap["job_id"], meta.title, meta.company, meta.source, meta.location,
                     blob, "zlib" if compress else "none", len(raw)),
                )
                new_snaps += 0 if existed else 1
            cur_n = 0
            for r in records:
                cur = conn.execute(
                    """INSERT INTO job_decisions
                       (decision_id, schema_version, run_id, job_id, decided_at, outcome, deciding_stage,
                        strategy_version, fit_score, decision_confidence, confidence_calibrated,
                        evidence_json, conflicts_json, signals_json, stage_trace_json, exposure_json,
                        snapshot_hash, title, company, source, location, content_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id, job_id) DO NOTHING""",   # only the intended duplicate is ignored
                    (
                        r.decision_id, r.schema_version, r.run_id, r.job_id, r.decided_at, r.outcome,
                        r.deciding_stage, r.strategy_version, r.fit_score, r.decision_confidence,
                        int(r.confidence_calibrated),
                        json.dumps([e.model_dump() for e in r.evidence], ensure_ascii=False),
                        json.dumps([c.model_dump() for c in r.conflicts], ensure_ascii=False),
                        json.dumps(r.signals, ensure_ascii=False, default=str),
                        json.dumps([t.model_dump() for t in r.stage_trace], ensure_ascii=False),
                        json.dumps(r.exposure.model_dump(), ensure_ascii=False) if r.exposure else None,
                        r.snapshot_hash, r.job.title, r.job.company, r.job.source, r.job.location,
                        r.job.content_hash,
                    ),
                )
                cur_n += cur.rowcount
        return cur_n, new_snaps

    # -- reads --------------------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DecisionRecord:
        from src.decisions.models import (
            ConflictItem, EvidenceItem, Exposure, JobMeta, StageTraceEntry,
        )

        return DecisionRecord(
            schema_version=row["schema_version"], decision_id=row["decision_id"], run_id=row["run_id"],
            job_id=row["job_id"], decided_at=row["decided_at"], outcome=row["outcome"],
            deciding_stage=row["deciding_stage"], strategy_version=row["strategy_version"],
            fit_score=row["fit_score"], decision_confidence=row["decision_confidence"],
            confidence_calibrated=bool(row["confidence_calibrated"]),
            evidence=[EvidenceItem(**e) for e in json.loads(row["evidence_json"])],
            conflicts=[ConflictItem(**c) for c in json.loads(row["conflicts_json"])],
            signals=json.loads(row["signals_json"]),
            stage_trace=[StageTraceEntry(**t) for t in json.loads(row["stage_trace_json"])],
            exposure=Exposure(**json.loads(row["exposure_json"])) if row["exposure_json"] else None,
            snapshot_hash=row["snapshot_hash"],
            job=JobMeta(title=row["title"], company=row["company"], source=row["source"],
                        location=row["location"], content_hash=row["content_hash"]),
        )

    def get_latest_decision(self, job_id: str) -> DecisionRecord | None:
        row = self._get_conn().execute(
            "SELECT * FROM job_decisions WHERE job_id = ? ORDER BY decided_at DESC, rowid DESC LIMIT 1", (job_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def list_for_run(
        self, run_id: str, *, outcome: str | None = None, stage: str | None = None,
        limit: int = 100, offset: int = 0, needs_review: bool = False,
    ) -> tuple[int, list[DecisionRecord]]:
        """``(total, page)`` of a run's decisions, optionally filtered by outcome / deciding stage /
        the disagreement engine's ``review`` flag."""
        where, params = "run_id = ?", [run_id]
        if needs_review:
            where += " AND json_extract(signals_json, '$.assessment.state') = 'review'"
        if outcome:
            where += " AND outcome = ?"
            params.append(outcome)
        if stage:
            where += " AND deciding_stage = ?"
            params.append(stage)
        conn = self._get_conn()
        total = int(conn.execute(f"SELECT COUNT(*) FROM job_decisions WHERE {where}", params).fetchone()[0])
        rows = conn.execute(
            f"SELECT * FROM job_decisions WHERE {where} ORDER BY outcome, deciding_stage, job_id LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return total, [self._row_to_record(r) for r in rows]

    # A decision of job ``d`` in the same *episode* as ``d`` (no decision with another outcome after it)
    # was marked by the rejection audit sample (src/decisions/audit.py).
    _EPISODE_AUDITED = """EXISTS (SELECT 1 FROM job_decisions p
                   WHERE p.job_id = d.job_id AND p.outcome = d.outcome
                     AND json_extract(p.signals_json, '$.audit.sampled') = 1
                     AND NOT EXISTS (SELECT 1 FROM job_decisions q
                                     WHERE q.job_id = d.job_id AND q.outcome <> d.outcome
                                       AND (q.decided_at > p.decided_at
                                            OR (q.decided_at = p.decided_at AND q.rowid > p.rowid))))"""
    _LATEST = """(SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY decided_at DESC, rowid DESC) AS rn
                  FROM job_decisions) d"""

    def _has_feedback_table(self) -> bool:
        return self._get_conn().execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'feedback_events'").fetchone() is not None

    def _unanswered_clause(self) -> str:
        """SQL condition: latest decision ``d`` has no explicit answer in its episode (see review_queue)."""
        if not self._has_feedback_table():
            return ""
        return """AND NOT EXISTS (SELECT 1 FROM feedback_events f WHERE f.job_id = d.job_id
                         AND f.signal_class = 'explicit'
                         AND ((f.decision_id IS NULL AND f.occurred_at > d.decided_at)
                              OR f.decision_id IN (
                                  SELECT p.decision_id FROM job_decisions p
                                  WHERE p.job_id = d.job_id AND p.outcome = d.outcome
                                    AND NOT EXISTS (SELECT 1 FROM job_decisions q
                                                    WHERE q.job_id = d.job_id AND q.outcome <> d.outcome
                                                      AND (q.decided_at > p.decided_at
                                                           OR (q.decided_at = p.decided_at
                                                               AND q.rowid > p.rowid))))))"""

    def review_queue(
        self, *, outcome: str | None = None, limit: int = 50, offset: int = 0,
    ) -> tuple[int, list[DecisionRecord]]:
        """``(total, page)`` of jobs whose LATEST decision the disagreement engine flagged ``review``, or
        that the rejection audit sample picked in the current episode, and that the user has not
        answered. A doubtful decision is asked about once per *episode*: the run of this job's latest
        decisions with the same outcome. Explicit feedback linked to any decision of the current episode
        answers it (a job rejected run after run is asked about once, not after every run); unlinked
        explicit feedback answers it when it came strictly after the latest decision (timestamps are to
        the second, so a tie is not an answer). Feedback linked to a decision of an earlier episode never
        does, whatever its timestamp — e.g. the user liked the job while it was accepted, or answered an
        earlier rejection before the job was accepted again — so a job comes back here each time its
        outcome changes to a doubtful one.

        Rejected jobs are included: this is where a possibly-wrong rejection becomes visible. Newest
        first. A database without a feedback table simply has nothing answered yet."""
        conn = self._get_conn()
        where, params = "", []
        if outcome:
            where, params = "AND d.outcome = ?", [outcome]
        base = f"""FROM {self._LATEST}
                   WHERE d.rn = 1
                     AND (json_extract(d.signals_json, '$.assessment.state') = 'review'
                          OR {self._EPISODE_AUDITED})
                   {self._unanswered_clause()} {where}"""
        total = int(conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0])
        rows = conn.execute(f"SELECT d.* {base} ORDER BY d.decided_at DESC, d.job_id LIMIT ? OFFSET ?",
                            [*params, limit, offset]).fetchall()
        return total, [self._row_to_record(r) for r in rows]

    def open_audit_count(self) -> int:
        """Jobs whose latest decision is a rejection audited in its episode and not yet answered."""
        return int(self._get_conn().execute(
            f"""SELECT COUNT(*) FROM {self._LATEST}
                WHERE d.rn = 1 AND d.outcome = 'rejected' AND {self._EPISODE_AUDITED}
                {self._unanswered_clause()}""").fetchone()[0])

    def audit_ineligible_jobs(self, job_ids: list[str]) -> set[str]:
        """Of ``job_ids`` (about to be journaled as rejected), those not to sample again: the current
        rejection episode was already audited, or the user already gave explicit feedback on the job
        (their view is known; the disagreement engine uses it)."""
        if not job_ids:
            return set()
        conn = self._get_conn()
        out: set[str] = set()
        for i in range(0, len(job_ids), 500):
            chunk = job_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            out |= {r[0] for r in conn.execute(
                f"""SELECT d.job_id FROM {self._LATEST}
                    WHERE d.rn = 1 AND d.outcome = 'rejected' AND d.job_id IN ({marks})
                      AND {self._EPISODE_AUDITED}""", chunk)}
            if self._has_feedback_table():
                out |= {r[0] for r in conn.execute(
                    f"""SELECT DISTINCT job_id FROM feedback_events
                        WHERE signal_class = 'explicit' AND job_id IN ({marks})""", chunk)}
        return out

    def get_decision_for_run(self, job_id: str, run_id: str) -> DecisionRecord | None:
        """The decision a specific run made about a job (not shadowed by newer runs)."""
        row = self._get_conn().execute(
            "SELECT * FROM job_decisions WHERE job_id = ? AND run_id = ?", (job_id, run_id)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_decision_at(self, job_id: str, ts: str) -> DecisionRecord | None:
        """Latest decision made at or before ``ts`` (ISO-8601, UTC) for a job."""
        row = self._get_conn().execute(
            "SELECT * FROM job_decisions WHERE job_id = ? AND decided_at <= ? "
            "ORDER BY decided_at DESC, rowid DESC LIMIT 1", (job_id, ts),
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        row = self._get_conn().execute("SELECT * FROM job_decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def get_decisions_for_run(self, run_id: str) -> list[DecisionRecord]:
        rows = self._get_conn().execute(
            "SELECT * FROM job_decisions WHERE run_id = ? ORDER BY job_id", (run_id,)
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_snapshot(self, h: str) -> dict[str, Any] | None:
        """Decoded job payload, or ``None`` when unknown or its text was purged."""
        row = self._get_conn().execute(
            "SELECT text_blob, compression FROM job_snapshots WHERE snapshot_hash = ?", (h,)
        ).fetchone()
        if row is None or row["text_blob"] is None:
            return None
        raw = zlib.decompress(row["text_blob"]) if row["compression"] == "zlib" else bytes(row["text_blob"])
        return dict(json.loads(raw.decode("utf-8")))

    def get_strategy(self, strategy_version: str) -> dict[str, Any] | None:
        row = self._get_conn().execute(
            "SELECT manifest_json FROM strategy_versions WHERE strategy_version = ?", (strategy_version,)
        ).fetchone()
        return dict(json.loads(row["manifest_json"])) if row else None

    # -- retention ----------------------------------------------------------------------------

    def purge_text(self, older_than_days: int) -> tuple[int, int]:
        """Remove job TEXT not referenced for N days → ``(snapshots_purged, decisions_scrubbed)``.

        Snapshot text is nulled and the quoted evidence spans (``span.text``) of old decisions are
        blanked (offsets/extractor ids stay). Decision rows and snapshot metadata are kept, so the
        audit trail survives without the personal text. ``secure_delete`` + a WAL truncate make the
        removal real on disk."""
        if older_than_days <= 0:
            return 0, 0
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        conn = self._get_conn()
        conn.execute("PRAGMA secure_delete=ON")
        scrubbed = 0
        with conn:
            purged = conn.execute(
                "UPDATE job_snapshots SET text_blob = NULL, text_purged_at = ? "
                "WHERE text_blob IS NOT NULL AND last_seen_at < ?", (_now(), cutoff),
            ).rowcount
            rows = conn.execute(
                "SELECT decision_id, evidence_json FROM job_decisions "
                "WHERE evidence_text_purged = 0 AND decided_at < ?", (cutoff,),
            ).fetchall()
            for row in rows:
                items = json.loads(row["evidence_json"])
                for item in items:
                    if item.get("span"):
                        item["span"]["text"] = ""
                conn.execute(
                    "UPDATE job_decisions SET evidence_json = ?, evidence_text_purged = 1 WHERE decision_id = ?",
                    (json.dumps(items, ensure_ascii=False), row["decision_id"]),
                )
                scrubbed += 1
        if purged or scrubbed:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return purged, scrubbed

    def purge_records(self, older_than_days: int) -> tuple[int, int]:
        """Delete decisions older than N days and snapshots no decision references any more."""
        if older_than_days <= 0:
            return 0, 0
        cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        conn = self._get_conn()
        conn.execute("PRAGMA secure_delete=ON")
        with conn:
            d = conn.execute("DELETE FROM job_decisions WHERE decided_at < ?", (cutoff,)).rowcount
            s = conn.execute(
                "DELETE FROM job_snapshots WHERE NOT EXISTS "
                "(SELECT 1 FROM job_decisions d WHERE d.snapshot_hash = job_snapshots.snapshot_hash)"
            ).rowcount
        return d, s
