# 0016 — Signals: only visible exposure is logged; durable actions go through an acknowledged, ordered outbox

Status: accepted (2026-11-05). Not implemented yet (stage L1). Details and tests:
`docs/design/learning-architecture-decisions.md` §4.6 (AD-6), with Save, Hide and active
questions as in the v1 proposal.

## Context
Personal learning (ADR 0014) needs to know what the user actually saw and did, without asking for
labels. Every card of a list is rendered today (no virtualization), so "rendered" says nothing about
attention. `sendBeacon` does not report whether the server stored a request, the client sends no
idempotency key, and the server stamps `occurred_at` on receipt: a retried event duplicates, an
out-of-order "hide/unhide" ends in the wrong state, and a late delivery gets the wrong time.

## Decision
- **Exposure levels.** *Rendered* is never logged. *Visible* (≥ 50 % of the card in the viewport for
  ≥ 1 s, tab visible; one shared `IntersectionObserver` per list) is logged to `exposure_log` as
  context, never as evidence. *Opened* (`viewed`, apply link) is a weak positive in
  `feedback_events`. *Ignored* is derived (visible in ≥ 3 engaged sessions without action, never on
  randomized slots, never on the embedding) and **inert at launch** (owner decision).
- **Event context.** Every event carries `surface`, `position`, `sort_key`, `randomized` and a random
  per-tab `session_id`, in the existing `feedback_events.metadata_json`.
- **Two delivery classes.** *Durable* (explicit stances and reasons, save/unsave, hide/unhide,
  application status, calibration answers, `apply_link_opened`): written to an IndexedDB outbox
  before sending, POSTed with a `client_event_id` under a partial unique index, acknowledged only
  after commit, removed only on acknowledgement, retried with backoff and at start or focus.
  *Best-effort* (exposure batches, `viewed`): fire-and-forget. **A lost `viewed` is neutral**: it
  never produces a negative or an "ignored".
- **Replies.** 4xx validation errors are shown, never dropped silently; 409
  `erasure_generation_mismatch` drops the entry (the activity was erased, ADR 0015); 423 keeps it
  until the erasure ends. "Enregistré" is shown only after acknowledgement.
- **No IndexedDB** (private mode): memory outbox, a permanent visible warning, and no claim of
  durability (owner decision).
- **Ordering per offer.** Events of a state family (save, hide, explicit stance, application status)
  carry `client_instance_id` and a monotonic `client_seq` shared by the browser's tabs; the outbox
  sends one event per offer at a time, coordinated by Web Locks. The server stores an event that
  arrives after a higher `client_seq` of the same family, offer and instance with `superseded = 1`
  (set at insert, never updated, so the append-only triggers stay). Current state = latest
  non-superseded row in insert order. Replays insert nothing.
- **Across browser profiles** there is no shared counter: server insert order decides. This is a
  documented limit (owner decision, 2026-11-05).
- **Two timestamps.** `occurred_at` stays the server receipt time (retention, delivery health,
  erasure bookkeeping, cross-profile order). New `client_created_at` is the effective time only when
  trusted (within `[occurred_at − max_lag, occurred_at + skew]`, initial 30 days / 5 minutes,
  unapproved); otherwise `client_time_trusted = 0` and the effective time is `occurred_at`. Client
  time never decides generation, idempotency, retention or erasure.
- **Schema.** `feedback_events` gains `client_event_id`, `generation`, `client_instance_id`,
  `client_seq`, `client_created_at`, `client_time_trusted`, `superseded`, via `ensure_columns`;
  legacy rows keep NULL. Server-side writers derive their `client_event_id` from their natural key.
- Save and Hide are list management (Hide semantics in the validity ADR); no counters or quotas
  are shown to the user.

We chose an acknowledged outbox over fire-and-forget because lost or duplicated explicit actions
would teach the model the wrong thing and break the user's trust in "Enregistré"; we log only
visible exposure because rendered cards would turn every scroll into fake negatives.

## Consequences
- The erasure generation and data epoch (ADR 0015) are stamped on every outbox entry and exposure
  batch when the user acts.
- Learning (AD-3) and shadow evaluation (AD-5) read a family's events in this effective order and use
  effective time for recency and windows.
- Tests: card visible < 1 s not logged; nothing logged while hidden; server killed before commit
  gives exactly one row after retry; duplicate `client_event_id` gives one row; "unhide" delivered
  before "hide" ends visible with "hide" superseded; skewed client times stored as untrusted.
- ADR 0009 stays append-only: every new column is set at insert.
