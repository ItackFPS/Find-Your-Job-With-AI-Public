"""Load notification configuration from config/notifications.yaml.

Secrets (webhook URLs) should be provided via environment variables
(DISCORD_WEBHOOK_URL, N8N_WEBHOOK_URL, DISCORD_PIPELINE_WEBHOOK_URL,
DISCORD_ERRORS_WEBHOOK_URL, and the dynamic per-location env vars below)
rather than committed to the config file. The YAML values are used as a
fallback when the env var is absent, making local-only setups convenient
without requiring a .env.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import yaml

from src.config_files import local_config_path, resolve_config_path

logger = logging.getLogger(__name__)

# Static, operational Discord channels — unrelated to job location routing.
# This is the single source of truth for what STATIC channels exist:
# src/notifications/engine.py builds one DiscordProvider per channel here;
# src/notifications/discord.py itself stays channel-agnostic (it just
# formats/sends whatever payload it is given). Location-specific channels
# (one per configured department/remote preference) are NOT enumerated here
# — they come entirely from the user's profile.geography.location_preferences
# at runtime (via DiscordConfig.location_channels below), never hardcoded.
DISCORD_STATIC_CHANNEL_ENV_VARS: dict[str, str] = {
    "pipeline": "DISCORD_PIPELINE_WEBHOOK_URL",
    "errors": "DISCORD_ERRORS_WEBHOOK_URL",
    "statistics": "DISCORD_STATISTICS_WEBHOOK_URL",
    "system-status": "DISCORD_SYSTEM_STATUS_WEBHOOK_URL",
}


def default_location_webhook_env(slug: str) -> str:
    """Deterministic env var name for a location channel slug (e.g.
    "dept-83" -> "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL"), used when a
    location_channels entry doesn't override ``webhook_url_env`` explicitly.
    """
    return f"DISCORD_LOCATION_{slug.upper().replace('-', '_')}_WEBHOOK_URL"


def default_top10_webhook_env(slug: str) -> str:
    """Deterministic env var name for a location's top-10 leaderboard
    channel (e.g. "dept-83" -> "DISCORD_TOP10_DEPT_83_WEBHOOK_URL") — the
    top10 companion to default_location_webhook_env(), same convention.
    """
    return f"DISCORD_TOP10_{slug.upper().replace('-', '_')}_WEBHOOK_URL"


@dataclass
class DiscordChannelConfig:
    """Destination for one STATIC operational channel (e.g. "pipeline",
    "errors", "statistics", "system-status") — see
    DISCORD_STATIC_CHANNEL_ENV_VARS for the fixed name -> env-var mapping.

    ``channel_id``/``webhook_id`` are non-secret Discord snowflakes, recorded
    once src.discord_admin has created or adopted the channel/webhook —
    same reconciliation-tracking convention as DiscordLocationChannelConfig.
    ``message_id`` is only used by channels that render a standing,
    edited-in-place summary (currently just "statistics" — see
    NotificationEngine._update_job_statistics); it stays empty for
    fire-and-forget channels like "pipeline"/"errors".
    """

    enabled: bool = False
    webhook_url: str = ""
    webhook_url_env: str = ""
    channel_id: str = ""
    webhook_id: str = ""
    message_id: str = ""


@dataclass
class DiscordLocationChannelConfig:
    """Discord destination for one location slug (e.g. "dept-83",
    "remote-remote_france") — slugs come from
    src.pipeline.location_match.resolve_job_location, never hardcoded here.
    A slug with no entry (or enabled=False) has NO Discord destination: the
    matching job is not sent this run, but remains fully visible via the
    normal job list and the weekly digest — see
    src/notifications/engine.py's unmapped-location handling.

    ``channel_id``/``webhook_id`` are Discord snowflake ids — non-secret,
    safe to store here — recorded once src.discord_admin has created or
    adopted the underlying Discord resources. They let reconciliation
    verify/reuse existing infrastructure instead of re-creating it, and let
    a deactivated slug (enabled=False) be reactivated by simply flipping
    the flag back, reusing the exact same channel/webhook.

    ``original_category_id`` records the channel's parent category
    immediately before a deactivation moved it into
    DiscordConfig.inactive_category_id — empty means "no category" (guild
    top level) or "never deactivated". Reactivation moves the channel back
    here rather than guessing which category it used to sit in.
    """

    enabled: bool = False
    webhook_url: str = ""
    # Optional override; defaults to default_location_webhook_env(slug).
    webhook_url_env: str = ""
    channel_id: str = ""
    webhook_id: str = ""
    original_category_id: str = ""


@dataclass
class DiscordTop10ChannelConfig:
    """Discord destination for one location's TOP-10 LEADERBOARD channel
    (e.g. "top10-83") — a companion to DiscordLocationChannelConfig's
    "job-83" new-arrivals feed, added alongside it (2026-09-11) rather than
    replacing it: job-83 shows every new matching job as it's discovered;
    top10-83 is a standing, re-ranked "best 10 currently active" snapshot.

    ``message_id`` is the ONE Discord message this channel's leaderboard
    content is edited in place on — see src/notifications/top10_leaderboard.py.
    Empty until the first successful send; never touched by
    src.discord_admin (channel/webhook administration) — only the
    leaderboard-content sender ever writes it.
    """

    enabled: bool = False
    webhook_url_env: str = ""
    channel_id: str = ""
    webhook_id: str = ""
    message_id: str = ""
    # Same "restore on reactivation" purpose as
    # DiscordLocationChannelConfig.original_category_id — independent per
    # channel kind since a location's job-feed and top10 channels can sit
    # in different categories.
    original_category_id: str = ""


@dataclass
class ReconciliationIssue:
    """An ambiguous case src.discord_admin.reconciler could not safely
    resolve on its own (e.g. two Discord channels named "job-83") — recorded
    here instead of guessing, so a human can resolve it in Discord and the
    next reconciliation attempt (automatic or manual) can proceed cleanly.
    """

    slug: str = ""
    kind: str = ""  # "ambiguous_channel" | "ambiguous_webhook"
    detail: str = ""
    detected_at: str = ""


@dataclass
class DiscordConfig:
    enabled: bool = False
    # Legacy single-webhook fallback (DISCORD_WEBHOOK_URL / this field). Kept
    # for backward compatibility: a deployment that only ever configured one
    # Discord webhook keeps receiving every enabled static channel's
    # notifications on that same webhook until per-channel webhooks are
    # configured. NOT used as a fallback for location channels — an unmapped
    # location intentionally gets no Discord send (see
    # DiscordLocationChannelConfig), never a mislabeled shared channel.
    webhook_url: str = ""
    channels: dict[str, DiscordChannelConfig] = field(
        default_factory=lambda: {
            name: DiscordChannelConfig(enabled=True) for name in DISCORD_STATIC_CHANNEL_ENV_VARS
        }
    )
    # Keyed by location slug (e.g. "dept-83"). Empty by default — no
    # location channel exists until the operator configures one for a slug
    # the user has actually added to their profile.
    location_channels: dict[str, DiscordLocationChannelConfig] = field(default_factory=dict)
    # Keyed by the same location slugs as location_channels — one top-10
    # leaderboard channel per active location. Independent enable state:
    # a location's top10 channel can be created/deactivated without
    # touching its job-feed channel, and vice versa.
    top10_channels: dict[str, DiscordTop10ChannelConfig] = field(default_factory=dict)
    # Names of the static channels above that must never be affected by
    # location-channel reconciliation — config-driven so a future static
    # channel (e.g. "system-status") can be added without a code change.
    permanent_channels: list[str] = field(
        default_factory=lambda: list(DISCORD_STATIC_CHANNEL_ENV_VARS)
    )
    # Optional Discord category (channel of type GUILD_CATEGORY) new dynamic
    # location (job-feed) channels are created under. Purely cosmetic; empty
    # means "create at the guild top level".
    location_category_id: str = ""
    # Companion to location_category_id for the top-10 leaderboard channels
    # — a SEPARATE category, since job-feed and top10 channels live under
    # their own distinct categories (e.g. "Job-Département" vs
    # "Top10-Département"). Empty means "create at the guild top level".
    top10_category_id: str = ""
    # Optional Discord category a location's job-feed/top10 channels are
    # MOVED INTO when deactivated, and moved back out of (to
    # DiscordLocationChannelConfig/DiscordTop10ChannelConfig's recorded
    # original_category_id) on reactivation — purely cosmetic sidebar
    # organization, never affects routing (that's controlled entirely by
    # the registry's `enabled` flag). Empty disables this entirely: a
    # deactivated channel's category is left untouched, same as before this
    # feature existed.
    inactive_category_id: str = ""
    # Ambiguous cases reconciliation refused to guess on — see
    # ReconciliationIssue.
    reconciliation_issues: list[ReconciliationIssue] = field(default_factory=list)


@dataclass
class N8nConfig:
    enabled: bool = False
    webhook_url: str = ""


@dataclass
class NotificationRules:
    notify_pipeline_completed: bool = True
    notify_new_jobs: bool = True
    # Legacy n8n preview-list gate only (the "new jobs" summary folded into
    # the full PipelineCompletedEvent payload still forwarded to n8n
    # unchanged) — NOT used by Discord location routing below, which has
    # its own tier-based floor.
    min_score_for_notification: float = 0.80
    max_jobs_in_notification: int = 5
    # Location-based routing (2026-09-10): Discord notification volume is
    # controlled by a per-location CAP, not a score threshold — real data
    # showed even a strict score floor alone (e.g. "excellent, >=72") still
    # produces hundreds of notifications across a handful of locations, so
    # the floor below is only a sanity minimum (rarely binds), and the real
    # lever is max_notifications_per_location_per_run.
    max_notifications_per_location_per_run: int = 3
    # Tier name (see src.pipeline.scoring.TIER_RANK) — a job must reach at
    # least this tier to be notification-eligible at all, regardless of
    # which location it matches. Checked via
    # src.pipeline.scoring.tier_meets_minimum().
    notification_score_floor_tier: str = "relevant"
    # Whether a job matching NO configured location preference should ever
    # be notified. Default False (per the approved design: Discord is
    # purely location-driven), but exposed as a flag rather than baked into
    # the routing code so it stays reversible without a code change.
    notify_unmatched_jobs: bool = False
    # Department-notification strategy (2026-09-15 redesign): a candidate is
    # typed NEW / UNSEEN_FOR_DEPARTMENT / BACKFILL by
    # NotificationEngine._route_notification_candidates — see that method's
    # docstring for the full priority/cap algorithm. The three knobs below
    # are a SAFETY CEILING, never a target to fill: fewer eligible jobs than
    # a cap allows is a legitimate "nothing more to send" outcome, not
    # something to compensate for.
    #
    # Hard ceiling across ALL locations combined, applied after every
    # per-location cap below — prevents N departments from each
    # independently maxing out max_notifications_per_location_per_run and
    # summing to a spammy total in one run.
    max_total_notifications_per_run: int = 30
    # BACKFILL-type cap for a department still draining its pre-activation
    # backlog (department_notification_state.initial_backfill_completed_at
    # IS NULL) — deliberately larger than the steady-state knob below so a
    # newly activated department's channel isn't left looking empty.
    initial_backfill_max_per_department: int = 5
    # UNSEEN_FOR_DEPARTMENT-type cap for an already-initialized department
    # (backlog already drained) — kept small so an established department's
    # feed stays dominated by genuinely NEW jobs, with old-but-unsent jobs
    # only topping up remaining quota.
    regular_backfill_max_per_department: int = 3


@dataclass
class NotificationConfig:
    enabled: bool = True
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    n8n: N8nConfig = field(default_factory=N8nConfig)
    rules: NotificationRules = field(default_factory=NotificationRules)


def load_notification_config(config_dir: str = "config") -> NotificationConfig:
    """Load notifications.yaml; return defaults when file is absent or malformed.

    A personal ``notifications.local.yaml`` in the same directory replaces the
    tracked default when present (see src/config_files.py).
    """
    path = resolve_config_path(config_dir, "notifications.yaml")
    if not path.exists():
        logger.debug("notifications.yaml not found at %s — using defaults", path)
        return NotificationConfig()

    try:
        with path.open() as f:
            raw = yaml.safe_load(f) or {}

        n = raw.get("notifications", {})
        disc_raw = n.get("discord", {}) or {}
        n8n_raw = n.get("n8n", {}) or {}
        rules_raw = n.get("rules", {}) or {}

        channels_raw = disc_raw.get("channels", {}) or {}
        channels: dict[str, DiscordChannelConfig] = {}
        for chan_name in DISCORD_STATIC_CHANNEL_ENV_VARS:
            chan_raw = channels_raw.get(chan_name, {}) or {}
            # Same "default on" reasoning as DiscordConfig.channels' factory:
            # only an explicit `enabled: false` turns a channel off.
            channels[chan_name] = DiscordChannelConfig(
                enabled=bool(chan_raw.get("enabled", True)),
                webhook_url=str(chan_raw.get("webhook_url", "") or ""),
                webhook_url_env=str(chan_raw.get("webhook_url_env", "") or ""),
                channel_id=str(chan_raw.get("channel_id", "") or ""),
                webhook_id=str(chan_raw.get("webhook_id", "") or ""),
                message_id=str(chan_raw.get("message_id", "") or ""),
            )

        location_channels_raw = disc_raw.get("location_channels", {}) or {}
        location_channels: dict[str, DiscordLocationChannelConfig] = {}
        for slug, chan_raw in location_channels_raw.items():
            chan_raw = chan_raw or {}
            # Location channels default OFF — a slug must be explicitly
            # enabled once its webhook exists; unlike the static channels,
            # there's no fixed enumeration to default-on.
            location_channels[str(slug)] = DiscordLocationChannelConfig(
                enabled=bool(chan_raw.get("enabled", False)),
                webhook_url=str(chan_raw.get("webhook_url", "") or ""),
                webhook_url_env=str(chan_raw.get("webhook_url_env", "") or ""),
                channel_id=str(chan_raw.get("channel_id", "") or ""),
                webhook_id=str(chan_raw.get("webhook_id", "") or ""),
                original_category_id=str(chan_raw.get("original_category_id", "") or ""),
            )

        top10_channels_raw = disc_raw.get("top10_channels", {}) or {}
        top10_channels: dict[str, DiscordTop10ChannelConfig] = {}
        for slug, chan_raw in top10_channels_raw.items():
            chan_raw = chan_raw or {}
            top10_channels[str(slug)] = DiscordTop10ChannelConfig(
                enabled=bool(chan_raw.get("enabled", False)),
                webhook_url_env=str(chan_raw.get("webhook_url_env", "") or ""),
                channel_id=str(chan_raw.get("channel_id", "") or ""),
                webhook_id=str(chan_raw.get("webhook_id", "") or ""),
                message_id=str(chan_raw.get("message_id", "") or ""),
                original_category_id=str(chan_raw.get("original_category_id", "") or ""),
            )

        issues_raw = disc_raw.get("reconciliation_issues", []) or []
        reconciliation_issues = [
            ReconciliationIssue(
                slug=str(issue.get("slug", "") or ""),
                kind=str(issue.get("kind", "") or ""),
                detail=str(issue.get("detail", "") or ""),
                detected_at=str(issue.get("detected_at", "") or ""),
            )
            for issue in issues_raw
            if issue
        ]

        permanent_channels_raw = disc_raw.get("permanent_channels")
        permanent_channels = (
            [str(name) for name in permanent_channels_raw]
            if permanent_channels_raw
            else list(DISCORD_STATIC_CHANNEL_ENV_VARS)
        )

        discord = DiscordConfig(
            enabled=bool(disc_raw.get("enabled", False)),
            webhook_url=str(disc_raw.get("webhook_url", "") or ""),
            channels=channels,
            location_channels=location_channels,
            top10_channels=top10_channels,
            permanent_channels=permanent_channels,
            location_category_id=str(disc_raw.get("location_category_id", "") or ""),
            top10_category_id=str(disc_raw.get("top10_category_id", "") or ""),
            inactive_category_id=str(disc_raw.get("inactive_category_id", "") or ""),
            reconciliation_issues=reconciliation_issues,
        )
        n8n = N8nConfig(
            enabled=bool(n8n_raw.get("enabled", False)),
            webhook_url=str(n8n_raw.get("webhook_url", "") or ""),
        )

        rules = NotificationRules(
            notify_pipeline_completed=bool(
                rules_raw.get("notify_pipeline_completed", True)
            ),
            notify_new_jobs=bool(rules_raw.get("notify_new_jobs", True)),
            min_score_for_notification=float(
                rules_raw.get("min_score_for_notification", 0.80)
            ),
            max_jobs_in_notification=int(
                rules_raw.get("max_jobs_in_notification", 20)
            ),
            max_notifications_per_location_per_run=int(
                rules_raw.get("max_notifications_per_location_per_run", 3)
            ),
            notification_score_floor_tier=str(
                rules_raw.get("notification_score_floor_tier", "relevant")
            ),
            notify_unmatched_jobs=bool(rules_raw.get("notify_unmatched_jobs", False)),
            max_total_notifications_per_run=int(
                rules_raw.get("max_total_notifications_per_run", 30)
            ),
            initial_backfill_max_per_department=int(
                rules_raw.get("initial_backfill_max_per_department", 5)
            ),
            regular_backfill_max_per_department=int(
                rules_raw.get("regular_backfill_max_per_department", 3)
            ),
        )

        return NotificationConfig(
            enabled=bool(n.get("enabled", True)),
            discord=discord,
            n8n=n8n,
            rules=rules,
        )

    except Exception as exc:
        logger.warning(
            "Failed to parse notifications.yaml — using defaults: %s", exc
        )
        return NotificationConfig()


def save_discord_channel_registries(
    config_dir: str,
    *,
    channels: dict[str, DiscordChannelConfig] | None = None,
    location_channels: dict[str, DiscordLocationChannelConfig] | None = None,
    top10_channels: dict[str, DiscordTop10ChannelConfig] | None = None,
    reconciliation_issues: list[ReconciliationIssue] | None = None,
) -> None:
    """Persist reconciliation's non-secret registry updates.

    Each of ``channels``/``location_channels``/``top10_channels``/
    ``reconciliation_issues`` is independently optional — pass only the section(s) a given call
    actually changed; ``None`` leaves that section on disk untouched. Every
    other key (rules, n8n, static channels, ...) round-trips unchanged in
    content regardless. Note: like src/api/routes/profile.py's atomic write
    of profile.yaml, this re-serializes the file via PyYAML, which does not
    preserve human-authored comments (e.g. notifications.yaml's migration
    notes) — an accepted, pre-existing tradeoff in this codebase, not
    something new introduced here.

    Never writes a secret: DiscordLocationChannelConfig.webhook_url is
    intentionally NOT round-tripped by this function — only
    channel_id/webhook_id/webhook_url_env/enabled(/message_id for top10),
    which are all non-secret. See src/discord_admin/secrets.py for the one
    place actual webhook URLs are written (to .env, never here).

    Always writes the personal ``notifications.local.yaml``, seeded from the
    file currently in effect, so the tracked default is never rewritten.
    """
    read_path = resolve_config_path(config_dir, "notifications.yaml")
    path = local_config_path(config_dir, "notifications.yaml")
    raw: dict = {}
    if read_path.exists():
        with read_path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    raw.setdefault("notifications", {})
    raw["notifications"].setdefault("discord", {})

    if channels is not None:
        raw["notifications"]["discord"]["channels"] = {
            name: {
                "enabled": cfg.enabled,
                "webhook_url_env": cfg.webhook_url_env,
                "channel_id": cfg.channel_id,
                "webhook_id": cfg.webhook_id,
                "message_id": cfg.message_id,
            }
            for name, cfg in channels.items()
        }

    if location_channels is not None:
        raw["notifications"]["discord"]["location_channels"] = {
            slug: {
                "enabled": cfg.enabled,
                "webhook_url_env": cfg.webhook_url_env,
                "channel_id": cfg.channel_id,
                "webhook_id": cfg.webhook_id,
                "original_category_id": cfg.original_category_id,
            }
            for slug, cfg in location_channels.items()
        }

    if top10_channels is not None:
        raw["notifications"]["discord"]["top10_channels"] = {
            slug: {
                "enabled": cfg.enabled,
                "webhook_url_env": cfg.webhook_url_env,
                "channel_id": cfg.channel_id,
                "webhook_id": cfg.webhook_id,
                "message_id": cfg.message_id,
                "original_category_id": cfg.original_category_id,
            }
            for slug, cfg in top10_channels.items()
        }

    if reconciliation_issues is not None:
        raw["notifications"]["discord"]["reconciliation_issues"] = [
            {
                "slug": issue.slug,
                "kind": issue.kind,
                "detail": issue.detail,
                "detected_at": issue.detected_at,
            }
            for issue in reconciliation_issues
        ]

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".yaml.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)  # never leave a stray copy (issue #69)
        raise
