# 0034 — The job list is persistent: offers leave it only by an explicit rule

Status: accepted (2026-11-16, owner decisions on issue #253). Supersedes Phase B open question OQ-3
("replace the list, mark failed sources") and the "no age-based expiry for staged sources" clause of
owner decision D1.

## Context
The main list is the set of offers scored in the latest visible run. Until this decision a run scored
only what it collected; LinkedIn and HelloWork alone re-injected their stored offers. A source that
returned nothing, failed or was skipped therefore took its still-active offers out of the list
without notice. In the B11 rehearsal an empty run took the list from 797 to 273 offers (only the
re-injected LinkedIn and HelloWork offers stayed) and reported `no_new_offers`. Staged offers, on
the other hand, never expired.

## Decision
- Every run carries forward the stored `active` offers of every enabled source that it did not
  collect (`src/pipeline/carryover.py`, `src.source_enablement.enabled_sources`). Carried offers are
  re-filtered and re-scored like any other offer; they come after collected offers, so a collected
  copy of the same offer wins deduplication.
- A carried offer is not a sighting: it is not written back, does not reset its miss streak and does
  not refresh its provenance (`job_provenance.last_seen_at`).
- An offer leaves the main list only because:
  1. it expired: its end date passed (`valid_through`), it was missing from 3 complete listings
     in a row (`not_seen`, issue #86), or no source collected it for `expire_after_days_unseen` days
     (`stale`, default 30, `config/retention.yaml`), whatever its source;
  2. its source is off (inactive in `sources.yaml`, a removed type, or switched off by the user);
  3. the user hid it;
  4. the current profile rejects it;
  5. it is shown in Découverte or the outside-criteria lane instead.
  Expiry is reversible when the offer is collected again. Nothing is deleted.
- A source that ran is incomplete when it failed, came back partial, reported a network outage,
  or is suspect: it completed with no offer although it returned offers the last time it ran
  (`suspect_empty`). Incomplete sources are named (`stats.incomplete_sources`); a run claims "nothing
  new" only when no source was incomplete. Runs where every source that ran is incomplete keep the
  previous visible run (`no_successful_source`), as before.
- The collection freeze (ADR 0032) is not "off": frozen sources keep their offers carried.

## Consequences
- The list no longer shrinks because a source had a bad day; it shrinks only for a reason that can
  be counted and shown.
- Runs score more offers (the carried ones); embeddings are cached, so the cost is the filter and
  scoring stages.
- On the first run after the upgrade, active offers that recent runs had stopped listing come back
  until they turn stale. LinkedIn and HelloWork provenance was refreshed by the old re-injection, so
  those offers age out at the earliest 30 days after the upgrade.
- `stats.collected` stays what the sources returned; `stats.carried` and `stats.carried_by_source`
  report the carried offers.
