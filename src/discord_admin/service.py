"""One full reconciliation pass — load profile + registries, diff, reconcile
both channel kinds (job-feed "job-83" and leaderboard "top10-83").

This is the shared orchestration BOTH the automatic backend worker
(src.notifications.discord_reconciler_worker) and Claude's on-demand MCP
tool (src.discord_admin.mcp_server) call. Before this module existed, that
"read profile, read config, build plan, reconcile" sequence risked being
written twice — once for the worker's tick, once for the MCP server's
reconcile_now tool. Factoring it here keeps it a single implementation,
consistent with the rest of src.discord_admin.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.discord_admin.client import DiscordAdminClient
from src.discord_admin.lock import ReconciliationInProgressError
from src.discord_admin.reconciler import SlugOutcome, reconcile, reconcile_static, reconcile_top10
from src.notifications.config import ReconciliationIssue, load_notification_config
from src.notifications.discord_reconciliation import (
    ReconciliationPlan,
    build_reconciliation_plan,
    build_static_reconciliation_plan,
    build_top10_reconciliation_plan,
)
from src.pipeline.filter import load_profile

logger = logging.getLogger(__name__)

BOT_TOKEN_ENV_VAR = "DISCORD_BOT_TOKEN"
GUILD_ID_ENV_VAR = "DISCORD_GUILD_ID"

# Status values, shared vocabulary for both callers:
# "no_credentials" | "no_changes" | "reconciled" | "partial" | "locked" | "error"
# "partial" means one channel kind reconciled but the other hit an error or
# lock contention — see run_reconciliation_pass's docstring.


def _needs_reconciliation(plan: ReconciliationPlan) -> bool:
    """Whether ``plan`` justifies calling into src.discord_admin.reconciler
    at all: either there's something to activate/deactivate, or there are
    2+ desired active locations whose relative Discord order could have
    drifted from the profile since the last pass (see
    src.discord_admin.reconciler._reorder_active_channels — ordering is
    meaningless with fewer than two channels, so skip the redundant
    Discord round trip in that case, same threshold that function itself
    uses to no-op).
    """
    return not plan.is_empty or len(plan.desired_order) >= 2


@dataclass
class ReconciliationRunReport:
    status: str
    plan_invalid: list[str] = field(default_factory=list)
    outcomes: list[SlugOutcome] = field(default_factory=list)
    issues: list[ReconciliationIssue] = field(default_factory=list)
    error: str | None = None


def run_reconciliation_pass(
    config_dir: str,
    profile_path: str,
    *,
    lock_path: Path | None = None,
    env_path: Path | None = None,
    client_factory: Callable[[], DiscordAdminClient] | None = None,
) -> ReconciliationRunReport:
    """Run exactly one reconciliation pass for BOTH channel kinds and
    report what happened.

    Job-feed channels are reconciled first, then top10 leaderboard
    channels, sharing one Discord client. Each kind's reconciliation is
    independent — a lock contention or error on one never prevents the
    other from proceeding, since they write to separate registry sections.
    ``status`` reflects the combined outcome: "reconciled" only if
    everything that needed action succeeded; "partial" if one kind
    succeeded and the other was locked/errored; "locked"/"error" if BOTH
    were locked/errored (in that priority — locked is preferred purely for
    a friendlier message, since it means "try again shortly" rather than
    "something is broken").

    Never raises: every failure mode (missing credentials, malformed
    profile, lock contention, a Discord API error) is captured in the
    returned report's ``status``/``error`` instead, so a caller (a
    background loop, or an MCP tool handler) never has to guess which
    exceptions are "expected."
    """
    bot_token = os.environ.get(BOT_TOKEN_ENV_VAR)
    guild_id = os.environ.get(GUILD_ID_ENV_VAR)
    if not bot_token or not guild_id:
        logger.debug(
            "discord_admin.service: %s/%s not configured — nothing to do.",
            BOT_TOKEN_ENV_VAR,
            GUILD_ID_ENV_VAR,
        )
        return ReconciliationRunReport(status="no_credentials")

    try:
        profile = load_profile(profile_path)
    except Exception as exc:  # noqa: BLE001 - malformed/missing profile must not raise here
        logger.warning(
            "discord_admin.service: could not load profile from %s: %s", profile_path, exc
        )
        return ReconciliationRunReport(status="error", error=str(exc))

    config = load_notification_config(config_dir)
    job_plan = build_reconciliation_plan(profile, config.discord.location_channels)
    top10_plan = build_top10_reconciliation_plan(profile, config.discord.top10_channels)
    static_plan = build_static_reconciliation_plan(
        config.discord.permanent_channels, config.discord.channels
    )

    all_invalid = list(dict.fromkeys(job_plan.invalid + top10_plan.invalid + static_plan.invalid))
    for note in all_invalid:
        logger.warning("discord_admin.service: %s", note)

    if not _needs_reconciliation(job_plan) and not _needs_reconciliation(top10_plan) and static_plan.is_empty:
        return ReconciliationRunReport(status="no_changes", plan_invalid=all_invalid)

    client = None
    outcomes: list[SlugOutcome] = []
    issues: list[ReconciliationIssue] = []
    job_status: str | None = None
    top10_status: str | None = None
    static_status: str | None = None
    error_detail: str | None = None

    try:
        client = client_factory() if client_factory else DiscordAdminClient(bot_token, guild_id)

        if _needs_reconciliation(job_plan):
            try:
                job_result = reconcile(
                    job_plan,
                    dict(config.discord.location_channels),
                    client,
                    location_category_id=config.discord.location_category_id,
                    inactive_category_id=config.discord.inactive_category_id,
                    config_dir=config_dir,
                    lock_path=lock_path,
                    env_path=env_path,
                )
                outcomes.extend(job_result.outcomes)
                issues.extend(job_result.issues)
                job_status = "reconciled"
            except ReconciliationInProgressError as exc:
                logger.info("discord_admin.service: job-channel pass — %s — will retry later.", exc)
                job_status = "locked"
            except Exception as exc:  # noqa: BLE001 - top10 must still get a chance below
                logger.exception("discord_admin.service: job-channel reconciliation pass failed")
                job_status = "error"
                error_detail = str(exc)

        if _needs_reconciliation(top10_plan):
            try:
                top10_result = reconcile_top10(
                    top10_plan,
                    dict(config.discord.top10_channels),
                    client,
                    location_category_id=config.discord.top10_category_id,
                    inactive_category_id=config.discord.inactive_category_id,
                    config_dir=config_dir,
                    lock_path=lock_path,
                    env_path=env_path,
                    carry_forward_issues=issues,
                )
                outcomes.extend(top10_result.outcomes)
                # top10_result.issues holds only issues NEW to this top10
                # pass (reconcile_top10's returned ReconciliationResult, as
                # opposed to what it persists to disk, which additionally
                # includes carry_forward_issues) — safe to extend directly
                # without duplicating the job issues already added above.
                issues.extend(top10_result.issues)
                top10_status = "reconciled"
            except ReconciliationInProgressError as exc:
                logger.info("discord_admin.service: top10 pass — %s — will retry later.", exc)
                top10_status = "locked"
            except Exception as exc:  # noqa: BLE001
                logger.exception("discord_admin.service: top10 reconciliation pass failed")
                top10_status = "error"
                error_detail = error_detail or str(exc)

        if not static_plan.is_empty:
            try:
                static_result = reconcile_static(
                    static_plan,
                    dict(config.discord.channels),
                    client,
                    location_category_id=config.discord.location_category_id,
                    config_dir=config_dir,
                    lock_path=lock_path,
                    env_path=env_path,
                    carry_forward_issues=issues,
                )
                outcomes.extend(static_result.outcomes)
                issues.extend(static_result.issues)
                static_status = "reconciled"
            except ReconciliationInProgressError as exc:
                logger.info("discord_admin.service: static-channel pass — %s — will retry later.", exc)
                static_status = "locked"
            except Exception as exc:  # noqa: BLE001
                logger.exception("discord_admin.service: static-channel reconciliation pass failed")
                static_status = "error"
                error_detail = error_detail or str(exc)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    # At least one of job_status/top10_status/static_status is non-None here
    # (the all-empty case already returned "no_changes" above).
    active_statuses = [s for s in (job_status, top10_status, static_status) if s is not None]
    if all(s == "reconciled" for s in active_statuses):
        status = "reconciled"
    elif any(s == "reconciled" for s in active_statuses):
        status = "partial"
    elif any(s == "locked" for s in active_statuses):
        status = "locked"
    else:
        status = "error"

    return ReconciliationRunReport(
        status=status,
        plan_invalid=all_invalid,
        outcomes=outcomes,
        issues=issues,
        error=error_detail,
    )
