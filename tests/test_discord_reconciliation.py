"""Tests for src.notifications.discord_reconciliation — the pure
desired-state/diff computation reconciliation is built on. No I/O, no
Discord dependency, no mocks needed.
"""

from __future__ import annotations

from src.models.profile import GeographyPreferences, JobSearchProfile, LocationPreference
from src.notifications.config import (
    DiscordChannelConfig,
    DiscordLocationChannelConfig,
    DiscordTop10ChannelConfig,
)
from src.notifications.discord_reconciliation import (
    build_reconciliation_plan,
    build_static_reconciliation_plan,
    build_top10_reconciliation_plan,
    diff_location_channels,
    list_desired_location_channels,
    list_desired_static_channels,
    list_desired_top10_channels,
)


def _profile(prefs: list[LocationPreference]) -> JobSearchProfile:
    return JobSearchProfile(geography=GeographyPreferences(location_preferences=prefs))


def _pref(type_: str, value: str, status: str = "preferred", label: str | None = None) -> LocationPreference:
    return LocationPreference(type=type_, value=value, status=status, label=label)


class TestListDesiredLocationChannels:
    def test_empty_profile_yields_nothing(self):
        specs, invalid = list_desired_location_channels(_profile([]))
        assert specs == []
        assert invalid == []

    def test_department_preference_becomes_job_channel(self):
        specs, invalid = list_desired_location_channels(_profile([_pref("department", "83")]))
        assert invalid == []
        assert len(specs) == 1
        assert specs[0].slug == "dept-83"
        assert specs[0].channel_name == "job-83"
        assert specs[0].kind == "department"

    def test_remote_preference_becomes_job_remote_channel(self):
        specs, invalid = list_desired_location_channels(
            _profile([_pref("remote", "remote_france")])
        )
        assert len(specs) == 1
        assert specs[0].slug == "remote-remote_france"
        assert specs[0].channel_name == "job-remote-france"

    def test_excluded_entry_is_not_desired(self):
        specs, invalid = list_desired_location_channels(
            _profile([_pref("department", "83", status="excluded")])
        )
        assert specs == []
        assert invalid == []

    def test_unrecognized_department_code_is_skipped_and_reported(self):
        specs, invalid = list_desired_location_channels(_profile([_pref("department", "999")]))
        assert specs == []
        assert len(invalid) == 1
        assert "999" in invalid[0]

    def test_multiple_valid_entries_all_desired(self):
        specs, invalid = list_desired_location_channels(
            _profile(
                [
                    _pref("department", "83"),
                    _pref("department", "69", status="acceptable"),
                    _pref("remote", "remote_france"),
                ]
            )
        )
        assert invalid == []
        assert {s.slug for s in specs} == {"dept-83", "dept-69", "remote-remote_france"}

    def test_label_is_carried_through(self):
        specs, _ = list_desired_location_channels(
            _profile([_pref("department", "83", label="Var")])
        )
        assert specs[0].label == "Var"


