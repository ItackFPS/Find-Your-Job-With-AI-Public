"""Tests for src.discord_admin.service.run_reconciliation_pass — the single
orchestration function BOTH DiscordReconcilerWorker (automatic) and the MCP
server's reconcile_now tool (manual/on-demand) call. Covered here once so
neither caller needs its own duplicate test of this logic.
"""

from __future__ import annotations

import yaml

import src.discord_admin.service as service_module
from src.discord_admin.service import run_reconciliation_pass
from src.notifications.config import DISCORD_STATIC_CHANNEL_ENV_VARS


class FakeDiscordClient:
    def __init__(self):
        self.channels: list[dict] = []
        self.webhooks: dict[str, list[dict]] = {}
        self._next_id = 1000
        self._next_position = 1000
        self.create_channel_calls: list[str] = []
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
        return next((c for c in self.channels if c["id"] == channel_id), None)

    def create_channel(self, name, parent_id=None):
        self.create_channel_calls.append(name)
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
        webhook = {"id": self._new_id(), "name": name, "token": f"tok-{self._new_id()}"}
        self.webhooks.setdefault(channel_id, []).append(webhook)
        return webhook

    def close(self):
        pass


def _write_config_static_provisioned(config_dir):
    """Like _write_config, but with every static channel already fully
    provisioned (enabled + channel_id + webhook_id) — used by tests that
    are about job/top10-channel behavior and would otherwise be muddied by
    the static-channel reconciliation pass also having work to do on a
    fresh/minimal config (see TestStaticChannelReconciliation below for
    dedicated static-channel coverage)."""
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


def _write_config(config_dir):
    (config_dir / "notifications.yaml").write_text(
        yaml.safe_dump({"notifications": {"enabled": True, "discord": {"enabled": True}}}),
        encoding="utf-8",
    )


def _write_profile(path, departments):
    prefs = [{"type": "department", "value": d, "status": "preferred"} for d in departments]
    path.write_text(yaml.safe_dump({"geography": {"location_preferences": prefs}}), encoding="utf-8")


