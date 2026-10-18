"""Orchestrates a ReconciliationPlan into actual Discord state changes.

This is the ONE implementation of "activate/deactivate a channel" — the
automatic backend worker, Claude's MCP-triggered on-demand reconcile, AND
both channel kinds (job-feed channels via reconcile(), top-10 leaderboard
channels via reconcile_top10()) all go through the same channel/webhook
resolution logic (_resolve_channel_and_webhook) — there is never a second,
divergent implementation of "how to create-or-adopt a Discord channel and
its webhook."

Deactivation is deliberately just a registry flag flip (no Discord API call)
— NotificationEngine already refuses to send to a disabled slug, so the
channel/webhook being visually untouched in Discord has no functional
effect on routing. See src/notifications/discord_reconciliation.py and the
approved plan for the reasoning. The same applies to top10 channels: an
NotificationEngine-side top10 sender (src/notifications/top10_leaderboard.py)
must likewise skip a disabled slug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from src.discord_admin.client import DiscordAdminClient, webhook_url
from src.discord_admin.lock import default_lock_path, reconciliation_lock
from src.discord_admin.matcher import MatchOutcome, match_channel_by_name, match_webhook_by_name_prefix
from src.discord_admin.secrets import redact, store_webhook_secret
from src.notifications.config import (
    DISCORD_STATIC_CHANNEL_ENV_VARS,
    DiscordChannelConfig,
    DiscordLocationChannelConfig,
    DiscordTop10ChannelConfig,
    ReconciliationIssue,
    default_location_webhook_env,
    default_top10_webhook_env,
    save_discord_channel_registries,
)
from src.notifications.discord_reconciliation import DesiredLocationChannel, ReconciliationPlan

logger = logging.getLogger(__name__)


@dataclass
class SlugOutcome:
    slug: str
    outcome: str  # "created" | "adopted" | "reactivated" | "deactivated" | "ambiguous" | "error"
    detail: str = ""
    # "job" (new-arrivals feed, e.g. "job-83") | "top10" (leaderboard, e.g.
    # "top10-83") — lets a combined report (both channel kinds reconciled
    # in the same pass, e.g. on first bootstrap) disambiguate two outcomes
    # for the same slug instead of looking like a duplicate.
    channel_kind: str = "job"


@dataclass
class ReconciliationResult:
    outcomes: list[SlugOutcome] = field(default_factory=list)
    issues: list[ReconciliationIssue] = field(default_factory=list)


@dataclass
class _Resolved:
    """Successful channel+webhook resolution — the shared result type both
    reconcile() and reconcile_top10() build their own registry dataclass
    from."""

    outcome_kind: str  # "created" | "adopted"
    channel_id: str
    webhook_id: str
    url: str


def _webhook_name_for(channel_name: str) -> str:
    return f"{channel_name}-webhook"


def _live_ids_still_valid(
    client: DiscordAdminClient, channel_id: str, webhook_id: str, slug: str
) -> bool:
    """Verify a registry-recorded channel_id/webhook_id pair still resolves
    live — used for reactivation (no Discord write at all when both still
    exist) and shared by both channel kinds.
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        logger.warning(
            "discord_admin: slug %s's recorded channel_id %s no longer exists — "
            "falling back to fresh channel resolution",
            slug,
            channel_id,
        )
        return False
    webhook = client.get_webhook(webhook_id)
    if webhook is None:
        logger.warning(
            "discord_admin: slug %s's recorded webhook_id %s no longer exists — "
            "falling back to fresh webhook resolution",
            slug,
            webhook_id,
        )
        return False
    return True


