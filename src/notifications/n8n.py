"""n8n webhook notification provider.

Posts the raw event dict as JSON to the configured n8n webhook URL.
The payload is stable (versioned) so n8n workflows can rely on field
names without breaking when new optional fields are added.

Webhook URL precedence (first non-empty wins):
  1. N8N_WEBHOOK_URL environment variable
  2. webhook_url from notifications.yaml

The Discord webhook URL is forwarded as a request header (not stored in
n8n) so the n8n workflow can post to Discord without keeping the secret
in a Variable/Credential — n8n Community edition doesn't have Variables
available. The Python side (this repo's .env) stays the single source
of truth for the secret.

If n8n is unavailable:
  - provider.send() returns False
  - pipeline status is not affected
  - the failure is logged and recorded in notification_history
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from src.notifications.http_retry import send_with_retry

logger = logging.getLogger(__name__)

_TIMEOUT = 15.0  # seconds; n8n may take a moment to respond


class N8nProvider:
    """Send notifications to n8n via a generic webhook trigger."""

    def __init__(self, webhook_url: str = "") -> None:
        self._webhook_url = (
            os.environ.get("N8N_WEBHOOK_URL", "")
            or webhook_url
        )

    # -- NotificationProvider interface --------------------------------------

    @property
    def name(self) -> str:
        return "n8n"

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def send(self, payload: dict[str, Any]) -> bool:
        """POST the event payload to the n8n webhook.

        Returns True on HTTP 2xx, False on any error.
        """
        if not self._webhook_url:
            logger.debug("n8n provider: no webhook URL configured, skipping.")
            return False
        headers = {}
        discord_webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if discord_webhook_url:
            headers["X-Discord-Webhook-Url"] = discord_webhook_url
        try:
            resp = send_with_retry(lambda: httpx.post(
                self._webhook_url,
                json=payload,
                headers=headers,
                timeout=_TIMEOUT,
            ))
            resp.raise_for_status()
            logger.debug("n8n: sent event '%s'", payload.get("event"))
            return True
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "n8n webhook returned HTTP %d: %s",
                exc.response.status_code,
                exc.response.text[:200],
            )
            return False
        except Exception as exc:
            logger.warning("n8n notification failed: %s", exc)
            return False


_LIVE_STATUS_TIMEOUT = 3.0  # seconds; n8n runs locally, keep the UI-facing check snappy


def check_n8n_live_status() -> bool | None:
    """Best-effort check of whether the n8n SERVER PROCESS is up right now.

    Deliberately independent of ``notifications.yaml``'s ``n8n.enabled``
    (that flag controls whether THIS app pushes pipeline events to n8n — a
    separate concern from "is the n8n instance itself running"). Uses n8n's
    own REST API (N8N_HOST/N8N_PORT/N8N_PROTOCOL + N8N_API_KEY — the
    credential for calling n8n's API, NOT N8N_CALLER_API_KEY/webhook config)
    so both the frontend sidebar and the #system-status Discord message can
    show the actual on/off state of the n8n instance regardless of whether
    the webhook push feature is toggled on.

    Returns None (there is nothing to check) if N8N_API_KEY isn't set,
    True/False otherwise. Never raises — any request failure is False.
    """
    api_key = os.environ.get("N8N_API_KEY", "")
    if not api_key:
        return None
    host = os.environ.get("N8N_HOST", "localhost")
    port = os.environ.get("N8N_PORT", "5678")
    protocol = os.environ.get("N8N_PROTOCOL", "http")
    url = f"{protocol}://{host}:{port}/api/v1/workflows?limit=1"
    try:
        resp = httpx.get(
            url,
            headers={"X-N8N-API-KEY": api_key},
            timeout=_LIVE_STATUS_TIMEOUT,
        )
        return resp.status_code < 500
    except Exception:
        return False
