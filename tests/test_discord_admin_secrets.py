"""Tests for src.discord_admin.secrets — the only code allowed to hold a
raw webhook URL. Verifies the atomic .env read-modify-write and the
in-process os.environ update, using a temp file (never the real .env).
"""

from __future__ import annotations

import os

from src.discord_admin.secrets import redact, store_webhook_secret


class TestRedact:
    def test_shows_only_last_four_characters(self):
        assert redact("https://discord.com/api/webhooks/123/abcdEFGH") == "...EFGH"

    def test_short_value_is_fully_masked(self):
        assert redact("abc") == "***"


class TestStoreWebhookSecret:
    def test_appends_new_var_to_empty_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        store_webhook_secret("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://example/1", env_path)
        content = env_path.read_text(encoding="utf-8")
        assert "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL=https://example/1" in content

    def test_preserves_other_existing_lines(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("SOME_OTHER_VAR=keep-me\n# a comment\n", encoding="utf-8")
        store_webhook_secret("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://example/1", env_path)
        content = env_path.read_text(encoding="utf-8")
        assert "SOME_OTHER_VAR=keep-me" in content
        assert "# a comment" in content
        assert "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL=https://example/1" in content

    def test_replaces_existing_value_for_same_var_in_place(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text(
            "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL=https://old\nOTHER=x\n", encoding="utf-8"
        )
        store_webhook_secret("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://new", env_path)
        lines = env_path.read_text(encoding="utf-8").splitlines()
        assert lines.count("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL=https://new") == 1
        assert "https://old" not in env_path.read_text(encoding="utf-8")
        assert "OTHER=x" in lines

    def test_updates_process_environment_immediately(self, tmp_path):
        env_path = tmp_path / ".env"
        var_name = "DISCORD_LOCATION_DEPT_TEST_WEBHOOK_URL"
        try:
            store_webhook_secret(var_name, "https://example/live", env_path)
            assert os.environ[var_name] == "https://example/live"
        finally:
            os.environ.pop(var_name, None)

    def test_creates_env_file_when_missing(self, tmp_path):
        env_path = tmp_path / "nested" / ".env"
        store_webhook_secret("X_WEBHOOK_URL", "https://example/2", env_path)
        assert env_path.exists()