def _resolve_channel_and_webhook(
    client: DiscordAdminClient,
    slug: str,
    channel_name: str,
    location_category_id: str,
) -> tuple[_Resolved | None, ReconciliationIssue | None]:
    """Ambiguity-safe create-or-adopt for one channel + its one webhook.

    Shared by reconcile() (job-feed channels, e.g. "job-83") and
    reconcile_top10() (leaderboard channels, e.g. "top10-83") — the only
    difference between the two callers is which registry dataclass/env-var
    convention they build from the result, never how Discord resolution
    itself works.
    """
    now = datetime.now(UTC).isoformat()

    channels = client.list_channels()
    channel_match = match_channel_by_name(channels, channel_name)
    if channel_match.outcome is MatchOutcome.AMBIGUOUS:
        return None, ReconciliationIssue(
            slug=slug,
            kind="ambiguous_channel",
            detail=(
                f"{len(channel_match.candidates)} channels named "
                f"{channel_name!r} found in the guild — resolve the "
                "duplicate manually in Discord, then re-run reconciliation."
            ),
            detected_at=now,
        )
    if channel_match.outcome is MatchOutcome.ONE:
        # MatchResult.match is always set when outcome is ONE.
        channel_id = str(cast(dict[str, Any], channel_match.match)["id"])
    else:
        created = client.create_channel(channel_name, parent_id=location_category_id or None)
        channel_id = str(created["id"])

    webhook_name = _webhook_name_for(channel_name)
    webhooks = client.list_webhooks(channel_id)
    webhook_match = match_webhook_by_name_prefix(webhooks, webhook_name)
    if webhook_match.outcome is MatchOutcome.AMBIGUOUS:
        return None, ReconciliationIssue(
            slug=slug,
            kind="ambiguous_webhook",
            detail=(
                f"{len(webhook_match.candidates)} webhooks named like "
                f"{webhook_name!r} found on channel {channel_id} — resolve "
                "the duplicate manually in Discord, then re-run reconciliation."
            ),
            detected_at=now,
        )

    if webhook_match.outcome is MatchOutcome.ONE:
        webhook_obj = cast(dict[str, Any], webhook_match.match)  # set when outcome is ONE
        url = webhook_url(webhook_obj)
        if url is None:
            return None, ReconciliationIssue(
                slug=slug,
                kind="ambiguous_webhook",
                detail=(
                    f"Webhook {webhook_obj.get('id')} on channel {channel_id} has no "
                    "retrievable token — cannot safely adopt it. Resolve manually."
                ),
                detected_at=now,
            )
        return _Resolved("adopted", channel_id, str(webhook_obj["id"]), url), None

    webhook_obj = client.create_webhook(channel_id, webhook_name)
    url = webhook_url(webhook_obj)
    if url is None:
        return None, ReconciliationIssue(
            slug=slug,
            kind="ambiguous_webhook",
            detail="Webhook created but its URL was not retrievable — resolve manually.",
            detected_at=now,
        )
    return _Resolved("created", channel_id, str(webhook_obj["id"]), url), None


# ---------------------------------------------------------------------------
# Cosmetic category moves (deactivate -> inactive_category_id, reactivate ->
# back to whichever category it came from) — shared by job-feed and top10
# channels. Purely visual sidebar organization: NEVER affects notification
# routing (that's controlled entirely by the registry's `enabled` flag,
# checked independently by NotificationEngine), and a failure here (missing
# permission, deleted category, network error) must never block the
# functional enable/disable it accompanies — every call is best-effort and
# swallows its own exceptions.
# ---------------------------------------------------------------------------


def _move_to_inactive_category(
    client: DiscordAdminClient,
    entry: DiscordLocationChannelConfig | DiscordTop10ChannelConfig,
    inactive_category_id: str,
    context: str,
) -> None:
    if not inactive_category_id or not entry.channel_id:
        return
    try:
        live = client.get_channel(entry.channel_id)
        if live is None:
            return
        current_parent = live.get("parent_id") or ""
        if current_parent == inactive_category_id:
            return  # already there (e.g. re-deactivating without ever reactivating)
        entry.original_category_id = current_parent
        client.move_channel(entry.channel_id, inactive_category_id)
    except Exception as exc:  # noqa: BLE001 - cosmetic only, must never block deactivation
        logger.warning("discord_admin: could not move %s to inactive category: %s", context, exc)


