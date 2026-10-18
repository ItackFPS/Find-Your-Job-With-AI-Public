"""Thin Discord REST v10 wrapper for channel/webhook administration.

Channel/webhook create/list/get are plain bot-token REST calls — no gateway
connection needed, so this client is stateless and safe to use from a
short-lived MCP tool call or a long-running backend worker alike.

This is the ONLY module in the whole system that sends HTTP requests to
Discord for administration purposes. src/notifications/discord.py (the
existing outbound webhook sender) is unrelated and untouched.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://discord.com/api/v10"
_GUILD_TEXT_CHANNEL_TYPE = 0
_TIMEOUT = 10.0
_MAX_RATE_LIMIT_RETRIES = 3


class DiscordAdminError(Exception):
    """Base class for administration-layer errors."""


class DiscordPermissionError(DiscordAdminError):
    """The bot lacks a required permission (403) for this operation."""


class DiscordRateLimitExceededError(DiscordAdminError):
    """Discord kept rate-limiting this request past the retry budget."""


class DiscordAdminClient:
    """Bot-token REST client scoped to one guild.

    ``bot_token`` and ``guild_id`` are passed explicitly (never read from a
    module-level global) so both the automatic worker and the MCP server can
    construct independent instances from their own environment without
    sharing process state.
    """

    def __init__(self, bot_token: str, guild_id: str) -> None:
        self._guild_id = guild_id
        self._client = httpx.Client(
            base_url=_API_BASE,
            headers={"Authorization": f"Bot {bot_token}"},
            timeout=_TIMEOUT,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "DiscordAdminClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- internal request helper ------------------------------------------------

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | list[Any] | None = None
    ) -> httpx.Response:
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            response = self._client.request(method, path, json=json)
            if response.status_code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(response.json().get("retry_after", 1.0))
                except Exception:
                    pass
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    raise DiscordRateLimitExceededError(
                        f"{method} {path} rate-limited after {attempt + 1} attempts"
                    )
                logger.warning(
                    "Discord rate limit on %s %s — retrying in %.1fs (attempt %d/%d)",
                    method,
                    path,
                    retry_after,
                    attempt + 1,
                    _MAX_RATE_LIMIT_RETRIES,
                )
                time.sleep(retry_after)
                continue
            if response.status_code == 403:
                raise DiscordPermissionError(
                    f"Bot missing required permission for {method} {path}"
                )
            return response
        raise DiscordRateLimitExceededError(f"{method} {path} rate-limited")

    # -- channels -----------------------------------------------------------

    def list_channels(self) -> list[dict[str, Any]]:
        response = self._request("GET", f"/guilds/{self._guild_id}/channels")
        response.raise_for_status()
        return list(response.json())

    def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/channels/{channel_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    def create_channel(self, name: str, parent_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"name": name, "type": _GUILD_TEXT_CHANNEL_TYPE}
        if parent_id:
            body["parent_id"] = parent_id
        response = self._request("POST", f"/guilds/{self._guild_id}/channels", json=body)
        response.raise_for_status()
        return dict(response.json())

    def move_channel(self, channel_id: str, parent_id: str | None) -> dict[str, Any]:
        """Change one channel's category (parent_id) — purely cosmetic
        (sidebar organization in Discord), used only to move a deactivated
        location's channels into/out of an operator-configured "inactive"
        category. Never affects notification routing, which is controlled
        entirely by the registry's ``enabled`` flag regardless of where a
        channel visually sits. ``parent_id=None`` moves the channel to the
        guild top level (out of any category).
        """
        response = self._request("PATCH", f"/channels/{channel_id}", json={"parent_id": parent_id})
        response.raise_for_status()
        return dict(response.json())

    def set_channel_positions(self, positions: list[tuple[str, int]]) -> None:
        """Bulk-reorder sibling channels in one atomic request — Discord's
        documented way to reorder several channels at once (PATCH
        /guilds/{guild.id}/channels with a list of {id, position}), instead
        of one PATCH per channel (which would be both slower and racier
        under Discord's per-route rate limit). ``position`` is only
        meaningful relative to a channel's siblings (channels sharing the
        same parent_id); it does not need to be globally unique.
        """
        if not positions:
            return
        body = [{"id": channel_id, "position": position} for channel_id, position in positions]
        response = self._request("PATCH", f"/guilds/{self._guild_id}/channels", json=body)
        response.raise_for_status()

    # -- webhooks -------------------------------------------------------------

    def list_webhooks(self, channel_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"/channels/{channel_id}/webhooks")
        response.raise_for_status()
        return list(response.json())

    def get_webhook(self, webhook_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/webhooks/{webhook_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return dict(response.json())

    def create_webhook(self, channel_id: str, name: str) -> dict[str, Any]:
        response = self._request(
            "POST", f"/channels/{channel_id}/webhooks", json={"name": name}
        )
        response.raise_for_status()
        return dict(response.json())


def webhook_url(webhook: dict[str, Any]) -> str | None:
    """Build the usable incoming-webhook URL from a Discord webhook object,
    or None if it has no retrievable token (e.g. it isn't an incoming
    webhook the bot can use) — callers must treat that the same as an
    ambiguous match: never guess, never silently create a second webhook
    without first checking why the token is missing.
    """
    webhook_id = webhook.get("id")
    token = webhook.get("token")
    if not webhook_id or not token:
        return None
    return f"https://discord.com/api/webhooks/{webhook_id}/{token}"
