"""Mandatory pipeline preflight: verify external connections before collection.

Runs once at the very start of every pipeline run (see
``src.pipeline.orchestrator.run_pipeline``), before any collector executes.
Checks:

  1. France Travail — credentials present, then a real OAuth2 token fetch
     via the existing collector's token logic (no second OAuth client).
  2. n8n — webhook notification server reachable, and the configured
     webhook path actually registered (best-effort; see ``_probe_webhook``).

Each check is mandatory only when that integration is actually enabled in
its own configuration file (``sources.yaml`` for France Travail,
``notifications.yaml`` / ``N8N_WEBHOOK_URL`` for n8n) — a disabled/inactive
integration is skipped rather than failing the run.

Never raises: any unexpected error while checking is captured as a failed
status for that integration, since a preflight that crashes is worse than
one that reports a clear failure.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

_BANNER_WIDTH = 36
_HEALTHZ_TIMEOUT = 5.0
_WEBHOOK_PROBE_TIMEOUT = 5.0


@dataclass
class FranceTravailPreflight:
    enabled: bool
    credentials: str   # FOUND | MISSING | SKIPPED
    authentication: str  # OK | FAILED | SKIPPED
    status: str        # READY | CONFIG_MISSING | AUTH_FAILED | SKIPPED
    detail: str | None = None


@dataclass
class N8nPreflight:
    enabled: bool
    server: str        # REACHABLE | UNREACHABLE | SKIPPED
    webhook: str        # AVAILABLE | UNAVAILABLE | SKIPPED
    status: str        # READY | CONFIG_MISSING | CONNECTION_FAILED | WEBHOOK_UNAVAILABLE | SKIPPED
    detail: str | None = None


@dataclass
class PreflightResult:
    france_travail: FranceTravailPreflight
    n8n: N8nPreflight
    status: str            # READY | DEGRADED | FAILED
    ready: bool             # True whenever collection may proceed (FAILED is the only blocker)
    report_lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)          # blocking (FAILED) reasons
    degraded_errors: list[str] = field(default_factory=list)  # non-blocking (DEGRADED) reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "france_travail": asdict(self.france_travail),
            "n8n": asdict(self.n8n),
            "report_text": "\n".join(self.report_lines),
        }


_READY_LIKE = frozenset({"READY", "SKIPPED"})


def run_preflight(config_dir: str = "config") -> PreflightResult:
    """Check France Travail and n8n connectivity/auth before collection starts.

    France Travail is a collection source: an active source that cannot
    authenticate blocks the run entirely (status FAILED) — a partial scan
    silently missing a configured source is worse than no run at all.

    n8n is downstream notification infrastructure: a reachability or webhook
    problem is reported (status DEGRADED) but never blocks collection,
    filtering, scoring, or ranking. A temporary n8n outage should not cost
    an otherwise successful job scan.
    """
    ft = _check_france_travail(config_dir)
    n8n = _check_n8n(config_dir)

    ft_ok = ft.status in _READY_LIKE
    n8n_ok = n8n.status in _READY_LIKE

    if not ft_ok:
        status = "FAILED"
    elif not n8n_ok:
        status = "DEGRADED"
    else:
        status = "READY"

    errors: list[str] = []
    if not ft_ok and ft.detail:
        errors.append(f"France Travail: {ft.detail}")

    degraded_errors: list[str] = []
    if not n8n_ok and n8n.detail:
        degraded_errors.append(f"n8n: {n8n.detail}")

    result = PreflightResult(
        france_travail=ft, n8n=n8n, status=status, ready=ft_ok,
        errors=errors, degraded_errors=degraded_errors,
    )
    result.report_lines = _format_report(result)
    return result


# ---------------------------------------------------------------------------
# France Travail
# ---------------------------------------------------------------------------

def _check_france_travail(config_dir: str) -> FranceTravailPreflight:
    from src.discovery.registry import SourceRegistry

    sources_path = Path(config_dir) / "sources.yaml"
    ft_source_cfg: dict[str, Any] | None = None
    try:
        registry = SourceRegistry(config_path=str(sources_path))
        for src in registry.all_sources:
            if src.get("type") == "france_travail" and src.get("active", True):
                ft_source_cfg = src
                break
    except Exception as exc:
        logger.warning("Preflight: could not read sources.yaml: %s", exc)

    if ft_source_cfg is None:
        return FranceTravailPreflight(
            enabled=False, credentials="SKIPPED", authentication="SKIPPED",
            status="SKIPPED",
            detail="france_travail source is not active in sources.yaml — check skipped",
        )

    from src.collectors.france_travail import FranceTravailCollector

    merged_cfg = _merge_collector_config(ft_source_cfg)
    collector = FranceTravailCollector(config=merged_cfg)

    if not collector.client_id or not collector.client_secret:
        return FranceTravailPreflight(
            enabled=True, credentials="MISSING", authentication="SKIPPED",
            status="CONFIG_MISSING",
            detail=(
                "FRANCE_TRAVAIL_CLIENT_ID and FRANCE_TRAVAIL_CLIENT_SECRET are not set "
                "(environment variables or source config keys 'client_id'/'client_secret'). "
                "Register at https://developer.francetravail.fr to obtain credentials."
            ),
        )

    try:
        with httpx.Client(timeout=collector.timeout) as client:
            collector._get_or_refresh_token(client)
    except (httpx.HTTPError, RuntimeError) as exc:
        return FranceTravailPreflight(
            enabled=True, credentials="FOUND", authentication="FAILED",
            status="AUTH_FAILED",
            detail=f"OAuth2 authentication failed: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 — preflight must never raise
        return FranceTravailPreflight(
            enabled=True, credentials="FOUND", authentication="FAILED",
            status="AUTH_FAILED",
            detail=f"Unexpected error during authentication: {exc}",
        )

    return FranceTravailPreflight(
        enabled=True, credentials="FOUND", authentication="OK", status="READY",
    )


def _merge_collector_config(source_config: dict[str, Any]) -> dict[str, Any]:
    """Mirror orchestrator._build_collector_config without importing it (avoids
    a preflight -> orchestrator -> preflight import cycle)."""
    merged = dict(source_config)
    cfg = source_config.get("config") or {}
    merged.update(cfg)
    return merged


# ---------------------------------------------------------------------------
# n8n
# ---------------------------------------------------------------------------

def _check_n8n(config_dir: str) -> N8nPreflight:
    """n8n reachability + webhook-registration check.

    Reused (not duplicated) by src.notifications.engine's system-status
    payload builder via the ``check_n8n_reachability`` alias below — the
    "is n8n reachable" question is the same question in both places, so
    there is exactly one implementation of it.
    """
    from src.notifications.config import load_notification_config
    from src.notifications.n8n import N8nProvider

    notif_cfg = load_notification_config(config_dir)
    if not notif_cfg.n8n.enabled:
        return N8nPreflight(
            enabled=False, server="SKIPPED", webhook="SKIPPED", status="SKIPPED",
            detail="n8n notifications disabled in notifications.yaml — check skipped",
        )

    provider = N8nProvider(notif_cfg.n8n.webhook_url)
    webhook_url = provider._webhook_url  # resolved with N8N_WEBHOOK_URL env precedence
    if not webhook_url:
        return N8nPreflight(
            enabled=True, server="SKIPPED", webhook="SKIPPED", status="CONFIG_MISSING",
            detail=(
                "n8n is enabled but no webhook URL is configured "
                "(set N8N_WEBHOOK_URL or notifications.yaml n8n.webhook_url)"
            ),
        )

    parsed = urlsplit(webhook_url)
    if not parsed.scheme or not parsed.netloc:
        return N8nPreflight(
            enabled=True, server="SKIPPED", webhook="SKIPPED", status="CONFIG_MISSING",
            detail=f"n8n webhook URL is not a valid absolute URL: {webhook_url!r}",
        )
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    try:
        httpx.get(f"{base_url}/healthz", timeout=_HEALTHZ_TIMEOUT)
    except httpx.HTTPError as exc:
        return N8nPreflight(
            enabled=True, server="UNREACHABLE", webhook="SKIPPED",
            status="CONNECTION_FAILED",
            detail=f"n8n server unreachable at {base_url}: {exc}",
        )

    try:
        probe_resp = httpx.get(webhook_url, timeout=_WEBHOOK_PROBE_TIMEOUT)
    except httpx.HTTPError as exc:
        return N8nPreflight(
            enabled=True, server="REACHABLE", webhook="UNAVAILABLE",
            status="WEBHOOK_UNAVAILABLE",
            detail=f"n8n webhook probe failed at {webhook_url}: {exc}",
        )

    if _probe_indicates_registered(probe_resp):
        return N8nPreflight(enabled=True, server="REACHABLE", webhook="AVAILABLE", status="READY")

    if probe_resp.status_code in (401, 403):
        return N8nPreflight(
            enabled=True, server="REACHABLE", webhook="UNAVAILABLE",
            status="WEBHOOK_UNAVAILABLE",
            detail=(
                f"n8n rejected the webhook probe at {webhook_url} as "
                f"unauthorized (HTTP {probe_resp.status_code}: "
                f"{probe_resp.text[:200]!r}). This is an authentication/"
                "credential problem, not a missing workflow — check the "
                "webhook's auth configuration in n8n."
            ),
        )

    return N8nPreflight(
        enabled=True, server="REACHABLE", webhook="UNAVAILABLE",
        status="WEBHOOK_UNAVAILABLE",
        detail=(
            f"Webhook not registered at {webhook_url} "
            f"(HTTP {probe_resp.status_code}: {probe_resp.text[:200]!r}). "
            "Activate the n8n workflow that listens on this path."
        ),
    )


# Public alias: the system-status worker (src/notifications/engine.py's
# build_system_status_payload) reuses this exact check rather than
# re-implementing n8n reachability a second time. `_check_n8n` stays the
# name used internally by run_preflight() and its existing tests.
check_n8n_reachability = _check_n8n


def _probe_indicates_registered(resp: httpx.Response) -> bool:
    """Best-effort read of an n8n webhook probe response.

    The probe is a GET, while the real trigger is a POST — so a 404 is
    expected either way. n8n distinguishes the two cases in its error
    message: a path with no matching workflow says just "is not registered",
    while a path that exists but rejects the HTTP method used names the
    method it does accept (e.g. "...is not registered for GET requests. Did
    you mean to make a POST request?"). We treat the latter as "registered".

    This is inherently a heuristic tied to n8n's message wording and may not
    hold across every n8n version — a false negative here only means the
    preflight is stricter than necessary, never that a broken webhook is
    reported as healthy.
    """
    if resp.status_code < 400:
        return True
    if resp.status_code != 404:
        return False
    try:
        message = str(resp.json().get("message", ""))
    except Exception:
        message = resp.text
    lowered = message.lower()
    return "did you mean" in lowered or "not registered for" in lowered


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _format_report(result: PreflightResult) -> list[str]:
    lines: list[str] = []
    rule = "━" * _BANNER_WIDTH
    lines.append(rule)
    lines.append("PIPELINE PREFLIGHT")
    lines.append(rule)
    lines.append("")

    ft = result.france_travail
    if ft.status == "SKIPPED":
        lines.append(f"○ France Travail: {ft.detail}")
    else:
        lines.append(
            f"{'✓' if ft.credentials == 'FOUND' else '✗'} "
            f"France Travail credentials {'found' if ft.credentials == 'FOUND' else 'missing'}"
        )
        if ft.credentials == "FOUND":
            lines.append(
                f"{'✓' if ft.authentication == 'OK' else '✗'} "
                f"France Travail authentication {'OK' if ft.authentication == 'OK' else 'failed'}"
            )
        if ft.status != "READY" and ft.detail:
            lines.append(f"  → {ft.detail}")
        if ft.status == "AUTH_FAILED":
            lines.append("  → Collection will not start until the connection is fixed.")

    n8n = result.n8n
    if n8n.status == "SKIPPED":
        lines.append(f"○ n8n: {n8n.detail}")
    else:
        lines.append(
            f"{'✓' if n8n.server == 'REACHABLE' else '✗'} "
            f"n8n server {'reachable' if n8n.server == 'REACHABLE' else 'unreachable'}"
        )
        if n8n.server == "REACHABLE":
            lines.append(
                f"{'✓' if n8n.webhook == 'AVAILABLE' else '✗'} "
                f"pipeline webhook {'registered' if n8n.webhook == 'AVAILABLE' else 'not registered'}"
            )
        if n8n.status != "READY" and n8n.detail:
            lines.append(f"  → {n8n.detail}")
        if n8n.status not in _READY_LIKE:
            lines.append("  → Notifications will be unavailable for this run.")

    lines.append("")
    lines.append(f"Preflight: {result.status}")
    lines.append("Pipeline aborted." if result.status == "FAILED" else "Starting pipeline...")
    return lines