def _ensure_in_category(
    client: DiscordAdminClient,
    channel_id: str,
    target_category_id: str,
    context: str,
) -> None:
    """Move ``channel_id`` into ``target_category_id`` if it isn't already
    there — repairs a channel that was created/adopted outside its category
    (e.g. created before this category was configured, or adopted by name
    from a pre-existing misplaced channel) without recreating anything.

    Same conventions as _move_to_inactive_category/_restore_original_category:
    opt-in (no-op when target_category_id is empty, e.g. no category
    configured for this channel kind), purely cosmetic, and best-effort — a
    failure here (missing permission, deleted category, network error) must
    never block activation.
    """
    if not target_category_id:
        return
    try:
        live = client.get_channel(channel_id)
        if live is None:
            return
        if (live.get("parent_id") or "") == target_category_id:
            return  # already correctly placed
        client.move_channel(channel_id, target_category_id)
    except Exception as exc:  # noqa: BLE001 - cosmetic only, must never block activation
        logger.warning("discord_admin: could not move %s into its category: %s", context, exc)


def _reorder_active_channels(
    client: DiscordAdminClient,
    desired_order: list[str],
    registry: dict[str, DiscordLocationChannelConfig] | dict[str, DiscordTop10ChannelConfig],
    category_id: str,
    context: str,
) -> None:
    """Converge the category's channel order to ``desired_order`` (profile
    priority order, front-to-back) — purely cosmetic sidebar ordering,
    independent of and never affecting notification routing.

    Opt-in (no-op without a configured category, same convention as
    _ensure_in_category/_move_to_inactive_category) and best-effort: a
    failure here must never block activation/deactivation.

    Only orders slugs this registry actually has a live channel_id for —
    a slug that's disabled, or that failed/is ambiguous this run, has no
    business occupying (or reserving) a position among the active channels.
    The comparison against Discord's current live order (scoped to this
    category only) makes this idempotent: already-correct order costs zero
    Discord writes.
    """
    if not category_id:
        return
    desired_channel_ids = [
        registry[slug].channel_id
        for slug in desired_order
        if slug in registry and registry[slug].enabled and registry[slug].channel_id
    ]
    if len(desired_channel_ids) < 2:
        return  # nothing to order relative to
    try:
        live_channels = client.list_channels()
        wanted = set(desired_channel_ids)
        current_order = [
            str(c["id"])
            for c in sorted(
                (c for c in live_channels if (c.get("parent_id") or "") == category_id and str(c["id"]) in wanted),
                key=lambda c: c.get("position", 0),
            )
        ]
        if current_order == desired_channel_ids:
            return  # already in the desired order
        client.set_channel_positions([(cid, i) for i, cid in enumerate(desired_channel_ids)])
    except Exception as exc:  # noqa: BLE001 - cosmetic only, must never block reconciliation
        logger.warning("discord_admin: could not reorder %s: %s", context, exc)


def _restore_original_category(
    client: DiscordAdminClient,
    entry: DiscordLocationChannelConfig | DiscordTop10ChannelConfig,
    inactive_category_id: str,
    context: str,
) -> None:
    if not inactive_category_id or not entry.channel_id:
        return
    try:
        live = client.get_channel(entry.channel_id)
        if live is None:
            return
        current_parent = live.get("parent_id") or ""
        if current_parent != inactive_category_id:
            return  # not sitting in the inactive category — a manual move, leave it alone
        client.move_channel(entry.channel_id, entry.original_category_id or None)
    except Exception as exc:  # noqa: BLE001 - cosmetic only, must never block reactivation
        logger.warning("discord_admin: could not restore %s's category: %s", context, exc)
    finally:
        entry.original_category_id = ""


# ---------------------------------------------------------------------------
# Job-feed channels (e.g. "job-83")
# ---------------------------------------------------------------------------


