"""Tests for the #system-status feature:
- NotificationEngine.build_system_status_payload() — the 5-signal health
  aggregation (backend/database/pipeline/n8n/discord config).
- NotificationEngine.update_system_status() — the standing-message
  send_or_update wiring, same pattern as statistics/top10.
- DiscordProvider._system_status_embed() — rendering.

Each signal reuses an EXISTING check (RunStorage's run-state queries,
src.pipeline.preflight's n8n reachability check, get_unconfigured_enabled_
channels from the channel-health work) rather than a new implementation —
these tests assert on that reuse via monkeypatching the underlying
functions, not by re-deriving the logic themselves.
"""

from __future__ import annotations

import uuid



# ---------------------------------------------------------------------------
# build_system_status_payload
# ---------------------------------------------------------------------------

class TestBuildSystemStatusPayload:
    def _engine(self, tmp_path, db_path=None):
        from src.notifications.config import NotificationConfig
        from src.notifications.engine import NotificationEngine
        return NotificationEngine(
            db_path=str(db_path or (tmp_path / "t.db")),
            config_dir=str(tmp_path / "config"),
            config=NotificationConfig(),
        )

    def test_backend_is_always_online(self, tmp_path):
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["backend"] == "ONLINE"

    def test_database_online_when_reachable(self, tmp_path):
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["database"] == "ONLINE"

    def test_database_degraded_when_check_fails(self, tmp_path, monkeypatch):
        from src.storage.sqlite import Storage
        monkeypatch.setattr(Storage, "init_db", lambda self: (_ for _ in ()).throw(RuntimeError("locked")))
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["database"] == "DEGRADED"

    def test_pipeline_unknown_when_no_runs_ever(self, tmp_path):
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["pipeline"] == "UNKNOWN"

    def test_pipeline_ready_when_latest_run_completed(self, tmp_path):
        db_path = tmp_path / "t.db"
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=str(db_path))
        rs.init_db()
        try:
            run_id = str(uuid.uuid4())
            rs.create_run(run_id)
            rs.complete_run(run_id, {"personalized": 0})
        finally:
            rs.close()
        engine = self._engine(tmp_path, db_path=db_path)
        payload = engine.build_system_status_payload()
        assert payload["pipeline"] == "READY"

    def test_pipeline_failed_when_latest_run_failed(self, tmp_path):
        db_path = tmp_path / "t.db"
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=str(db_path))
        rs.init_db()
        try:
            run_id = str(uuid.uuid4())
            rs.create_run(run_id)
            rs.fail_run(run_id, "boom", {})
        finally:
            rs.close()
        engine = self._engine(tmp_path, db_path=db_path)
        payload = engine.build_system_status_payload()
        assert payload["pipeline"] == "FAILED"

    def test_pipeline_running_when_a_run_is_in_progress(self, tmp_path):
        db_path = tmp_path / "t.db"
        from src.storage.runs import RunStorage
        rs = RunStorage(db_path=str(db_path))
        rs.init_db()
        try:
            rs.create_run(str(uuid.uuid4()))  # never completed/failed -> still 'running'
        finally:
            rs.close()
        engine = self._engine(tmp_path, db_path=db_path)
        payload = engine.build_system_status_payload()
        assert payload["pipeline"] == "RUNNING"

    def test_n8n_unknown_when_no_api_key(self, tmp_path, monkeypatch):
        import src.notifications.engine as engine_module

        monkeypatch.setattr(engine_module, "check_n8n_live_status", lambda: None)
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["n8n"] == "UNKNOWN"

    def test_n8n_online_when_reachable(self, tmp_path, monkeypatch):
        import src.notifications.engine as engine_module

        monkeypatch.setattr(engine_module, "check_n8n_live_status", lambda: True)
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["n8n"] == "ONLINE"

    def test_n8n_offline_when_unreachable(self, tmp_path, monkeypatch):
        import src.notifications.engine as engine_module

        monkeypatch.setattr(engine_module, "check_n8n_live_status", lambda: False)
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        assert payload["n8n"] == "OFFLINE"

    def test_n8n_offline_when_check_itself_raises(self, tmp_path, monkeypatch):
        import src.notifications.engine as engine_module

        def _boom():
            raise RuntimeError("network stack unavailable")

        monkeypatch.setattr(engine_module, "check_n8n_live_status", _boom)
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()  # must not raise
        assert payload["n8n"] == "OFFLINE"

    def test_discord_healthy_when_no_missing_channels(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        monkeypatch.setattr(engine, "get_unconfigured_enabled_channels", lambda: [])
        payload = engine.build_system_status_payload()
        assert payload["discord"] == "healthy"
        assert payload["missing_channels"] == []

    def test_discord_degraded_when_channels_missing(self, tmp_path, monkeypatch):
        engine = self._engine(tmp_path)
        missing = [{"kind": "location", "name": "dept-83"}]
        monkeypatch.setattr(engine, "get_unconfigured_enabled_channels", lambda: missing)
        payload = engine.build_system_status_payload()
        assert payload["discord"] == "degraded"
        assert payload["missing_channels"] == missing

    def test_checked_at_is_a_real_iso_timestamp(self, tmp_path):
        from datetime import datetime
        engine = self._engine(tmp_path)
        payload = engine.build_system_status_payload()
        # Must parse without raising.
        datetime.fromisoformat(payload["checked_at"])

    def test_never_raises_when_every_signal_check_fails(self, tmp_path, monkeypatch):
        from src.storage.sqlite import Storage
        from src.storage.runs import RunStorage
        monkeypatch.setattr(Storage, "init_db", lambda self: (_ for _ in ()).throw(RuntimeError("db boom")))
        monkeypatch.setattr(RunStorage, "init_db", lambda self: (_ for _ in ()).throw(RuntimeError("rs boom")))
        engine = self._engine(tmp_path)
        monkeypatch.setattr(
            engine, "get_unconfigured_enabled_channels",
            lambda: (_ for _ in ()).throw(RuntimeError("discord boom")),
        )
        payload = engine.build_system_status_payload()  # must not raise
        assert payload["database"] == "DEGRADED"
        assert payload["pipeline"] == "UNKNOWN"
        assert payload["discord"] == "degraded"
        assert payload["missing_channels"] == []


# ---------------------------------------------------------------------------
# update_system_status
# ---------------------------------------------------------------------------

class _RecordingSystemStatusProvider:
    def __init__(self, should_succeed: bool = True, returned_message_id: str = "new-msg-id"):
        self.enabled = True
        self.calls: list[tuple[dict, str]] = []
        self.should_succeed = should_succeed
        self.returned_message_id = returned_message_id

    def send_or_update(self, payload: dict, message_id: str) -> str | None:
        self.calls.append((payload, message_id))
        return self.returned_message_id if self.should_succeed else None


def _write_system_status_notifications_yaml(config_dir, *, enabled: bool = True, message_id: str = "") -> None:
    import yaml
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "notifications.yaml").write_text(
        yaml.safe_dump({
            "notifications": {
                "enabled": True,
                "discord": {
                    "enabled": True,
                    "channels": {
                        "system-status": {
                            "enabled": enabled,
                            "webhook_url_env": "X",
                            "channel_id": "chan-1",
                            "webhook_id": "wh-1",
                            "message_id": message_id,
                        }
                    },
                },
            }
        }),
        encoding="utf-8",
    )


