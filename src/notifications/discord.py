"""Discord webhook notification provider.

Sends structured embed messages to a Discord channel via an incoming
webhook URL.  Failures are logged and returned as False — they never
propagate to the caller and cannot fail the pipeline.

This class is channel-agnostic transport + embed formatting: one instance
targets one webhook.  Multi-channel routing (which channel gets which
event) is decided by NotificationEngine — see
src/notifications/config.py::DISCORD_STATIC_CHANNEL_ENV_VARS (operational
channels) and DiscordConfig.location_channels (dynamic, one per configured
department/remote preference) for the channel/env var mapping, and
src/notifications/engine.py::_build_discord_channel()/
_build_location_discord_channel() for how one instance is constructed per
channel.

Webhook URL precedence (first non-empty wins):
  1. `env_var` environment variable (defaults to DISCORD_WEBHOOK_URL)
  2. `webhook_url` constructor argument
  3. `fallback_env_var` environment variable, if given
  4. `fallback_webhook_url` constructor argument, if given

The fallback pair exists purely for backward compatibility: a deployment
that only ever configured the single legacy DISCORD_WEBHOOK_URL keeps
working (every channel resolves to that one webhook) until per-channel
webhooks are configured.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import httpx

from src.notifications.http_retry import send_with_retry

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0         # seconds
# Discord embed limits (https://discord.com/developers/docs/resources/message#embed-object-embed-limits).
_MAX_FIELD_LEN = 1024   # one field value
_MAX_EMBED_LEN = 6000   # title + description + field names/values + footer, all embeds combined
_MAX_EMBED_FIELDS = 25  # fields per embed
_MAX_DESCRIPTION_LEN = 4096  # one embed description

_JOBS_FIELD_NAME = "Jobs"
_JOBS_CONTINUATION_FIELD_NAME = "\u200b"  # renders blank: continuation fields read as one list

# Discord colour integers (decimal)
_COLOR_GREEN = 0x2ECC71
_COLOR_RED = 0xE74C3C
_COLOR_YELLOW = 0xF1C40F
_COLOR_LOCATION_DEPARTMENT = 0x3498DB
_COLOR_LOCATION_REMOTE = 0x1ABC9C

# Tier emoji — mirrors the palette the retired n8n workflow used, kept for
# visual continuity. Tier labels always come from
# src.pipeline.scoring.score_to_tier(); this module never re-derives them.
_TIER_EMOJI: dict[str, str] = {
    "exceptional": "🔥",
    "excellent": "⭐",
    "strong": "👍",
    "relevant": "📋",
    "low_priority": "🗄",
}


@lru_cache(maxsize=1)
def _tier_legend() -> str:
    """Compact "Tiers: 🔥80+ ⭐72+ 👍65+ 📋60+" legend, DERIVED from the
    canonical thresholds (src.pipeline.scoring.TIER_THRESHOLDS) rather than
    a hand-typed string — a future recalibration of those thresholds can
    no longer leave this legend silently out of sync (2026-09-12 cleanup;
    previously a hand-maintained duplicate of the same 4 numbers).

    Deferred import, cached after first call: src.pipeline's package
    __init__ eagerly imports sentence-transformers/torch (see
    src/notifications/events.py's JobSummary.to_dict for the same concern)
    — this module must not force that cost onto every import of
    src.notifications.discord, only the first Discord send that actually
    needs this string.
    """
    from src.pipeline.scoring import TIER_THRESHOLDS

    parts = [f"{_TIER_EMOJI.get(tier, '')}{threshold}+" for threshold, tier in TIER_THRESHOLDS]
    return "Tiers: " + " ".join(parts)


def _work_mode_label(remote: bool, work_mode: str | None) -> str:
    """Human-readable work mode from the same fields explain.py/n8n used."""
    if remote:
        return "Remote"
    if work_mode == "hybrid":
        return "Hybrid"
    if work_mode == "on_site":
        return "On-site"
    if work_mode:
        return work_mode.replace("_", " ").title()
    return "Unknown"


def _format_duration(seconds: float) -> str:
    """Render a duration in seconds as a compact "Xm Ys" / "Zs" string."""
    if not seconds:
        return "–"
    total = int(round(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _discord_len(text: str) -> int:
    """Length for Discord's limits, counted in UTF-16 code units — never less
    than what Discord counts (an emoji such as 🏠 counts 2 here), so staying
    under a limit by this measure is always safe."""
    return len(text.encode("utf-16-le")) // 2


def _location_batch_item(j: dict[str, Any], *, is_remote_channel: bool) -> str:
    """One compact item block shared by #var-jobs and #remote-jobs.

    Only fields that actually exist on the job are rendered — no field
    (contract type, location, remote status, ...) is ever invented.
    """
    tier = j.get("tier") or "relevant"
    emoji = _TIER_EMOJI.get(tier, "📋")
    score_100 = j.get("score_100", 0)
    title = j.get("title") or "Unknown Position"
    company = j.get("company") or "Unknown"
    location = j.get("location")
    url = j.get("canonical_url") or j.get("apply_url")

    header = f"{emoji} **{score_100}** · {title}"
    subline = company
    if is_remote_channel and "France" in (j.get("countries") or []):
        subline += " · 🇫🇷 France"
    elif location:
        subline += f" · {location}"

    mode = _work_mode_label(bool(j.get("remote")), j.get("work_mode"))
    mode_icon = {"Remote": "🏠", "Hybrid": "🔀", "On-site": "🏢"}.get(mode, "❔")
    mode_text = "Fully Remote" if is_remote_channel else mode

    lines = [header, subline, f"{mode_icon} {mode_text}"]
    employment_type = j.get("employment_type")
    if employment_type:
        lines.append(f"💼 {employment_type}")
    if url:
        lines.append(f"[Apply]({url})")
    return "\n".join(lines)


def _location_batch_title(event: dict[str, Any]) -> str:
    is_remote = event.get("location_type") == "remote"
    label = event.get("location_label") or ("Remote" if is_remote else "Location")
    icon = "🌍" if is_remote else "📍"
    return f"{icon} BEST JOBS — {label.upper()}"


def _location_batch_description(total: int, shown: int, overflow: int) -> str:
    text = f"**{total} new relevant job(s) this run**"
    if overflow > 0:
        text += f"\n*Showing {shown} of {total} — {overflow} more did not fit in this message*"
    return text


def _location_batch_footer(event: dict[str, Any]) -> str:
    return f"Run: {str(event.get('run_id', ''))[:8]}… • {_tier_legend()}"


def _pack_job_blocks(blocks: list[str], budget: int) -> tuple[list[dict[str, Any]], list[int]]:
    """Pack whole job blocks, in order, into embed fields of at most
    _MAX_FIELD_LEN, spending at most ``budget`` characters on field names and
    values. A block is never split: one that does not fit is left out and
    packing continues with the next one.

    Returns (fields, indexes of the blocks included). Whether a block joins
    the current field or opens a new one depends only on the blocks already
    placed and the field limit, never on ``budget``, so packing just the
    included blocks again with at least the same budget gives the same fields.
    """
    fields: list[dict[str, Any]] = []
    included: list[int] = []
    used = 0
    for index, block in enumerate(blocks):
        size = _discord_len(block)
        if size > _MAX_FIELD_LEN:
            continue
        if fields and _discord_len(fields[-1]["value"]) + 2 + size <= _MAX_FIELD_LEN:
            if used + 2 + size > budget:
                continue
            fields[-1]["value"] += "\n\n" + block
            used += 2 + size
        else:
            name = _JOBS_CONTINUATION_FIELD_NAME if fields else _JOBS_FIELD_NAME
            cost = _discord_len(name) + size
            if len(fields) >= _MAX_EMBED_FIELDS or used + cost > budget:
                continue
            fields.append({"name": name, "value": block, "inline": False})
            used += cost
        included.append(index)
    return fields, included


def fit_location_batch(event: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a ``jobs.location_notify`` event's jobs into (included, left_out):
    the jobs whose complete block fits in ONE Discord message, in priority
    order, and those that do not.

    The engine sends only ``included`` (and records only those as sent);
    ``left_out`` stay eligible for a later run. Room is reserved for the
    largest "N more" note the message can carry, so the included jobs
    always fit once the final counts are known.
    """
    jobs = list(event.get("jobs") or [])
    is_remote = event.get("location_type") == "remote"
    n = len(jobs)
    budget = _MAX_EMBED_LEN - sum(map(_discord_len, (
        _location_batch_title(event),
        _location_batch_description(n, n, n),
        _location_batch_footer(event),
    )))
    blocks = [_location_batch_item(j, is_remote_channel=is_remote) for j in jobs]
    _fields, included = _pack_job_blocks(blocks, budget)
    kept = set(included)
    return [jobs[i] for i in included], [j for i, j in enumerate(jobs) if i not in kept]


def _join_whole_entries(blocks: list[str], limit: int = _MAX_DESCRIPTION_LEN) -> str:
    """Join ranked entries with blank lines within ``limit``, never cutting an
    entry: when they do not all fit, the leading ranks are kept and a closing
    note says how many are not shown."""
    text = "\n\n".join(blocks)
    if _discord_len(text) <= limit:
        return text

    def note(hidden: int) -> str:
        return f"*{hidden} more not shown (Discord length limit)*"

    room = limit - _discord_len("\n\n" + note(len(blocks)))
    shown: list[str] = []
    for block in blocks:
        if _discord_len("\n\n".join([*shown, block])) > room:
            break
        shown.append(block)
    return "\n\n".join([*shown, note(len(blocks) - len(shown))])


def _format_time(timestamp: Any) -> str:
    """Render an ISO-8601 timestamp as a compact "HH:MM" string, or "–"."""
    if not timestamp:
        return "–"
    try:
        from datetime import datetime
        ts = str(timestamp).replace("Z", "+00:00")
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (ValueError, TypeError):
        return "–"


class DiscordProvider:
    """Send notifications to Discord via an incoming webhook."""

    def __init__(
        self,
        webhook_url: str = "",
        env_var: str = "DISCORD_WEBHOOK_URL",
        fallback_env_var: str = "",
        fallback_webhook_url: str = "",
    ) -> None:
        self._webhook_url = (
            os.environ.get(env_var, "")
            or webhook_url
            or (os.environ.get(fallback_env_var, "") if fallback_env_var else "")
            or fallback_webhook_url
        )

    # -- NotificationProvider interface --------------------------------------

    @property
    def name(self) -> str:
        return "discord"

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def send(self, payload: dict[str, Any]) -> bool:
        """Send an event payload as a Discord embed.

        Returns True on HTTP 2xx, False on any error.
        """
        if not self._webhook_url:
            logger.debug("Discord provider: no webhook URL configured, skipping.")
            return False
        try:
            discord_payload = self._build_embed(payload)
            resp = send_with_retry(lambda: httpx.post(
                self._webhook_url,
                json=discord_payload,
                timeout=_TIMEOUT,
            ))
            resp.raise_for_status()
            logger.debug("Discord: sent event '%s'", payload.get("event"))
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Discord webhook returned HTTP %d: %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("Discord notification failed: %s", exc)
            return False

    # -- payload builders ----------------------------------------------------

    def _build_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = event.get("event", "unknown")
        if event_type == "pipeline.completed":
            return self._pipeline_completed_embed(event)
        if event_type == "pipeline.failed":
            return self._pipeline_failed_embed(event)
        if event_type == "pipeline.warnings":
            return self._pipeline_warnings_embed(event)
        if event_type == "source.yield_anomaly":
            return self._source_yield_anomaly_embed(event)
        if event_type == "jobs.location_notify":
            return self._location_notify_embed(event)
        if event_type == "jobs.top10_leaderboard":
            return self._top10_leaderboard_embed(event)
        if event_type == "jobs.statistics":
            return self._statistics_embed(event)
        if event_type == "system.status":
            return self._system_status_embed(event)
        # Generic fallback
        return {
            "content": f"**{event_type}** (v{event.get('version', 1)})",
            "embeds": [{
                "description": f"```json\n{str(event)[:500]}\n```",
                "color": _COLOR_YELLOW,
            }],
        }

    def _pipeline_completed_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for the #pipeline channel — successful runs only.

        No per-stage health grid: the pipeline only tracks aggregate counts
        and a flat error list, not a pass/fail signal per stage, so a row of
        green checkmarks would be decorative rather than derived from real
        data. What IS shown: the funnel, optional deltas vs. the previous
        completed run (when available), and a warning *count* (never a
        fabricated zero) pointing at #errors for detail.
        """
        stats = event.get("stats", {})
        previous = event.get("previous_stats")
        run_id = str(event.get("run_id", ""))[:8]
        dur_str = _format_duration(stats.get("duration_seconds", 0.0))

        def _with_delta(label: str, key: str) -> str:
            value = stats.get(key, 0)
            line = f"{label} **{value:,}**"
            if previous is not None and previous.get(key) is not None:
                diff = value - previous[key]
                if diff != 0:
                    sign = "+" if diff > 0 else ""
                    line += f" ({sign}{diff:,})"
            return line

        funnel = "\n".join([
            _with_delta("Collected", "collected"),
            _with_delta("Validated", "validated"),
            _with_delta("Deduplicated", "deduplicated"),
            _with_delta("Filtered", "filtered"),
            _with_delta("Scored", "scored"),
            _with_delta("Personalized", "personalized"),
        ])

        fields: list[dict[str, Any]] = [
            {"name": "Pipeline funnel", "value": funnel[:_MAX_FIELD_LEN], "inline": False},
            {"name": "Duration", "value": dur_str, "inline": True},
            {"name": "Run", "value": f"`{run_id}…`", "inline": True},
        ]

        errors = stats.get("errors") or []
        if errors:
            fields.append({
                "name": "Warnings",
                "value": f"⚠️ {len(errors)} warning(s) — see #errors",
                "inline": True,
            })
        fields.append({
            "name": "Status",
            "value": "🟢 Healthy" if not errors else "🟡 Completed with warnings",
            "inline": True,
        })

        return {
            "embeds": [{
                "title": "🚀 Pipeline Completed",
                "color": _COLOR_GREEN if not errors else _COLOR_YELLOW,
                "fields": fields,
                "footer": {"text": f"Run: {run_id}… • {_tier_legend()}"},
                "timestamp": event.get("timestamp"),
            }]
        }

    def _pipeline_failed_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for the #errors channel — a run that failed outright.

        No traceback: `error` is the top-level exception message, truncated.
        Retry is stated as a plain fact about the current system (the
        pipeline itself has no automatic retry) rather than guessed per
        source/stage from the error text.
        """
        run_id = str(event.get("run_id", ""))[:8]
        error = event.get("error") or "Pipeline run failed without error details."

        fields: list[dict[str, Any]] = [
            {"name": "Run", "value": f"`{run_id}…`", "inline": True},
            {"name": "🕐 Time", "value": _format_time(event.get("timestamp")), "inline": True},
            {"name": "Status", "value": "🔴 Failed", "inline": True},
            {"name": "🔁 Retry", "value": "Manual — no automatic pipeline retry is configured", "inline": True},
            {"name": "Error", "value": f"```{str(error)[:300]}```", "inline": False},
        ]

        return {
            "embeds": [{
                "title": "🚨 CRITICAL — Pipeline Failed",
                "color": _COLOR_RED,
                "fields": fields,
                "timestamp": event.get("timestamp"),
            }]
        }

    def _pipeline_warnings_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for the #errors channel — a run that completed but logged errors.

        Only ever built when `errors` is non-empty (the engine gates this;
        this method does not send an empty-warnings embed either).
        """
        errors = event.get("errors") or []
        run_id = str(event.get("run_id", ""))[:8]
        text = "\n".join(f"• {e}" for e in errors[:5])[:_MAX_FIELD_LEN] or "—"

        return {
            "embeds": [{
                "title": f"⚠️ WARNING — {len(errors)} issue(s) during a completed run",
                "color": _COLOR_YELLOW,
                "fields": [
                    {"name": "Details", "value": text, "inline": False},
                    {"name": "Run", "value": f"`{run_id}…`", "inline": True},
                    {"name": "Pipeline", "value": "🟢 Completed", "inline": True},
                    {"name": "Warnings", "value": f"🟡 {len(errors)}", "inline": True},
                ],
                "timestamp": event.get("timestamp"),
            }]
        }

    def _source_yield_anomaly_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for the #errors channel — Phase NOW / I16: a source's job
        yield dropped unexpectedly this run (see
        src.notifications.source_health.detect_source_yield_anomalies).

        Only ever built for NEWLY-detected anomalies (see engine.py's
        caller) — a source still broken from a prior run does not get a
        fresh embed every run.
        """
        source = str(event.get("source", "unknown"))
        severity = event.get("severity", "drop")
        current_yield = event.get("current_yield", 0)
        baseline_yield = event.get("baseline_yield", 0)
        baseline_run_count = event.get("baseline_run_count", 0)
        run_id = str(event.get("run_id", ""))[:8]

        if severity == "zero":
            title = f"🚨 Source produced ZERO jobs: {source}"
            color = _COLOR_RED
        else:
            title = f"⚠️ Source yield dropped sharply: {source}"
            color = _COLOR_YELLOW

        return {
            "embeds": [{
                "title": title,
                "color": color,
                "fields": [
                    {"name": "Source", "value": f"`{source}`", "inline": True},
                    {"name": "This run", "value": f"**{current_yield:,}** job(s)", "inline": True},
                    {
                        "name": f"Recent baseline (last {baseline_run_count} run(s))",
                        "value": f"~{baseline_yield:,.1f} job(s)",
                        "inline": True,
                    },
                    {"name": "Run", "value": f"`{run_id}…`", "inline": True},
                    {
                        "name": "Possible cause",
                        "value": (
                            "An API key or credential may have expired, or the "
                            "source may have changed its site/API — check this "
                            "source's collector logs for this run."
                        ),
                        "inline": False,
                    },
                ],
                "timestamp": event.get("timestamp"),
            }]
        }

    def _location_notify_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for a per-location Discord channel (e.g. #jobs-var,
        #jobs-remote) — best new jobs for ONE configured department/remote
        preference, capped per rules.max_notifications_per_location_per_run.

        The location itself (label/type) comes entirely from the event
        payload, built by NotificationEngine from the resolved
        location_preferences match — nothing here is hardcoded to a
        specific department or to remote specifically, so any number of
        configured locations render through this single builder.

        One message per channel per run — never one Discord message per job.
        The job blocks are spread over as many fields as needed, whole (issue
        #66). This builder never drops or cuts a job: if the jobs do not all
        fit (the caller did not apply fit_location_batch() first), it raises,
        so send() fails and nothing is recorded as sent.
        """
        is_remote = event.get("location_type") == "remote"
        jobs = event.get("jobs") or []
        total = event.get("total_count", len(jobs))
        shown = event.get("shown_count", len(jobs))
        overflow = event.get("overflow_count", max(0, total - len(jobs)))

        title = _location_batch_title(event)
        description = _location_batch_description(total, shown, overflow)
        footer = _location_batch_footer(event)
        budget = _MAX_EMBED_LEN - sum(map(_discord_len, (title, description, footer)))
        blocks = [_location_batch_item(j, is_remote_channel=is_remote) for j in jobs]
        fields, included = _pack_job_blocks(blocks, budget)
        if len(included) != len(blocks):
            raise ValueError(
                f"{len(blocks) - len(included)} of {len(blocks)} job(s) do not fit in one "
                "Discord message; fit_location_batch() must select the jobs first"
            )

        return {
            "embeds": [{
                "title": title,
                "description": description,
                "color": _COLOR_LOCATION_REMOTE if is_remote else _COLOR_LOCATION_DEPARTMENT,
                "fields": fields or [{"name": _JOBS_FIELD_NAME, "value": "—", "inline": False}],
                "footer": {"text": footer},
            }]
        }

    def _top10_leaderboard_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for a location's top-10 leaderboard channel (e.g.
        #top10-83) — this is the ONE message that channel ever has; every
        pipeline run re-renders it in place via send_or_update() rather
        than posting a new message, so this builder must be a complete,
        self-contained snapshot (no "N more" delta language — that belongs
        to _location_notify_embed's new-arrivals feed, not this standing
        ranking).

        ``event["entries"]`` — see src.notifications.top10_query's output
        shape: rank, title, company, location, remote, final_score,
        score_100, canonical_url, apply_url.
        """
        is_remote = event.get("location_type") == "remote"
        label = event.get("location_label") or ("Remote" if is_remote else "Location")
        icon = "🌍" if is_remote else "📍"
        entries = event.get("entries") or []

        lines: list[str] = []
        for entry in entries:
            rank = entry.get("rank", 0)
            score_100 = entry.get("score_100", 0)
            title = entry.get("title") or "Unknown Position"
            company = entry.get("company") or "Unknown"
            location = entry.get("location")
            url = entry.get("canonical_url") or entry.get("apply_url")
            tier = entry.get("tier") or "relevant"
            emoji = _TIER_EMOJI.get(tier, "📋")

            block = f"**{rank}. {emoji} {score_100}** — {title}\n🏢 {company}"
            if not is_remote and location:
                block += f" · 📍 {location}"
            if url:
                block += f"\n[Apply]({url})"
            lines.append(block)

        description = _join_whole_entries(lines) if lines else "*No active jobs currently match this location.*"
        return {
            "embeds": [{
                "title": f"{icon} TOP 10 — {label.upper()}",
                "description": description,
                "color": _COLOR_LOCATION_REMOTE if is_remote else _COLOR_LOCATION_DEPARTMENT,
                "footer": {"text": f"Updated every pipeline run • {_tier_legend()}"},
            }]
        }

    def _statistics_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for the #statistics channel — a standing snapshot of the
        job database (not one pipeline run), edited in place every run via
        send_or_update(), same "one message" convention as
        _top10_leaderboard_embed. Content comes from
        src.api.routes.jobs.compute_job_stats() — no numbers computed here,
        this method only formats what NotificationEngine already fetched.
        """
        total_jobs = event.get("total_jobs", 0)
        top_skills = event.get("top_skills") or []
        score_tiers = event.get("score_tiers") or []
        title_categories = event.get("title_categories") or []

        skills_text = ", ".join(f"{name} ({count})" for name, count in top_skills[:10]) or "—"

        # Same 5 tiers/order as src.pipeline.scoring's _TIER_THRESHOLDS
        # (best first) — see _TIER_EMOJI above for the shared vocabulary.
        tier_order = ["exceptional", "excellent", "strong", "relevant", "low_priority"]
        tiers_by_name = {t: c for t, c in score_tiers}
        tiers_text = "\n".join(
            f"{_TIER_EMOJI.get(t, '📋')} {t.replace('_', ' ').capitalize()}: **{tiers_by_name[t]:,}**"
            for t in tier_order
            if t in tiers_by_name
        ) or "—"

        categories_text = "\n".join(
            f"• {name}: **{count:,}**" for name, count in title_categories[:8]
        ) or "—"

        fields: list[dict[str, Any]] = [
            {"name": "Total active jobs", "value": f"**{total_jobs:,}**", "inline": True},
            {"name": "Score tiers", "value": tiers_text[:_MAX_FIELD_LEN], "inline": True},
            {"name": "Top skills", "value": skills_text[:_MAX_FIELD_LEN], "inline": False},
            {"name": "Job categories", "value": categories_text[:_MAX_FIELD_LEN], "inline": False},
        ]

        return {
            "embeds": [{
                "title": "📊 Job Statistics",
                "color": _COLOR_GREEN,
                "fields": fields,
                "footer": {"text": "Updated every pipeline run"},
            }]
        }

    def _system_status_embed(self, event: dict[str, Any]) -> dict[str, Any]:
        """Embed for the #system-status standing message — the ONE message
        this channel ever has, re-rendered in place every heartbeat via
        send_or_update() (see NotificationEngine.update_system_status /
        SystemStatusWorker). A self-contained snapshot of backend/database/
        pipeline/n8n/Discord-configuration health plus when it was last
        checked, so a glance at this one Discord channel from a phone
        answers "is my local job-search system alive and usable right now".
        """
        icons = {
            "ONLINE": "🟢", "READY": "🟢", "healthy": "🟢",
            "RUNNING": "🔵",
            "DEGRADED": "🟡", "degraded": "🟡",
            "OFFLINE": "🔴", "FAILED": "🔴",
            "UNKNOWN": "⚪",
        }
        backend = event.get("backend", "UNKNOWN")
        database = event.get("database", "UNKNOWN")
        pipeline = event.get("pipeline", "UNKNOWN")
        n8n = event.get("n8n", "UNKNOWN")
        discord_cfg = event.get("discord", "unknown")
        missing_channels = event.get("missing_channels") or []
        checked_at = _format_time(event.get("checked_at"))

        fields: list[dict[str, Any]] = [
            {"name": "Backend", "value": f"{icons.get(backend, '⚪')} {backend}", "inline": True},
            {"name": "Database", "value": f"{icons.get(database, '⚪')} {database}", "inline": True},
            {"name": "Pipeline", "value": f"{icons.get(pipeline, '⚪')} {pipeline}", "inline": True},
            {"name": "n8n", "value": f"{icons.get(n8n, '⚪')} {n8n}", "inline": True},
            {
                "name": "Discord config",
                "value": f"{icons.get(discord_cfg, '⚪')} {str(discord_cfg).capitalize()}",
                "inline": True,
            },
        ]
        if missing_channels:
            names = ", ".join(f"{m['kind']}:{m['name']}" for m in missing_channels[:10])
            fields.append({
                "name": "Unconfigured channels",
                "value": names[:_MAX_FIELD_LEN],
                "inline": False,
            })

        overall_bad = database == "DEGRADED" or pipeline == "FAILED" or n8n == "OFFLINE"
        overall_warn = discord_cfg == "degraded"
        color = _COLOR_RED if overall_bad else (_COLOR_YELLOW if overall_warn else _COLOR_GREEN)

        return {
            "embeds": [{
                "title": "🩺 System Status",
                "color": color,
                "fields": fields,
                "footer": {"text": f"Last checked {checked_at} • updated every heartbeat"},
            }]
        }

    def send_or_update(self, payload: dict[str, Any], message_id: str) -> str | None:
        """Like send(), but creates-or-edits ONE standing message instead of
        always posting a new one — used only for the top10 leaderboard
        channels (see src/notifications/top10_query.py and
        NotificationEngine's leaderboard-update path). Never used for the
        new-arrivals feed, pipeline/errors channels, which stay
        fire-and-forget via send().

        Returns the message id to persist for the next call (unchanged when
        editing, a fresh one when creating or when the previous message was
        deleted out from under it), or None on failure. Never raises.
        """
        if not self._webhook_url:
            logger.debug("Discord provider: no webhook URL configured, skipping.")
            return None
        discord_payload = self._build_embed(payload)
        try:
            if message_id:
                resp = send_with_retry(lambda: httpx.patch(
                    f"{self._webhook_url}/messages/{message_id}",
                    json=discord_payload,
                    timeout=_TIMEOUT,
                ))
                if resp.status_code == 404:
                    # The message was deleted (manually, or the channel was
                    # recreated) — fall through to create a fresh one rather
                    # than failing outright.
                    message_id = ""
                else:
                    resp.raise_for_status()
                    return message_id

            resp = send_with_retry(lambda: httpx.post(
                f"{self._webhook_url}?wait=true",
                json=discord_payload,
                timeout=_TIMEOUT,
            ))
            resp.raise_for_status()
            try:
                return str(resp.json()["id"])
            except Exception as parse_exc:
                # The message genuinely posted (2xx) but we can't recover its
                # id from a malformed response body, so there's no id to
                # persist — the next successful update will create a second
                # standing message instead of editing this one. Logged at
                # warning (not silently folded into the generic except below)
                # so an operator can see the orphan risk instead of it
                # looking identical to an ordinary send failure.
                logger.warning(
                    "Discord send_or_update: message posted (HTTP %d) but its "
                    "id could not be parsed from the response body (%s) — a "
                    "duplicate standing message will be created on the next "
                    "update since there is no id left to edit.",
                    resp.status_code, parse_exc,
                )
                return None
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Discord send_or_update returned HTTP %d: %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return None
        except Exception as exc:
            logger.warning("Discord send_or_update failed: %s", exc)
            return None
