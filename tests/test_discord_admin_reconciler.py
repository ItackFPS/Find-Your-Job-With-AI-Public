"""Tests for src.discord_admin.reconciler.reconcile — the single
implementation of activate/deactivate, exercised against a hand-written
in-memory fake Discord client (no live network, no mocking library
dependency) plus temp files for the registry (config/notifications.yaml)
and the secret store (.env), so nothing here ever touches real project
files.
"""

from __future__ import annotations

import yaml

from src.discord_admin.reconciler import reconcile, reconcile_static, reconcile_top10
from src.notifications.config import (
    DiscordChannelConfig,
    DiscordLocationChannelConfig,
    DiscordTop10ChannelConfig,
    load_notification_config,
)
from src.notifications.discord_reconciliation import DesiredLocationChannel, ReconciliationPlan


class FakeDiscordClient:
    """Duck-types src.discord_admin.client.DiscordAdminClient's interface
    against in-memory state — no HTTP, no network, fully deterministic.
    """

    def __init__(self, channels=None, webhooks_by_channel=None):
        self.channels: list[dict] = list(channels or [])
        self.webhooks: dict[str, list[dict]] = {
            cid: list(whs) for cid, whs in (webhooks_by_channel or {}).items()
        }
        self._next_id = 1000
        self._next_position = 1000
        self.create_channel_calls: list[str] = []
        self.create_webhook_calls: list[str] = []
        self.move_channel_calls: list[tuple[str, str | None]] = []
        self.set_positions_calls: list[list[tuple[str, int]]] = []

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def _new_position(self) -> int:
        self._next_position += 1
        return self._next_position

    def list_channels(self):
        return list(self.channels)

    def get_channel(self, channel_id):
        for c in self.channels:
            if c["id"] == channel_id:
                return c
        return None

    def create_channel(self, name, parent_id=None):
        self.create_channel_calls.append(name)
        # A real new channel always gets appended at the end (highest
        # position) — mirrors that so ordering tests can exercise "newly
        # created channel ends up in the correct position" honestly.
        channel = {"id": self._new_id(), "name": name, "parent_id": parent_id, "position": self._new_position()}
        self.channels.append(channel)
        self.webhooks[channel["id"]] = []
        return channel

    def set_channel_positions(self, positions):
        self.set_positions_calls.append(list(positions))
        for channel_id, position in positions:
            for c in self.channels:
                if c["id"] == channel_id:
                    c["position"] = position

    def list_webhooks(self, channel_id):
        return list(self.webhooks.get(channel_id, []))

    def get_webhook(self, webhook_id):
        for whs in self.webhooks.values():
            for w in whs:
                if w["id"] == webhook_id:
                    return w
        return None

    def create_webhook(self, channel_id, name):
        self.create_webhook_calls.append(name)
        webhook = {"id": self._new_id(), "name": name, "token": f"tok-{self._new_id()}"}
        self.webhooks.setdefault(channel_id, []).append(webhook)
        return webhook

    def move_channel(self, channel_id, parent_id):
        self.move_channel_calls.append((channel_id, parent_id))
        for c in self.channels:
            if c["id"] == channel_id:
                c["parent_id"] = parent_id
        return {"id": channel_id, "parent_id": parent_id}


def _write_minimal_notifications_yaml(config_dir):
    (config_dir / "notifications.yaml").write_text(
        yaml.safe_dump({"notifications": {"enabled": True, "discord": {"enabled": True}}}),
        encoding="utf-8",
    )


class TestActivateCreatesFreshInfrastructure:
    def test_no_existing_channel_or_webhook_creates_both(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        env_path = tmp_path / ".env"
        lock_path = tmp_path / "data" / "reconcile.lock"

        client = FakeDiscordClient()
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")]
        )
        result = reconcile(
            plan,
            {},
            client,
            config_dir=str(config_dir),
            lock_path=lock_path,
            env_path=env_path,
        )

        assert result.outcomes[0].outcome == "created"
        assert client.create_channel_calls == ["job-83"]
        assert client.create_webhook_calls == ["job-83-webhook"]
        assert "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL=" in env_path.read_text(encoding="utf-8")

        saved = load_notification_config(str(config_dir))
        entry = saved.discord.location_channels["dept-83"]
        assert entry.enabled is True
        assert entry.channel_id
        assert entry.webhook_id
        assert entry.webhook_url_env == "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL"


