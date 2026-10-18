"""Tests for src.notifications.discord_reconciler_worker.DiscordReconcilerWorker
— the automatic, Claude/MCP-independent reconciliation loop.

Covers:
- start() is idempotent (the primary in-process guard against duplicate
  worker threads, independent of the cross-process advisory file lock).
- tick() skips harmlessly when Discord credentials aren't configured.
- tick() recomputes desired state from the CURRENT profile every time.
- tick() delegates to the shared src.discord_admin library — no duplicated
  Discord logic in the worker itself.
- A failure in one location does not prevent others, and does not kill the
  loop thread.
- Two overlapping reconciliation attempts are serialized by the advisory
  lock (the cross-process safety net), never producing duplicate infra.
- Interval is configurable via DISCORD_RECONCILER_INTERVAL_SECONDS.

Uses a hand-written fake Discord client (see test_discord_admin_reconciler.py)
and temp files throughout — nothing here touches the real .env,
config/notifications.yaml, or config/profile.yaml.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
import yaml

from src.notifications.config import load_notification_config
from src.notifications.discord_reconciler_worker import (
    DiscordReconcilerWorker,
    _interval_from_env,
)


class FakeDiscordClient:
    """Same duck-typed fake used by test_discord_admin_reconciler.py,
    duplicated locally to keep this test module self-contained and to allow
    an optional artificial delay for concurrency testing.
    """

    def __init__(self, channels=None, webhooks_by_channel=None, delay: float = 0.0, fail_slug: str | None = None):
        self.channels = list(channels or [])
        self.webhooks = {cid: list(whs) for cid, whs in (webhooks_by_channel or {}).items()}
        self._next_id = 1000
        self.create_channel_calls: list[str] = []
        self.create_webhook_calls: list[str] = []
        self._delay = delay
        self._fail_slug = fail_slug

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def list_channels(self):
        return list(self.channels)

    def get_channel(self, channel_id):
        for c in self.channels:
            if c["id"] == channel_id:
                return c
        return None

    def create_channel(self, name, parent_id=None):
        if self._fail_slug and self._fail_slug in name:
            raise RuntimeError(f"simulated failure creating {name}")
        if self._delay:
            time.sleep(self._delay)
        self.create_channel_calls.append(name)
        channel = {"id": self._new_id(), "name": name, "parent_id": parent_id}
        self.channels.append(channel)
        self.webhooks[channel["id"]] = []
        return channel

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

    def close(self):
        pass


def _write_profile(path, departments):
    prefs = [{"type": "department", "value": d, "status": "preferred"} for d in departments]
    Path(path).write_text(
        yaml.safe_dump({"geography": {"location_preferences": prefs}}),
        encoding="utf-8",
    )


def _write_notifications_yaml(config_dir):
    """Static channels are pre-provisioned so these job/top10-focused
    worker tests aren't muddied by the independent static-channel
    reconciliation pass also having work to do — see
    tests/test_discord_admin_service.py's TestStaticChannelReconciliation
    for dedicated coverage of that pass."""
    from src.notifications.config import DISCORD_STATIC_CHANNEL_ENV_VARS

    (config_dir / "notifications.yaml").write_text(
        yaml.safe_dump({
            "notifications": {
                "enabled": True,
                "discord": {
                    "enabled": True,
                    "channels": {
                        name: {"enabled": True, "channel_id": f"static-{name}", "webhook_id": f"wh-{name}"}
                        for name in DISCORD_STATIC_CHANNEL_ENV_VARS
                    },
                },
            }
        }),
        encoding="utf-8",
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_notifications_yaml(config_dir)
    profile_path = config_dir / "profile.yaml"
    _write_profile(profile_path, ["83"])

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild-1")

    return {
        "config_dir": str(config_dir),
        "profile_path": str(profile_path),
        "env_path": tmp_path / ".env",
        "lock_path": tmp_path / "data" / "reconcile.lock",
    }


class TestStartIsIdempotent:
    def test_second_start_does_not_spawn_a_new_thread(self, env):
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            interval_seconds=1000,  # long enough that no real tick fires mid-test
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: FakeDiscordClient(),
        )
        try:
            worker.start()
            first_thread = worker._thread
            assert first_thread is not None and first_thread.is_alive()

            worker.start()  # second call — must be a no-op
            assert worker._thread is first_thread
            assert threading.active_count() >= 1
        finally:
            worker.stop()

    def test_stop_then_start_again_creates_a_fresh_thread(self, env):
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            interval_seconds=1000,
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: FakeDiscordClient(),
        )
        worker.start()
        first_thread = worker._thread
        worker.stop()
        assert not first_thread.is_alive()

        worker.start()
        try:
            assert worker._thread is not first_thread
            assert worker.is_running()
        finally:
            worker.stop()


class TestTickCredentialGating:
    def test_missing_bot_token_skips_without_error(self, env, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: (_ for _ in ()).throw(AssertionError("must not build a client")),
        )
        assert worker.tick() == "no_credentials"

    def test_missing_guild_id_skips_without_error(self, env, monkeypatch):
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: (_ for _ in ()).throw(AssertionError("must not build a client")),
        )
        assert worker.tick() == "no_credentials"


class TestTickReconciliation:
    def test_tick_creates_infrastructure_for_desired_location(self, env):
        client = FakeDiscordClient()
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: client,
        )
        assert worker.tick() == "reconciled"
        # Each desired location now gets BOTH its job-feed channel and its
        # top10 leaderboard companion — see discord_admin.service's
        # run_reconciliation_pass docstring.
        assert client.create_channel_calls == ["job-83", "top10-83"]

        saved = load_notification_config(env["config_dir"])
        assert saved.discord.location_channels["dept-83"].enabled is True
        assert saved.discord.top10_channels["dept-83"].enabled is True

    def test_tick_with_nothing_to_do_reports_no_changes(self, env):
        client = FakeDiscordClient()
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: client,
        )
        worker.tick()  # first tick creates it
        assert client.create_channel_calls == ["job-83", "top10-83"]

        second_status = worker.tick()  # nothing changed since
        assert second_status == "no_changes"
        assert client.create_channel_calls == ["job-83", "top10-83"]  # still just the originals

    def test_tick_recomputes_from_current_profile_every_time(self, env):
        """Editing the profile between ticks must be picked up on the very
        next tick, with no restart and no cache."""
        client = FakeDiscordClient()
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: client,
        )
        worker.tick()
        assert client.create_channel_calls == ["job-83", "top10-83"]

        _write_profile(env["profile_path"], ["83", "06"])  # add a department
        worker.tick()
        assert client.create_channel_calls == ["job-83", "top10-83", "job-06", "top10-06"]

    def test_malformed_profile_is_skipped_not_fatal(self, env):
        with open(env["profile_path"], "w", encoding="utf-8") as f:
            f.write("not: valid: yaml: [")
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: FakeDiscordClient(),
        )
        assert worker.tick() == "error"


class TestPartialFailureIsolation:
    def test_one_failing_location_does_not_block_others(self, env):
        _write_profile(env["profile_path"], ["83", "06"])
        client = FakeDiscordClient(fail_slug="job-83")
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: client,
        )
        status = worker.tick()
        # fail_slug only matches the literal string "job-83" — dept-83's
        # top10 channel ("top10-83") is a different Discord channel name
        # and is unaffected, demonstrating the two channel kinds' failure
        # domains are fully independent.
        assert status == "reconciled"
        assert client.create_channel_calls == ["job-06", "top10-83", "top10-06"]

        saved = load_notification_config(env["config_dir"])
        assert saved.discord.location_channels["dept-06"].enabled is True
        assert "dept-83" not in saved.discord.location_channels
        assert saved.discord.top10_channels["dept-83"].enabled is True
        assert saved.discord.top10_channels["dept-06"].enabled is True

    def test_failed_slug_is_retried_on_the_next_tick(self, env):
        _write_profile(env["profile_path"], ["83"])
        failing_client = FakeDiscordClient(fail_slug="job-83")
        worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: failing_client,
        )
        worker.tick()
        saved = load_notification_config(env["config_dir"])
        assert "dept-83" not in saved.discord.location_channels
        # top10-83 is unaffected by the job-only failure and succeeds immediately.
        assert saved.discord.top10_channels["dept-83"].enabled is True

        recovered_client = FakeDiscordClient()
        worker._client_factory = lambda: recovered_client
        worker.tick()
        saved = load_notification_config(env["config_dir"])
        assert saved.discord.location_channels["dept-83"].enabled is True


class TestConcurrentReconciliationIsSerialized:
    def test_two_overlapping_ticks_never_both_write_at_once(self, env):
        """Force a real overlap (one tick's Discord call sleeps while
        holding the lock) and confirm the second tick backs off cleanly
        instead of racing on the registry file."""
        _write_profile(env["profile_path"], ["83"])
        slow_client = FakeDiscordClient(delay=0.3)
        fast_client = FakeDiscordClient()

        slow_worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: slow_client,
        )
        fast_worker = DiscordReconcilerWorker(
            config_dir=env["config_dir"],
            profile_path=env["profile_path"],
            lock_path=env["lock_path"],
            env_path=env["env_path"],
            client_factory=lambda: fast_client,
        )

        results: dict[str, str] = {}

        def run_slow():
            results["slow"] = slow_worker.tick()

        def run_fast():
            time.sleep(0.05)  # ensure slow_worker acquires the lock first
            results["fast"] = fast_worker.tick()

        t1 = threading.Thread(target=run_slow)
        t2 = threading.Thread(target=run_fast)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert results["slow"] == "reconciled"
        assert results["fast"] == "locked"
        # slow_worker's tick creates BOTH channel kinds for dept-83 (job-83
        # then top10-83, each delayed) — fast_worker's whole tick executes
        # within that window and finds the lock held both times it tries,
        # so it creates nothing. No duplicate infrastructure either way.
        assert slow_client.create_channel_calls == ["job-83", "top10-83"]
        assert fast_client.create_channel_calls == []


class TestIntervalFromEnv:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("DISCORD_RECONCILER_INTERVAL_SECONDS", raising=False)
        assert _interval_from_env(default=300) == 300

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("DISCORD_RECONCILER_INTERVAL_SECONDS", "60")
        assert _interval_from_env() == 60

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DISCORD_RECONCILER_INTERVAL_SECONDS", "not-a-number")
        assert _interval_from_env(default=300) == 300

    def test_too_low_value_is_floored(self, monkeypatch):
        monkeypatch.setenv("DISCORD_RECONCILER_INTERVAL_SECONDS", "1")
        assert _interval_from_env() == 30
