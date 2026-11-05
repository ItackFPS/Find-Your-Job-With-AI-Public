# 0015 — Personal data has four in-app actions; erasure is a resumable, verified job

Status: accepted (2026-11-05). Not implemented yet (stage L1). Full procedure, tables and tests:
`docs/plans/2026-11-05-learning-architecture-decisions.md` §4.12 (AD-12).

## Context
Learning (ADR 0014) turns user actions into stored personal data. ADR 0009 treated erasure as a
deliberate owner-side database operation; a public application needs it in the UI, complete in
every copy the application controls (INV-10), and safe across crashes. A database transaction alone
is not enough: backups are separate files, and in WAL mode old page images survive a commit until
a checkpoint.

## Decision
- **Four actions**, each with an exact keep/delete table (§4.12):
  - *Pause learning*: a reversible switch.
  - *Reset learning*: a non-destructive marker; learning ignores earlier signals; derived learned
    state is cleared; hidden offers stay hidden; stored `job_scores.personal_*` are kept as history
    and never read for ranking again.
  - *Erase my activity*: deletes personal activity, its derived state, flow-4 metrics and **all
    pre-erasure application backups**. The application tracker is deleted unless the user ticks
    "Conserver mon suivi de candidatures"; calibration labels go with it (option ticked by default).
    Offers, market and non-personal ops data stay. **No backup is taken before an erasure** (owner
    decision).
  - *Delete all application data*: everything, back to the setup wizard.
- **Erasure is a job with a persistent, content-free state machine**: REQUESTED → QUIESCED →
  INVENTORIED → DB_ERASED → WAL_TRUNCATED → COPIES_REMOVED → VERIFIED → COMPLETE, or BLOCKED with
  the reason. A sidecar `ERASURE_PENDING` marker is fsynced before anything is touched; the
  `erasure_jobs` control table holds counts, paths and opaque ids, never content. Every step is
  idempotent; at startup the marker is checked before migrations and the scheduler. The UI says
  "Effacement terminé" only after VERIFIED (zero rows in scope, empty WAL, sampled ids absent from
  the database and WAL bytes, no older backup, generation checks).
- **Erasure generation.** A server-held integer in the one-row `app_state` control table, incremented
  inside the DB_ERASED transaction. Every personal write carries `<data_epoch>:<generation>`; a
  mismatch is rejected (409) inside the insert transaction, so writes created before an erasure can
  never be re-inserted after it, whatever their timestamps. A new database writes a random
  `data_epoch`, so entries from a deleted database are rejected too.
- **During an erasure** the pipeline and scheduler stop, personal writes get HTTP 423 (owner
  decision: no queuing), and `backup_database` refuses while the marker exists.
- **Delete all** uses its own database-free procedure: an atomic `DELETE_ALL_PENDING.json` in the
  data folder's parent holds the step, the inventory and the checks, so it resumes after the
  database file is gone.
- **Lab data.** Erase keeps `lab.db` and `data/lab/` by default (they hold the `active_strategy`
  pointer and the engine audit trail, ADR 0011). When a lab file exists in either location, the
  dialog says so and offers a separately confirmed "Effacer aussi les données du laboratoire". The
  purge covers **both** `data/lab.db` and `data/lab/lab.db`, is BLOCKED unless each shows the
  shipped default strategy, suspends lab writers with a `LAB_SUSPENDED` marker that the single
  lab-DB opener honours (refusing to open or create), proves no writer is active with an exclusive
  lock, deletes files rather than rows, and re-checks at VERIFIED that nothing reappeared. Delete
  all always removes lab files, with the same suspension.
- **Retention defaults** (exposure 180 days with 30-day compaction, shadow 90 days, health and ops 400
  days) are initial values, not approved parameters.
- **Limits stated to the user:** copies outside the application (exports, OneDrive or File History,
  restore points, quarantine) are not erased; freed disk blocks may persist physically.

## Verifications done before this ADR (code at `1c2faf5`)
- **Lab writers store label-derived per-offer content: yes.** Experiment results keep
  `new_unsafe_job_ids` (`src/lab/experiment.py`); agent proposals keep `job_ids` and title tokens
  mined from reject-labelled offers (`src/lab/agent.py`), stored in `lab.db` `result_json`,
  `details_json` and `report_json`. So the lab-purge option is needed, and the dialog sentence that
  lab files "may contain results derived from your labels" is accurate.
- **The isolated lab agent can read `LAB_SUSPENDED` in `data/lab/`: yes.** `src/lab/sandbox.py`
  grants its AppContainer SID Modify (`(OI)(CI)M`) on `data/lab/`. Modify also allows **delete**, so
  the marker is a cooperative signal, not an enforcement: the guarantees are rule 4 (exclusive lock;
  on Windows an open file cannot be deleted, so a running agent makes the purge BLOCKED) and the
  VERIFIED re-check. A missing or recreated file is BLOCKED, never success.
- **The application's account can delete files the lab agent created in `data/lab/`: yes, by
  construction.** The grant adds an ACE without breaking inheritance, so the user's inherited full
  control applies to new files, and an AppContainer token's user is the logged-on user. Not measured
  on a machine; the L1 tests include the BLOCKED path naming the file if deletion is refused.

## Consequences
- ADR 0009's "erasure is a deliberate owner-side DB operation" is superseded for the application by
  this ADR; the journal stays append-only outside an erasure (triggers are dropped and recreated
  inside the DB_ERASED transaction only).
- A table classification test fails on any table not classified as personal, derived, control,
  public or operational; a log lint test keeps notes, reasons, preference vectors and per-offer event
  lists out of logs; an INV-2 socket-guard test keeps personal data on the install.
- Fault-injection tests kill the process after each state (including after the database is deleted
  in Delete all) and require resume to reach COMPLETE with verification passing.
- Labelling workspaces outside the data folder are outside every in-app action.
