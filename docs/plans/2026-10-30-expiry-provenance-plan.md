# Expiry and cross-source provenance — plan (P1.4, F6.6)

Date: 2026-10-30. Completion plan P1.4 (plan F6.6, audit D5). Contract change, so it has its
own plan (CLAUDE.md §11.4). Migration registry: #84.

## Problem

- An offer that closed stays "active" for ever: nothing reads an end date, and an offer no
  source lists any more is never marked. The Jobs list fills with dead offers.
- One offer listed by several sources (France Travail API and website, EURES) is merged
  inside a run (`src/pipeline/deduplicate.py` keeps the first and lists the others in
  `Job.sources`), but `sources` is never stored: the user cannot see where else the offer
  appears, and across runs nothing records it.
- Pipeline-history retention exists (`config/retention.yaml`), but the `jobs` table only
  grows (the owner's database is several GB) and nothing in the app lets the user reclaim it.

## Acceptance (from the completion plan)

- A duplicate across FT web, the FT API and EURES appears once, with its provenances
  ("seen on …") in the UI.
- An offer past its end date, or no longer listed by a source that was fully collected N
  times in a row, or reported by the user, is marked expired with its reason. It is hidden
  from the default lists and can still be opened.
- Settings › Data shows the database size and offers to remove expired offers older than a
  chosen age (with confirmation), then compacts the file.
- The migration is rehearsed on copies of the large databases, with a backup first. Time and
  size are measured.

## Design

### Data (additive only; `src/storage/migrations.py`)

- New table `job_provenance(job_id, source, source_job_id, url, first_seen_at, last_seen_at,
  PRIMARY KEY (job_id, source))`.
- New `jobs` columns:
  - `valid_through TEXT`: the source's end date, when given;
  - `missed_runs INTEGER NOT NULL DEFAULT 0`;
  - `expired_at TEXT`;
  - `expiry_reason TEXT` (`valid_through` | `not_seen` | `user_report`).
- `jobs.status` takes `expired` (today only `active` is written).
- A one-shot data migration backfills `job_provenance` from `jobs.source` (one row per job).
  It is measured on the copies.
- Before any data migration on a database over 200 MB, `init_db` copies it to
  `data/backups/jobs-<date>.db` (SQLite backup API), keeping the last 2, and logs the
  duration. Skipped if there is not enough disk space; then the migration does not run
  either, and the app runs on the old schema with a warning.

### Provenance (slice 1)

- The storage stage writes one `job_provenance` row per (kept job, source in `Job.sources`),
  updating `last_seen_at`.
- `JobOut` gains `provenance: [{source, url, first_seen_at, last_seen_at}]`. Job detail
  shows "Seen on France Travail, EURES" with each source's link.
- The dedup keeper order becomes deterministic by source kind (official API, then public
  page, then unofficial, then config order). The same offer then keeps the same id from one
  run to the next. Only the choice of keeper changes; what is merged does not.

### Expiry (slice 2)

- `valid_through`: read from JobPosting JSON-LD where a collector already parses it
  (HelloWork; France Travail web if its page carries it). The date is parsed leniently, and
  an unparseable value is ignored.
- After a run, for each source whose result is `completed` and not `request_cap_reached`:
  - each active job with a provenance from that source that was not seen gets
    `missed_runs += 1`;
  - a job that was seen is reset to 0.

  Partial, failed, capped or skipped sources never count.
- A job is expired when:
  - `valid_through` is before now; or
  - every source it came from has missed it at least N = 3 times (config
    `retention.yaml: expire_after_missed_runs`); or
  - the user reports it with `POST /api/jobs/{id}/expired`.

  A job seen again becomes active (unless the user reported it).
- **Risk:** a profile change also makes sources stop returning offers. The reason text says
  "no longer listed in recent searches", not "closed". Expired offers are hidden, never
  deleted, by this slice.
- UI:
  - Jobs list: an "Include expired" filter, off by default;
  - Job detail: an expired banner with its reason, and "This offer is no longer available"
    for the user report.
- Scoring and the evaluation exports ignore `status`, so no strategy-fingerprint change.

### Settings › Data (slice 3)

- `GET /api/settings/data`: database size, offers by status, expired older than 30/90/180 days.
- `POST /api/settings/data/cleanup {older_than_days}` deletes expired offers older than that
  (and their FTS and embedding rows), then `VACUUM`. It runs in the background with progress
  and refuses to start while a run is in progress. The UI asks for confirmation with the
  count.
- Reuses the Settings page cards (P1.2).

## Not in scope

ATS auto-follow (F6.7); merging duplicates already stored under different ids in the past
(they age out through expiry).

## Tests

- Unit tests:
  - provenance upsert;
  - keeper order;
  - miss counting (completed vs partial/capped);
  - expiry rules;
  - reactivation;
  - user report;
  - backup trigger and space check;
  - cleanup.
- API and frontend tests for each UI.
- Rehearsal script run on copies of both candidate databases. It reports time, size before
  and after, and row counts.

## Slices and PRs

1. Provenance + deterministic keeper + migration backup (backend, then UI).
2. Expiry (backend, then UI).
3. Settings › Data cleanup.
