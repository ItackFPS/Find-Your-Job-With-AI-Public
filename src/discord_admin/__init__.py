"""Discord administration layer — the single implementation of Discord REST
calls, ambiguity-safe channel/webhook matching, and webhook-secret handling.

Shared by two callers, both of which must stay thin adapters with no
Discord logic of their own:
- src.notifications.discord_reconciler_worker (automatic, in-process,
  no Claude/MCP dependency).
- The Discord administration MCP server (manual/on-demand, used by Claude
  for inspection, debugging, and triggering an immediate reconcile).

See src/notifications/discord_reconciliation.py for the desired-state/diff
computation this package acts on — that module has no Discord dependency,
this package has no profile/notifications-config dependency beyond the
plan/registry types it's handed.
"""
