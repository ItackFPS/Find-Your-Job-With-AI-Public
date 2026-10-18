"""Tests for src.discord_admin.mcp_server — Claude's manual/on-demand
Discord administration tools. Calls the plain (undecorated-in-effect)
tool functions directly rather than spinning up the actual MCP stdio
transport — FastMCP's mcp.tool()(fn) returns fn unchanged (verified), so
this exercises exactly the same code Claude's tool calls would run.

Every test monkeypatches the module's config/profile/lock/env path globals
and injects a fake client factory (same convention as
src/api/routes/profile.py's _PROFILE_PATH monkeypatching) — nothing here
ever touches the real project .env, config/notifications.yaml, or the real
Discord API.
"""

from __future__ import annotations

import pytest
import yaml

import src.discord_admin.mcp_server as mcp_server


class FakeDiscordClient:
    def __init__(self, channels=None, webhooks_by_channel=None):
        self.channels = list(channels or [])
        self.webhooks = {cid: list(whs) for cid, whs in (webhooks_by_channel or {}).items()}
        self._next_id = 1000
        self.create_channel_calls: list[str] = []

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def list_channels(self):
        return list(self.channels)

    def get_channel(self, channel_id):
        return next((c for c in self.channels if c["id"] == channel_id), None)

    def create_channel(self, name, parent_id=None):
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
        webhook = {"id": self._new_id(), "name": name, "token": f"tok-{self._new_id()}"}
        self.webhooks.setdefault(channel_id, []).append(webhook)
        return webhook

    def close(self):
        pass


def _write_config(config_dir):
    """Static channels are pre-provisioned so the tests below (about
    job/top10 location-channel behavior) aren't muddied by the independent
    static-channel reconciliation pass also having work to do — see
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


def _write_profile(path, departments):
    prefs = [{"type": "department", "value": d, "status": "preferred"} for d in departments]
    path.write_text(yaml.safe_dump({"geography": {"location_preferences": prefs}}), encoding="utf-8")


@pytest.fixture
def wired(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_config(config_dir)
    profile_path = config_dir / "profile.yaml"
    _write_profile(profile_path, ["83"])

    monkeypatch.setattr(mcp_server, "_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(mcp_server, "_PROFILE_PATH", str(profile_path))
    monkeypatch.setattr(mcp_server, "_LOCK_PATH", tmp_path / "l.lock")
    monkeypatch.setattr(mcp_server, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("DISCORD_GUILD_ID", "guild-1")

    return {"config_dir": config_dir, "profile_path": profile_path}


class TestNoDeleteToolsExist:
    def test_module_exposes_no_delete_capability(self):
        public_names = [n for n in dir(mcp_server) if not n.startswith("_")]
        assert not any("delete" in n.lower() or "remove" in n.lower() for n in public_names)


class TestListChannelsRequiresCredentials:
    def test_missing_credentials_raises_clear_error(self, wired, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="DISCORD_BOT_TOKEN"):
            mcp_server.list_channels()


class TestListChannelsSanitization:
    def test_returns_only_whitelisted_fields(self, wired, monkeypatch):
        client = FakeDiscordClient(
            channels=[{"id": "1", "name": "job-83", "type": 0, "parent_id": "cat-1", "topic": "secret internal note"}]
        )
        monkeypatch.setattr(mcp_server, "_client_factory", lambda: client)
        result = mcp_server.list_channels()
        assert result == [{"id": "1", "name": "job-83", "type": 0, "parent_id": "cat-1"}]
        assert "topic" not in result[0]


class TestListWebhooksNeverExposesTheSecret:
    def test_token_field_is_stripped(self, wired, monkeypatch):
        client = FakeDiscordClient(
            webhooks_by_channel={"1": [{"id": "wh-1", "name": "job-83-webhook", "channel_id": "1", "token": "SUPER-SECRET-TOKEN"}]}
        )
        monkeypatch.setattr(mcp_server, "_client_factory", lambda: client)
        result = mcp_server.list_webhooks("1")
        assert result == [{"id": "wh-1", "name": "job-83-webhook", "channel_id": "1"}]
        assert "token" not in result[0]
        assert "SUPER-SECRET-TOKEN" not in str(result)


class TestGetReconciliationPlan:
    def test_returns_desired_activate_list_for_both_channel_kinds_without_touching_discord(self, wired):
        """No client_factory is set here at all — if this touched Discord
        it would raise, since no fake client is wired up."""
        plan = mcp_server.get_reconciliation_plan()
        assert plan["job"]["activate"] == [
            {"slug": "dept-83", "channel_name": "job-83", "label": None, "kind": "department"}
        ]
        assert plan["top10"]["activate"] == [
            {"slug": "dept-83", "channel_name": "top10-83", "label": None, "kind": "department"}
        ]
        assert plan["job"]["deactivate"] == []
        assert plan["static"]["is_empty"] is True  # pre-provisioned by _write_config
        assert plan["is_empty"] is False


class TestListReconciliationIssues:
    def test_empty_registry_has_no_issues(self, wired):
        assert mcp_server.list_reconciliation_issues() == []


class TestReconcileNow:
    def test_creates_infrastructure_and_returns_redacted_outcome(self, wired, monkeypatch):
        client = FakeDiscordClient()
        monkeypatch.setattr(mcp_server, "_client_factory", lambda: client)

        result = mcp_server.reconcile_now()

        assert result["status"] == "reconciled"
        assert result["error"] is None
        # Both channel kinds get reconciled for the one desired location.
        assert len(result["outcomes"]) == 2
        assert {o["channel_kind"] for o in result["outcomes"]} == {"job", "top10"}
        for outcome in result["outcomes"]:
            assert outcome["slug"] == "dept-83"
            assert outcome["outcome"] == "created"
            # The detail is a human-readable confirmation, never the raw secret.
            assert "webhook stored as" in outcome["detail"]
            assert "discord.com/api/webhooks" not in outcome["detail"]
        assert client.create_channel_calls == ["job-83", "top10-83"]

    def test_missing_credentials_returns_status_not_exception(self, wired, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        result = mcp_server.reconcile_now()
        assert result["status"] == "no_credentials"

    def test_never_touches_real_env_or_lock_file(self, wired, monkeypatch, tmp_path):
        """Regression guard: reconcile_now must use the monkeypatched
        _ENV_PATH/_LOCK_PATH, never the real project files."""
        real_env = tmp_path.parent / "should_not_be_touched.env"
        assert not real_env.exists()
        client = FakeDiscordClient()
        monkeypatch.setattr(mcp_server, "_client_factory", lambda: client)
        mcp_server.reconcile_now()
        assert not real_env.exists()
        assert (mcp_server._LOCK_PATH).exists() is False  # released after use


def test_profile_path_resolves_personal_override_when_not_pinned(tmp_path, monkeypatch):
    """Issue #8: with _PROFILE_PATH unset, config/profile.local.yaml wins
    over the tracked config/profile.yaml."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(mcp_server, "_PROFILE_PATH", None)

    assert mcp_server._profile_path() == str(config_dir / "profile.yaml")
    (config_dir / "profile.local.yaml").write_text("{}\n", encoding="utf-8")
    assert mcp_server._profile_path() == str(config_dir / "profile.local.yaml")
