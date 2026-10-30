"""SQLite storage layer for jobs and sources."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.models.job import Job
from src.models.source import Source
from src.storage.migrations import (
    JOB_PROVENANCE_DDL,
    JOBS_COLUMNS,
    JOBS_DATA_MIGRATIONS,
    apply_data_migrations,
    backup_before_migrating,
    ensure_columns,
    pending_data_migrations,
)


# ---------------------------------------------------------------------------
# FTS helpers
# ---------------------------------------------------------------------------

_FTS_TOKENIZER = "unicode61 remove_diacritics 2"
# Matches the tokenizer spec embedded in the CREATE statement used by init_db.
# If the stored schema doesn't contain this string the table is migrated.

_FTS_SPECIAL_RE = re.compile(r'["\'^{}()\[\]|!,:.@*?]')


def _build_fts_prefix_query(query: str) -> str | None:
    """Convert a user search string into a safe FTS5 prefix query.

    Each whitespace-separated token becomes ``"token"*`` (quoted prefix).
    Tokens shorter than 1 char are dropped.  Returns None when there is
    nothing left after sanitisation (caller should skip the FTS query).

    Examples::

        "ingé"           → '"ingé"*'
        "python django"  → '"python"* "django"*'
        "ingénieur QA"   → '"ingénieur"* "qa"*'
    """
    # Strip characters that break FTS5 query syntax
    cleaned = _FTS_SPECIAL_RE.sub(" ", query)
    tokens = [t.lower() for t in cleaned.split() if t]
    if not tokens:
        return None
    return " ".join(f'"{t}"*' for t in tokens)


class Storage:
    """Thin wrapper around SQLite with indexes for common queries."""

    # Schema/migration setup only needs to happen once per database file per
    # process. Each API request builds a fresh Storage instance (see
    # src/api/routes/jobs.py::_get_storages), so without this guard every
    # request re-ran init_db()'s writes (content_hash backfill, ALTER TABLE
    # checks, migrations) inside one long implicit transaction — contending
    # with the pipeline's own writes and surfacing as "database is locked".
    _initialized_paths: set[str] = set()

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- connection helpers --------------------------------------------------

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            # check_same_thread=False: matches SchedulerStorage's connection
            # (src/scheduler/storage.py) — every current call site constructs
            # a fresh Storage within the thread that uses it, so this is
            # defense-in-depth against a future caller sharing one instance
            # across threads (which would otherwise raise
            # sqlite3.ProgrammingError), not a fix for a live bug.
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            # See src/linkedin/staging.py for why this is needed: several
            # modules open their own connection to the same jobs.db file.
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- schema --------------------------------------------------------------

    def init_db(self) -> None:
        resolved = str(self._db_path.resolve())
        if resolved in Storage._initialized_paths:
            return
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                company TEXT,
                domain TEXT,
                careers_url TEXT,
                ats TEXT,
                board_identifier TEXT,
                config TEXT NOT NULL DEFAULT '{}',
                active INTEGER NOT NULL DEFAULT 1,
                last_success_at TEXT,
                last_error_at TEXT
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_job_id TEXT,
                canonical_url TEXT,
                apply_url TEXT,
                company TEXT,
                title TEXT,
                description TEXT,
                location TEXT,
                countries TEXT NOT NULL DEFAULT '[]',
                department TEXT,
                remote INTEGER NOT NULL DEFAULT 0,
                work_mode TEXT,
                employment_type TEXT,
                salary_min REAL,
                salary_max REAL,
                salary_currency TEXT NOT NULL DEFAULT 'EUR',
                published_at TEXT,
                updated_at TEXT,
                discovered_at TEXT NOT NULL,
                skills TEXT NOT NULL DEFAULT '[]',
                raw_payload TEXT NOT NULL DEFAULT '{}',
                content_hash TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT,
                last_seen_at TEXT,
                status TEXT NOT NULL DEFAULT 'active'
            );

            -- Indexes for common queries
            CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
            CREATE INDEX IF NOT EXISTS idx_jobs_countries ON jobs(countries);
            CREATE INDEX IF NOT EXISTS idx_jobs_remote ON jobs(remote);
            CREATE INDEX IF NOT EXISTS idx_jobs_published_at ON jobs(published_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
            CREATE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source, source_job_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_source_content_hash ON jobs(source, content_hash);

        """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS job_embeddings (
                job_id      TEXT    NOT NULL,
                model_name  TEXT    NOT NULL,
                content_hash TEXT   NOT NULL,
                vector      TEXT    NOT NULL,
                computed_at TEXT    NOT NULL,
                PRIMARY KEY (job_id, model_name)
            );
            CREATE INDEX IF NOT EXISTS idx_job_embeddings_model
                ON job_embeddings(model_name);
        """)

        # Migration: existing tables (pre-constraint) may have NULL content_hash rows
        # from direct SQL inserts or imports that bypassed the normalizer.
        conn.execute("UPDATE jobs SET content_hash = '' WHERE content_hash IS NULL")

        # Columns added to jobs after its first release, then the one-shot data
        # migrations recorded in db_migrations (src/storage/migrations.py).
        ensure_columns(conn, "jobs", JOBS_COLUMNS)
        conn.executescript(JOB_PROVENANCE_DDL)
        # A large database is copied before its data migrations run (issue #86); without
        # room for the copy they wait for a later start.
        if not pending_data_migrations(conn, JOBS_DATA_MIGRATIONS) or backup_before_migrating(conn, self._db_path):
            apply_data_migrations(conn, JOBS_DATA_MIGRATIONS)

        # FTS5 table with accent-stripping tokenizer.
        # Check whether the existing job_fts was created with the current tokenizer;
        # if not (old schema or first run), drop and rebuild it.
        fts_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='job_fts'"
        ).fetchone()
        fts_sql = (fts_row[0] or "") if fts_row else ""
        if _FTS_TOKENIZER not in fts_sql:
            conn.execute("DROP TABLE IF EXISTS job_fts")
            conn.execute(
                f"""CREATE VIRTUAL TABLE job_fts USING fts5(
                    id UNINDEXED, title, description, company, skills,
                    tokenize="{_FTS_TOKENIZER}"
                )"""
            )
            # Rebuild FTS index from the jobs table.
            conn.execute(
                """INSERT INTO job_fts (id, title, description, company, skills)
                   SELECT id,
                          COALESCE(title, ''),
                          COALESCE(description, ''),
                          COALESCE(company, ''),
                          COALESCE(skills, '')
                   FROM jobs"""
            )
        conn.commit()
        Storage._initialized_paths.add(resolved)

    # -- source CRUD ---------------------------------------------------------

    def upsert_source(self, src: Source) -> None:
        conn = self._get_conn()
        config_str = json.dumps(src.config) if isinstance(src.config, dict) else str(src.config)
        conn.execute(
            """INSERT OR REPLACE INTO sources (id, name, type, company, domain, careers_url, ats, board_identifier, config, active, last_success_at, last_error_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                src.id, src.name, src.type, src.company, src.domain,
                src.careers_url, src.ats, src.board_identifier,
                config_str, int(src.active),
                src.last_success_at.isoformat() if src.last_success_at else None,
                src.last_error_at.isoformat() if src.last_error_at else None,
            ),
        )
        conn.commit()

    def get_active_sources(self) -> list[Source]:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM sources WHERE active = 1").fetchall()
        return [Source.from_dict(dict(r)) for r in rows]

    # -- job CRUD ------------------------------------------------------------

    def upsert_job(self, job: Job) -> None:
        conn = self._get_conn()
        # Use ON CONFLICT DO UPDATE (upsert) to avoid replacing the whole row,
        # which would wipe the user-set application_status field.
        conn.execute(
            """INSERT INTO jobs (id, source, source_job_id, canonical_url, apply_url, company, title, description, location, countries, department, remote, work_mode, employment_type, salary_min, salary_max, salary_currency, published_at, updated_at, discovered_at, skills, raw_payload, content_hash, first_seen_at, last_seen_at, status, valid_through)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   source=excluded.source, source_job_id=excluded.source_job_id,
                   canonical_url=excluded.canonical_url, apply_url=excluded.apply_url,
                   company=excluded.company, title=excluded.title, description=excluded.description,
                   location=excluded.location, countries=excluded.countries,
                   department=excluded.department, remote=excluded.remote,
                   work_mode=excluded.work_mode, employment_type=excluded.employment_type,
                   salary_min=excluded.salary_min, salary_max=excluded.salary_max,
                   salary_currency=excluded.salary_currency,
                   published_at=excluded.published_at, updated_at=excluded.updated_at,
                   discovered_at=excluded.discovered_at, skills=excluded.skills,
                   raw_payload=excluded.raw_payload, content_hash=excluded.content_hash,
                   first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,
                   status=CASE WHEN jobs.expiry_reason = 'user_report' THEN jobs.status ELSE excluded.status END,
                   expired_at=CASE WHEN jobs.expiry_reason = 'user_report' THEN jobs.expired_at ELSE NULL END,
                   expiry_reason=CASE WHEN jobs.expiry_reason = 'user_report' THEN jobs.expiry_reason ELSE NULL END,
                   missed_runs=0, valid_through=excluded.valid_through""",
            (
                job.id, job.source, job.source_job_id, job.canonical_url, job.apply_url,
                job.company, job.title, job.description, job.location,
                json.dumps(job.countries), job.department, int(job.remote), job.work_mode,
                job.employment_type,
                job.salary_min, job.salary_max, job.salary_currency,
                job.published_at.isoformat() if job.published_at else None,
                job.updated_at.isoformat() if job.updated_at else None,
                job.discovered_at.isoformat(),
                json.dumps(job.skills),
                json.dumps(job.raw_payload),
                job.content_hash or "",
                job.first_seen_at.isoformat() if job.first_seen_at else None,
                job.last_seen_at.isoformat() if job.last_seen_at else None,
                job.status,
                job.valid_through.isoformat() if job.valid_through else None,
            ),
        )
        # Keep job_fts in sync manually (delete-then-insert handles upserts cleanly)
        conn.execute("DELETE FROM job_fts WHERE id = ?", (job.id,))
        conn.execute(
            "INSERT INTO job_fts (id, title, description, company, skills) VALUES (?, ?, ?, ?, ?)",
            (
                job.id,
                job.title or "",
                job.description or "",
                job.company or "",
                " ".join(job.skills) if job.skills else "",
            ),
        )
        conn.commit()

    # Rows processed per write transaction inside upsert_jobs_batch(). A pipeline
    # run can upsert thousands of jobs; committing only once at the very end
    # holds SQLite's single writer lock for the whole loop (seconds, for a
    # large run), starving concurrent scanner/API writers past their
    # busy_timeout and surfacing as "database is locked". Committing every
    # _UPSERT_CHUNK_SIZE rows bounds how long the lock is held at a stretch
    # while still committing far less often than one-commit-per-job.
    _UPSERT_CHUNK_SIZE = 200

    def record_provenance(self, jobs: list[Job], seen_at: datetime) -> int:
        """Record where each stored offer was seen (issue #86): one ``job_provenance`` row
        per (offer, source in ``Job.sources``, its own source included), its first and
        latest sighting. The offer's own source keeps its id and URL; a source merged into
        it by deduplication has neither. Returns the rows written."""
        conn = self._get_conn()
        when = seen_at.isoformat()
        rows = []
        for job in jobs:
            for source in dict.fromkeys([job.source, *job.sources]):
                if not source:
                    continue
                own = source == job.source
                rows.append((job.id, source, job.source_job_id if own else None,
                             job.canonical_url if own else None, when, when))
        conn.executemany(
            """INSERT INTO job_provenance (job_id, source, source_job_id, url, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(job_id, source) DO UPDATE SET
                   last_seen_at = excluded.last_seen_at,
                   source_job_id = COALESCE(excluded.source_job_id, job_provenance.source_job_id),
                   url = COALESCE(excluded.url, job_provenance.url)""",
            rows,
        )
        conn.commit()
        return len(rows)

    def get_provenance(self, job_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Each offer's sightings, its own source first then by first sighting."""
        if not job_ids:
            return {}
        conn = self._get_conn()
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(job_ids), 500):
            chunk = job_ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"""SELECT p.job_id, p.source, p.url, p.first_seen_at, p.last_seen_at, j.source AS own
                    FROM job_provenance p LEFT JOIN jobs j ON j.id = p.job_id
                    WHERE p.job_id IN ({marks})
                    ORDER BY p.job_id, (p.source = j.source) DESC, p.first_seen_at, p.source""",
                chunk,
            ).fetchall()
            for r in rows:
                out.setdefault(r["job_id"], []).append({
                    "source": r["source"], "url": r["url"],
                    "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"],
                })
        return out

    def apply_expiry(
        self, seen_ids: set[str], complete_sources: set[str], now: datetime, max_missed: int,
    ) -> dict[str, int]:
        """Mark offers expired after a run (issue #86), and active again when seen.

        ``seen_ids``: every offer this run collected (after deduplication, before the
        filters). ``complete_sources``: the sources fully collected this run (completed, not
        capped); only they count a miss. An active offer not seen, listed by one of them,
        gets one more missed run. It expires (status ``expired``, with ``expired_at`` and
        ``expiry_reason``) once its end date (``valid_through``) has passed, or after
        ``max_missed`` missed runs in a row. An offer expired for either reason and seen
        again is active again; an offer the user reported stays expired.
        """
        conn = self._get_conn()
        when = now.isoformat()
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS expiry_seen (id TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM expiry_seen")
        conn.executemany("INSERT OR IGNORE INTO expiry_seen (id) VALUES (?)", [(i,) for i in seen_ids])
        seen = "SELECT id FROM expiry_seen"
        out = {"reactivated": 0, "missed": 0, "expired_valid_through": 0, "expired_not_seen": 0}
        conn.execute(f"UPDATE jobs SET missed_runs = 0 WHERE missed_runs != 0 AND id IN ({seen})")
        out["reactivated"] = conn.execute(
            f"""UPDATE jobs SET status = 'active', expired_at = NULL, expiry_reason = NULL
                WHERE status = 'expired' AND id IN ({seen})
                  AND (expiry_reason = 'not_seen'
                       OR (expiry_reason = 'valid_through' AND (valid_through IS NULL OR valid_through >= ?)))""",
            (when,),
        ).rowcount
        if complete_sources:
            marks = ",".join("?" * len(complete_sources))
            out["missed"] = conn.execute(
                f"""UPDATE jobs SET missed_runs = missed_runs + 1
                    WHERE status = 'active' AND id NOT IN ({seen})
                      AND id IN (SELECT job_id FROM job_provenance WHERE source IN ({marks}))""",
                sorted(complete_sources),
            ).rowcount
        out["expired_valid_through"] = conn.execute(
            """UPDATE jobs SET status = 'expired', expired_at = ?, expiry_reason = 'valid_through'
               WHERE status = 'active' AND valid_through IS NOT NULL AND valid_through < ?""",
            (when, when),
        ).rowcount
        out["expired_not_seen"] = conn.execute(
            """UPDATE jobs SET status = 'expired', expired_at = ?, expiry_reason = 'not_seen'
               WHERE status = 'active' AND missed_runs >= ?""",
            (when, max_missed),
        ).rowcount
        conn.execute("DELETE FROM expiry_seen")
        conn.commit()
        return out

    def set_reported_expired(self, job_id: str, expired: bool, now: datetime) -> bool:
        """The user says an offer is no longer available (``expired`` True), or available
        after all (False). Returns False when there is no such offer."""
        conn = self._get_conn()
        if expired:
            cur = conn.execute(
                "UPDATE jobs SET status = 'expired', expired_at = ?, expiry_reason = 'user_report' WHERE id = ?",
                (now.isoformat(), job_id),
            )
        else:
            cur = conn.execute(
                "UPDATE jobs SET status = 'active', expired_at = NULL, expiry_reason = NULL, missed_runs = 0 "
                "WHERE id = ?",
                (job_id,),
            )
        conn.commit()
        return cur.rowcount > 0

    def upsert_jobs_batch(
        self, jobs: list[Job], detect_field_changes: bool = False
    ) -> dict[str, Any]:
        """Upsert a batch of jobs, committing periodically in chunks.

        Compared to calling upsert_job() in a loop this is substantially
        faster because:
        - Chunked transactions replace 1-per-job commits and WAL flushes.
        - FTS is only updated for jobs whose text content actually changed,
          avoiding the expensive full-table scan on job_fts for every job.

        Returns a dict with counts: {"inserted": N, "updated": N, "unchanged": N,
        "by_source": {source: {"inserted": N, "updated": N, "unchanged": N}}}.
        ``by_source`` is additive-only (Phase NEXT / I11): existing callers
        that only read the three top-level counts are unaffected; it exists
        so a caller can distinguish new-vs-already-known jobs PER SOURCE
        without a second query, feeding source_checkpoints observability.

        ``detect_field_changes`` (EXPERIMENTAL / I19, default False — zero
        cost and byte-identical return shape when omitted) additionally
        bulk-fetches each already-known job's previous full row (one extra
        query, only for ids already present in this batch) and runs
        ``src.experimental.job_change_detector.detect_change`` against it,
        returning ``"field_changes": {job_id: [field, ...]}`` for every job
        whose meaningful fields actually changed. This is purely observational
        bookkeeping about what changed — it never alters which fields get
        written (the upsert below is unchanged either way) and never affects
        ranking/eligibility.
        """
        if not jobs:
            empty_result: dict[str, Any] = {
                "inserted": 0, "updated": 0, "unchanged": 0, "by_source": {}
            }
            if detect_field_changes:
                empty_result["field_changes"] = {}
            return empty_result

        conn = self._get_conn()
        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)

        # Bulk-fetch current content hashes and first_seen_at for all job IDs.
        job_ids = [job.id for job in jobs]
        placeholders = ",".join("?" * len(job_ids))
        existing = {
            row[0]: (row[1], row[2])  # id -> (content_hash, first_seen_at)
            for row in conn.execute(
                f"SELECT id, content_hash, first_seen_at FROM jobs WHERE id IN ({placeholders})",
                job_ids,
            ).fetchall()
        }

        old_jobs_by_id: dict[str, Job] = {}
        field_changes: dict[str, list[str]] = {}
        if detect_field_changes and existing:
            old_jobs_by_id = self.get_jobs_by_ids(list(existing.keys()))

        # Cross-run same-source content-hash dedup: detect location variants that
        # survived intra_source_deduplicate because they arrived in a prior scan
        # run (and therefore have a different stable_id in the DB).
        existing_source_hashes: set[tuple[str, str]] = set()
        hashes_by_source: dict[str, list[str]] = {}
        for job in jobs:
            if job.content_hash and job.id not in existing:
                hashes_by_source.setdefault(job.source, []).append(job.content_hash)
        for src, hashes in hashes_by_source.items():
            h_ph = ",".join("?" * len(hashes))
            for row in conn.execute(
                f"SELECT content_hash FROM jobs WHERE source=? AND content_hash IN ({h_ph})",
                [src, *hashes],
            ).fetchall():
                existing_source_hashes.add((src, row[0]))

        inserted = updated = unchanged = 0
        by_source: dict[str, dict[str, int]] = defaultdict(
            lambda: {"inserted": 0, "updated": 0, "unchanged": 0}
        )
        fts_deletes: list[str] = []
        fts_inserts: list[tuple] = []

        def _flush_fts_and_commit() -> None:
            """Apply accumulated FTS changes and commit, releasing the writer lock."""
            if fts_deletes:
                del_placeholders = ",".join("?" * len(fts_deletes))
                conn.execute(
                    f"DELETE FROM job_fts WHERE id IN ({del_placeholders})", fts_deletes
                )
                fts_deletes.clear()
            if fts_inserts:
                conn.executemany(
                    "INSERT INTO job_fts (id, title, description, company, skills) VALUES (?, ?, ?, ?, ?)",
                    fts_inserts,
                )
                fts_inserts.clear()
            conn.commit()

        for idx, job in enumerate(jobs):
            current = existing.get(job.id)

            # Skip cross-run location variants: same source, same content, different stable_id.
            if current is None and job.content_hash and (job.source, job.content_hash) in existing_source_hashes:
                unchanged += 1
                by_source[job.source]["unchanged"] += 1
                continue

            if detect_field_changes and current is not None:
                old_job = old_jobs_by_id.get(job.id)
                if old_job is not None:
                    from src.pipeline.job_change_detector import detect_change

                    change_result = detect_change(old_job, job)
                    if change_result.status == "changed":
                        field_changes[job.id] = [c.field for c in change_result.changed_fields]

            new_hash = job.content_hash or ""
            first_seen = (
                current[1]  # preserve original first_seen_at
                if current is not None
                else (job.first_seen_at.isoformat() if job.first_seen_at else now.isoformat())
            )

            conn.execute(
                """INSERT INTO jobs (
                    id, source, source_job_id, canonical_url, apply_url, company, title,
                    description, location, countries, department, remote, work_mode,
                    employment_type,
                    salary_min, salary_max, salary_currency, published_at, updated_at,
                    discovered_at, skills, raw_payload, content_hash,
                    first_seen_at, last_seen_at, status, valid_through
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source=excluded.source, source_job_id=excluded.source_job_id,
                    canonical_url=excluded.canonical_url, apply_url=excluded.apply_url,
                    company=excluded.company, title=excluded.title, description=excluded.description,
                    location=excluded.location, countries=excluded.countries,
                    department=excluded.department, remote=excluded.remote,
                    work_mode=excluded.work_mode, employment_type=excluded.employment_type,
                    salary_min=excluded.salary_min, salary_max=excluded.salary_max,
                    salary_currency=excluded.salary_currency,
                    published_at=excluded.published_at, updated_at=excluded.updated_at,
                    discovered_at=excluded.discovered_at, skills=excluded.skills,
                    raw_payload=excluded.raw_payload, content_hash=excluded.content_hash,
                    first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,
                    status=CASE WHEN jobs.expiry_reason = 'user_report' THEN jobs.status ELSE excluded.status END,
                    expired_at=CASE WHEN jobs.expiry_reason = 'user_report' THEN jobs.expired_at ELSE NULL END,
                    expiry_reason=CASE WHEN jobs.expiry_reason = 'user_report' THEN jobs.expiry_reason ELSE NULL END,
                    missed_runs=0, valid_through=excluded.valid_through""",
                (
                    job.id, job.source, job.source_job_id, job.canonical_url, job.apply_url,
                    job.company, job.title, job.description, job.location,
                    json.dumps(job.countries), job.department, int(job.remote), job.work_mode,
                    job.employment_type,
                    job.salary_min, job.salary_max, job.salary_currency,
                    job.published_at.isoformat() if job.published_at else None,
                    job.updated_at.isoformat() if job.updated_at else None,
                    job.discovered_at.isoformat(),
                    json.dumps(job.skills),
                    json.dumps(job.raw_payload),
                    new_hash,
                    first_seen,
                    now.isoformat(),
                    job.status,
                    job.valid_through.isoformat() if job.valid_through else None,
                ),
            )

            if current is None:
                # New job — add to FTS.
                inserted += 1
                by_source[job.source]["inserted"] += 1
                # Update within-batch guard so subsequent same-batch same-hash jobs
                # are also caught (intra_source_deduplicate should prevent this, but
                # guard defensively in case batches from different sources merge).
                if new_hash:
                    existing_source_hashes.add((job.source, new_hash))
                fts_inserts.append((
                    job.id,
                    job.title or "",
                    job.description or "",
                    job.company or "",
                    " ".join(job.skills) if job.skills else "",
                ))
            elif current[0] != new_hash:
                # Content changed — update FTS.
                updated += 1
                by_source[job.source]["updated"] += 1
                fts_deletes.append(job.id)
                fts_inserts.append((
                    job.id,
                    job.title or "",
                    job.description or "",
                    job.company or "",
                    " ".join(job.skills) if job.skills else "",
                ))
            else:
                # Unchanged — skip FTS entirely.
                unchanged += 1
                by_source[job.source]["unchanged"] += 1

            if (idx + 1) % self._UPSERT_CHUNK_SIZE == 0:
                _flush_fts_and_commit()

        # Final partial chunk (or the whole batch, if smaller than one chunk).
        _flush_fts_and_commit()
        result: dict[str, Any] = {
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "by_source": dict(by_source),
        }
        if detect_field_changes:
            result["field_changes"] = field_changes
        return result

    # -- embedding cache --------------------------------------------------------

    def get_cached_embeddings(
        self,
        job_ids: list[str],
        model_name: str,
        content_hashes: dict[str, str],
    ) -> dict[str, list[float]]:
        """Return cached embedding vectors for jobs whose content hasn't changed.

        A cached vector is only returned when the stored content_hash matches
        the current content_hash for that job.  Any mismatch (content changed
        or job is new) is excluded so the caller knows it must re-encode.

        Args:
            job_ids:       Jobs to look up.
            model_name:    The embedding model identifier (cache is model-specific).
            content_hashes: Mapping of job_id → current content_hash.

        Returns:
            Mapping of job_id → vector (list[float]) for cache hits only.
        """
        if not job_ids:
            return {}
        conn = self._get_conn()
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"""SELECT job_id, content_hash, vector
                FROM job_embeddings
                WHERE job_id IN ({placeholders}) AND model_name = ?""",
            (*job_ids, model_name),
        ).fetchall()

        result: dict[str, list[float]] = {}
        for row in rows:
            jid, stored_hash, vector_json = row[0], row[1], row[2]
            if content_hashes.get(jid) == stored_hash:
                result[jid] = json.loads(vector_json)
        return result

    def store_embeddings(
        self,
        records: list[tuple[str, str, str, list[float]]],
        model_name: str,
    ) -> None:
        """Persist computed embedding vectors in a single transaction.

        Args:
            records:    List of (job_id, content_hash, computed_at_iso, vector).
            model_name: The model that produced these vectors.
        """
        if not records:
            return
        conn = self._get_conn()
        conn.executemany(
            """INSERT OR REPLACE INTO job_embeddings
               (job_id, model_name, content_hash, vector, computed_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (job_id, model_name, content_hash, json.dumps(vector), computed_at)
                for job_id, content_hash, computed_at, vector in records
            ],
        )
        conn.commit()

    def get_jobs(self, limit: int = 100) -> list[Job]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY discovered_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [Job.from_dict(dict(r)) for r in rows]

    def get_job_by_id(self, job_id: str) -> Job | None:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_dict(dict(row)) if row is not None else None

    def get_jobs_by_ids(self, job_ids: list[str]) -> dict[str, Job]:
        """Batch-fetch jobs by id, returning a dict keyed by id (missing ids omitted).

        Avoids N+1 `SELECT ... WHERE id = ?` round-trips when a caller already
        holds a list of ids (e.g. run/score rows) and needs the full job rows.
        """
        if not job_ids:
            return {}
        conn = self._get_conn()
        result: dict[str, Job] = {}
        # Chunk to stay well under SQLite's default 999-variable limit.
        for i in range(0, len(job_ids), 500):
            chunk = job_ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE id IN ({placeholders})", chunk
            ).fetchall()
            for row in rows:
                job = Job.from_dict(dict(row))
                result[job.id] = job
        return result

    def get_first_seen_at(self, job_ids: list[str]) -> dict[str, datetime]:
        """Stored ``first_seen_at`` for the already-known ids (unknown ids and unusable values omitted).

        Read before the hard filter so an undated listing re-collected today is aged from its first
        sighting, not from today (ADR 0012). Naive stored values are read as UTC.
        """
        result: dict[str, datetime] = {}
        if not job_ids:
            return result
        conn = self._get_conn()
        for i in range(0, len(job_ids), 500):
            chunk = job_ids[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            for job_id, raw in conn.execute(
                f"SELECT id, first_seen_at FROM jobs WHERE id IN ({placeholders}) AND first_seen_at IS NOT NULL",
                chunk,
            ).fetchall():
                try:
                    dt = datetime.fromisoformat(raw)
                except (TypeError, ValueError):
                    continue
                result[job_id] = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        return result

    def get_jobs_by_source(self, source_id: str) -> list[Job]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE source = ? ORDER BY discovered_at DESC", (source_id,)
        ).fetchall()
        return [Job.from_dict(dict(r)) for r in rows]

    def get_active_jobs_by_source(self, source: str, exclude_ids: set[str] | None = None) -> list[Job]:
        """Return all active jobs from ``source``, optionally excluding specific stable IDs.

        Used by the pipeline runner to re-inject previously stored jobs so they
        are re-scored in every run and remain visible in the API.
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND status = 'active' ORDER BY discovered_at DESC",
            (source,),
        ).fetchall()
        jobs = [Job.from_dict(dict(r)) for r in rows]
        # Normalize ISO country codes (e.g. "FR" → "France") so that jobs
        # stored by an older pipeline run are corrected on re-injection.
        try:
            from src.pipeline.normalize import normalize_country_code as _ncc
            for j in jobs:
                if j.countries:
                    j.countries = [_ncc(c) for c in j.countries]
        except ImportError:
            pass
        if exclude_ids:
            jobs = [j for j in jobs if j.id not in exclude_ids]
        return jobs

    def count_jobs(self) -> int:
        conn = self._get_conn()
        row = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return int(row[0]) if row else 0

    def update_application_status(self, job_id: str, status: str | None) -> None:
        """Set the user-facing application status for a job (persists across pipeline reruns)."""
        conn = self._get_conn()
        conn.execute("UPDATE jobs SET application_status = ? WHERE id = ?", (status, job_id))
        conn.commit()

    # -- full-text search ----------------------------------------------------

    def search_jobs(self, query: str, limit: int = 50) -> list[Job]:
        """Prefix full-text search across title, description, company, and skills.

        The query is tokenised and each token becomes a prefix search
        (``"tok"*``) so partial terms like "pyth" match "Python" and
        "ingé" matches "Ingénieur".  The FTS5 tokenizer strips diacritics,
        making search accent-tolerant in both directions.
        """
        fts_query = _build_fts_prefix_query(query)
        if not fts_query:
            return []
        conn = self._get_conn()
        try:
            # bm25 column weights: id(UNINDEXED)=0, title=10, description=1,
            # company=5, skills=3.  Title matches rank far above description
            # mentions so that a job titled "Ingénieur" beats a "Chef de projet"
            # whose 2000-word description happens to mention the word once.
            rows = conn.execute(
                """SELECT j.* FROM jobs j
                   JOIN job_fts ON j.id = job_fts.id
                   WHERE job_fts MATCH ?
                   ORDER BY bm25(job_fts, 0.0, 10.0, 1.0, 5.0, 3.0)
                   LIMIT ?""",
                (fts_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Malformed query edge case — return empty rather than 500.
            return []
        return [Job.from_dict(dict(r)) for r in rows]