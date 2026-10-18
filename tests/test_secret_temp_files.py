"""Issue #69: temp files written during atomic saves must never linger or be
committable — .env.tmp holds every secret in .env.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name",
    [".env.tmp", "config/notifications.local.yaml.tmp", "config/profile.local.yaml.tmp"],
)
def test_temp_files_are_gitignored(name: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", name], cwd=_REPO_ROOT, check=False)
    assert result.returncode == 0, f"{name} must be gitignored"


def _failing_replace(self: Path, target: Path) -> Path:
    raise PermissionError("target locked by another process")


def test_store_webhook_secret_leaves_no_temp_file_when_the_rename_fails(tmp_path, monkeypatch):
    from src.discord_admin.secrets import store_webhook_secret

    env_path = tmp_path / ".env"
    env_path.write_text("DISCORD_BOT_TOKEN=secret\n", encoding="utf-8")
    monkeypatch.setattr(Path, "replace", _failing_replace)

    with pytest.raises(PermissionError):
        store_webhook_secret("DISCORD_X_WEBHOOK_URL", "https://discord.com/api/webhooks/1/t", env_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
    assert env_path.read_text(encoding="utf-8") == "DISCORD_BOT_TOKEN=secret\n"


def test_registry_save_leaves_no_temp_file_when_the_rename_fails(tmp_path, monkeypatch):
    from src.notifications.config import save_discord_channel_registries

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setattr(Path, "replace", _failing_replace)

    with pytest.raises(PermissionError):
        save_discord_channel_registries(str(config_dir), channels={})

    assert list(config_dir.iterdir()) == []
