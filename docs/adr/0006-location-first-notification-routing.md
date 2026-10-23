# Job notifications are routed by location, one channel per job, no fallback

**Status: superseded (2.0).** Discord and n8n notifications were removed with the 2.0
update (`docs/plans/2026-10-23-final-update-plan.md`, §C8). This record is kept for
history; the code it cites no longer exists.

`NotificationEngine` (`src/notifications/engine.py`) sends one Discord batch per
configured location (from `profile.geography.location_preferences`), plus fixed
operational channels (`pipeline`, `errors`, `statistics`, `system-status`) and
per-location top-10 leaderboards (`src/notifications/top10_query.py`). Each job is
resolved to **at most one** location channel with the same `resolve_job_location()` used
by scoring, so it is never fanned out. The legacy single `DISCORD_WEBHOOK_URL` is a
fallback for static channels only: an unmapped location gets no send rather than a
mislabeled shared channel (`DiscordConfig` in `src/notifications/config.py`). Channels
and webhooks for current locations are created/adopted by the reconciler
(`src/discord_admin/`, `DiscordReconcilerWorker`).

We chose location as the routing key because location is the highest-priority scoring
factor (ADR 0003) and the way the user triages jobs; one resolver for scoring and
routing keeps "where a job ranks" and "where it is announced" consistent.

## Consequences

- Duplicate sends are prevented by the partial unique index on
  `notification_history(provider, event_type, entity_id) WHERE status='sent'`, and a
  send is only marked sent after the HTTP call succeeds.
- Notification failures are logged and never fail the pipeline.