class TestActivateAdoptsExistingInfrastructure:
    def test_existing_channel_and_webhook_are_reused_not_duplicated(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        env_path = tmp_path / ".env"

        client = FakeDiscordClient(
            channels=[{"id": "chan-1", "name": "job-83"}],
            webhooks_by_channel={"chan-1": [{"id": "wh-1", "name": "job-83-webhook", "token": "tok1"}]},
        )
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")]
        )
        result = reconcile(
            plan, {}, client, config_dir=str(config_dir), lock_path=tmp_path / "l.lock", env_path=env_path
        )

        assert result.outcomes[0].outcome == "adopted"
        assert client.create_channel_calls == []
        assert client.create_webhook_calls == []

        saved = load_notification_config(str(config_dir))
        entry = saved.discord.location_channels["dept-83"]
        assert entry.channel_id == "chan-1"
        assert entry.webhook_id == "wh-1"


class TestAmbiguousChannelIsNeverGuessed:
    def test_two_matching_channels_reports_ambiguous_and_creates_nothing(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "1", "name": "job-83"}, {"id": "2", "name": "job-83"}]
        )
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")]
        )
        result = reconcile(
            plan,
            {},
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env",
        )

        assert result.outcomes[0].outcome == "ambiguous"
        assert len(result.issues) == 1
        assert result.issues[0].kind == "ambiguous_channel"
        assert client.create_channel_calls == []

        saved = load_notification_config(str(config_dir))
        assert "dept-83" not in saved.discord.location_channels
        assert len(saved.discord.reconciliation_issues) == 1


class TestAmbiguousWebhookIsNeverGuessed:
    def test_two_matching_webhooks_reports_ambiguous(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-1", "name": "job-83"}],
            webhooks_by_channel={
                "chan-1": [
                    {"id": "wh-1", "name": "job-83-webhook", "token": "tok1"},
                    {"id": "wh-2", "name": "job-83-webhook-old", "token": "tok2"},
                ]
            },
        )
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")]
        )
        result = reconcile(
            plan,
            {},
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env",
        )

        assert result.outcomes[0].outcome == "ambiguous"
        assert result.issues[0].kind == "ambiguous_webhook"
        assert client.create_webhook_calls == []


class TestDeactivateOnlyFlipsRegistryFlag:
    def test_deactivate_makes_no_discord_calls(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "chan-1", "name": "job-01"}])
        registry = {
            "dept-01": DiscordLocationChannelConfig(
                enabled=True, channel_id="chan-1", webhook_id="wh-1", webhook_url_env="X"
            )
        }
        plan = ReconciliationPlan(deactivate=["dept-01"])
        result = reconcile(
            plan,
            registry,
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env",
        )

        assert result.outcomes[0].outcome == "deactivated"
        assert client.create_channel_calls == []
        assert client.create_webhook_calls == []

        saved = load_notification_config(str(config_dir))
        entry = saved.discord.location_channels["dept-01"]
        assert entry.enabled is False
        # Channel/webhook ids are preserved for a future reactivation.
        assert entry.channel_id == "chan-1"
        assert entry.webhook_id == "wh-1"


class TestDeactivateMovesToInactiveCategory:
    def test_moves_channel_and_records_original_category(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "chan-1", "name": "job-01", "parent_id": "cat-active"}])
        registry = {
            "dept-01": DiscordLocationChannelConfig(
                enabled=True, channel_id="chan-1", webhook_id="wh-1", webhook_url_env="X"
            )
        }
        plan = ReconciliationPlan(deactivate=["dept-01"])
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            inactive_category_id="cat-inactive",
        )

        assert client.move_channel_calls == [("chan-1", "cat-inactive")]
        saved = load_notification_config(str(config_dir))
        entry = saved.discord.location_channels["dept-01"]
        assert entry.enabled is False
        assert entry.original_category_id == "cat-active"

    def test_no_inactive_category_configured_makes_no_move_calls(self, tmp_path):
        """Feature is opt-in: an empty inactive_category_id must behave
        exactly like before this feature existed."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "chan-1", "name": "job-01", "parent_id": "cat-active"}])
        registry = {
            "dept-01": DiscordLocationChannelConfig(enabled=True, channel_id="chan-1", webhook_id="wh-1")
        }
        plan = ReconciliationPlan(deactivate=["dept-01"])
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
        )
        assert client.move_channel_calls == []

    def test_already_in_inactive_category_is_not_moved_again(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "chan-1", "name": "job-01", "parent_id": "cat-inactive"}])
        registry = {
            "dept-01": DiscordLocationChannelConfig(enabled=True, channel_id="chan-1", webhook_id="wh-1")
        }
        plan = ReconciliationPlan(deactivate=["dept-01"])
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            inactive_category_id="cat-inactive",
        )
        assert client.move_channel_calls == []

    def test_a_failed_move_never_blocks_the_functional_deactivation(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        class _BoomOnMove(FakeDiscordClient):
            def move_channel(self, channel_id, parent_id):
                raise RuntimeError("simulated permission error")

        client = _BoomOnMove(channels=[{"id": "chan-1", "name": "job-01", "parent_id": "cat-active"}])
        registry = {
            "dept-01": DiscordLocationChannelConfig(enabled=True, channel_id="chan-1", webhook_id="wh-1")
        }
        plan = ReconciliationPlan(deactivate=["dept-01"])
        result = reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            inactive_category_id="cat-inactive",
        )
        assert result.outcomes[0].outcome == "deactivated"  # must not raise or report an error
        saved = load_notification_config(str(config_dir))
        assert saved.discord.location_channels["dept-01"].enabled is False


class TestReactivationRestoresOriginalCategory:
    def test_reactivation_moves_channel_back_out_of_inactive_category(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-1", "name": "job-01", "parent_id": "cat-inactive"}],
            webhooks_by_channel={"chan-1": [{"id": "wh-1", "name": "job-01-webhook", "token": "tok"}]},
        )
        registry = {
            "dept-01": DiscordLocationChannelConfig(
                enabled=False, channel_id="chan-1", webhook_id="wh-1",
                webhook_url_env="X", original_category_id="cat-active",
            )
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-01", channel_name="job-01", label=None, kind="department")]
        )
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            inactive_category_id="cat-inactive",
        )

        assert client.move_channel_calls == [("chan-1", "cat-active")]
        saved = load_notification_config(str(config_dir))
        entry = saved.discord.location_channels["dept-01"]
        assert entry.enabled is True
        assert entry.original_category_id == ""  # cleared after restore

    def test_manually_relocated_channel_is_left_alone(self, tmp_path):
        """If the channel isn't actually sitting in the inactive category
        any more (the user moved it manually while it was deactivated),
        reactivation must not clobber that manual choice."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-1", "name": "job-01", "parent_id": "cat-user-chosen"}],
            webhooks_by_channel={"chan-1": [{"id": "wh-1", "name": "job-01-webhook", "token": "tok"}]},
        )
        registry = {
            "dept-01": DiscordLocationChannelConfig(
                enabled=False, channel_id="chan-1", webhook_id="wh-1",
                webhook_url_env="X", original_category_id="cat-active",
            )
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-01", channel_name="job-01", label=None, kind="department")]
        )
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            inactive_category_id="cat-inactive",
        )
        assert client.move_channel_calls == []