def _activate_one(
    client: DiscordAdminClient,
    spec: DesiredLocationChannel,
    registry: dict[str, DiscordLocationChannelConfig],
    location_category_id: str,
    env_path: Path | None,
    inactive_category_id: str = "",
) -> tuple[SlugOutcome, ReconciliationIssue | None]:
    slug = spec.slug
    existing = registry.get(slug)

    if existing and existing.channel_id and existing.webhook_id:
        if _live_ids_still_valid(client, existing.channel_id, existing.webhook_id, slug):
            existing.enabled = True
            _restore_original_category(client, existing, inactive_category_id, f"job channel {slug}")
            _ensure_in_category(client, existing.channel_id, location_category_id, f"job channel {slug}")
            return SlugOutcome(slug, "reactivated"), None

    resolved, issue = _resolve_channel_and_webhook(
        client, slug, spec.channel_name, location_category_id
    )
    if resolved is None:
        assert issue is not None
        return SlugOutcome(slug, "ambiguous", issue.detail), issue

    _ensure_in_category(client, resolved.channel_id, location_category_id, f"job channel {slug}")

    env_var_name = (existing.webhook_url_env if existing and existing.webhook_url_env else "") or (
        default_location_webhook_env(slug)
    )
    store_webhook_secret(env_var_name, resolved.url, env_path)

    registry[slug] = DiscordLocationChannelConfig(
        enabled=True,
        webhook_url_env=env_var_name,
        channel_id=resolved.channel_id,
        webhook_id=resolved.webhook_id,
    )
    detail = f"webhook stored as {env_var_name} ({redact(resolved.url)})"
    return SlugOutcome(slug, resolved.outcome_kind, detail), None


def reconcile(
    plan: ReconciliationPlan,
    registry: dict[str, DiscordLocationChannelConfig],
    client: DiscordAdminClient,
    *,
    location_category_id: str = "",
    inactive_category_id: str = "",
    config_dir: str = "config",
    lock_path: Path | None = None,
    env_path: Path | None = None,
) -> ReconciliationResult:
    """Apply ``plan`` to Discord + the job-feed channel registry, then
    persist it.

    Holds the advisory lock (src.discord_admin.lock) for the full duration —
    callers should let ReconciliationInProgressError propagate as "try again
    next tick" rather than retrying immediately.
    """
    lock = lock_path or default_lock_path()
    result = ReconciliationResult()

    with reconciliation_lock(lock):
        for slug in plan.deactivate:
            entry = registry.get(slug)
            if entry is not None:
                entry.enabled = False
                _move_to_inactive_category(client, entry, inactive_category_id, f"job channel {slug}")
            result.outcomes.append(SlugOutcome(slug, "deactivated"))

        for spec in plan.activate:
            try:
                outcome, issue = _activate_one(
                    client, spec, registry, location_category_id, env_path, inactive_category_id
                )
            except Exception as exc:  # noqa: BLE001 - one slug's failure must not block others
                logger.exception("discord_admin: reconciling slug %s failed", spec.slug)
                outcome, issue = SlugOutcome(spec.slug, "error", str(exc)), None
            result.outcomes.append(outcome)
            if issue is not None:
                result.issues.append(issue)

        _reorder_active_channels(client, plan.desired_order, registry, location_category_id, "job channels")

        # No carry-forward needed: an unresolved ambiguous slug never gets
        # enabled=True, so diff_location_channels() puts it back in
        # `activate` on every subsequent run until a human fixes the
        # Discord-side duplicate — at which point it resolves cleanly and
        # simply stops producing an issue. Persisting exactly this run's
        # issues (nothing more, nothing less) is therefore always correct.
        save_discord_channel_registries(
            config_dir, location_channels=registry, reconciliation_issues=result.issues
        )

    return result


# ---------------------------------------------------------------------------
# Top-10 leaderboard channels (e.g. "top10-83")
# ---------------------------------------------------------------------------