class TestUpdateSystemStatus:
    def _engine(self, config_dir, db_path, provider):
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=str(db_path), config_dir=str(config_dir))
        engine._build_system_status_discord_channel = lambda: provider
        engine.build_system_status_payload = lambda: {"event": "system.status", "backend": "ONLINE"}
        return engine

    def test_enabled_channel_gets_updated(self, tmp_path):
        _write_system_status_notifications_yaml(tmp_path / "config")
        provider = _RecordingSystemStatusProvider()
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)

        engine.update_system_status()

        assert len(provider.calls) == 1
        payload, message_id = provider.calls[0]
        assert payload["event"] == "system.status"
        assert message_id == ""

    def test_disabled_channel_is_never_sent(self, tmp_path):
        _write_system_status_notifications_yaml(tmp_path / "config", enabled=False)
        provider = _RecordingSystemStatusProvider()
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)
        engine.update_system_status()
        assert provider.calls == []

    def test_discord_disabled_globally_skips(self, tmp_path):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "notifications.yaml").write_text(
            "notifications:\n  enabled: true\n  discord:\n    enabled: false\n", encoding="utf-8",
        )
        provider = _RecordingSystemStatusProvider()
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)
        engine.update_system_status()
        assert provider.calls == []

    def test_send_failure_does_not_persist_and_does_not_crash(self, tmp_path):
        _write_system_status_notifications_yaml(tmp_path / "config")
        provider = _RecordingSystemStatusProvider(should_succeed=False)
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)

        engine.update_system_status()  # must not raise

        from src.notifications.config import load_notification_config
        saved = load_notification_config(str(tmp_path / "config"))
        assert saved.discord.channels["system-status"].message_id == ""

    def test_new_message_id_is_persisted_to_the_registry(self, tmp_path):
        _write_system_status_notifications_yaml(tmp_path / "config", message_id="")
        provider = _RecordingSystemStatusProvider(returned_message_id="brand-new-id")
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)

        engine.update_system_status()

        from src.notifications.config import load_notification_config
        saved = load_notification_config(str(tmp_path / "config"))
        assert saved.discord.channels["system-status"].message_id == "brand-new-id"

    def test_unchanged_message_id_is_not_rewritten(self, tmp_path, monkeypatch):
        _write_system_status_notifications_yaml(tmp_path / "config", message_id="existing-id")
        provider = _RecordingSystemStatusProvider(returned_message_id="existing-id")
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)
        called = []
        monkeypatch.setattr(
            engine, "_persist_static_channel_message_id",
            lambda *a, **k: called.append(1),
        )
        engine.update_system_status()
        assert called == []

    def test_payload_build_failure_does_not_crash(self, tmp_path):
        _write_system_status_notifications_yaml(tmp_path / "config")
        provider = _RecordingSystemStatusProvider()
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)
        engine.build_system_status_payload = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        engine.update_system_status()  # must not raise
        assert provider.calls == []

    def test_never_creates_a_new_message_on_repeated_heartbeats(self, tmp_path):
        """The core anti-spam property: calling update_system_status()
        repeatedly must keep editing the SAME message id, never create a
        second one."""
        _write_system_status_notifications_yaml(tmp_path / "config", message_id="")
        provider = _RecordingSystemStatusProvider(returned_message_id="msg-1")
        engine = self._engine(tmp_path / "config", tmp_path / "t.db", provider)

        engine.update_system_status()
        engine.update_system_status()
        engine.update_system_status()

        message_ids_used = [call[1] for call in provider.calls]
        # First call has no message id yet (""); every subsequent call must
        # pass the persisted id back in, never "" again.
        assert message_ids_used[0] == ""
        assert all(mid == "msg-1" for mid in message_ids_used[1:])