class TestReactivationReusesRecordedIds:
    def test_live_ids_still_valid_reactivates_without_any_discord_write(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-1", "name": "job-01"}],
            webhooks_by_channel={"chan-1": [{"id": "wh-1", "name": "job-01-webhook", "token": "t"}]},
        )
        registry = {
            "dept-01": DiscordLocationChannelConfig(
                enabled=False, channel_id="chan-1", webhook_id="wh-1", webhook_url_env="X"
            )
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-01", channel_name="job-01", label=None, kind="department")]
        )
        result = reconcile(
            plan,
            registry,
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env",
        )

        assert result.outcomes[0].outcome == "reactivated"
        assert client.create_channel_calls == []
        assert client.create_webhook_calls == []

        saved = load_notification_config(str(config_dir))
        assert saved.discord.location_channels["dept-01"].enabled is True

    def test_stale_recorded_ids_fall_back_to_fresh_resolution(self, tmp_path):
        """If the user manually deleted the channel/webhook, reactivation
        must not silently no-op — it should detect the drift and create
        fresh infrastructure instead."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient()  # nothing exists live anymore
        registry = {
            "dept-01": DiscordLocationChannelConfig(
                enabled=False, channel_id="stale-chan", webhook_id="stale-wh", webhook_url_env="X"
            )
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-01", channel_name="job-01", label=None, kind="department")]
        )
        result = reconcile(
            plan,
            registry,
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env",
        )

        assert result.outcomes[0].outcome == "created"
        assert client.create_channel_calls == ["job-01"]


class TestIdempotency:
    def test_reconciling_the_same_plan_twice_creates_nothing_new_the_second_time(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        env_path = tmp_path / ".env"

        client = FakeDiscordClient()
        registry: dict[str, DiscordLocationChannelConfig] = {}
        spec = DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")

        reconcile(
            ReconciliationPlan(activate=[spec]),
            registry,
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=env_path,
        )
        assert len(client.create_channel_calls) == 1
        assert len(client.create_webhook_calls) == 1

        # Re-run against the SAME desired spec, but load the registry back
        # from disk first (as a real caller would on the next tick).
        saved = load_notification_config(str(config_dir))
        second_result = reconcile(
            ReconciliationPlan(activate=[spec]),
            dict(saved.discord.location_channels),
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=env_path,
        )
        assert second_result.outcomes[0].outcome == "reactivated"
        assert len(client.create_channel_calls) == 1  # still just the one
        assert len(client.create_webhook_calls) == 1


class TestReconcileTop10CreatesLeaderboardChannel:
    def test_no_existing_channel_or_webhook_creates_both_with_top10_naming(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        env_path = tmp_path / ".env"

        client = FakeDiscordClient()
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="top10-83", label=None, kind="department")]
        )
        result = reconcile_top10(
            plan,
            {},
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=env_path,
        )

        assert result.outcomes[0].outcome == "created"
        assert client.create_channel_calls == ["top10-83"]
        assert client.create_webhook_calls == ["top10-83-webhook"]
        assert "DISCORD_TOP10_DEPT_83_WEBHOOK_URL=" in env_path.read_text(encoding="utf-8")

        saved = load_notification_config(str(config_dir))
        entry = saved.discord.top10_channels["dept-83"]
        assert entry.enabled is True
        assert entry.channel_id
        assert entry.webhook_id
        assert entry.message_id == ""  # no leaderboard sent yet — that's a separate step
        assert entry.webhook_url_env == "DISCORD_TOP10_DEPT_83_WEBHOOK_URL"

    def test_does_not_touch_job_feed_registry(self, tmp_path):
        """The two channel kinds must be fully independent — reconciling
        top10 for a slug must not create/modify that slug's job-feed entry."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient()
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="top10-83", label=None, kind="department")]
        )
        reconcile_top10(
            plan, {}, client, config_dir=str(config_dir), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
        )

        saved = load_notification_config(str(config_dir))
        assert saved.discord.location_channels == {}
        assert "dept-83" in saved.discord.top10_channels