def _activate_one_top10(
    client: DiscordAdminClient,
    spec: DesiredLocationChannel,
    registry: dict[str, DiscordTop10ChannelConfig],
    location_category_id: str,
    env_path: Path | None,
    inactive_category_id: str = "",
) -> tuple[SlugOutcome, ReconciliationIssue | None]:
    slug = spec.slug
    existing = registry.get(slug)

    if existing and existing.channel_id and existing.webhook_id:
        if _live_ids_still_valid(client, existing.channel_id, existing.webhook_id, slug):
            existing.enabled = True
            _restore_original_category(client, existing, inactive_category_id, f"top10 channel {slug}")
            _ensure_in_category(client, existing.channel_id, location_category_id, f"top10 channel {slug}")
            return SlugOutcome(slug, "reactivated", channel_kind="top10"), None

    resolved, issue = _resolve_channel_and_webhook(
        client, slug, spec.channel_name, location_category_id
    )
    if resolved is None:
        assert issue is not None
        return SlugOutcome(slug, "ambiguous", issue.detail, channel_kind="top10"), issue

    _ensure_in_category(client, resolved.channel_id, location_category_id, f"top10 channel {slug}")

    env_var_name = (existing.webhook_url_env if existing and existing.webhook_url_env else "") or (
        default_top10_webhook_env(slug)
    )
    store_webhook_secret(env_var_name, resolved.url, env_path)

    # A brand-new/re-resolved channel+webhook has no leaderboard message
    # posted yet — message_id starts empty and is set by
    # src.notifications.top10_leaderboard on its first successful send.
    # Preserve a previously-recorded message_id only if this is the SAME
    # webhook being adopted back (rare: e.g. reactivation fell through to
    # fresh resolution because the recorded ids went stale) — safer to
    # start fresh than risk editing a message that no longer corresponds
    # to this webhook.
    registry[slug] = DiscordTop10ChannelConfig(
        enabled=True,
        webhook_url_env=env_var_name,
        channel_id=resolved.channel_id,
        webhook_id=resolved.webhook_id,
        message_id="",
    )
    detail = f"webhook stored as {env_var_name} ({redact(resolved.url)})"
    return SlugOutcome(slug, resolved.outcome_kind, detail, channel_kind="top10"), None


def reconcile_top10(
    plan: ReconciliationPlan,
    registry: dict[str, DiscordTop10ChannelConfig],
    client: DiscordAdminClient,
    *,
    location_category_id: str = "",
    inactive_category_id: str = "",
    config_dir: str = "config",
    lock_path: Path | None = None,
    env_path: Path | None = None,
    carry_forward_issues: list[ReconciliationIssue] | None = None,
) -> ReconciliationResult:
    """Top10-channel twin of reconcile() — same Discord resolution logic
    (via _resolve_channel_and_webhook), writing to the top10_channels
    registry section instead of location_channels.

    Uses the SAME advisory lock as reconcile() (both default to
    default_lock_path()) — called sequentially after reconcile() within one
    src.discord_admin.service.run_reconciliation_pass, never concurrently
    with it, so there is no cross-registry race on the lock itself.

    ``carry_forward_issues``: reconciliation_issues is one shared list at
    the discord level (not nested per channel kind), and each reconcile
    call independently persists it. Since this always runs AFTER reconcile()
    in the same pass, the caller must pass reconcile()'s result.issues here
    — otherwise this call's own save would silently overwrite (lose) the
    job-channel issues reconcile() just wrote.
    """
    lock = lock_path or default_lock_path()
    result = ReconciliationResult()

    with reconciliation_lock(lock):
        for slug in plan.deactivate:
            entry = registry.get(slug)
            if entry is not None:
                entry.enabled = False
                _move_to_inactive_category(client, entry, inactive_category_id, f"top10 channel {slug}")
            result.outcomes.append(SlugOutcome(slug, "deactivated", channel_kind="top10"))

        for spec in plan.activate:
            try:
                outcome, issue = _activate_one_top10(
                    client, spec, registry, location_category_id, env_path, inactive_category_id
                )
            except Exception as exc:  # noqa: BLE001 - one slug's failure must not block others
                logger.exception("discord_admin: reconciling top10 slug %s failed", spec.slug)
                outcome, issue = SlugOutcome(spec.slug, "error", str(exc), channel_kind="top10"), None
            result.outcomes.append(outcome)
            if issue is not None:
                result.issues.append(issue)

        _reorder_active_channels(client, plan.desired_order, registry, location_category_id, "top10 channels")

        combined_issues = list(carry_forward_issues or []) + result.issues
        save_discord_channel_registries(
            config_dir, top10_channels=registry, reconciliation_issues=combined_issues
        )

    return result


