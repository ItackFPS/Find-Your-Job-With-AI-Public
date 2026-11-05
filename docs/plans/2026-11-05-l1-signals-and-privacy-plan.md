# L1 — Signals and privacy: implementation plan

Status: in progress (2026-11-05). Stage L1 of the approved learning architecture v5
(`docs/plans/2026-11-05-learning-architecture-decisions.md` §6), under ADRs 0014 (planes and
invariants), 0015 (privacy, retention, erasure), 0016 (signal capture and delivery) and 0017 (Hide).
Owner mandate of 2026-11-05: implement the architecture without stopping between small tasks.

Exit criteria (v5 §6): the ADR 0015 and ADR 0016 tests pass; the erase verification test passes on a
temporary database.

## Slices (one PR each, in order)

| # | Slice | Contract / schema change | Main files | Tests |
|---|---|---|---|---|
| L1.1 | Table classification registry, INV-2 socket guard, log lint | none (new module) | `src/storage/data_classes.py` | `tests/test_data_classification.py`, `tests/test_personal_data_stays_local.py` |
| L1.2 | `app_state` (data epoch, erasure generation), `X-Erasure-Generation` header, session endpoint; `feedback_events` delivery columns (`client_event_id`, `generation`, `client_instance_id`, `client_seq`, `client_created_at`, `client_time_trusted`, `superseded`); idempotent, generation-checked writes; Save/Hide event types and current-state queries | `feedback_events` columns (additive); API request fields (optional); new response header | `src/storage/app_state.py`, `src/feedback/*`, `src/api/routes/feedback.py`, `src/api/app.py` | idempotency, supersede ordering, trusted/untrusted client time, 409 on generation or epoch mismatch |
| L1.3 | `exposure_log` (visible exposure batches), retention and 30-day compaction; hidden offers excluded from job lists | new table; jobs list filter | `src/feedback/exposure.py`, `src/api/routes/jobs.py` | batch validation, generation check, compaction, hidden offers absent from lists |
| L1.4 | Frontend: IndexedDB outbox (ack, retry, Web Locks per offer, memory fallback with warning), one shared `IntersectionObserver` per list (≥ 50 % for ≥ 1 s, tab visible), Save/Hide buttons, "Enregistré" only after ack | client only | `frontend/src/api/outbox.ts`, `frontend/src/hooks/useVisibleExposure.ts`, job cards | Vitest: < 1 s not logged, hidden tab not logged, retry gives one row, 409 drops, 423 keeps |
| L1.5 | Pause and Reset learning (`app_state` flags, reset marker); Reset clears derived tables | `app_state` fields; settings API | `src/storage/app_state.py`, `src/api/routes/privacy.py`, Settings › Data | derived tables cleared, signals kept, hidden stays hidden |
| L1.6 | Erase my activity: persistent state machine (`ERASURE_PENDING`, `erasure_jobs`), HTTP 423 guard, startup check before migrations and scheduler, `backup_database` refusal, verification | new control table; startup order | `src/privacy/erasure.py`, `src/storage/migrations.py`, `src/api/app.py` | fault injection after each state, verification checks, probe write rejected |
| L1.7 | Delete all application data: `DELETE_ALL_PENDING.json` procedure, resume before the database opens | none in the DB | `src/privacy/delete_all.py`, launcher/startup | kill after each step incl. after the DB is gone; old-epoch write rejected |
| L1.8 | Lab purge option: `LAB_SUSPENDED` honoured by the single lab-DB opener, exclusive-lock proof, both `lab.db` locations, default-strategy check | lab opener refuses while suspended | `src/lab/store.py`, `src/privacy/lab_purge.py` | writer during purge fails, non-default strategy blocks, held-open file gives BLOCKED |

Each slice is committed green and merged before the next starts (K1). Frontend checks
(`npm run lint`, `npm run build`, `npm test`) run for slices touching `frontend/`.

## Notes from L1.1
- Logs include discovery queries built from profile skills. They are sent to the sources anyway, so
  the log lint does not forbid them; it forbids notes and reason lists. A profile free-text field
  (v2 summary, CV import) must be added to the lint when it exists.
- Calibration answers are files under `<data>/calibration/` (`PERSONAL_FILE_GLOBS`), not tables.