class TestReconcileStaticCreatesOperationalChannel:
    def test_no_existing_channel_or_webhook_creates_both_with_static_naming(self, tmp_path):
        """Unlike job/top10 channels, a static channel's name IS the
        channel_name — no slug prefix transform."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        env_path = tmp_path / ".env"

        client = FakeDiscordClient()
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="statistics", channel_name="statistics", label=None, kind="static")]
        )
        result = reconcile_static(
            plan,
            {},
            client,
            config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock",
            env_path=env_path,
        )

        assert result.outcomes[0].outcome == "created"
        assert result.outcomes[0].channel_kind == "static"
        assert client.create_channel_calls == ["statistics"]
        assert client.create_webhook_calls == ["statistics-webhook"]
        assert "DISCORD_STATISTICS_WEBHOOK_URL=" in env_path.read_text(encoding="utf-8")

        saved = load_notification_config(str(config_dir))
        entry = saved.discord.channels["statistics"]
        assert entry.enabled is True
        assert entry.channel_id
        assert entry.webhook_id
        assert entry.message_id == ""
        assert entry.webhook_url_env == "DISCORD_STATISTICS_WEBHOOK_URL"

    def test_adopts_a_manually_created_channel_by_exact_name(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "c1", "name": "pipeline"}])
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="pipeline", channel_name="pipeline", label=None, kind="static")]
        )
        result = reconcile_static(
            plan, {}, client, config_dir=str(config_dir), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
        )

        assert result.outcomes[0].outcome == "created"  # webhook still had to be created
        assert client.create_channel_calls == []  # channel adopted, never re-created

    def test_reactivation_reuses_recorded_ids_with_no_discord_write(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "c1", "name": "pipeline"}])
        registry = {
            "pipeline": DiscordChannelConfig(
                enabled=False, webhook_url_env="DISCORD_PIPELINE_WEBHOOK_URL",
                channel_id="c1", webhook_id="w1",
            )
        }
        client.webhooks["c1"] = [{"id": "w1", "name": "pipeline-webhook", "token": "tok"}]
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="pipeline", channel_name="pipeline", label=None, kind="static")]
        )
        result = reconcile_static(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
        )

        assert result.outcomes[0].outcome == "reactivated"
        assert client.create_channel_calls == []
        assert client.create_webhook_calls == []


class TestReconcileTop10AmbiguityAndDeactivation:
    def test_ambiguous_channel_is_never_guessed(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "1", "name": "top10-83"}, {"id": "2", "name": "top10-83"}]
        )
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="top10-83", label=None, kind="department")]
        )
        result = reconcile_top10(
            plan, {}, client, config_dir=str(config_dir), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
        )
        assert result.outcomes[0].outcome == "ambiguous"
        assert client.create_channel_calls == []

    def test_deactivate_flips_flag_and_preserves_message_id(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "chan-1", "name": "top10-01"}])
        registry = {
            "dept-01": DiscordTop10ChannelConfig(
                enabled=True,
                channel_id="chan-1",
                webhook_id="wh-1",
                webhook_url_env="X",
                message_id="msg-999",
            )
        }
        plan = ReconciliationPlan(deactivate=["dept-01"])
        reconcile_top10(
            plan, registry, client, config_dir=str(config_dir), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
        )

        saved = load_notification_config(str(config_dir))
        entry = saved.discord.top10_channels["dept-01"]
        assert entry.enabled is False
        assert entry.message_id == "msg-999"  # preserved for when it's reactivated


class TestReconcileAndReconcileTop10IssuesDoNotClobberEachOther:
    def test_job_issues_survive_a_subsequent_top10_reconcile_in_the_same_pass(self, tmp_path):
        """Reproduces the exact ordering src.discord_admin.service.
        run_reconciliation_pass uses: reconcile() then reconcile_top10() in
        the same pass. The second call must not silently erase the first
        call's ambiguous-issue record."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        lock_path = tmp_path / "l.lock"
        env_path = tmp_path / ".env"

        job_client = FakeDiscordClient(channels=[{"id": "1", "name": "job-83"}, {"id": "2", "name": "job-83"}])
        job_plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")]
        )
        job_result = reconcile(
            job_plan, {}, job_client, config_dir=str(config_dir), lock_path=lock_path, env_path=env_path
        )
        assert len(job_result.issues) == 1

        top10_client = FakeDiscordClient()
        top10_plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="top10-83", label=None, kind="department")]
        )
        reconcile_top10(
            top10_plan,
            {},
            top10_client,
            config_dir=str(config_dir),
            lock_path=lock_path,
            env_path=env_path,
            carry_forward_issues=job_result.issues,
        )

        saved = load_notification_config(str(config_dir))
        kinds = {issue.kind for issue in saved.discord.reconciliation_issues}
        assert "ambiguous_channel" in kinds
        assert len(saved.discord.reconciliation_issues) == 1  # job issue preserved, no duplicate