# ---------------------------------------------------------------------------
# Static operational channels (e.g. "pipeline", "errors", "statistics")
# ---------------------------------------------------------------------------


def _activate_one_static(
    client: DiscordAdminClient,
    spec: DesiredLocationChannel,
    registry: dict[str, DiscordChannelConfig],
    location_category_id: str,
    env_path: Path | None,
) -> tuple[SlugOutcome, ReconciliationIssue | None]:
    """Static-channel twin of _activate_one — name IS the slug (no per-slug
    naming transform), and the env var name is the FIXED mapping in
    DISCORD_STATIC_CHANNEL_ENV_VARS rather than a deterministic slug-derived
    convention (static channels aren't profile-driven)."""
    name = spec.slug
    existing = registry.get(name)

    if existing and existing.channel_id and existing.webhook_id:
        if _live_ids_still_valid(client, existing.channel_id, existing.webhook_id, name):
            existing.enabled = True
            return SlugOutcome(name, "reactivated", channel_kind="static"), None

    resolved, issue = _resolve_channel_and_webhook(
        client, name, spec.channel_name, location_category_id
    )
    if resolved is None:
        assert issue is not None
        return SlugOutcome(name, "ambiguous", issue.detail, channel_kind="static"), issue

    env_var_name = DISCORD_STATIC_CHANNEL_ENV_VARS.get(name) or (
        existing.webhook_url_env if existing and existing.webhook_url_env else ""
    )
    store_webhook_secret(env_var_name, resolved.url, env_path)

    registry[name] = DiscordChannelConfig(
        enabled=True,
        webhook_url_env=env_var_name,
        channel_id=resolved.channel_id,
        webhook_id=resolved.webhook_id,
        # A fresh channel/webhook has no standing message yet — same
        # "start fresh" reasoning as _activate_one_top10's message_id="".
        message_id="",
    )
    detail = f"webhook stored as {env_var_name} ({redact(resolved.url)})"
    return SlugOutcome(name, resolved.outcome_kind, detail, channel_kind="static"), None


def reconcile_static(
    plan: ReconciliationPlan,
    registry: dict[str, DiscordChannelConfig],
    client: DiscordAdminClient,
    *,
    location_category_id: str = "",
    config_dir: str = "config",
    lock_path: Path | None = None,
    env_path: Path | None = None,
    carry_forward_issues: list[ReconciliationIssue] | None = None,
) -> ReconciliationResult:
    """Static-channel twin of reconcile()/reconcile_top10() — same Discord
    resolution logic, writing to the `channels` registry section. There is
    no profile-driven "excluded" concept for these, so ``plan.deactivate``
    is expected to stay empty in practice (a name only leaves
    permanent_channels via a config change, not a job-search preference).

    ``carry_forward_issues`` follows the same "issues is one shared list"
    reasoning as reconcile_top10 — this always runs LAST in a pass, after
    reconcile() and reconcile_top10(), so it must be handed everything
    accumulated so far or it would overwrite it on save.
    """
    lock = lock_path or default_lock_path()
    result = ReconciliationResult()

    with reconciliation_lock(lock):
        for name in plan.deactivate:
            entry = registry.get(name)
            if entry is not None:
                entry.enabled = False
            result.outcomes.append(SlugOutcome(name, "deactivated", channel_kind="static"))

        for spec in plan.activate:
            try:
                outcome, issue = _activate_one_static(
                    client, spec, registry, location_category_id, env_path
                )
            except Exception as exc:  # noqa: BLE001 - one channel's failure must not block others
                logger.exception("discord_admin: reconciling static channel %s failed", spec.slug)
                outcome, issue = SlugOutcome(spec.slug, "error", str(exc), channel_kind="static"), None
            result.outcomes.append(outcome)
            if issue is not None:
                result.issues.append(issue)

        combined_issues = list(carry_forward_issues or []) + result.issues
        save_discord_channel_registries(
            config_dir, channels=registry, reconciliation_issues=combined_issues
        )

    return result
