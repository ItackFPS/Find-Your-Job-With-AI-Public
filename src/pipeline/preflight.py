"""Mandatory pipeline preflight: verify external connections before collection.

Runs once at the very start of every pipeline run (see
``src.pipeline.orchestrator.run_pipeline``), before any collector executes.
Checks:

  France Travail — credentials present, then a real OAuth2 token fetch
     via the existing collector's token logic (no second OAuth client).

The check is mandatory only when France Travail is active in ``sources.yaml``;
an inactive source is skipped rather than failing the run.

France Travail with no credentials at all (a fresh install) is not a broken
connection but one that was never set up: the run goes ahead DEGRADED without
that source, the skip named in the report and the run's errors. Credentials
that are present but fail to authenticate still block the run. Indeed is an
unofficial source, off unless the user chose it (``src.app_settings``, plan rule
I1): when it is off it is skipped with that reason, which is the intended default
and does not mark the run degraded.

Never raises: any unexpected error while checking is captured as a failed
status for that integration, since a preflight that crashes is worse than
one that reports a clear failure.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BANNER_WIDTH = 36


@dataclass
class FranceTravailPreflight:
    enabled: bool
    credentials: str   # FOUND | MISSING | SKIPPED
    authentication: str  # OK | FAILED | SKIPPED
    status: str        # READY | CONFIG_MISSING | AUTH_FAILED | SKIPPED
    detail: str | None = None


@dataclass
class PreflightResult:
    france_travail: FranceTravailPreflight
    status: str            # READY | DEGRADED | FAILED
    ready: bool             # True whenever collection may proceed (FAILED is the only blocker)
    report_lines: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)          # blocking (FAILED) reasons
    degraded_errors: list[str] = field(default_factory=list)  # non-blocking (DEGRADED) reasons
    # Source types the run must not collect from (an unconfigured France Travail or Indeed).
    skipped_source_types: list[str] = field(default_factory=list)
    # Why each of them is skipped, and what to set up (source type -> sentence).
    skip_reasons: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "ready": self.ready,
            "france_travail": asdict(self.france_travail),
            "skipped_source_types": list(self.skipped_source_types),
            "skip_reasons": dict(self.skip_reasons),
            "report_text": "\n".join(self.report_lines),
        }


_READY_LIKE = frozenset({"READY", "SKIPPED"})


def run_preflight(config_dir: str = "config", settings_path: str | Path | None = None) -> PreflightResult:
    """Check France Travail connectivity/auth before collection starts.

    France Travail is a collection source: an active source whose credentials
    cannot authenticate blocks the run entirely (status FAILED) — a partial
    scan silently missing a configured source is worse than no run at all. An
    active source with no credentials set was never configured: it is skipped
    for this run, visibly (status DEGRADED), so a fresh install can still
    collect from the sources that need no account.

    ``settings_path``: the app settings file (``data/settings.json``) holding the user's
    choice of unofficial sources; None reads no file (the legacy setup alone decides).
    """
    ft = _check_france_travail(config_dir)
    indeed_off = _indeed_not_chosen(config_dir, settings_path)

    ft_unconfigured = ft.status == "CONFIG_MISSING"
    ft_ok = ft.status in _READY_LIKE or ft_unconfigured

    if not ft_ok:
        status = "FAILED"
    elif ft_unconfigured:
        status = "DEGRADED"
    else:
        status = "READY"

    errors: list[str] = []
    if not ft_ok and ft.detail:
        errors.append(f"France Travail: {ft.detail}")

    skip_reasons: dict[str, str] = {}
    if ft_unconfigured:
        skip_reasons["france_travail"] = ft.detail or "credentials are not set"
    if indeed_off:
        skip_reasons["indeed"] = _INDEED_OFF

    degraded_errors: list[str] = []
    if ft_unconfigured:
        degraded_errors.append(f"France Travail skipped for this run: {ft.detail}")

    result = PreflightResult(
        france_travail=ft, status=status, ready=ft_ok,
        errors=errors, degraded_errors=degraded_errors,
        skipped_source_types=list(skip_reasons), skip_reasons=skip_reasons,
    )
    result.report_lines = _format_report(result)
    return result


# ---------------------------------------------------------------------------
# Indeed
# ---------------------------------------------------------------------------

_INDEED_OFF = (
    "Indeed is off. It is an unofficial source (Indeed has no public API), so it only "
    "runs if you choose it; the other sources still run."
)


def _indeed_not_chosen(config_dir: str, settings_path: str | Path | None) -> bool:
    """An active Indeed source the user has not chosen (plan rule I1)."""
    from src.app_settings import unofficial_source_enabled
    from src.discovery.registry import SourceRegistry

    try:
        registry = SourceRegistry(config_path=str(Path(config_dir) / "sources.yaml"))
    except Exception as exc:  # noqa: BLE001 — preflight must never raise
        logger.warning("Preflight: could not read sources.yaml: %s", exc)
        return False
    active = any(s.get("type") == "indeed" and s.get("active", True) for s in registry.all_sources)
    return active and not unofficial_source_enabled(Path(settings_path) if settings_path else None, "indeed")


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
        if ft.status == "CONFIG_MISSING":
            lines.append(
                "  → France Travail is skipped for this run; the other sources still run. "
                "Add the credentials to .env, or set active: false in sources.yaml."
            )
    if "indeed" in result.skip_reasons:
        lines.append(f"○ Indeed skipped: {result.skip_reasons['indeed']}")

    lines.append("")
    lines.append(f"Preflight: {result.status}")
    lines.append("Pipeline aborted." if result.status == "FAILED" else "Starting pipeline...")
    return lines