class TestNewChannelsAreCreatedInTheirCategory:
    def test_new_job_channel_created_under_job_category(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient()
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="job-75", label=None, kind="department")]
        )
        result = reconcile(
            plan, {}, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-job-dept",
        )

        assert result.outcomes[0].outcome == "created"
        assert client.get_channel(client.channels[0]["id"])["parent_id"] == "cat-job-dept"

    def test_new_top10_channel_created_under_top10_category(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient()
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="top10-75", label=None, kind="department")]
        )
        result = reconcile_top10(
            plan, {}, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-top10-dept",
        )

        assert result.outcomes[0].outcome == "created"
        assert client.get_channel(client.channels[0]["id"])["parent_id"] == "cat-top10-dept"


class TestMisplacedChannelsAreRepairedNotDuplicated:
    def test_job_channel_found_outside_category_is_adopted_and_moved(self, tmp_path):
        """job-75 already exists in Discord (found by name) but sits at the
        guild top level — reconciliation must adopt it (no duplicate
        creation) AND move it into Job-Département."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-75", "name": "job-75", "parent_id": None}],
            webhooks_by_channel={"chan-75": [{"id": "wh-75", "name": "job-75-webhook", "token": "tok"}]},
        )
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="job-75", label=None, kind="department")]
        )
        result = reconcile(
            plan, {}, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-job-dept",
        )

        assert result.outcomes[0].outcome == "adopted"
        assert client.create_channel_calls == []  # no duplicate
        assert client.move_channel_calls == [("chan-75", "cat-job-dept")]
        assert client.get_channel("chan-75")["parent_id"] == "cat-job-dept"

    def test_top10_channel_found_outside_category_is_adopted_and_moved(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-75", "name": "top10-75", "parent_id": None}],
            webhooks_by_channel={"chan-75": [{"id": "wh-75", "name": "top10-75-webhook", "token": "tok"}]},
        )
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="top10-75", label=None, kind="department")]
        )
        result = reconcile_top10(
            plan, {}, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-top10-dept",
        )

        assert result.outcomes[0].outcome == "adopted"
        assert client.create_channel_calls == []
        assert client.move_channel_calls == [("chan-75", "cat-top10-dept")]

    def test_registered_job_channel_reactivated_outside_category_is_moved(self, tmp_path):
        """A slug already recorded in the registry (channel_id/webhook_id
        known) but whose live channel sits outside the target category —
        e.g. it was created before the category was configured — must be
        repaired on reactivation, not left misplaced."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-75", "name": "job-75", "parent_id": None}],
            webhooks_by_channel={"chan-75": [{"id": "wh-75", "name": "job-75-webhook", "token": "tok"}]},
        )
        registry = {
            "dept-75": DiscordLocationChannelConfig(
                enabled=False, channel_id="chan-75", webhook_id="wh-75", webhook_url_env="X"
            )
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="job-75", label=None, kind="department")]
        )
        result = reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-job-dept",
        )

        assert result.outcomes[0].outcome == "reactivated"
        assert client.move_channel_calls == [("chan-75", "cat-job-dept")]

    def test_correctly_placed_channel_is_left_unchanged(self, tmp_path):
        """No spurious move calls when the channel is already in the right
        category — cosmetic repair must not cause churn on every tick."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(
            channels=[{"id": "chan-83", "name": "job-83", "parent_id": "cat-job-dept"}],
            webhooks_by_channel={"chan-83": [{"id": "wh-83", "name": "job-83-webhook", "token": "tok"}]},
        )
        registry = {
            "dept-83": DiscordLocationChannelConfig(
                enabled=False, channel_id="chan-83", webhook_id="wh-83", webhook_url_env="X"
            )
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-83", channel_name="job-83", label=None, kind="department")]
        )
        result = reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-job-dept",
        )

        assert result.outcomes[0].outcome == "reactivated"
        assert client.move_channel_calls == []

    def test_no_category_configured_makes_no_move_calls(self, tmp_path):
        """Opt-in feature: an empty location_category_id must behave exactly
        like before this feature existed — never touches Discord to move a
        channel."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "chan-75", "name": "job-75", "parent_id": None}])
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="job-75", label=None, kind="department")]
        )
        reconcile(
            plan, {}, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
        )

        assert client.create_channel_calls == []  # channel adopted, not duplicated
        assert client.move_channel_calls == []


