"""GET /api/pipeline/notifications/status exposes the sidebar's Discord link.

The link is built from DISCORD_GUILD_ID, so the dashboard links to the
user's own server, or shows no link at all — never a hard-coded one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import app


@pytest.fixture()
def client(monkeypatch):
    # No engine: the route answers from its built-in default, no database opened.
    monkeypatch.setattr(app.state, "notification_engine", None)
    return TestClient(app)


def test_no_link_without_guild_id(client, monkeypatch):
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    body = client.get("/api/pipeline/notifications/status").json()
    assert body["discord_server_url"] is None


def test_link_from_guild_id(client, monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", " 123456789012345678 ")  # gitleaks:allow (synthetic guild ID)
    body = client.get("/api/pipeline/notifications/status").json()
    assert body["discord_server_url"] == "https://discord.com/channels/123456789012345678"


@pytest.mark.parametrize("value", ["", "abc", "123/../evil", "https://evil.example"])
def test_non_numeric_guild_id_gives_no_link(client, monkeypatch, value):
    monkeypatch.setenv("DISCORD_GUILD_ID", value)
    body = client.get("/api/pipeline/notifications/status").json()
    assert body["discord_server_url"] is None


def test_frontend_has_no_hard_coded_discord_server():
    from pathlib import Path

    sidebar = Path(__file__).resolve().parents[1] / "frontend/src/components/Sidebar.tsx"
    assert "discord.com/channels/" not in sidebar.read_text(encoding="utf-8")