class TestDiffLocationChannels:
    def test_all_new_slugs_are_activate(self):
        specs, _ = list_desired_location_channels(_profile([_pref("department", "83")]))
        activate, deactivate, keep = diff_location_channels(specs, {})
        assert [s.slug for s in activate] == ["dept-83"]
        assert deactivate == []
        assert keep == []

    def test_already_enabled_slug_is_kept_not_reactivated(self):
        specs, _ = list_desired_location_channels(_profile([_pref("department", "83")]))
        registry = {"dept-83": DiscordLocationChannelConfig(enabled=True)}
        activate, deactivate, keep = diff_location_channels(specs, registry)
        assert activate == []
        assert deactivate == []
        assert keep == ["dept-83"]

    def test_disabled_registry_entry_is_activate_not_keep(self):
        specs, _ = list_desired_location_channels(_profile([_pref("department", "83")]))
        registry = {"dept-83": DiscordLocationChannelConfig(enabled=False)}
        activate, deactivate, keep = diff_location_channels(specs, registry)
        assert [s.slug for s in activate] == ["dept-83"]
        assert keep == []

    def test_no_longer_desired_enabled_slug_is_deactivate(self):
        registry = {"dept-01": DiscordLocationChannelConfig(enabled=True)}
        activate, deactivate, keep = diff_location_channels([], registry)
        assert activate == []
        assert deactivate == ["dept-01"]

    def test_no_longer_desired_already_disabled_slug_is_not_reported_again(self):
        """A slug that's already inactive doesn't need to be deactivated
        again — only an ENABLED-but-no-longer-desired slug is actionable."""
        registry = {"dept-01": DiscordLocationChannelConfig(enabled=False)}
        activate, deactivate, keep = diff_location_channels([], registry)
        assert deactivate == []

    def test_mixed_activate_deactivate_keep(self):
        specs, _ = list_desired_location_channels(
            _profile([_pref("department", "83"), _pref("department", "06")])
        )
        registry = {
            "dept-83": DiscordLocationChannelConfig(enabled=True),  # keep
            "dept-01": DiscordLocationChannelConfig(enabled=True),  # deactivate
        }
        activate, deactivate, keep = diff_location_channels(specs, registry)
        assert [s.slug for s in activate] == ["dept-06"]
        assert deactivate == ["dept-01"]
        assert keep == ["dept-83"]

    def test_idempotent_second_diff_on_same_registry_is_empty(self):
        """Running the diff twice against a registry already reflecting the
        first diff's outcome must yield no further changes."""
        specs, _ = list_desired_location_channels(_profile([_pref("department", "83")]))
        registry: dict[str, DiscordLocationChannelConfig] = {}
        activate, deactivate, keep = diff_location_channels(specs, registry)
        assert len(activate) == 1
        # Simulate a successful reconciliation updating the registry.
        registry[activate[0].slug] = DiscordLocationChannelConfig(enabled=True)
        activate2, deactivate2, keep2 = diff_location_channels(specs, registry)
        assert activate2 == []
        assert deactivate2 == []
        assert keep2 == ["dept-83"]


class TestBuildReconciliationPlan:
    def test_wraps_desired_and_diff_together(self):
        profile = _profile([_pref("department", "83"), _pref("department", "999")])
        plan = build_reconciliation_plan(profile, {})
        assert [s.slug for s in plan.activate] == ["dept-83"]
        assert len(plan.invalid) == 1
        assert not plan.is_empty

    def test_plan_with_nothing_to_do_is_empty(self):
        profile = _profile([_pref("department", "83")])
        registry = {"dept-83": DiscordLocationChannelConfig(enabled=True)}
        plan = build_reconciliation_plan(profile, registry)
        assert plan.is_empty

    def test_desired_order_matches_profile_priority_order_not_registry_or_diff_order(self):
        """List position in profile.geography.location_preferences IS
        priority (see src.models.profile.LocationPreference's docstring) —
        desired_order must reproduce that order exactly, regardless of how
        the activate/keep split falls, and regardless of alphabetic or
        numeric order of the department codes themselves."""
        profile = _profile(
            [
                _pref("department", "83"),
                _pref("department", "01"),
                _pref("department", "69"),
                _pref("department", "37"),
                _pref("department", "02"),
                _pref("remote", "remote_france"),
            ]
        )
        # 01 and 37 already enabled (keep); the rest need activation — a
        # mix designed so a naive activate+keep concatenation would NOT
        # reproduce the profile order, only the correct per-slug lookup would.
        registry = {
            "dept-01": DiscordLocationChannelConfig(enabled=True),
            "dept-37": DiscordLocationChannelConfig(enabled=True),
        }
        plan = build_reconciliation_plan(profile, registry)
        assert plan.desired_order == [
            "dept-83", "dept-01", "dept-69", "dept-37", "dept-02", "remote-remote_france",
        ]

    def test_desired_order_excludes_excluded_status_entries(self):
        profile = _profile(
            [_pref("department", "83"), _pref("department", "01", status="excluded")]
        )
        plan = build_reconciliation_plan(profile, {})
        assert plan.desired_order == ["dept-83"]


class TestListDesiredTop10Channels:
    def test_department_preference_becomes_top10_channel(self):
        specs, invalid = list_desired_top10_channels(_profile([_pref("department", "83")]))
        assert invalid == []
        assert len(specs) == 1
        assert specs[0].slug == "dept-83"
        assert specs[0].channel_name == "top10-83"

    def test_remote_preference_becomes_top10_remote_channel(self):
        specs, _ = list_desired_top10_channels(_profile([_pref("remote", "remote_france")]))
        assert specs[0].slug == "remote-remote_france"
        assert specs[0].channel_name == "top10-remote-france"

    def test_same_slug_as_job_feed_channel_different_channel_name(self):
        """The two channel kinds share a slug (they're the same location)
        but must never collide on Discord channel name."""
        profile = _profile([_pref("department", "83")])
        job_specs, _ = list_desired_location_channels(profile)
        top10_specs, _ = list_desired_top10_channels(profile)
        assert job_specs[0].slug == top10_specs[0].slug == "dept-83"
        assert job_specs[0].channel_name == "job-83"
        assert top10_specs[0].channel_name == "top10-83"

    def test_excluded_entry_has_no_top10_channel_either(self):
        specs, _ = list_desired_top10_channels(
            _profile([_pref("department", "83", status="excluded")])
        )
        assert specs == []