class TestCategoryMoveFailureNeverBlocksActivation:
    def test_move_failure_still_reports_created_and_persists_registry(self, tmp_path):
        """Mirrors TestDeactivateMovesToInactiveCategory's failure-tolerance
        convention: a missing/deleted category or permission error must not
        block the functional activation it accompanies."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        class _BoomOnMove(FakeDiscordClient):
            def move_channel(self, channel_id, parent_id):
                raise RuntimeError("simulated: category no longer exists")

        client = _BoomOnMove(channels=[{"id": "chan-75", "name": "job-75", "parent_id": None}])
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-75", channel_name="job-75", label=None, kind="department")]
        )
        result = reconcile(
            plan, {}, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-deleted",
        )

        assert result.outcomes[0].outcome != "error"  # must not raise or report "error"
        assert client.create_channel_calls == []  # channel adopted, not duplicated
        saved = load_notification_config(str(config_dir))
        assert saved.discord.location_channels["dept-75"].channel_id == "chan-75"


class TestStaticChannelsKeepTheirExistingCategoryBehavior:
    def test_reactivated_static_channel_is_never_moved(self, tmp_path):
        """System channels (pipeline/errors/statistics/system-status) are
        pre-provisioned under Système and reconciliation must not touch
        their category placement — only job/top10 channels are subject to
        the new category-repair logic."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = FakeDiscordClient(channels=[{"id": "c1", "name": "pipeline", "parent_id": "cat-systeme"}])
        registry = {
            "pipeline": DiscordChannelConfig(
                enabled=False, webhook_url_env="DISCORD_PIPELINE_WEBHOOK_URL",
                channel_id="c1", webhook_id="w1",
            )
        }
        client.webhooks["c1"] = [{"id": "w1", "name": "pipeline-webhook", "token": "tok"}]
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="pipeline", channel_name="pipeline", label=None, kind="static")]
        )
        result = reconcile_static(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id="cat-job-dept",  # even if misconfigured, static must stay put
        )

        assert result.outcomes[0].outcome == "reactivated"
        assert client.move_channel_calls == []
        assert client.get_channel("c1")["parent_id"] == "cat-systeme"


def _registry_entry(cls, channel_id, enabled=True):
    return cls(enabled=enabled, channel_id=channel_id, webhook_id=f"wh-{channel_id}", webhook_url_env="X")


