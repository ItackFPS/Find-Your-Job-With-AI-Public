"""Notification engine — orchestrates providers and deduplication.

The engine is the single entry point for all outbound notifications.  It:
  1. Loads configuration (which providers/channels are enabled).
  2. Builds the existing rich PipelineCompletedEvent payload and forwards it
     to n8n unchanged (n8n remains available for other automation — see
     config/n8n_weekly_digest.json — it is simply no longer responsible for
     formatting/sending the real-time Discord notifications below).
  3. Independently routes Discord notifications: one batch message PER
     CONFIGURED LOCATION (department/remote preference from
     profile.geography.location_preferences — never hardcoded), plus the
     fixed operational #pipeline/#errors channels. Every job is resolved to
     AT MOST ONE location channel via
     src.pipeline.location_match.resolve_job_location — the same resolver
     scoring uses — so a job is never fanned out to more than one channel
     (src/notifications/discord.py stays the single place Discord embeds
     are built; this module only decides what goes where).
  4. Detects whether a notification has already been sent (deduplication,
     via notification_history) and never marks something "sent" unless the
     HTTP call actually succeeded, so a failed delivery can be retried.
  5. NEVER raises — all errors are logged, pipeline result is unaffected.

Usage in PipelineRunner._execute():

    engine.notify_pipeline_completed(result, new_jobs=[...])
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.notifications.config import (
    DISCORD_STATIC_CHANNEL_ENV_VARS,
    DiscordLocationChannelConfig,
    NotificationConfig,
    default_location_webhook_env,
    default_top10_webhook_env,
    load_notification_config,
    save_discord_channel_registries,
)
from src.notifications.discord import DiscordProvider, fit_location_batch
from src.notifications.events import JobSummary, NotifPipelineStats, PipelineCompletedEvent
from src.notifications.n8n import N8nProvider, check_n8n_live_status
from src.notifications.storage import NotificationStorage
from src.notifications.top10_query import compute_top10_by_slug
from src.pipeline.departments import extract_department_code
from src.pipeline.location_match import LocationMatch, resolve_job_location, slug_for

if TYPE_CHECKING:
    from src.pipeline.orchestrator import PipelineResult

logger = logging.getLogger(__name__)

_LEGACY_DISCORD_ENV_VAR = "DISCORD_WEBHOOK_URL"

# Candidate-type priority for Discord location routing (2026-09-15 redesign)
# — NEW always outranks UNSEEN_FOR_DEPARTMENT, which always outranks
# BACKFILL. Lower rank = higher priority. See
# NotificationEngine._route_notification_candidates for how a candidate's
# type is determined and where this ordering is applied.
_CANDIDATE_TYPE_RANK: dict[str, int] = {
    "new": 0,
    "unseen_for_department": 1,
    "backfill": 2,
}


def _candidate_priority_key(c: dict[str, Any], tier_rank: dict[str, int]) -> tuple[int, int, int]:
    """Deterministic sort key: (type_rank ASC, tier_rank DESC, score DESC).

    Shared by NotificationEngine._route_notification_candidates (per
    location) and _send_pipeline_completed (the cross-location global cap)
    so both stages agree on what "highest priority" means. ``tier_rank`` is
    passed in (src.pipeline.scoring.TIER_RANK) rather than imported here to
    avoid a module-level import of src.pipeline (eagerly loads
    sentence-transformers/torch — see this file's other deferred imports of
    that package for the same concern).
    """
    return (
        _CANDIDATE_TYPE_RANK.get(c["candidate_type"], 99),
        -tier_rank.get(c["tier"], 0),
        -c["score_100"],
    )


class NotificationEngine:
    """Dispatches pipeline events to all enabled notification providers."""

    def __init__(
        self,
        db_path: str = "data/jobs.db",
        config_dir: str = "config",
        config: NotificationConfig | None = None,
    ) -> None:
        self._db_path = db_path
        self._config_dir = config_dir
        self._config = config or load_notification_config(config_dir)
        self._last_notification_stats: dict[str, dict[str, Any]] = {}

    # -- public API ----------------------------------------------------------

    def notify_pipeline_completed(
        self,
        result: "PipelineResult",
        new_jobs: list[dict[str, Any]] | None = None,
    ) -> None:
        """Send pipeline-completed notifications to all enabled providers.

        Parameters
        ----------
        result:
            The PipelineResult returned by run_pipeline().
        new_jobs:
            Optional list of dicts describing jobs that are new since the
            previous run.  Each dict has keys: job_id, title, company,
            location, remote, work_mode, employment_type, skills,
            final_score, canonical_url, apply_url, job_source.
        """
        if not self._config.enabled:
            return
        if not self._config.rules.notify_pipeline_completed:
            return
        self._warn_if_duplicate_discord_paths()
        try:
            self._send_pipeline_completed(result, new_jobs or [])
        except Exception as exc:
            logger.error("NotificationEngine: unexpected error: %s", exc)

    def _warn_if_duplicate_discord_paths(self) -> None:
        """Surface the one real duplicate-notification risk in this
        architecture: this engine's own Python Discord channels AND
        whatever n8n workflow ``config/n8n_weekly_digest.json`` triggers
        both draw from ``config/notifications.yaml``'s ``n8n.webhook_url``
        / legacy shared Discord webhook. Historically (Phase 5, 2026-09-10)
        this guarded against the now-deleted ``config/n8n_workflow.json``
        (a webhook-triggered workflow that independently posted its own
        Discord embeds) during the cutover to this engine's native Discord
        sending — that specific workflow no longer exists, but the check
        stays generic: ANY n8n workflow independently posting to the same
        Discord webhook this engine also targets would double-post, so this
        keeps warning whenever both ``discord.enabled`` and ``n8n.enabled``
        are true rather than assuming today's specific workflow is the only
        way that can happen. Documented end state remains
        ``n8n.enabled: false`` (see ``config/notifications.yaml``'s
        ``n8n`` section). Never changes behavior — n8n.enabled continues to
        mean exactly what it already means — it only makes the risk visible
        in logs instead of silently double-posting.
        """
        if self._config.discord.enabled and self._config.n8n.enabled:
            logger.warning(
                "NotificationEngine: both discord.enabled and n8n.enabled are true. "
                "If the active n8n workflow independently posts its own Discord "
                "notifications using the same webhook this engine's Python Discord "
                "channels also fall back to, expect duplicate Discord messages "
                "unless this is intentional. The documented end state is "
                "n8n.enabled: false once the Python Discord channels are verified "
                "(see config/notifications.yaml's n8n section)."
            )

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent notification records (for the dashboard)."""
        storage = self._open_storage()
        try:
            return storage.list_recent(limit=limit)
        finally:
            storage.close()

    @staticmethod
    def _channel_status_entry(enabled: bool, provider: DiscordProvider | None) -> dict[str, Any]:
        """One channel's status block, shared by every channel kind
        (static/location/top10) in get_provider_status().

        ``configured`` means "this channel's webhook actually resolves to a
        usable URL right now" (i.e. ``provider.enabled``) — NOT "an operator
        turned it on in notifications.yaml" (that's ``enabled``). The two can
        legitimately disagree: a channel can be ``enabled: true`` (the
        operator's intent, and what src.discord_admin has already created in
        Discord) while its webhook secret hasn't reached this environment's
        .env yet — that combination is exactly the silent-no-op state
        ``missing_webhook`` calls out explicitly, so a caller never has to
        re-derive "enabled AND NOT configured" itself.
        """
        configured = bool(provider and provider.enabled)
        return {
            "enabled": enabled,
            "configured": configured,
            "missing_webhook": enabled and not configured,
        }

    def get_provider_status(self) -> dict[str, Any]:
        """Return enabled/configured status for each provider.

        Additive: the existing top-level `providers.discord` / `providers.n8n`
        shape (enabled/configured/last_sent) is unchanged, so
        frontend/src/components/Sidebar.tsx keeps working. `channels`,
        `location_channels` and `top10_channels` are nested keys under
        `providers.discord` only — each entry additionally reports
        `missing_webhook` (see _channel_status_entry) so any caller (this
        API, a startup log, or the system-status worker) can identify an
        "enabled but unusable" channel without recomputing the check.
        """
        discord_legacy = DiscordProvider(self._config.discord.webhook_url)
        n8n = N8nProvider(self._config.n8n.webhook_url)
        storage = self._open_storage()
        try:
            channels_status: dict[str, Any] = {}
            for name in DISCORD_STATIC_CHANNEL_ENV_VARS:
                chan_cfg = self._config.discord.channels.get(name)
                provider = self._build_discord_channel(name)
                channels_status[name] = self._channel_status_entry(
                    bool(chan_cfg and chan_cfg.enabled), provider
                )
            location_channels_status: dict[str, Any] = {}
            # Reloaded fresh (see _current_location_channels' docstring) —
            # this status endpoint must never report a department as
            # unconfigured/disabled just because it was enabled after this
            # engine was constructed, which is exactly the class of bug the
            # notification-send path itself was fixed for. Without this,
            # get_unconfigured_enabled_channels()/log_unconfigured_channels()
            # (both built on this method) could misreport a genuinely
            # enabled-and-working department as needing attention.
            for slug, location_cfg in self._current_location_channels().items():
                provider = self._build_location_discord_channel(slug)
                location_channels_status[slug] = self._channel_status_entry(
                    bool(location_cfg.enabled), provider
                )
            top10_channels_status: dict[str, Any] = {}
            for slug, top10_cfg in self._config.discord.top10_channels.items():
                provider = self._build_top10_discord_channel(slug)
                top10_channels_status[slug] = self._channel_status_entry(
                    bool(top10_cfg.enabled), provider
                )
            return {
                "enabled": self._config.enabled,
                "providers": {
                    "discord": {
                        "enabled": self._config.discord.enabled,
                        "configured": discord_legacy.enabled,
                        "last_sent": storage.get_last_sent("discord"),
                        "channels": channels_status,
                        "location_channels": location_channels_status,
                        "top10_channels": top10_channels_status,
                    },
                    "n8n": {
                        "enabled": self._config.n8n.enabled,
                        "configured": n8n.enabled,
                        "last_sent": storage.get_last_sent("n8n"),
                        # Actual n8n server reachability right now (via n8n's own
                        # REST API + N8N_API_KEY) — independent of "configured"
                        # above, which only means the push-webhook URL is set.
                        "reachable": check_n8n_live_status(),
                    },
                },
                "counts": storage.count_by_status(),
            }
        finally:
            storage.close()

    def get_unconfigured_enabled_channels(self) -> list[dict[str, str]]:
        """Flat list of every enabled Discord channel (static/location/top10)
        currently missing a usable webhook — derived from
        get_provider_status() rather than re-walking the config, so there is
        exactly one place that decides what "missing" means.

        Returns ``[{"kind": "static"|"location"|"top10", "name": <channel
        name or location slug>}, ...]``. Cheap (one NotificationStorage
        open/close, no network I/O) — safe to call at startup and on every
        system-status heartbeat.
        """
        discord_status = self.get_provider_status()["providers"]["discord"]
        missing: list[dict[str, str]] = []
        for kind, key in (
            ("static", "channels"),
            ("location", "location_channels"),
            ("top10", "top10_channels"),
        ):
            for name, entry in discord_status[key].items():
                if entry.get("missing_webhook"):
                    missing.append({"kind": kind, "name": name})
        return missing

    def log_unconfigured_channels(self) -> None:
        """Log one clear warning per enabled-but-unusable Discord channel —
        the "enabled: true with no resolvable webhook" silent no-op state
        found in the 2026-09-12 notification audit. Intended to be called
        once at API startup (see src/api/app.py's _lifespan) so an operator
        sees this in the backend log instead of wondering why a channel
        never received anything.

        NEVER raises: a health-reporting failure must never prevent the
        backend from starting (matches this class's existing "never raises"
        guarantee for pipeline notifications).
        """
        try:
            missing = self.get_unconfigured_enabled_channels()
        except Exception as exc:  # noqa: BLE001 - startup health check must never block startup
            logger.warning(
                "NotificationEngine: could not check Discord channel health at startup: %s", exc
            )
            return
        for item in missing:
            logger.warning(
                "NotificationEngine: Discord %s channel '%s' is enabled in "
                "notifications.yaml but has no resolvable webhook URL in this "
                "environment — it will silently not send until its webhook "
                "env var is set (see .env.example).",
                item["kind"], item["name"],
            )

    # -- internals -----------------------------------------------------------

    def _open_storage(self) -> NotificationStorage:
        s = NotificationStorage(db_path=self._db_path)
        s.init_db()
        return s

    def _batch_fetch_job_details(self, job_ids: list[str]) -> dict[str, Any]:
        """Batch-fetch Job rows (location/url/description/...) for enrichment.

        ScoredJobRecord has no location/url/description — those live only in
        the jobs table. Chunked to stay well under SQLite's bound-parameter
        limit (a single `IN (...)` with thousands of ids would be unsafe).
        """
        if not job_ids:
            return {}
        from src.api.routes.jobs import _get_jobs_filtered
        from src.storage.sqlite import Storage as JobStorage

        job_storage = JobStorage(db_path=self._db_path)
        job_storage.init_db()
        try:
            by_id: dict[str, Any] = {}
            chunk_size = 500
            for i in range(0, len(job_ids), chunk_size):
                chunk = job_ids[i:i + chunk_size]
                for job in _get_jobs_filtered(job_storage, job_ids=chunk):
                    by_id[job.id] = job
            return by_id
        finally:
            job_storage.close()

    def _get_previous_run_stats(self, run_id: str) -> dict[str, Any] | None:
        """Return the previous completed run's stats dict, or None.

        Uses the existing RunStorage.get_previous_completed_run(), which
        already persists each run's stats as JSON — no new persistence is
        introduced for pipeline deltas. Returns None on the first-ever run,
        or when no previous stats are available for any reason.
        """
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=self._db_path)
        rs.init_db()
        try:
            prev = rs.get_previous_completed_run(run_id)
            if prev is None:
                return None
            return prev.get("stats") or None
        finally:
            rs.close()

    # Number of prior runs fetched for the source-yield baseline — matches
    # src.notifications.source_health's own default lookback window, plus a
    # small buffer (the current run's own now-"completed" row, and one extra
    # run so the "is this a NEW anomaly" check can look one step further
    # back — see that module's docstring).
    _SOURCE_HEALTH_HISTORY_FETCH_LIMIT = 8

    def _check_source_yield_anomalies(
        self, result: "PipelineResult", storage: NotificationStorage, timestamp: str
    ) -> None:
        """Detect and alert on newly-broken/dropped sources for this run.

        Reuses the exact same #errors channel + notification_history dedup
        as the pipeline.warnings alert above — see
        src.notifications.source_health.detect_source_yield_anomalies for
        the detection logic itself (a pure function, independently tested).
        """
        from src.notifications.source_health import detect_source_yield_anomalies
        from src.storage.runs import RunStorage

        current_by_source = result.stats.by_source
        if not current_by_source:
            return

        rs = RunStorage(db_path=self._db_path)
        rs.init_db()
        try:
            recent_runs = rs.list_runs(limit=self._SOURCE_HEALTH_HISTORY_FETCH_LIMIT)
        finally:
            rs.close()
        # Exclude the current run itself (already "completed" in the DB by
        # the time this fires — see src/api/runner.py's ordering) — history
        # must only ever contain PRIOR runs.
        history = [r for r in recent_runs if r.get("id") != result.run_id]

        anomalies = detect_source_yield_anomalies(current_by_source, history)
        for anomaly in anomalies:
            if not anomaly.is_new:
                continue  # still broken from a prior run — already alerted, don't repeat
            payload = {
                "event": "source.yield_anomaly",
                "run_id": result.run_id,
                "timestamp": timestamp,
                "source": anomaly.source,
                "severity": anomaly.severity,
                "current_yield": anomaly.current_yield,
                "baseline_yield": anomaly.baseline_yield,
                "baseline_run_count": anomaly.baseline_run_count,
            }
            self._dispatch_discord(
                storage, "errors",
                "source.yield_anomaly", f"{result.run_id}:{anomaly.source}",
                payload,
            )

    def _build_discord_channel(self, name: str) -> DiscordProvider | None:
        """Build the DiscordProvider for one channel, or None if disabled.

        One DiscordProvider instance = one channel = one webhook. No
        transport/formatting logic is duplicated — every instance is the
        same class, just pointed at a different, independently-configured
        webhook (env var first, then YAML, then the legacy single-webhook
        fallback for backward compatibility).
        """
        chan_cfg = self._config.discord.channels.get(name)
        if chan_cfg is None or not chan_cfg.enabled:
            return None
        return DiscordProvider(
            webhook_url=chan_cfg.webhook_url,
            env_var=DISCORD_STATIC_CHANNEL_ENV_VARS[name],
            fallback_env_var=_LEGACY_DISCORD_ENV_VAR,
            fallback_webhook_url=self._config.discord.webhook_url,
        )

    def _current_location_channels(self) -> dict[str, DiscordLocationChannelConfig]:
        """The location_channels registry, reloaded fresh from disk and
        reassigned onto self._config.discord.location_channels — the
        new-arrivals-feed equivalent of _update_top10_leaderboards' own
        top10_channels reload (same file, same "long-lived singleton vs.
        independently-timed reconciliation worker" reasoning; see that
        method's docstring for the full explanation).

        Called at the top of every context that needs to know "which
        location channels are configured/enabled right now": once per
        pipeline run from _backfill_notification_candidates (the first
        location-routing step _send_pipeline_completed runs, BEFORE
        _build_location_discord_channel is used later in that same run —
        so _build_location_discord_channel deliberately does NOT call this
        itself and just reads self._config.discord.location_channels
        directly, exactly like _build_top10_discord_channel already does
        for top10_channels reloaded once, up top, by
        _update_top10_leaderboards), and once per get_provider_status()
        call (so the status/debug endpoint — and
        get_unconfigured_enabled_channels()/log_unconfigured_channels(),
        both built on it — never misreports a just-enabled department as
        unconfigured between pipeline runs). This was the root cause of a
        department added (or re-enabled) in notifications.yaml after engine
        construction never receiving new-arrivals notifications: job
        resolution itself worked fine (_load_location_preferences was
        already reloaded fresh on every call), but both the backfill
        query's desired_slugs and the later provider lookup were reading a
        location_channels snapshot frozen at construction time — while
        Top10 worked, because top10_channels already had this reload.
        Safe to read unlocked: writes to notifications.yaml are atomic (tmp
        file + rename), same assumption _load_location_preferences already
        makes for profile.yaml.
        """
        location_channels = load_notification_config(self._config_dir).discord.location_channels
        self._config.discord.location_channels = location_channels
        return location_channels

    def _build_location_discord_channel(self, slug: str) -> DiscordProvider | None:
        """Build the DiscordProvider for one location slug (e.g. "dept-83"),
        or None if that slug has no configured/enabled channel.

        Deliberately NO fallback to the legacy shared webhook: an unmapped
        location gets no Discord send at all rather than a mislabeled
        message on someone else's channel (e.g. a Nice job must never land
        in a channel literally named/branded "Var") — the job stays fully
        visible via the normal job list and the weekly digest regardless.

        Reads self._config.discord.location_channels directly (no reload
        here) — relies on _current_location_channels() having already
        refreshed it earlier in the same pipeline run, via
        _backfill_notification_candidates (see that method and
        _current_location_channels' docstrings).
        """
        chan_cfg = self._config.discord.location_channels.get(slug)
        if chan_cfg is None or not chan_cfg.enabled:
            return None
        return DiscordProvider(
            webhook_url=chan_cfg.webhook_url,
            env_var=chan_cfg.webhook_url_env or default_location_webhook_env(slug),
        )

    def _build_top10_discord_channel(self, slug: str) -> DiscordProvider | None:
        """Build the DiscordProvider for one location's top10 leaderboard
        channel (e.g. "top10-83"), or None if not configured/enabled.

        Same no-fallback rule as _build_location_discord_channel: an
        unconfigured top10 channel simply doesn't get updated, never
        silently reuses another location's webhook.
        """
        chan_cfg = self._config.discord.top10_channels.get(slug)
        if chan_cfg is None or not chan_cfg.enabled:
            return None
        return DiscordProvider(
            env_var=chan_cfg.webhook_url_env or default_top10_webhook_env(slug),
        )

    def _build_system_status_discord_channel(self) -> DiscordProvider | None:
        """Build the DiscordProvider for the #system-status standing-summary
        channel, or None if not configured/enabled.

        Same no-fallback rule as _build_statistics_discord_channel: an
        edited standing message must never silently piggyback on another
        channel's webhook (see that method's docstring for why).
        """
        chan_cfg = self._config.discord.channels.get("system-status")
        if chan_cfg is None or not chan_cfg.enabled:
            return None
        return DiscordProvider(
            webhook_url=chan_cfg.webhook_url,
            env_var=chan_cfg.webhook_url_env or DISCORD_STATIC_CHANNEL_ENV_VARS["system-status"],
        )

    def _build_statistics_discord_channel(self) -> DiscordProvider | None:
        """Build the DiscordProvider for the #statistics standing-summary
        channel, or None if not configured/enabled.

        Deliberately NO fallback to the legacy shared webhook (unlike
        _build_discord_channel's pipeline/errors behavior): statistics
        edits one standing message via send_or_update, so piggy-backing on
        another channel's webhook would let it silently edit whatever
        message that webhook last created — same "unconfigured means no
        send" rule as _build_top10_discord_channel.
        """
        chan_cfg = self._config.discord.channels.get("statistics")
        if chan_cfg is None or not chan_cfg.enabled:
            return None
        return DiscordProvider(
            webhook_url=chan_cfg.webhook_url,
            env_var=chan_cfg.webhook_url_env or DISCORD_STATIC_CHANNEL_ENV_VARS["statistics"],
        )

    def _update_top10_leaderboards(self) -> None:
        """Re-render each enabled location's top10 leaderboard message in
        place — one DB pass (src.notifications.top10_query), one Discord
        edit-or-create per location, every pipeline run.

        Independent of the new-arrivals feed above: this never uses
        NotificationStorage dedup (there's nothing to deduplicate — it's an
        idempotent edit of one standing message, not a discrete event), and
        a location with a disabled/unconfigured top10 channel is skipped
        entirely before the DB query even considers it.

        Re-reads the top10_channels registry fresh from disk first — same
        "no restart required" reasoning as _load_location_preferences().
        This engine is normally a long-lived singleton reused for the whole
        backend process lifetime (see src/api/app.py), while
        src.discord_admin's reconciliation (its own worker, on its own
        timer) independently creates/enables/updates top10_channels entries
        in notifications.yaml as departments are added or re-enabled.
        Without this reload, self._config.discord.top10_channels stayed
        frozen at whatever it was when this engine was constructed, so a
        department added (or re-enabled) after that point would never be
        queried or sent to — its channel could exist in Discord yet never
        receive a leaderboard message. Safe to read unlocked: writes to
        notifications.yaml are atomic (tmp file + rename), same assumption
        _load_location_preferences already makes for profile.yaml.
        """
        top10_channels = load_notification_config(self._config_dir).discord.top10_channels
        self._config.discord.top10_channels = top10_channels
        desired_slugs = {slug for slug, cfg in top10_channels.items() if cfg.enabled}
        if not desired_slugs:
            return

        location_preferences = self._load_location_preferences()
        try:
            results = compute_top10_by_slug(self._db_path, location_preferences, desired_slugs)
        except Exception as exc:  # noqa: BLE001 - a bad query must not affect the rest of the run
            logger.warning("NotificationEngine: top10 leaderboard query failed: %s", exc)
            return

        for pref in location_preferences:
            if pref.get("status") == "excluded":
                continue
            slug = slug_for(pref["type"], pref["value"])
            if slug not in desired_slugs:
                continue
            provider = self._build_top10_discord_channel(slug)
            if provider is None or not provider.enabled:
                continue

            entries = results.get(slug, [])
            is_remote = pref["type"] == "remote"
            label = pref.get("label") or self._location_label(
                LocationMatch(
                    type=pref["type"], value=pref["value"], status=pref.get("status", "preferred"),
                    priority=0, slug=slug, label=pref.get("label"),
                )
            )
            payload = {
                "event": "jobs.top10_leaderboard",
                "location_type": "remote" if is_remote else "department",
                "location_label": label,
                "entries": entries,
            }

            current_message_id = top10_channels[slug].message_id
            new_message_id = provider.send_or_update(payload, current_message_id)
            if new_message_id is None:
                logger.warning("NotificationEngine: top10[%s] leaderboard update failed", slug)
                continue
            if new_message_id != current_message_id:
                self._persist_top10_message_id(slug, new_message_id)

    def _persist_top10_message_id(self, slug: str, message_id: str) -> None:
        """Record a leaderboard's (new-or-changed) message id, under the
        SAME advisory lock src.discord_admin.reconciler uses — both this
        method and reconciliation write to notifications.yaml's
        discord.top10_channels section, so they must never race.

        Re-reads the registry fresh under the lock rather than trusting
        self._config's snapshot, so a concurrent reconciliation pass's
        channel_id/webhook_id/enabled changes for OTHER slugs are never
        clobbered by this write. Also updates THIS engine's own in-memory
        self._config copy (see the comment at the end of this method) —
        without that, a NotificationEngine is normally a long-lived
        singleton (one instance for the whole backend process, reused
        across every pipeline run — see src/api/app.py), so a later call on
        the SAME instance would otherwise keep reading the original,
        never-updated in-memory message_id ("") and create a brand new
        Discord message every time instead of ever editing this one.
        """
        from src.discord_admin.lock import ReconciliationInProgressError, default_lock_path, reconciliation_lock

        try:
            with reconciliation_lock(default_lock_path()):
                fresh = load_notification_config(self._config_dir)
                entry = fresh.discord.top10_channels.get(slug)
                if entry is None:
                    return  # channel was removed between read and write — nothing to persist
                entry.message_id = message_id
                save_discord_channel_registries(
                    self._config_dir, top10_channels=fresh.discord.top10_channels
                )
        except ReconciliationInProgressError:
            # Rare: the automatic reconciliation worker is mid-tick right
            # now. Not persisting this run's message id means a future
            # send_or_update() call may create a fresh message instead of
            # editing this one — self-correcting (only the newest message
            # is ever edited going forward), not a correctness issue.
            logger.info(
                "NotificationEngine: could not persist top10[%s] message id — "
                "reconciliation in progress, will retry next run.", slug,
            )
            return

        # Keep this engine's own in-memory config in sync with what was
        # just written to disk (see docstring above) — this object is the
        # SAME one _update_top10_leaderboards reads chan_cfg.message_id
        # from on every future call.
        own_entry = self._config.discord.top10_channels.get(slug)
        if own_entry is not None:
            own_entry.message_id = message_id

    def _update_job_statistics(self) -> None:
        """Re-render the #statistics standing message in place — one DB
        query (src.api.routes.jobs.compute_job_stats, the SAME computation
        GET /api/jobs/stats uses, never duplicated here), one Discord
        edit-or-create, every pipeline run.

        Independent of dedup/new_jobs, same reasoning as
        _update_top10_leaderboards: this is an idempotent edit of one
        standing message, not a discrete per-job event.
        """
        chan_cfg = self._config.discord.channels.get("statistics")
        if chan_cfg is None or not chan_cfg.enabled:
            return
        provider = self._build_statistics_discord_channel()
        if provider is None or not provider.enabled:
            return

        from src.api.routes.jobs import compute_job_stats
        from src.storage.runs import RunStorage
        from src.storage.sqlite import Storage as JobStorage

        job_storage = JobStorage(db_path=self._db_path)
        job_storage.init_db()
        run_storage = RunStorage(db_path=self._db_path)
        run_storage.init_db()
        try:
            stats = compute_job_stats(job_storage, run_storage)
        finally:
            job_storage.close()
            run_storage.close()

        payload = {
            "event": "jobs.statistics",
            "total_jobs": stats.total_jobs,
            "top_skills": [(f.name, f.count) for f in stats.skills],
            "score_tiers": [(f.tier, f.count) for f in stats.score_tiers],
            "title_categories": [(f.name, f.count) for f in stats.title_categories],
        }

        current_message_id = chan_cfg.message_id
        new_message_id = provider.send_or_update(payload, current_message_id)
        if new_message_id is None:
            logger.warning("NotificationEngine: statistics update failed")
            return
        if new_message_id != current_message_id:
            self._persist_static_channel_message_id("statistics", new_message_id)

    def _persist_static_channel_message_id(self, channel_name: str, message_id: str) -> None:
        """Record a static standing-message channel's (new-or-changed)
        message id, under the SAME advisory lock src.discord_admin.reconciler
        uses — same reasoning as _persist_top10_message_id, applied to the
        `channels` registry section instead of `top10_channels`.

        Shared by every static channel that edits one standing message in
        place (currently "statistics" and "system-status") so there is
        exactly one implementation of this read-lock-write sequence rather
        than one per channel. Also syncs THIS engine's own in-memory
        self._config copy — see _persist_top10_message_id's docstring for
        why that matters for a long-lived NotificationEngine instance.
        """
        from src.discord_admin.lock import ReconciliationInProgressError, default_lock_path, reconciliation_lock

        try:
            with reconciliation_lock(default_lock_path()):
                fresh = load_notification_config(self._config_dir)
                entry = fresh.discord.channels.get(channel_name)
                if entry is None:
                    return
                entry.message_id = message_id
                save_discord_channel_registries(
                    self._config_dir, channels=fresh.discord.channels
                )
        except ReconciliationInProgressError:
            logger.info(
                "NotificationEngine: could not persist %s message id — "
                "reconciliation in progress, will retry next run.", channel_name,
            )
            return

        own_entry = self._config.discord.channels.get(channel_name)
        if own_entry is not None:
            own_entry.message_id = message_id

    def build_system_status_payload(self) -> dict[str, Any]:
        """Assemble the #system-status snapshot: backend/database/pipeline/
        n8n/Discord health in one payload, called by SystemStatusWorker on
        its own interval (independent of pipeline runs — the point is to
        answer "is the system alive" even when no pipeline has run
        recently).

        Every signal below reuses an existing check rather than a second
        implementation (see each block's comment for its source), and is
        independently guarded so one failing signal (e.g. a locked DB file)
        degrades only that one field instead of blacking out the whole
        status message. NEVER raises.
        """
        from datetime import UTC, datetime

        # Backend: trivially true — this code is executing inside the
        # backend process, so if it runs at all, the backend is online.
        backend_status = "ONLINE"

        # Database: a real, cheap connectivity check (init_db() opens the
        # connection and ensures the schema exists) — same call app.py's
        # own startup already performs.
        db_status = "ONLINE"
        try:
            from src.storage.sqlite import Storage

            s = Storage(db_path=self._db_path)
            s.init_db()
            s.close()
        except Exception as exc:  # noqa: BLE001 - one failing signal must not blacken the whole status
            db_status = "DEGRADED"
            logger.warning("System status: database check failed: %s", exc)

        # Pipeline: reuses RunStorage's existing run-state queries (the same
        # ones the dashboard/API already use) rather than a new query.
        pipeline_status = "UNKNOWN"
        try:
            from src.storage.runs import RunStorage

            rs = RunStorage(db_path=self._db_path)
            rs.init_db()
            try:
                if rs.get_current_running_run() is not None:
                    pipeline_status = "RUNNING"
                else:
                    recent = rs.list_runs(limit=1)
                    if recent:
                        pipeline_status = "READY" if recent[0]["status"] == "completed" else "FAILED"
            finally:
                rs.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("System status: pipeline check failed: %s", exc)

        # n8n: actual server reachability via n8n's own REST API + N8N_API_KEY
        # (check_n8n_live_status) — deliberately independent of
        # notifications.yaml's n8n.enabled (that flag is about whether THIS
        # app pushes pipeline events to n8n, a separate concern from "is the
        # n8n instance itself running"). UNKNOWN only when N8N_API_KEY isn't
        # set at all (there is nothing to check).
        try:
            live = check_n8n_live_status()
            n8n_status = "UNKNOWN" if live is None else ("ONLINE" if live else "OFFLINE")
        except Exception as exc:  # noqa: BLE001
            n8n_status = "OFFLINE"
            logger.warning("System status: n8n check failed: %s", exc)

        # Discord configuration: reuses get_unconfigured_enabled_channels()
        # (the same data get_provider_status()/the startup log use) —
        # "healthy" iff no enabled channel is silently unusable. Fails
        # CLOSED (reports "degraded"), not open, when the check itself
        # errors — a status feature that silently reports "healthy" because
        # it couldn't actually check would defeat its own purpose.
        try:
            missing_channels = self.get_unconfigured_enabled_channels()
            discord_status = "healthy" if not missing_channels else "degraded"
        except Exception as exc:  # noqa: BLE001
            logger.warning("System status: Discord channel health check failed: %s", exc)
            missing_channels = []
            discord_status = "degraded"

        return {
            "event": "system.status",
            "backend": backend_status,
            "database": db_status,
            "pipeline": pipeline_status,
            "n8n": n8n_status,
            "discord": discord_status,
            "missing_channels": missing_channels,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def update_system_status(self) -> None:
        """Re-render the #system-status standing message in place — one
        payload build (build_system_status_payload), one Discord
        edit-or-create, same "one standing message" convention as
        _update_top10_leaderboards/_update_job_statistics.

        Called by SystemStatusWorker on its own interval — deliberately NOT
        tied to pipeline runs, so the answer to "is my system alive" stays
        fresh even when no pipeline has run in a while. NEVER raises:
        a health-reporting failure must never crash the worker thread that
        calls this, or the application.
        """
        if not self._config.discord.enabled:
            return
        chan_cfg = self._config.discord.channels.get("system-status")
        if chan_cfg is None or not chan_cfg.enabled:
            return
        provider = self._build_system_status_discord_channel()
        if provider is None or not provider.enabled:
            return

        try:
            payload = self.build_system_status_payload()
        except Exception as exc:  # noqa: BLE001 - must never crash the caller
            logger.warning("NotificationEngine: system status payload build failed: %s", exc)
            return

        current_message_id = chan_cfg.message_id
        try:
            new_message_id = provider.send_or_update(payload, current_message_id)
        except Exception as exc:  # noqa: BLE001 - must never crash the caller
            logger.warning("NotificationEngine: system status send failed: %s", exc)
            return
        if new_message_id is None:
            logger.warning("NotificationEngine: system status update failed")
            return
        if new_message_id != current_message_id:
            self._persist_static_channel_message_id("system-status", new_message_id)

    def _dispatch_discord_to(
        self,
        storage: NotificationStorage,
        provider: DiscordProvider | None,
        channel_label: str,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Send one Discord payload via an already-built provider, with
        dedup + safe recording.

        Returns True only if a send actually happened and succeeded this
        call. No HTTP request is made when ``provider`` is None/disabled
        (e.g. an unmapped location channel), or when this exact
        (event_type, entity_id) was already recorded as successfully sent.

        Partial-failure rule: a failed send is recorded as 'failed', never
        'sent' — notification_history's unique-on-sent index only blocks
        retries for rows marked 'sent', so a failed delivery can always be
        retried on a later call (e.g. the next pipeline run, or a manual
        retry of the same run).
        """
        if provider is None or not provider.enabled:
            return False
        if storage.already_sent("discord", event_type, entity_id):
            logger.debug(
                "Discord[%s]: %s already sent for %s — skipping",
                channel_label, event_type, entity_id,
            )
            return False
        ok = provider.send(payload)
        storage.record(
            provider="discord",
            event_type=event_type,
            entity_id=entity_id,
            status="sent" if ok else "failed",
            error=None if ok else "provider.send() returned False",
        )
        if not ok:
            logger.warning(
                "Discord[%s]: %s send failed for %s", channel_label, event_type, entity_id
            )
        return ok

    def _dispatch_discord(
        self,
        storage: NotificationStorage,
        channel: str,
        event_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> bool:
        """Send to a STATIC channel (#pipeline/#errors) by name. See
        _dispatch_discord_to() for the underlying send/dedup logic shared
        with dynamic location channels.
        """
        provider = self._build_discord_channel(channel)
        return self._dispatch_discord_to(storage, provider, channel, event_type, entity_id, payload)

    def _load_location_preferences(self) -> list[dict[str, Any]]:
        """Load profile.geography.location_preferences as plain dicts — the
        SOLE source of truth for which departments/remote are notification-
        eligible, read fresh on every call so a profile edit takes effect on
        the very next pipeline run with no code/config change.
        """
        from pathlib import Path

        from src.api.routes.jobs import _load_profile_safe

        profile = _load_profile_safe(Path(self._config_dir))
        return [p.model_dump() for p in profile.geography.location_preferences]

    @staticmethod
    def _location_label(match: LocationMatch) -> str:
        """Human-readable label for a resolved match — the preference's own
        ``label`` if the user set one, else derived from the department name
        (src.pipeline.departments) or a fixed "Remote France" for remote.
        """
        if match.label:
            return match.label
        if match.type == "department":
            from src.pipeline.departments import get_department

            dept = get_department(match.value)
            name = dept.name if dept else match.value
            return f"{name} ({match.value})"
        return "Remote France"

    def _route_notification_candidates(
        self,
        storage: NotificationStorage,
        candidates: list[dict[str, Any]],
        new_jobs: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Resolve every CURRENTLY SCORED job (this run's full output — not
        just jobs that are new since the previous run) to at most ONE
        configured location (department/remote preference), drop jobs
        already delivered to that job's own Discord destination, TYPE and
        PRIORITIZE the remainder within each location, and cap.

        Three independent, deliberately separate concerns (2026-09-15
        redesign — do not re-conflate them):
          - Job freshness ("is this job new in the database?") — answered
            by whether job_id is in ``new_jobs`` (the run-over-run diff from
            _detect_new_jobs). Drives ONLY the "new" candidate_type below —
            it is NOT itself a gate (a pre-existing job is just as eligible
            as a brand-new one; see the BACKFILL/UNSEEN_FOR_DEPARTMENT
            candidate types).
          - Department notification state ("has this exact job already been
            sent to a Discord location channel, ever?") — the ONLY per-job
            eligibility gate below (storage.already_sent, GLOBAL by job_id —
            see the module docstring for why this must stay global, not
            department-scoped: a job resolves to AT MOST ONE destination,
            so "sent to department X" and "sent, period" are the same fact
            for that job). Checked BEFORE any cap, so an already-sent top
            scorer can never crowd out a not-yet-sent lower scorer for the
            same location.
          - Department lifecycle ("has THIS department finished draining
            its pre-activation backlog?" — department_notification_state,
            see storage.py) — drives the "backfill" vs
            "unseen_for_department" split for anything that isn't "new".
        Conflating freshness with eligibility previously meant a job that
        predates a location being configured/activated (or that simply
        wasn't picked as run-over-run "new" the one time its destination
        channel happened to be ready) could never be reconsidered again —
        including on a newly activated department's very first run, where
        every matching job is by definition already-existing.

        Within a location bucket, candidates are ordered
        (type_rank ASC, tier_rank DESC, score_100 DESC) — NEW always
        outranks UNSEEN_FOR_DEPARTMENT, which always outranks BACKFILL;
        ties within a type broken by tier then raw score. Reuses
        src.pipeline.scoring.TIER_RANK — deliberately no second scoring
        system. Sub-caps (rules.initial_backfill_max_per_department for
        "backfill", rules.regular_backfill_max_per_department for
        "unseen_for_department") are applied before the overall
        rules.max_notifications_per_location_per_run cap, so an
        ever-growing backlog can never permanently crowd NEW jobs out of
        their own department's quota. Every cap here is a SAFETY CEILING,
        never a target — fewer candidates than a cap allows is a
        legitimate "nothing more to send" outcome.

        This method NEVER calls storage.mark_initial_backfill_complete
        itself — it only selects/types/caps candidates, it does not know
        whether the cross-location global cap (applied afterward, by the
        caller) will trim some of this slug's "backfill" candidates back
        out, nor whether the resulting Discord batch actually sends
        successfully. See _send_pipeline_completed for where lifecycle
        completion is actually decided, and why it must wait until both of
        those are known (never a naive "if no history" check either; see
        storage.mark_initial_backfill_complete's docstring).

        Returns ``(buckets, stats)``:
          - buckets: ``{slug: {"match": LocationMatch, "jobs": [enriched job
            dicts (+ candidate_type), priority-ordered, already capped]}}``.
            A job matching no configured preference is dropped entirely
            unless rules.notify_unmatched_jobs is set, in which case it is
            bucketed under the synthetic slug "unmatched" (only actually
            sends if the operator has also configured a Discord channel for
            that slug — same "no mapping -> no send" rule as any other
            location).
          - stats: ``{slug: {"candidates", "already_sent",
            "below_threshold", "by_type": {"new", "unseen_for_department",
            "backfill"}, "queued_to_send", "capped_out"}}`` — lightweight
            per-run observability (see get_last_run_notification_stats),
            answering "why did department X get 0/2/8 notifications" without
            verbose logging.

        This is a CAP, not a score threshold: rules.notification_score_floor_tier
        is only a sanity minimum (real data shows even a strict floor alone
        still produces hundreds of matches across a handful of locations —
        volume is controlled by the per-location cap, not the floor).
        """
        from src.pipeline.scoring import TIER_RANK, score_to_tier, tier_meets_minimum

        rules = self._config.rules
        location_preferences = self._load_location_preferences()
        new_job_ids = {j["job_id"] for j in new_jobs if j.get("job_id")}

        buckets: dict[str, dict[str, Any]] = {}
        stats: dict[str, dict[str, Any]] = {}
        dept_states: dict[str, dict[str, Any]] = {}

        def stats_for(slug: str) -> dict[str, Any]:
            return stats.setdefault(slug, {
                "candidates": 0,
                "already_sent": 0,
                "below_threshold": 0,
                "by_type": {"new": 0, "unseen_for_department": 0, "backfill": 0},
                "queued_to_send": 0,
                "capped_out": 0,
            })

        for j in candidates:
            job_id = j.get("job_id")
            if not job_id:
                continue

            score = float(j.get("final_score") or 0)
            score_100 = round(score * 100)
            tier = score_to_tier(score_100)

            # Same "department or fall back to extracting it from the
            # location string" pattern src.pipeline.scoring and
            # src.notifications.top10_query already use — several
            # collectors (HelloWork, LinkedIn, Greenhouse, Lever, EURES, ...)
            # never populate the raw `department` column, so relying on it
            # alone silently drops every job from those sources out of
            # location routing even though scoring/top10 already correctly
            # resolve their department from `location`.
            dept = j.get("department") or extract_department_code(
                j.get("location") or "", j.get("countries")
            )

            match = resolve_job_location(
                dept,
                bool(j.get("remote")),
                list(j.get("countries") or []),
                location_preferences,
            )
            if match is None:
                if not rules.notify_unmatched_jobs:
                    continue
                slug = "unmatched"
            else:
                slug = match.slug

            slug_stats = stats_for(slug)
            slug_stats["candidates"] += 1

            # Global, job_id-only dedup — see module/method docstring for
            # why this must never become (job_id, department)-scoped.
            if storage.already_sent("discord", "jobs.location_notify", job_id):
                slug_stats["already_sent"] += 1
                continue
            if not tier_meets_minimum(tier, rules.notification_score_floor_tier):
                slug_stats["below_threshold"] += 1
                continue

            if job_id in new_job_ids:
                candidate_type = "new"
            else:
                state = dept_states.get(slug)
                if state is None:
                    state = storage.get_or_create_department_state(slug)
                    dept_states[slug] = state
                candidate_type = (
                    "backfill" if state["initial_backfill_completed_at"] is None
                    else "unseen_for_department"
                )
            slug_stats["by_type"][candidate_type] += 1

            enriched = {**j, "score_100": score_100, "tier": tier, "candidate_type": candidate_type}
            bucket = buckets.setdefault(slug, {"match": match, "jobs": []})
            bucket["jobs"].append(enriched)

        for slug, bucket in buckets.items():
            jobs = bucket["jobs"]
            jobs.sort(key=lambda c: _candidate_priority_key(c, TIER_RANK))

            capped: list[dict[str, Any]] = []
            backfill_used = 0
            unseen_used = 0
            for c in jobs:
                if c["candidate_type"] == "backfill":
                    if backfill_used >= rules.initial_backfill_max_per_department:
                        continue
                    backfill_used += 1
                elif c["candidate_type"] == "unseen_for_department":
                    if unseen_used >= rules.regular_backfill_max_per_department:
                        continue
                    unseen_used += 1
                capped.append(c)

            final_jobs = capped[: rules.max_notifications_per_location_per_run]
            bucket["jobs"] = final_jobs

            slug_stats = stats_for(slug)
            slug_stats["queued_to_send"] = len(final_jobs)
            slug_stats["capped_out"] = len(jobs) - len(final_jobs)
            # raw_backfill_count (== slug_stats["by_type"]["backfill"], the
            # per-slug candidate count BEFORE any capping) is deliberately
            # NOT used here to decide lifecycle completion. This method only
            # selects/types/caps candidates — it never dispatches. Whether a
            # department's backlog is truly drained can only be known once
            # the cross-location global cap (applied later, in
            # _send_pipeline_completed, on this method's OWN return value)
            # and the actual Discord send outcome are both known — see
            # _send_pipeline_completed for where
            # mark_initial_backfill_complete is actually called, and why.

        for slug, slug_stats in stats.items():
            logger.info(
                "NotificationEngine[%s]: %d candidates (new=%d unseen=%d backfill=%d) -> "
                "%d already sent, %d below threshold, %d queued to send, %d capped out",
                slug, slug_stats["candidates"],
                slug_stats["by_type"]["new"], slug_stats["by_type"]["unseen_for_department"],
                slug_stats["by_type"]["backfill"], slug_stats["already_sent"],
                slug_stats["below_threshold"], slug_stats["queued_to_send"], slug_stats["capped_out"],
            )
        self._last_notification_stats = stats

        return buckets, stats

    def get_last_notification_stats(self) -> dict[str, dict[str, Any]]:
        """Per-department diagnostics from the most recent
        _route_notification_candidates call this engine instance has made —
        candidates/already_sent/below_threshold/by_type/queued_to_send/
        capped_out per slug. Empty until the first pipeline run. Cheap,
        in-memory only (not persisted) — intended for tests, a debug
        endpoint, or a future dashboard; see that method's docstring for
        the exact shape.
        """
        return self._last_notification_stats

    def _backfill_notification_candidates(self) -> list[dict[str, Any]]:
        """Currently-active jobs matching a configured+enabled location
        channel, independent of run-over-run job freshness — see
        src.notifications.top10_query.compute_notification_backfill_candidates
        for why this must exist as a SEPARATE source of candidates from
        run-over-run new_jobs (a newly-activated department's matching jobs
        are almost always pre-existing, not new-to-this-run).

        Merged into new_jobs in _send_pipeline_completed before routing;
        _route_notification_candidates is what actually dedups (via
        notification_history) and caps per location, so this deliberately
        performs neither — it only answers "what's currently active and
        matches a configured location", never "what hasn't been sent yet".

        NEVER raises: a DB read failure here must not prevent the
        run-over-run new_jobs notifications below from being sent — matches
        this class's existing "never raises" guarantee.

        Also the place this class refreshes location_channels from disk
        each run (via _current_location_channels(), called unconditionally
        below before any early return) — see that method's docstring.
        """
        desired_slugs = {
            slug for slug, cfg in self._current_location_channels().items() if cfg.enabled
        }
        if not desired_slugs:
            return []
        try:
            from src.notifications.top10_query import compute_notification_backfill_candidates

            return compute_notification_backfill_candidates(
                self._db_path, self._load_location_preferences(), desired_slugs
            )
        except Exception as exc:  # noqa: BLE001 - matches this class's "never raises" guarantee
            logger.warning("NotificationEngine: notification backfill query failed: %s", exc)
            return []

    @staticmethod
    def _merge_notification_candidates(
        new_jobs: list[dict[str, Any]],
        backfill_candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Combine run-over-run new_jobs with the DB-driven backfill pool
        (see _backfill_notification_candidates), deduplicated by job_id — a
        genuinely new job is typically present in both (new_jobs via the
        run-over-run diff, backfill via this run's active-job scan) and must
        only be considered once. new_jobs wins on overlap (it already
        carries the full field set _detect_new_jobs builds).
        """
        merged: dict[str, dict[str, Any]] = {}
        for j in backfill_candidates:
            job_id = j.get("job_id")
            if job_id:
                merged[job_id] = j
        for j in new_jobs:
            job_id = j.get("job_id")
            if job_id:
                merged[job_id] = j
        return list(merged.values())

    def _build_location_batch_payload(
        self,
        match: LocationMatch | None,
        candidates: list[dict[str, Any]],
        run_id: str,
    ) -> dict[str, Any]:
        """Build a single per-location batch embed payload.

        Shared by every location channel — never one Discord message per
        job. ``candidates`` is already capped by
        _route_notification_candidates(); this method never truncates
        further. Only fields that actually exist on
        the job dict are forwarded; the Discord embed builder decides what
        to display.
        """
        return {
            "event": "jobs.location_notify",
            "run_id": run_id,
            "location_type": match.type if match else "unmatched",
            "location_label": self._location_label(match) if match else "Unmatched",
            "total_count": len(candidates),
            "shown_count": len(candidates),
            "overflow_count": 0,
            "jobs": [
                {
                    "job_id": j["job_id"],
                    "title": j.get("title"),
                    "company": j.get("company"),
                    "location": j.get("location"),
                    "remote": j.get("remote", False),
                    "work_mode": j.get("work_mode"),
                    "employment_type": j.get("employment_type"),
                    "countries": list(j.get("countries") or []),
                    "score_100": j["score_100"],
                    "tier": j["tier"],
                    "canonical_url": j.get("canonical_url"),
                    "apply_url": j.get("apply_url"),
                }
                for j in candidates
            ],
        }

    def _send_pipeline_completed(
        self,
        result: "PipelineResult",
        new_jobs: list[dict[str, Any]],
    ) -> None:
        stats_dict = result.stats.to_dict()
        notif_stats = NotifPipelineStats(
            collected=stats_dict.get("collected", 0),
            validated=stats_dict.get("validated", 0),
            deduplicated=stats_dict.get("deduplicated", 0),
            filtered=stats_dict.get("filtered", 0),
            scored=stats_dict.get("scored", 0),
            semantically_matched=stats_dict.get("semantically_matched", 0),
            personalized=stats_dict.get("personalized", 0),
            duration_seconds=stats_dict.get("duration_seconds", 0.0),
            errors=stats_dict.get("errors", []),
        )

        rules = self._config.rules

        # Preview list: top N new-since-last-run jobs above the score threshold.
        # Kept exactly as before — this is the subset still forwarded to n8n
        # (see PipelineCompletedEvent below), which remains available for
        # other automation and is untouched by the Discord redesign.
        preview: list[JobSummary] = []
        if rules.notify_new_jobs and new_jobs:
            threshold = rules.min_score_for_notification
            for j in new_jobs[:rules.max_jobs_in_notification]:
                score = float(j.get("final_score") or 0)
                if score >= threshold:
                    preview.append(JobSummary(
                        job_id=j.get("job_id", ""),
                        title=j.get("title"),
                        company=j.get("company"),
                        location=j.get("location"),
                        remote=bool(j.get("remote", False)),
                        work_mode=j.get("work_mode"),
                        skills=list(j.get("skills") or []),
                        final_score=score,
                        canonical_url=j.get("canonical_url"),
                        apply_url=j.get("apply_url"),
                    ))

        # Digest: classify ALL scored jobs (thousands) into the five tiers —
        # unchanged, still needed for the n8n payload.
        scored_jobs = list(getattr(result, "scored_jobs", None) or [])
        tier_counts: dict[str, int] = {"exceptional": 0, "excellent": 0, "strong": 0, "relevant": 0, "low_priority": 0}
        digest_jobs: dict[str, list[dict[str, Any]]] = {
            "exceptional": [], "excellent": [], "strong": [], "relevant": [], "low_priority": []
        }
        if scored_jobs:
            from src.pipeline.scoring import score_to_tier

            job_details = self._batch_fetch_job_details([sr.job_id for sr in scored_jobs])
            for sr in scored_jobs:
                score_100 = round(float(sr.final_score) * 100)
                tier = score_to_tier(score_100)
                tier_counts[tier] += 1
                detail = job_details.get(sr.job_id)
                digest_jobs[tier].append({
                    "id": sr.job_id,
                    "title": sr.job_title,
                    "company": sr.job_company,
                    "location": getattr(detail, "location", None),
                    "url": getattr(detail, "canonical_url", None) or getattr(detail, "apply_url", None),
                    "score": round(sr.final_score, 4),
                    "score_100": score_100,
                    "tier": tier,
                })

        event = PipelineCompletedEvent(
            run_id=result.run_id,
            status=result.status,
            stats=notif_stats,
            new_high_match_jobs=preview,
            error=result.error,
            all_jobs_count=notif_stats.personalized,
            tier_counts=tier_counts,
            digest_jobs=digest_jobs,
            total_scored_jobs=len(scored_jobs),
        )
        payload = event.to_dict()

        # A failed run is recorded distinctly from a completed one so
        # #pipeline and #errors (and n8n's own history) each get a correct,
        # independent dedup key.
        pipeline_event_type = "pipeline.completed" if result.status == "completed" else "pipeline.failed"

        storage = self._open_storage()
        try:
            # -----------------------------------------------------------
            # n8n — unchanged behavior (full legacy payload, one message),
            # kept for any other automation built on it.
            # -----------------------------------------------------------
            n8n_provider = N8nProvider(self._config.n8n.webhook_url)
            if n8n_provider.enabled:
                entity_id = result.run_id
                if not storage.already_sent(n8n_provider.name, pipeline_event_type, entity_id):
                    ok = n8n_provider.send(payload)
                    storage.record(
                        provider=n8n_provider.name,
                        event_type=pipeline_event_type,
                        entity_id=entity_id,
                        status="sent" if ok else "failed",
                        error=None if ok else "provider.send() returned False",
                    )
                    if ok:
                        logger.info(
                            "NotificationEngine: %s → n8n (run=%s, preview_jobs=%d, all_jobs=%d)",
                            pipeline_event_type, result.run_id[:8], len(preview), event.all_jobs_count,
                        )
                        for job_sum in preview:
                            if not storage.already_sent(n8n_provider.name, "jobs.high_match", job_sum.job_id):
                                storage.record(
                                    provider=n8n_provider.name,
                                    event_type="jobs.high_match",
                                    entity_id=job_sum.job_id,
                                    status="sent",
                                )
                    else:
                        logger.warning(
                            "NotificationEngine: %s failed → n8n (run=%s)",
                            pipeline_event_type, result.run_id[:8],
                        )

            # -----------------------------------------------------------
            # Discord — explicit per-channel routing. Independent of n8n
            # above: n8n being disabled/unavailable never blocks Discord,
            # and vice versa.
            # -----------------------------------------------------------
            if self._config.discord.enabled:
                if pipeline_event_type == "pipeline.completed":
                    pipeline_payload = {
                        "event": "pipeline.completed",
                        "run_id": result.run_id,
                        "timestamp": event.timestamp,
                        "stats": notif_stats.to_dict(),
                        "previous_stats": self._get_previous_run_stats(result.run_id),
                    }
                    self._dispatch_discord(
                        storage, "pipeline", "pipeline.completed", result.run_id, pipeline_payload
                    )
                else:
                    failed_payload = {
                        "event": "pipeline.failed",
                        "run_id": result.run_id,
                        "timestamp": event.timestamp,
                        "error": result.error,
                    }
                    self._dispatch_discord(
                        storage, "errors", "pipeline.failed", result.run_id, failed_payload
                    )

                if notif_stats.errors:
                    warnings_payload = {
                        "event": "pipeline.warnings",
                        "run_id": result.run_id,
                        "timestamp": event.timestamp,
                        "errors": notif_stats.errors,
                    }
                    self._dispatch_discord(
                        storage, "errors", "pipeline.warnings", result.run_id, warnings_payload
                    )

                # Phase NOW / I16 — source-yield health alerting. Only
                # meaningful for a run that actually completed (a failed
                # run's by_source data may be partial and isn't a fair
                # comparison point either way).
                if pipeline_event_type == "pipeline.completed":
                    try:
                        self._check_source_yield_anomalies(result, storage, event.timestamp)
                    except Exception as exc:  # noqa: BLE001 - matches this class's "never raises" guarantee
                        logger.warning(
                            "NotificationEngine: source-yield health check failed: %s", exc
                        )

                # One batch message per CONFIGURED LOCATION per run — never
                # more than one Discord channel per job (see
                # _route_notification_candidates, which resolves each job to
                # its single highest-priority non-excluded location match).
                #
                # Candidates are drawn from THIS RUN'S FULL scored-job output
                # (scored_jobs/job_details above), not just jobs that are new
                # since the previous run — see _route_notification_candidates'
                # docstring for why "job freshness" and "already sent to this
                # job's destination" must be separate gates: a newly-activated
                # department's matching jobs are almost always pre-existing,
                # not new-to-this-run, and must still be notification-eligible.
                #
                # Per-job dedup uses a single, GLOBAL event_type
                # ("jobs.location_notify") so a job is sent at most once,
                # ever, across every location channel combined — not
                # per-channel like the old high_matches/var_jobs/remote_jobs
                # scheme, which is exactly what allowed the same job to fan
                # out to multiple channels. The batch envelope itself still
                # uses a per-(run, slug) event_type so two different
                # locations' batches in the same run never collide with each
                # other in notification_history.
                job_event_type = "jobs.location_notify"
                routing_candidates = self._merge_notification_candidates(
                    new_jobs, self._backfill_notification_candidates()
                )
                location_buckets, routing_stats = self._route_notification_candidates(
                    storage, routing_candidates, new_jobs
                )

                # Cross-location safety ceiling (2026-09-15 redesign),
                # applied AFTER every per-location cap already enforced
                # inside _route_notification_candidates, using the exact
                # same priority ordering — so N simultaneously-busy
                # departments can never sum to a spammy total in one run.
                # A ceiling, never a target: this only ever trims the
                # lowest-priority overflow, never manufactures a send.
                from src.pipeline.scoring import TIER_RANK

                flat_candidates = [
                    (slug, c)
                    for slug, bucket in location_buckets.items()
                    for c in bucket["jobs"]
                ]
                flat_candidates.sort(key=lambda pair: _candidate_priority_key(pair[1], TIER_RANK))
                kept_job_ids: dict[str, set[str]] = {}
                for slug, c in flat_candidates[: rules.max_total_notifications_per_run]:
                    kept_job_ids.setdefault(slug, set()).add(c["job_id"])
                for slug, bucket in location_buckets.items():
                    bucket["jobs"] = [
                        c for c in bucket["jobs"] if c["job_id"] in kept_job_ids.get(slug, set())
                    ]

                for slug, bucket in location_buckets.items():
                    match: LocationMatch | None = bucket["match"]
                    candidates = bucket["jobs"]
                    if not candidates:
                        continue
                    provider = self._build_location_discord_channel(slug)
                    if provider is None or not provider.enabled:
                        # A location the user configured but hasn't wired a
                        # Discord webhook for yet — never lost: still in the
                        # DB, still in the normal job list, still eligible
                        # for the weekly digest. Just not sent this run.
                        continue
                    batch_payload = self._build_location_batch_payload(
                        match, candidates, result.run_id
                    )
                    # One Discord message holds only so much (issue #66):
                    # send, and record as sent, only the jobs whose complete
                    # block fits in it. The rest are never marked, so they
                    # stay eligible and are offered again on a later run.
                    included, left_out = fit_location_batch(batch_payload)
                    if left_out:
                        logger.warning(
                            "NotificationEngine[%s]: %d of %d job(s) do not fit in one Discord "
                            "message this run; not sent, still eligible later: %s",
                            slug, len(left_out), len(candidates),
                            ", ".join(j["job_id"] for j in left_out),
                        )
                    if not included:
                        continue
                    batch_payload.update(
                        jobs=included, shown_count=len(included), overflow_count=len(left_out),
                    )
                    sent = self._dispatch_discord_to(
                        storage, provider, slug,
                        f"jobs.location_batch:{slug}", result.run_id, batch_payload,
                    )
                    if sent:
                        for j in batch_payload["jobs"]:
                            if not storage.already_sent("discord", job_event_type, j["job_id"]):
                                storage.record(
                                    provider="discord",
                                    event_type=job_event_type,
                                    entity_id=j["job_id"],
                                    status="sent",
                                )
                        included_ids = {j["job_id"] for j in included}
                        backfill_sent_count = sum(
                            1 for c in candidates
                            if c["job_id"] in included_ids and c.get("candidate_type") == "backfill"
                        )
                        if backfill_sent_count:
                            # Observability (see storage.record_backfill_sent
                            # docstring) — cumulative, independent of the
                            # lifecycle-completion decision just below.
                            storage.record_backfill_sent(slug, backfill_sent_count)

                        # Lifecycle completion is decided HERE — after the
                        # batch dispatch's actual success/failure is known,
                        # and using the POST-global-cap candidates list —
                        # never inside _route_notification_candidates, which
                        # runs before either of those is known. raw_backfill
                        # is this slug's "backfill"-typed candidate count
                        # BEFORE any capping (per-type subcap, per-location
                        # cap, or this run's cross-location global cap);
                        # backfill_sent_count is how many of THOSE actually
                        # survived every cap, fitted in the message, AND were
                        # just dispatched successfully. Only when they're equal (and
                        # nonzero) has the entire currently-known backlog
                        # genuinely been delivered — if the global cap
                        # trimmed some out, or the send failed (sent is
                        # False, so this whole block is skipped and
                        # backfill_sent_count is never computed), the
                        # department must stay in "backfill" mode so the
                        # remainder is retried on a later run rather than
                        # being silently dropped or reclassified too early.
                        raw_backfill = routing_stats.get(slug, {}).get("by_type", {}).get("backfill", 0)
                        if raw_backfill and backfill_sent_count == raw_backfill:
                            storage.mark_initial_backfill_complete(slug)

                # Top10 leaderboard channels — independent of the
                # new-arrivals feed above (no dedup, no reliance on
                # new_jobs): re-ranks ALL currently active jobs per
                # location and edits each location's one standing message
                # in place. Same placement/level as the batch loop above —
                # runs whenever Discord is enabled, on every run.
                try:
                    self._update_top10_leaderboards()
                except Exception as exc:  # noqa: BLE001 - matches this class's "never raises" guarantee
                    logger.warning("NotificationEngine: top10 leaderboard update failed: %s", exc)

                # #statistics channel — same "one standing message, edited
                # every run" convention as the top10 leaderboards, but a
                # single database-wide snapshot rather than per-location.
                try:
                    self._update_job_statistics()
                except Exception as exc:  # noqa: BLE001 - matches this class's "never raises" guarantee
                    logger.warning("NotificationEngine: statistics update failed: %s", exc)
        finally:
            storage.close()