class TestCredentialGating:
    def test_missing_bot_token_reports_no_credentials(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        report = run_reconciliation_pass(str(tmp_path), str(tmp_path / "profile.yaml"))
        assert report.status == "no_credentials"

    def test_missing_guild_id_reports_no_credentials(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
        report = run_reconciliation_pass(str(tmp_path), str(tmp_path / "profile.yaml"))
        assert report.status == "no_credentials"


class TestFullPass:
    def test_successful_reconciliation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config_static_provisioned(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83"])

        client = FakeDiscordClient()
        report = run_reconciliation_pass(
            str(config_dir),
            str(profile_path),
            lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env",
            client_factory=lambda: client,
        )
        assert report.status == "reconciled"
        # One pass now reconciles BOTH channel kinds for a desired location:
        # the job-feed channel ("job-83") and its top10 leaderboard
        # companion ("top10-83") — see run_reconciliation_pass's docstring.
        assert len(report.outcomes) == 2
        assert {o.outcome for o in report.outcomes} == {"created"}
        assert {o.channel_kind for o in report.outcomes} == {"job", "top10"}
        assert client.create_channel_calls == ["job-83", "top10-83"]

    def test_nothing_to_do_reports_no_changes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config_static_provisioned(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, [])

        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
        )
        assert report.status == "no_changes"

    def test_malformed_profile_reports_error_not_exception(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config(config_dir)
        bad_profile = tmp_path / "bad.yaml"
        bad_profile.write_text("not: [valid", encoding="utf-8")

        report = run_reconciliation_pass(str(config_dir), str(bad_profile))
        assert report.status == "error"
        assert report.error

    def test_invalid_department_note_is_not_duplicated_across_both_plans(self, tmp_path, monkeypatch):
        """Both the job-feed and top10 desired-state functions independently
        validate the same profile and would each report the same invalid
        department — the combined report must de-duplicate, not show it
        twice."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config_static_provisioned(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["999"])  # not a real department

        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
        )
        assert report.status == "no_changes"
        assert len(report.plan_invalid) == 1

    def test_top10_only_reconciliation_does_not_touch_job_channels(self, tmp_path, monkeypatch):
        """If job-feed channels are already fully reconciled but top10
        channels aren't yet (e.g. the feature was added after job channels
        already existed), only top10 should activate."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config_static_provisioned(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83"])

        client = FakeDiscordClient()
        # First pass bootstraps both.
        run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            client_factory=lambda: client,
        )
        assert set(client.create_channel_calls) == {"job-83", "top10-83"}

        # Simulate top10 having been manually deleted from the registry only
        # (the registry the pass just wrote: the personal local override).
        import yaml as _yaml

        from src.config_files import resolve_config_path
        registry = resolve_config_path(config_dir, "notifications.yaml")
        raw = _yaml.safe_load(registry.read_text(encoding="utf-8"))
        raw["notifications"]["discord"]["top10_channels"] = {}
        registry.write_text(_yaml.safe_dump(raw), encoding="utf-8")

        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            client_factory=lambda: client,
        )
        assert report.status == "reconciled"
        assert len(report.outcomes) == 1
        assert report.outcomes[0].channel_kind == "top10"
        # job-83 already existed and was adopted, not re-created; top10-83
        # channel also already exists in Discord (from the first pass) so
        # this is an ADOPT, not a second create.
        assert client.create_channel_calls == ["job-83", "top10-83"]  # still just the originals

    def test_one_kind_failing_reports_partial_and_the_other_still_applies(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config_static_provisioned(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83"])

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated top10 failure")

        monkeypatch.setattr(service_module, "reconcile_top10", _boom)

        client = FakeDiscordClient()
        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
            client_factory=lambda: client,
        )
        assert report.status == "partial"
        assert client.create_channel_calls == ["job-83"]  # job-side still succeeded
        assert report.error == "simulated top10 failure"


class TestJobAndTop10UseSeparateCategories:
    def test_job_and_top10_channels_created_under_their_own_categories(self, tmp_path, monkeypatch):
        """location_category_id (Job-Département) and top10_category_id
        (Top10-Département) are independent config fields — a job-feed
        channel and its top10 companion must not end up under the same
        category."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "notifications.yaml").write_text(
            yaml.safe_dump({
                "notifications": {
                    "enabled": True,
                    "discord": {
                        "enabled": True,
                        "location_category_id": "cat-job-dept",
                        "top10_category_id": "cat-top10-dept",
                        "channels": {
                            name: {"enabled": True, "channel_id": f"static-{name}", "webhook_id": f"wh-{name}"}
                            for name in DISCORD_STATIC_CHANNEL_ENV_VARS
                        },
                    },
                }
            }),
            encoding="utf-8",
        )
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83"])

        client = FakeDiscordClient()
        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env", client_factory=lambda: client,
        )
        assert report.status == "reconciled"

        job_channel = next(c for c in client.channels if c["name"] == "job-83")
        top10_channel = next(c for c in client.channels if c["name"] == "top10-83")
        assert job_channel["parent_id"] == "cat-job-dept"
        assert top10_channel["parent_id"] == "cat-top10-dept"


class TestChannelOrderFollowsProfileOrderEndToEnd:
    """End-to-end (through run_reconciliation_pass, the same entry point the
    automatic worker and the MCP reconcile_now tool use) proof that Discord
    channel order converges to profile.geography.location_preferences'
    order — see src.notifications.discord_reconciliation.ReconciliationPlan.
    desired_order and src.discord_admin.reconciler._reorder_active_channels.
    """

    def _config_with_categories(self, config_dir):
        (config_dir / "notifications.yaml").write_text(
            yaml.safe_dump({
                "notifications": {
                    "enabled": True,
                    "discord": {
                        "enabled": True,
                        "location_category_id": "cat-job-dept",
                        "top10_category_id": "cat-top10-dept",
                        "channels": {
                            name: {"enabled": True, "channel_id": f"static-{name}", "webhook_id": f"wh-{name}"}
                            for name in DISCORD_STATIC_CHANNEL_ENV_VARS
                        },
                    },
                }
            }),
            encoding="utf-8",
        )

    def test_multi_department_profile_creates_channels_in_profile_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._config_with_categories(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83", "01", "69"])

        client = FakeDiscordClient()
        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env", client_factory=lambda: client,
        )
        assert report.status == "reconciled"

        job_order = [c["name"] for c in sorted(
            (c for c in client.channels if c["parent_id"] == "cat-job-dept"), key=lambda c: c["position"]
        )]
        top10_order = [c["name"] for c in sorted(
            (c for c in client.channels if c["parent_id"] == "cat-top10-dept"), key=lambda c: c["position"]
        )]
        assert job_order == ["job-83", "job-01", "job-69"]
        assert top10_order == ["top10-83", "top10-01", "top10-69"]

    def test_reordered_profile_reorders_discord_on_next_pass(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._config_with_categories(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83", "01", "69"])
        lock_path, env_path = tmp_path / "l.lock", tmp_path / ".env"

        client = FakeDiscordClient()
        run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=lock_path, env_path=env_path,
            client_factory=lambda: client,
        )

        # Profile is reordered: 83, 69, 01.
        _write_profile(profile_path, ["83", "69", "01"])
        report2 = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=lock_path, env_path=env_path,
            client_factory=lambda: client,
        )
        assert report2.status == "reconciled"

        job_order = [c["name"] for c in sorted(
            (c for c in client.channels if c["parent_id"] == "cat-job-dept"), key=lambda c: c["position"]
        )]
        assert job_order == ["job-83", "job-69", "job-01"]


class TestStaticChannelReconciliation:
    """A fresh/minimal config (no location_preferences at all) still gets
    its permanent static channels (pipeline, errors, statistics,
    system-status) bootstrapped — these aren't profile-driven."""

    def test_fresh_config_bootstraps_all_permanent_channels(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, [])  # no locations — only static channels to reconcile

        client = FakeDiscordClient()
        report = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env", client_factory=lambda: client,
        )
        assert report.status == "reconciled"
        assert {o.channel_kind for o in report.outcomes} == {"static"}
        assert set(client.create_channel_calls) == {"pipeline", "errors", "statistics", "system-status"}

    def test_second_pass_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, [])

        client = FakeDiscordClient()
        run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env", client_factory=lambda: client,
        )
        report2 = run_reconciliation_pass(
            str(config_dir), str(profile_path), lock_path=tmp_path / "l.lock",
            env_path=tmp_path / ".env", client_factory=lambda: client,
        )
        assert report2.status == "no_changes"
        assert len(client.create_channel_calls) == 4  # nothing re-created


class TestLockContention:
    def test_locked_registry_reports_locked_not_exception(self, tmp_path, monkeypatch):
        from src.discord_admin.lock import reconciliation_lock

        monkeypatch.setenv("DISCORD_BOT_TOKEN", "t")
        monkeypatch.setenv("DISCORD_GUILD_ID", "g")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        _write_config(config_dir)
        profile_path = config_dir / "profile.yaml"
        _write_profile(profile_path, ["83"])
        lock_path = tmp_path / "l.lock"

        with reconciliation_lock(lock_path):
            report = run_reconciliation_pass(
                str(config_dir),
                str(profile_path),
                lock_path=lock_path,
                env_path=tmp_path / ".env",
                client_factory=lambda: FakeDiscordClient(),
            )
        assert report.status == "locked"