class TestChannelOrderMatchesProfilePriority:
    """The profile's location_preferences order (see
    src.models.profile.LocationPreference's docstring: list position IS
    priority) is the sole source of truth for Discord channel order within
    Job-Département/Top10-Département — never Discord's current order."""

    CATEGORY = "cat-job-dept"

    def _client_with_channels(self, order_with_positions):
        """order_with_positions: list of (channel_id, name, position)."""
        return FakeDiscordClient(
            channels=[
                {"id": cid, "name": name, "parent_id": self.CATEGORY, "position": pos}
                for cid, name, pos in order_with_positions
            ],
            webhooks_by_channel={
                cid: [{"id": f"wh-{cid}", "name": f"{name}-webhook", "token": "tok"}]
                for cid, name, _ in order_with_positions
            },
        )

    def _live_order(self, client):
        return [
            c["id"]
            for c in sorted(
                (c for c in client.channels if c.get("parent_id") == self.CATEGORY),
                key=lambda c: c.get("position", 0),
            )
        ]

    def test_profile_order_produces_matching_discord_order(self, tmp_path):
        """job-83, job-01, job-69, job-37, job-02, job-remote-france — all
        pre-existing but sitting in Discord in a DIFFERENT order — must
        converge to the profile's order in one reconcile() call."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        slugs_in_profile_order = ["dept-83", "dept-01", "dept-69", "dept-37", "dept-02", "remote-france"]
        names = {
            "dept-83": "job-83", "dept-01": "job-01", "dept-69": "job-69",
            "dept-37": "job-37", "dept-02": "job-02", "remote-france": "job-remote-france",
        }
        channel_ids = {slug: f"chan-{slug}" for slug in slugs_in_profile_order}
        # Discord currently has them in the WRONG order (matches the bug
        # report: 02 and remote-france swapped relative to profile order).
        wrong_order = ["dept-83", "dept-01", "dept-69", "dept-37", "remote-france", "dept-02"]
        client = self._client_with_channels(
            [(channel_ids[s], names[s], i) for i, s in enumerate(wrong_order)]
        )
        registry = {slug: _registry_entry(DiscordLocationChannelConfig, channel_ids[slug]) for slug in slugs_in_profile_order}

        plan = ReconciliationPlan(
            keep=slugs_in_profile_order,
            desired_order=slugs_in_profile_order,
        )
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id=self.CATEGORY,
        )

        assert self._live_order(client) == [channel_ids[s] for s in slugs_in_profile_order]

    def test_wrong_order_is_corrected(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels([("c-01", "job-01", 0), ("c-83", "job-83", 1)])
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "c-01"),
        }
        plan = ReconciliationPlan(keep=["dept-83", "dept-01"], desired_order=["dept-83", "dept-01"])
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id=self.CATEGORY,
        )

        assert self._live_order(client) == ["c-83", "c-01"]
        assert client.set_positions_calls == [[("c-83", 0), ("c-01", 1)]]

    def test_newly_created_channel_is_placed_in_correct_position(self, tmp_path):
        """dept-02 doesn't exist yet in Discord; the other two do, already
        in order. The new channel must end up in the MIDDLE (its profile
        position), not appended at the end."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels([("c-83", "job-83", 0), ("c-69", "job-69", 1)])
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-69": _registry_entry(DiscordLocationChannelConfig, "c-69"),
        }
        plan = ReconciliationPlan(
            activate=[DesiredLocationChannel(slug="dept-02", channel_name="job-02", label=None, kind="department")],
            keep=["dept-83", "dept-69"],
            desired_order=["dept-83", "dept-02", "dept-69"],
        )
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id=self.CATEGORY,
        )

        saved = load_notification_config(str(config_dir))
        new_channel_id = saved.discord.location_channels["dept-02"].channel_id
        assert self._live_order(client) == ["c-83", new_channel_id, "c-69"]

    def test_correct_order_causes_no_reorder_calls(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels([("c-83", "job-83", 0), ("c-01", "job-01", 1)])
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "c-01"),
        }
        plan = ReconciliationPlan(keep=["dept-83", "dept-01"], desired_order=["dept-83", "dept-01"])
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id=self.CATEGORY,
        )

        assert client.set_positions_calls == []
        assert client.move_channel_calls == []

    def test_profile_reorder_converges_discord_on_next_reconcile(self, tmp_path):
        """Changing the profile's order (simulated here by passing a
        different desired_order on the second call — the same thing a real
        `run_reconciliation_pass` does once the profile file changes) must
        re-converge Discord, not leave it at the old order."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels(
            [("c-83", "job-83", 0), ("c-01", "job-01", 1), ("c-69", "job-69", 2), ("c-02", "job-02", 3)]
        )
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "c-01"),
            "dept-69": _registry_entry(DiscordLocationChannelConfig, "c-69"),
            "dept-02": _registry_entry(DiscordLocationChannelConfig, "c-02"),
        }
        lock_path, env_path = tmp_path / "l.lock", tmp_path / ".env"
        slugs = ["dept-83", "dept-01", "dept-69", "dept-02"]

        # First pass: already in this order — no-op.
        reconcile(
            ReconciliationPlan(keep=slugs, desired_order=slugs),
            registry, client, config_dir=str(config_dir), lock_path=lock_path, env_path=env_path,
            location_category_id=self.CATEGORY,
        )
        assert client.set_positions_calls == []

        # Profile changes to 83, 69, 01, 02.
        new_order = ["dept-83", "dept-69", "dept-01", "dept-02"]
        reconcile(
            ReconciliationPlan(keep=slugs, desired_order=new_order),
            registry, client, config_dir=str(config_dir), lock_path=lock_path, env_path=env_path,
            location_category_id=self.CATEGORY,
        )
        assert self._live_order(client) == ["c-83", "c-69", "c-01", "c-02"]

    def test_job_and_top10_ordered_independently_from_the_same_profile_order(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)
        slugs = ["dept-83", "dept-01", "dept-69"]

        job_client = self._client_with_channels([("j-01", "job-01", 0), ("j-83", "job-83", 1), ("j-69", "job-69", 2)])
        job_registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "j-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "j-01"),
            "dept-69": _registry_entry(DiscordLocationChannelConfig, "j-69"),
        }
        reconcile(
            ReconciliationPlan(keep=slugs, desired_order=slugs),
            job_registry, job_client, config_dir=str(config_dir),
            lock_path=tmp_path / "l1.lock", env_path=tmp_path / ".env",
            location_category_id="cat-job-dept",
        )

        top10_client = FakeDiscordClient(
            channels=[
                {"id": "t-69", "name": "top10-69", "parent_id": "cat-top10-dept", "position": 0},
                {"id": "t-01", "name": "top10-01", "parent_id": "cat-top10-dept", "position": 1},
                {"id": "t-83", "name": "top10-83", "parent_id": "cat-top10-dept", "position": 2},
            ],
            webhooks_by_channel={
                "t-69": [{"id": "wh-t-69", "name": "top10-69-webhook", "token": "tok"}],
                "t-01": [{"id": "wh-t-01", "name": "top10-01-webhook", "token": "tok"}],
                "t-83": [{"id": "wh-t-83", "name": "top10-83-webhook", "token": "tok"}],
            },
        )
        top10_registry = {
            "dept-83": _registry_entry(DiscordTop10ChannelConfig, "t-83"),
            "dept-01": _registry_entry(DiscordTop10ChannelConfig, "t-01"),
            "dept-69": _registry_entry(DiscordTop10ChannelConfig, "t-69"),
        }
        reconcile_top10(
            ReconciliationPlan(keep=slugs, desired_order=slugs),
            top10_registry, top10_client, config_dir=str(config_dir),
            lock_path=tmp_path / "l2.lock", env_path=tmp_path / ".env",
            location_category_id="cat-top10-dept",
        )

        assert [c["id"] for c in sorted(job_client.channels, key=lambda c: c.get("position", 0))] == ["j-83", "j-01", "j-69"]
        assert [c["id"] for c in sorted(top10_client.channels, key=lambda c: c.get("position", 0))] == ["t-83", "t-01", "t-69"]

    def test_disabled_location_does_not_occupy_an_active_position(self, tmp_path):
        """dept-06 was deactivated (excluded from the current profile, so
        it's absent from desired_order) but its channel — with no
        inactive_category_id configured — is still physically sitting
        inside Job-Département. Reordering the active channels must not be
        thrown off by it, and it must never appear in the position update."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels(
            [("c-06", "job-06", 0), ("c-83", "job-83", 1), ("c-01", "job-01", 2)]
        )
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "c-01"),
            "dept-06": _registry_entry(DiscordLocationChannelConfig, "c-06", enabled=False),
        }
        plan = ReconciliationPlan(keep=["dept-83", "dept-01"], desired_order=["dept-83", "dept-01"])
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id=self.CATEGORY,
        )

        for call in client.set_positions_calls:
            assert "c-06" not in [cid for cid, _ in call]

    def test_inactive_channel_move_is_unaffected_by_reordering(self, tmp_path):
        """Deactivating dept-01 (moved to Inactive) in the same pass that
        reorders the remaining active channels must not interfere with
        either mechanism."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels(
            [("c-83", "job-83", 0), ("c-01", "job-01", 1), ("c-69", "job-69", 2)]
        )
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "c-01"),
            "dept-69": _registry_entry(DiscordLocationChannelConfig, "c-69"),
        }
        plan = ReconciliationPlan(
            deactivate=["dept-01"],
            keep=["dept-83", "dept-69"],
            desired_order=["dept-83", "dept-69"],
        )
        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            location_category_id=self.CATEGORY,
            inactive_category_id="cat-inactive",
        )

        assert client.move_channel_calls == [("c-01", "cat-inactive")]
        assert client.get_channel("c-01")["parent_id"] == "cat-inactive"
        # Only the two still-active channels are reordered; the deactivated
        # one never appears in a position update.
        for call in client.set_positions_calls:
            assert "c-01" not in [cid for cid, _ in call]

    def test_reconciliation_is_idempotent_after_order_is_corrected(self, tmp_path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_minimal_notifications_yaml(config_dir)

        client = self._client_with_channels([("c-01", "job-01", 0), ("c-83", "job-83", 1)])
        registry = {
            "dept-83": _registry_entry(DiscordLocationChannelConfig, "c-83"),
            "dept-01": _registry_entry(DiscordLocationChannelConfig, "c-01"),
        }
        lock_path, env_path = tmp_path / "l.lock", tmp_path / ".env"
        plan = ReconciliationPlan(keep=["dept-83", "dept-01"], desired_order=["dept-83", "dept-01"])

        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=lock_path, env_path=env_path, location_category_id=self.CATEGORY,
        )
        assert len(client.set_positions_calls) == 1  # first call actually reorders

        reconcile(
            plan, registry, client, config_dir=str(config_dir),
            lock_path=lock_path, env_path=env_path, location_category_id=self.CATEGORY,
        )
        assert len(client.set_positions_calls) == 1  # second call is a no-op
