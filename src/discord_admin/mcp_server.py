"""Discord administration MCP server — Claude's manual/on-demand interface
to src.discord_admin, registered in .mcp.json.

This is a THIN ADAPTER ONLY. Every tool below delegates to the exact same
code the automatic backend worker uses
(src.notifications.discord_reconciler_worker → src.discord_admin.service →
src.discord_admin.reconciler) — there is no second implementation of
"how to reconcile," "how to list channels," or "how to create a webhook"
here. This server exists purely so Claude Code can inspect Discord state,
preview the reconciliation plan, and trigger an immediate reconcile instead
of waiting for the worker's next tick — automatic reconciliation does not
require this server or a Claude Code session at all (see
discord_reconciler_worker.py's module docstring).

No tool here can delete a channel or webhook, and no tool returns a raw
webhook secret: list_webhooks strips the "token" field Discord's API
returns for incoming webhooks (that field, combined with the webhook id,
IS the usable webhook URL), and reconcile_now's outcomes are already
redacted at the source (src.discord_admin.reconciler / secrets.redact).

Run standalone for manual testing: `python -m src.discord_admin.mcp_server`
(stdio transport). Claude Code launches it automatically per .mcp.json.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

from mcp.server.fastmcp import FastMCP  # noqa: E402 - after load_dotenv, matches src/main.py's convention

from src.config_files import resolve_config_path  # noqa: E402
from src.discord_admin.client import DiscordAdminClient  # noqa: E402
from src.discord_admin.lock import default_lock_path  # noqa: E402
from src.discord_admin.service import (  # noqa: E402
    BOT_TOKEN_ENV_VAR,
    GUILD_ID_ENV_VAR,
    run_reconciliation_pass,
)
from src.notifications.config import load_notification_config  # noqa: E402
from src.notifications.discord_reconciliation import (  # noqa: E402
    ReconciliationPlan,
    build_reconciliation_plan,
    build_static_reconciliation_plan,
    build_top10_reconciliation_plan,
)
from src.pipeline.filter import load_profile  # noqa: E402

_CONFIG_DIR = str(_PROJECT_ROOT / "config")
# None = resolve config/profile(.local).yaml at call time (src/config_files.py).
_PROFILE_PATH: str | None = None
# None means "use the real defaults" (default_lock_path(), the real .env,
# a real DiscordAdminClient). Tests monkeypatch these module attributes —
# same convention as src/api/routes/profile.py's _PROFILE_PATH — so no test
# ever touches the real project lock file, .env, or notifications.yaml.
_LOCK_PATH: Path | None = None
_ENV_PATH: Path | None = None
_client_factory: Callable[[], DiscordAdminClient] | None = None


def _profile_path() -> str:
    return _PROFILE_PATH or str(resolve_config_path(_CONFIG_DIR, "profile.yaml"))


def _require_client() -> DiscordAdminClient:
    bot_token = os.environ.get(BOT_TOKEN_ENV_VAR)
    guild_id = os.environ.get(GUILD_ID_ENV_VAR)
    if not bot_token or not guild_id:
        raise RuntimeError(
            f"{BOT_TOKEN_ENV_VAR} and {GUILD_ID_ENV_VAR} must both be set in .env "
            "before Discord administration tools can be used."
        )
    if _client_factory is not None:
        return _client_factory()
    return DiscordAdminClient(bot_token, guild_id)


def _sanitize_channel(channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": channel.get("id"),
        "name": channel.get("name"),
        "type": channel.get("type"),
        "parent_id": channel.get("parent_id"),
    }


def _sanitize_webhook(webhook: dict[str, Any]) -> dict[str, Any]:
    """Deliberately drops "token" — combined with "id" that IS the usable
    webhook URL. This tool is for inspection/debugging only; it must never
    be a second way to obtain a webhook secret outside src.discord_admin.secrets.
    """
    return {
        "id": webhook.get("id"),
        "name": webhook.get("name"),
        "channel_id": webhook.get("channel_id"),
    }


def list_channels() -> list[dict[str, Any]]:
    """List Discord channels in the configured guild (id, name, type, parent_id only)."""
    client = _require_client()
    try:
        return [_sanitize_channel(c) for c in client.list_channels()]
    finally:
        client.close()


def list_webhooks(channel_id: str) -> list[dict[str, Any]]:
    """List webhooks on one Discord channel (id, name, channel_id only — never the secret token/URL)."""
    client = _require_client()
    try:
        return [_sanitize_webhook(w) for w in client.list_webhooks(channel_id)]
    finally:
        client.close()


def _plan_to_dict(plan: ReconciliationPlan) -> dict[str, Any]:
    return {
        "activate": [
            {"slug": s.slug, "channel_name": s.channel_name, "label": s.label, "kind": s.kind}
            for s in plan.activate
        ],
        "deactivate": list(plan.deactivate),
        "keep": list(plan.keep),
        "invalid": list(plan.invalid),
        "is_empty": plan.is_empty,
    }


def get_reconciliation_plan() -> dict[str, Any]:
    """Preview what reconciliation would do right now, without touching
    Discord — the same desired-state/diff computation the automatic worker
    runs before every reconcile, for all THREE channel kinds: "job"
    (new-arrivals feed, e.g. "job-83"), "top10" (leaderboard, e.g.
    "top10-83"), and "static" (fixed operational channels, e.g. "pipeline",
    "statistics").
    """
    profile = load_profile(_profile_path())
    config = load_notification_config(_CONFIG_DIR)
    job_plan = build_reconciliation_plan(profile, config.discord.location_channels)
    top10_plan = build_top10_reconciliation_plan(profile, config.discord.top10_channels)
    static_plan = build_static_reconciliation_plan(
        config.discord.permanent_channels, config.discord.channels
    )
    return {
        "job": _plan_to_dict(job_plan),
        "top10": _plan_to_dict(top10_plan),
        "static": _plan_to_dict(static_plan),
        "is_empty": job_plan.is_empty and top10_plan.is_empty and static_plan.is_empty,
    }


def list_reconciliation_issues() -> list[dict[str, Any]]:
    """Ambiguous cases reconciliation refused to guess on (e.g. two Discord
    channels with the same name) — resolve these manually in Discord, then
    call reconcile_now (or wait for the next automatic tick).
    """
    config = load_notification_config(_CONFIG_DIR)
    return [
        {
            "slug": issue.slug,
            "kind": issue.kind,
            "detail": issue.detail,
            "detected_at": issue.detected_at,
        }
        for issue in config.discord.reconciliation_issues
    ]


def reconcile_now() -> dict[str, Any]:
    """Trigger an immediate reconciliation pass instead of waiting for the
    automatic worker's next tick. Uses the exact same
    src.discord_admin.service.run_reconciliation_pass the worker calls, and
    the same advisory lock — if the worker is mid-tick, this safely reports
    "locked" rather than racing it.
    """
    report = run_reconciliation_pass(
        _CONFIG_DIR,
        _profile_path(),
        lock_path=_LOCK_PATH or default_lock_path(),
        env_path=_ENV_PATH,
        client_factory=_client_factory,
    )
    return {
        "status": report.status,
        "invalid": list(report.plan_invalid),
        "outcomes": [
            {"slug": o.slug, "outcome": o.outcome, "detail": o.detail, "channel_kind": o.channel_kind}
            for o in report.outcomes
        ],
        "issues": [
            {"slug": i.slug, "kind": i.kind, "detail": i.detail, "detected_at": i.detected_at}
            for i in report.issues
        ],
        "error": report.error,
    }


mcp = FastMCP("discord-admin")
for _tool_fn in (
    list_channels,
    list_webhooks,
    get_reconciliation_plan,
    list_reconciliation_issues,
    reconcile_now,
):
    mcp.tool()(_tool_fn)


if __name__ == "__main__":
    mcp.run()
