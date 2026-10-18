"""Desired-state and diff computation for Discord location-channel
reconciliation — pure functions, no I/O, no Discord dependency.

Turns profile.geography.location_preferences (the single source of truth)
into the set of location channels that SHOULD exist, and diffs that desired
set against the known registry (DiscordConfig.location_channels) to produce
a reconciliation plan. This module only decides WHAT should happen — HOW to
actually talk to Discord (create/adopt channels and webhooks) lives in
src.discord_admin, so this stays fully unit-testable with no network/mock
dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from typing import Callable

from src.models.profile import JobSearchProfile
from src.notifications.config import (
    DiscordChannelConfig,
    DiscordLocationChannelConfig,
    DiscordTop10ChannelConfig,
)
from src.pipeline.departments import get_department
from src.pipeline.location_match import slug_for


@dataclass(frozen=True)
class DesiredLocationChannel:
    """One location the profile says should have Discord infrastructure."""

    slug: str
    channel_name: str
    label: str | None
    kind: str  # "department" | "remote"


@dataclass(frozen=True)
class ReconciliationPlan:
    """What should change to bring Discord location infrastructure in line
    with the profile. ``activate`` covers both first-time creation and
    reactivation of a previously deactivated slug — src.discord_admin
    decides which of those it is by checking the registry.
    """

    activate: list[DesiredLocationChannel] = field(default_factory=list)
    deactivate: list[str] = field(default_factory=list)  # slugs
    keep: list[str] = field(default_factory=list)  # slugs, no-op
    invalid: list[str] = field(default_factory=list)  # human-readable skip reasons
    # Every currently-desired (non-excluded) slug, in profile priority order
    # — the union of `activate` and `keep`, but in the profile's actual
    # order rather than split across two lists. This is the SOLE source of
    # truth src.discord_admin.reconciler uses to reorder Discord channels
    # within their category; it must be derived from the profile every run,
    # never from whatever order Discord currently shows. Empty for the
    # static-channel plan (permanent channels have no profile-driven order).
    desired_order: list[str] = field(default_factory=list)  # slugs

    @property
    def is_empty(self) -> bool:
        return not self.activate and not self.deactivate


def _channel_name_for(slug: str) -> str:
    """"dept-83" -> "job-83"; "remote-remote_france" -> "job-remote-france".

    Purely derived from the slug — no per-department special-casing, so any
    current or future department code works automatically.
    """
    if slug.startswith("dept-"):
        return f"job-{slug[len('dept-'):]}"
    if slug.startswith("remote-"):
        return f"job-{slug[len('remote-'):].replace('_', '-')}"
    return f"job-{slug}"


def _top10_channel_name_for(slug: str) -> str:
    """"dept-83" -> "top10-83"; "remote-remote_france" -> "top10-remote-france".

    Companion to _channel_name_for() for the top-10 leaderboard channel —
    same slug, different prefix, so the two channel kinds are named
    consistently and never collide.
    """
    if slug.startswith("dept-"):
        return f"top10-{slug[len('dept-'):]}"
    if slug.startswith("remote-"):
        return f"top10-{slug[len('remote-'):].replace('_', '-')}"
    return f"top10-{slug}"


def _list_desired_channels(
    profile: JobSearchProfile, channel_name_fn: Callable[[str], str]
) -> tuple[list[DesiredLocationChannel], list[str]]:
    """Shared desired-state derivation for both channel kinds — every
    non-excluded location_preferences entry, mapped to the Discord channel
    it should have (naming controlled by ``channel_name_fn``).

    Returns ``(specs, invalid_notes)``: an entry whose department code isn't
    recognized by src.pipeline.departments is skipped and reported in
    ``invalid_notes`` rather than silently turned into a garbage channel.
    """
    specs: list[DesiredLocationChannel] = []
    invalid: list[str] = []
    for pref in profile.geography.location_preferences:
        if pref.status == "excluded":
            continue
        if pref.type == "department" and get_department(pref.value) is None:
            invalid.append(
                f"Unrecognized department code {pref.value!r} in "
                "location_preferences — skipped, no channel created."
            )
            continue
        slug = slug_for(pref.type, pref.value)
        specs.append(
            DesiredLocationChannel(
                slug=slug,
                channel_name=channel_name_fn(slug),
                label=pref.label,
                kind=pref.type,
            )
        )
    return specs, invalid


def list_desired_location_channels(
    profile: JobSearchProfile,
) -> tuple[list[DesiredLocationChannel], list[str]]:
    """Every non-excluded location_preferences entry, mapped to the
    new-arrivals job-feed channel it should have (e.g. "job-83")."""
    return _list_desired_channels(profile, _channel_name_for)


def list_desired_top10_channels(
    profile: JobSearchProfile,
) -> tuple[list[DesiredLocationChannel], list[str]]:
    """Every non-excluded location_preferences entry, mapped to the top-10
    leaderboard channel it should have (e.g. "top10-83")."""
    return _list_desired_channels(profile, _top10_channel_name_for)


def list_desired_static_channels(permanent_channels: list[str]) -> list[DesiredLocationChannel]:
    """Every configured STATIC operational channel (e.g. "pipeline",
    "errors", "statistics", "system-status") — these have no profile-driven
    "excluded" concept like location channels; the channel name IS the slug
    (no per-slug transform), so this always wants exactly
    ``permanent_channels``.
    """
    return [
        DesiredLocationChannel(slug=name, channel_name=name, label=None, kind="static")
        for name in permanent_channels
    ]


def diff_static_channels(
    desired: list[DesiredLocationChannel],
    registry: dict[str, DiscordChannelConfig],
) -> tuple[list[DesiredLocationChannel], list[str], list[str]]:
    """Static-channel twin of diff_location_channels — NOT reusable as-is
    because DiscordChannelConfig defaults ``enabled=True`` (the static
    channels' pre-existing "only an explicit false turns it off"
    convention, config.py's DISCORD_STATIC_CHANNEL_ENV_VARS factory), unlike
    DiscordLocationChannelConfig/DiscordTop10ChannelConfig which default
    ``enabled=False``. Diffing on ``.enabled`` alone would therefore treat a
    brand-new static channel entry (enabled=True by default, but with NO
    channel_id/webhook_id yet) as already provisioned and never create it.
    A static channel is considered provisioned only once BOTH ``enabled``
    is True AND ``channel_id``/``webhook_id`` are populated.
    """
    desired_by_slug = {spec.slug: spec for spec in desired}

    activate: list[DesiredLocationChannel] = []
    keep: list[str] = []
    for slug, spec in desired_by_slug.items():
        entry = registry.get(slug)
        if entry is None or not entry.enabled or not entry.channel_id or not entry.webhook_id:
            activate.append(spec)
        else:
            keep.append(slug)

    deactivate = [
        slug for slug, entry in registry.items() if entry.enabled and slug not in desired_by_slug
    ]

    return activate, deactivate, keep


def build_static_reconciliation_plan(
    permanent_channels: list[str],
    registry: dict[str, DiscordChannelConfig],
) -> ReconciliationPlan:
    """Convenience wrapper combining desired-state derivation and diffing
    for static operational channels (e.g. "pipeline", "statistics")."""
    desired = list_desired_static_channels(permanent_channels)
    activate, deactivate, keep = diff_static_channels(desired, registry)
    return ReconciliationPlan(activate=activate, deactivate=deactivate, keep=keep, invalid=[])


def diff_location_channels(
    desired: list[DesiredLocationChannel],
    registry: dict[str, DiscordLocationChannelConfig] | dict[str, DiscordTop10ChannelConfig],
) -> tuple[list[DesiredLocationChannel], list[str], list[str]]:
    """Pure diff: no I/O, no Discord calls.

    Duck-typed on ``registry`` values needing only ``.enabled`` — shared by
    both job-feed and top10 registries (see diff_top10_channels below),
    never re-implemented per channel kind.

    Returns ``(activate, deactivate, keep)``:
    - ``activate``: desired slugs whose registry entry is missing or disabled.
    - ``deactivate``: enabled registry slugs no longer desired.
    - ``keep``: desired slugs already enabled in the registry (no-op).
    """
    desired_by_slug = {spec.slug: spec for spec in desired}

    activate: list[DesiredLocationChannel] = []
    keep: list[str] = []
    for slug, spec in desired_by_slug.items():
        entry = registry.get(slug)
        if entry is None or not entry.enabled:
            activate.append(spec)
        else:
            keep.append(slug)

    deactivate = [
        slug for slug, entry in registry.items() if entry.enabled and slug not in desired_by_slug
    ]

    return activate, deactivate, keep


def build_reconciliation_plan(
    profile: JobSearchProfile,
    registry: dict[str, DiscordLocationChannelConfig],
) -> ReconciliationPlan:
    """Convenience wrapper combining desired-state derivation and diffing
    for job-feed channels (e.g. "job-83")."""
    desired, invalid = list_desired_location_channels(profile)
    activate, deactivate, keep = diff_location_channels(desired, registry)
    desired_order = [spec.slug for spec in desired]
    return ReconciliationPlan(
        activate=activate, deactivate=deactivate, keep=keep, invalid=invalid, desired_order=desired_order
    )


def build_top10_reconciliation_plan(
    profile: JobSearchProfile,
    registry: dict[str, DiscordTop10ChannelConfig],
) -> ReconciliationPlan:
    """Convenience wrapper combining desired-state derivation and diffing
    for top-10 leaderboard channels (e.g. "top10-83")."""
    desired, invalid = list_desired_top10_channels(profile)
    activate, deactivate, keep = diff_location_channels(desired, registry)
    desired_order = [spec.slug for spec in desired]
    return ReconciliationPlan(
        activate=activate, deactivate=deactivate, keep=keep, invalid=invalid, desired_order=desired_order
    )