# ---------------------------------------------------------------------------
# _system_status_embed
# ---------------------------------------------------------------------------

class TestSystemStatusEmbed:
    def _build(self, **overrides):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        event = {
            "event": "system.status",
            "backend": "ONLINE",
            "database": "ONLINE",
            "pipeline": "READY",
            "n8n": "UNKNOWN",
            "discord": "healthy",
            "missing_channels": [],
            "checked_at": "2026-09-12T10:00:00+00:00",
            **overrides,
        }
        return p._build_embed(event)

    def test_all_healthy_is_green(self):
        from src.notifications.discord import _COLOR_GREEN
        payload = self._build()
        assert payload["embeds"][0]["color"] == _COLOR_GREEN

    def test_database_degraded_is_red(self):
        from src.notifications.discord import _COLOR_RED
        payload = self._build(database="DEGRADED")
        assert payload["embeds"][0]["color"] == _COLOR_RED

    def test_pipeline_failed_is_red(self):
        from src.notifications.discord import _COLOR_RED
        payload = self._build(pipeline="FAILED")
        assert payload["embeds"][0]["color"] == _COLOR_RED

    def test_n8n_offline_is_red(self):
        from src.notifications.discord import _COLOR_RED
        payload = self._build(n8n="OFFLINE")
        assert payload["embeds"][0]["color"] == _COLOR_RED

    def test_discord_degraded_alone_is_yellow_not_red(self):
        from src.notifications.discord import _COLOR_YELLOW
        payload = self._build(discord="degraded")
        assert payload["embeds"][0]["color"] == _COLOR_YELLOW

    def test_missing_channels_are_listed(self):
        payload = self._build(
            discord="degraded",
            missing_channels=[{"kind": "location", "name": "dept-83"}, {"kind": "top10", "name": "dept-01"}],
        )
        fields = payload["embeds"][0]["fields"]
        unconfigured_field = next(f for f in fields if f["name"] == "Unconfigured channels")
        assert "dept-83" in unconfigured_field["value"]
        assert "dept-01" in unconfigured_field["value"]

    def test_no_missing_channels_field_when_all_configured(self):
        payload = self._build()
        field_names = [f["name"] for f in payload["embeds"][0]["fields"]]
        assert "Unconfigured channels" not in field_names

    def test_every_signal_rendered_as_a_field(self):
        payload = self._build()
        field_names = {f["name"] for f in payload["embeds"][0]["fields"]}
        assert {"Backend", "Database", "Pipeline", "n8n", "Discord config"} <= field_names

    def test_last_checked_footer_present(self):
        payload = self._build()
        assert "10:00" in payload["embeds"][0]["footer"]["text"]