class TestBuildTop10ReconciliationPlan:
    def test_wraps_desired_and_diff_together(self):
        profile = _profile([_pref("department", "83")])
        plan = build_top10_reconciliation_plan(profile, {})
        assert [s.slug for s in plan.activate] == ["dept-83"]
        assert [s.channel_name for s in plan.activate] == ["top10-83"]

    def test_plan_with_nothing_to_do_is_empty(self):
        profile = _profile([_pref("department", "83")])
        registry = {"dept-83": DiscordTop10ChannelConfig(enabled=True)}
        plan = build_top10_reconciliation_plan(profile, registry)
        assert plan.is_empty

    def test_independent_of_job_feed_registry_state(self):
        """A location's job-feed channel being enabled must not affect
        whether its top10 channel is considered desired — they're tracked
        in completely separate registries."""
        profile = _profile([_pref("department", "83")])
        top10_registry: dict = {}  # nothing created yet for top10
        plan = build_top10_reconciliation_plan(profile, top10_registry)
        assert not plan.is_empty
        assert [s.slug for s in plan.activate] == ["dept-83"]

    def test_desired_order_matches_profile_priority_order(self):
        profile = _profile(
            [_pref("department", "83"), _pref("department", "69"), _pref("department", "01")]
        )
        plan = build_top10_reconciliation_plan(profile, {})
        assert plan.desired_order == ["dept-83", "dept-69", "dept-01"]


class TestListDesiredStaticChannels:
    def test_names_are_their_own_slug_and_channel_name(self):
        """Static channels have no profile-driven naming transform — the
        configured name IS both the slug and the Discord channel name."""
        specs = list_desired_static_channels(["pipeline", "errors"])
        assert [s.slug for s in specs] == ["pipeline", "errors"]
        assert [s.channel_name for s in specs] == ["pipeline", "errors"]
        assert all(s.kind == "static" for s in specs)

    def test_empty_list_yields_nothing(self):
        assert list_desired_static_channels([]) == []


class TestBuildStaticReconciliationPlan:
    def test_fresh_registry_wants_every_permanent_channel_created(self):
        plan = build_static_reconciliation_plan(
            ["pipeline", "errors", "statistics", "system-status"], {}
        )
        assert {s.slug for s in plan.activate} == {"pipeline", "errors", "statistics", "system-status"}
        assert plan.deactivate == []

    def test_plan_with_nothing_to_do_is_empty(self):
        registry = {
            "pipeline": DiscordChannelConfig(enabled=True, channel_id="c1", webhook_id="w1"),
            "errors": DiscordChannelConfig(enabled=True, channel_id="c2", webhook_id="w2"),
        }
        plan = build_static_reconciliation_plan(["pipeline", "errors"], registry)
        assert plan.is_empty

    def test_enabled_but_unprovisioned_entry_still_needs_activation(self):
        """A fresh DiscordChannelConfig() defaults enabled=True (the static
        channels' "only explicit false turns it off" convention) but has no
        channel_id/webhook_id yet — must still be activated, unlike
        location/top10 channels where enabled=False is the "not yet
        provisioned" signal."""
        registry = {"pipeline": DiscordChannelConfig(enabled=True)}
        plan = build_static_reconciliation_plan(["pipeline"], registry)
        assert [s.slug for s in plan.activate] == ["pipeline"]

    def test_disabled_registry_entry_is_reactivated_not_left_alone(self):
        registry = {"pipeline": DiscordChannelConfig(enabled=False, channel_id="c1", webhook_id="w1")}
        plan = build_static_reconciliation_plan(["pipeline"], registry)
        assert [s.slug for s in plan.activate] == ["pipeline"]

    def test_a_name_removed_from_permanent_channels_is_deactivated(self):
        registry = {"old-channel": DiscordChannelConfig(enabled=True, channel_id="c1", webhook_id="w1")}
        plan = build_static_reconciliation_plan(["pipeline"], registry)
        assert plan.deactivate == ["old-channel"]
        assert [s.slug for s in plan.activate] == ["pipeline"]
