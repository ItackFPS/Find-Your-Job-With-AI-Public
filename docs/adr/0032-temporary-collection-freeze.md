# 0032 — Collection is frozen until each collector is validated and turned back on by the user

Status: accepted (2026-11-14, owner decision D7 of the Phase B plan). Temporary: it ends collector by
collector, never all at once and never through an update.

## Context
The Phase A audit and its A2 validation found that several collectors do not work as intended
(coverage, request budget, pagination, completeness of listings, enablement read from two places).
The owner decided to stop all external collection until there is a reliable strategy for each
collector, rather than tune budgets now:

- no external job source is contacted and no live collection runs;
- stored offers, saved offers and application history are preserved;
- collector code and configuration stay available for offline tests, replays and simulations;
- an update must never reactivate a collector;
- each collector comes back only after it passes its own validation criteria and the owner explicitly
  approves its reactivation.

## Decision
- `src/collectors/freeze.py` decides whether a collector may contact its source. Two conditions,
  both required: the collector's type is in `VALIDATED_COLLECTORS` (shipped with the code, empty
  today), and the user reactivated it (`collection_freeze.reactivated` in `data/settings.json`,
  written only by `POST /api/collection/{type}/reactivate`).
- A missing or unreadable state reads as frozen. A fresh install, an upgraded install and a
  command-line run are therefore frozen. The app records the freeze date once at start
  (`collection_freeze.since`) and never clears a reactivation.
- Enforcement at every entry point that can contact a source:
  - preflight lists each frozen source as skipped with the freeze reason and does not authenticate
    to a frozen France Travail API; the pipeline never builds a skipped collector, and no circuit
    breaker moves;
  - `PipelineRunner.start_run` refuses (`CollectionFrozen`, HTTP 409) while every configured collector
    is frozen: such a run would only re-score stored offers and replace the visible list with them;
  - the scheduler dispatches no frozen source (no execution row, no retry), and a manual or resume
    trigger raises `CollectionFrozen`;
  - LinkedIn and HelloWork scan and resume routes answer 409.
- The Dashboard and Settings show a notice; the Run button reads "Collection paused" and is disabled.
- Tests that exercise collection with mocked collectors run with enforcement off (`tests/conftest.py`);
  tests marked `collection_freeze` run with it on.

## Consequences
- No new offers arrive while the freeze holds. The visible list stays the last completed run.
- Making a collector eligible again is a code change that adds it to `VALIDATED_COLLECTORS` with a
  reference to its validation record; the user still has to turn it on.
- A future source-strategy decision (ADR 0031 is a proposal, not a decision) does not lift the freeze.
