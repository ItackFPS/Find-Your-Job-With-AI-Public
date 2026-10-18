"""Local personal config overrides (config/<name>.local.yaml) — issue #8."""

from __future__ import annotations

from pathlib import Path

from src.config_files import local_config_path, resolve_config_path


def test_local_path_is_sibling_with_local_suffix(tmp_path: Path) -> None:
    assert local_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.local.yaml"
    assert (
        local_config_path(str(tmp_path), "notifications.yaml")
        == tmp_path / "notifications.local.yaml"
    )


def test_resolves_to_default_when_no_local_file(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text("a: 1\n", encoding="utf-8")
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.yaml"


def test_resolves_to_default_path_even_when_nothing_exists(tmp_path: Path) -> None:
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.yaml"


def test_local_file_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / "profile.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "profile.local.yaml").write_text("a: 2\n", encoding="utf-8")
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.local.yaml"


def test_local_directory_is_not_treated_as_override(tmp_path: Path) -> None:
    (tmp_path / "profile.local.yaml").mkdir()
    assert resolve_config_path(tmp_path, "profile.yaml") == tmp_path / "profile.yaml"


def test_local_config_files_are_gitignored() -> None:
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    for name in ("config/profile.local.yaml", "config/notifications.local.yaml"):
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=repo_root, check=False
        )
        assert result.returncode == 0, f"{name} must be gitignored"


# ---------------------------------------------------------------------------
# Consumers resolve the personal override
# ---------------------------------------------------------------------------

_DEFAULT_PROFILE = "target_domains: [software_development]\ntarget_skills: [python]\n"
_LOCAL_PROFILE = "target_domains: [data_science]\ntarget_skills: [rust]\n"


def _config_dir_with_override(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(_DEFAULT_PROFILE, encoding="utf-8")
    (config_dir / "profile.local.yaml").write_text(_LOCAL_PROFILE, encoding="utf-8")
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")
    return config_dir


def test_orchestrator_uses_local_profile(tmp_path: Path) -> None:
    from src.pipeline.orchestrator import _hash_file, run_pipeline

    config_dir = _config_dir_with_override(tmp_path)
    result = run_pipeline(db_path=str(tmp_path / "t.db"), config_dir=str(config_dir))

    assert result.status == "completed", result.error
    assert result.stats.profile_hash == _hash_file(str(config_dir / "profile.local.yaml"))


def test_jobs_route_profile_loader_uses_local_profile(tmp_path: Path) -> None:
    from src.api.routes.jobs import _load_profile_safe

    profile = _load_profile_safe(_config_dir_with_override(tmp_path))
    assert profile.target_skills == ["rust"]


def test_reconciler_worker_resolves_profile_on_each_tick(tmp_path: Path, monkeypatch) -> None:
    import src.notifications.discord_reconciler_worker as worker_mod

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(_DEFAULT_PROFILE, encoding="utf-8")

    seen: list[str] = []

    class _Report:
        status = "no_credentials"
        plan_invalid: list[str] = []
        outcomes: list = []

    def _fake_pass(config_dir_arg, profile_path, **kwargs):
        seen.append(profile_path)
        return _Report()

    monkeypatch.setattr(worker_mod, "run_reconciliation_pass", _fake_pass)
    worker = worker_mod.DiscordReconcilerWorker(
        config_dir=str(config_dir), lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env"
    )

    worker.tick()
    (config_dir / "profile.local.yaml").write_text(_LOCAL_PROFILE, encoding="utf-8")
    worker.tick()

    assert seen == [str(config_dir / "profile.yaml"), str(config_dir / "profile.local.yaml")]


def test_reconciler_worker_explicit_profile_path_is_kept(tmp_path: Path, monkeypatch) -> None:
    import src.notifications.discord_reconciler_worker as worker_mod

    config_dir = _config_dir_with_override(tmp_path)
    explicit = str(tmp_path / "explicit.yaml")
    seen: list[str] = []

    class _Report:
        status = "no_credentials"
        plan_invalid: list[str] = []
        outcomes: list = []

    monkeypatch.setattr(
        worker_mod,
        "run_reconciliation_pass",
        lambda _c, profile_path, **_k: seen.append(profile_path) or _Report(),
    )
    worker_mod.DiscordReconcilerWorker(
        config_dir=str(config_dir), profile_path=explicit,
        lock_path=tmp_path / "l.lock", env_path=tmp_path / ".env",
    ).tick()
    assert seen == [explicit]


# ---------------------------------------------------------------------------
# notifications.yaml
# ---------------------------------------------------------------------------

_DEFAULT_NOTIFICATIONS = """\
notifications:
  enabled: true
  discord:
    enabled: true
    channels:
      pipeline:
        enabled: true
"""


def test_notification_config_prefers_local_file(tmp_path: Path) -> None:
    from src.notifications.config import load_notification_config

    (tmp_path / "notifications.yaml").write_text(_DEFAULT_NOTIFICATIONS, encoding="utf-8")
    assert load_notification_config(str(tmp_path)).discord.channels["pipeline"].enabled is True

    (tmp_path / "notifications.local.yaml").write_text(
        _DEFAULT_NOTIFICATIONS.replace("      pipeline:\n        enabled: true",
                                       "      pipeline:\n        enabled: false"),
        encoding="utf-8",
    )
    assert load_notification_config(str(tmp_path)).discord.channels["pipeline"].enabled is False


def test_registry_save_writes_local_file_seeded_from_default(tmp_path: Path) -> None:
    import yaml

    from src.notifications.config import (
        DiscordChannelConfig,
        load_notification_config,
        save_discord_channel_registries,
    )

    default = tmp_path / "notifications.yaml"
    default.write_text(_DEFAULT_NOTIFICATIONS, encoding="utf-8")
    before = default.read_bytes()

    save_discord_channel_registries(
        str(tmp_path),
        channels={"pipeline": DiscordChannelConfig(enabled=True, channel_id="c-1", webhook_id="w-1")},
    )

    assert default.read_bytes() == before
    local = yaml.safe_load((tmp_path / "notifications.local.yaml").read_text(encoding="utf-8"))
    assert local["notifications"]["enabled"] is True  # seeded from the default
    assert local["notifications"]["discord"]["channels"]["pipeline"]["channel_id"] == "c-1"
    assert load_notification_config(str(tmp_path)).discord.channels["pipeline"].channel_id == "c-1"


def test_registry_save_updates_existing_local_file(tmp_path: Path) -> None:
    from src.notifications.config import (
        DiscordChannelConfig,
        load_notification_config,
        save_discord_channel_registries,
    )

    (tmp_path / "notifications.yaml").write_text(_DEFAULT_NOTIFICATIONS, encoding="utf-8")
    (tmp_path / "notifications.local.yaml").write_text(
        _DEFAULT_NOTIFICATIONS.replace("  enabled: true\n  discord:", "  enabled: false\n  discord:"),
        encoding="utf-8",
    )
    save_discord_channel_registries(
        str(tmp_path), channels={"pipeline": DiscordChannelConfig(channel_id="c-2")}
    )

    cfg = load_notification_config(str(tmp_path))
    assert cfg.enabled is False  # personal value kept, not reset from the default
    assert cfg.discord.channels["pipeline"].channel_id == "c-2"
