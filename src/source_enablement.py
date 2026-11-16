"""Is this source on? One answer for every consumer (Phase B B4-A, finding P-001; owner decision D1).

Before this module each consumer asked its own question: the Settings switch read the user's choice
in ``data/settings.json``, the LinkedIn adapter read ``config/linkedin.yaml``, the HelloWork adapter
only checked that ``config/sources.yaml`` named the source, and the scheduler read the choice alone.
Turning HelloWork off in Settings therefore stopped the scans but its stored offers were still
re-injected, scored and listed in every run.

The rule, for every source:

* an official source is on when ``sources.yaml`` lists it as ``active``;
* a user-switchable source (:data:`src.app_settings.UNOFFICIAL_CHOICE_SOURCES`) is on when its
  configuration provides it AND the user chose it (``src.app_settings.unofficial_source_enabled``):
  LinkedIn through ``linkedin.yaml`` ``enabled`` (default off), HelloWork through its ``sources.yaml``
  entry (kept ``active: false`` on purpose, because its own scanner collects it outside the pipeline),
  Indeed through its ``sources.yaml`` entry being ``active``.

Owner decision D1 (2026-11-14): while a user-switchable source is off, its stored offers are hidden.
They are not re-injected into a run, not scored and not listed. Nothing is deleted, and an offer the
user saved or has in the application tracker (``jobs.application_status``) stays listed. Turning
the source back on brings the stored offers back.

Issue #253 (owner decisions 2026-11-16) extends this to every source and supersedes two earlier
clauses: every run carries forward the stored active offers of every source that is on
(:func:`enabled_sources`, ``src/pipeline/carryover.py``), not only LinkedIn's and HelloWork's, so a
source that returns nothing no longer empties its part of the list (this replaces OQ-3's "replace
the list"); and an offer not collected for ``expire_after_days_unseen`` days expires, LinkedIn and
HelloWork included (this replaces D1's "no age-based expiry for staged sources"). A source that is
off for any reason (inactive in ``sources.yaml``, a removed type, the user's switch) has its stored
offers hidden by the same rule (:func:`switched_off_sources`).

The collection freeze (``src/collectors/freeze.py``, D7) is a separate, temporary switch: it stops
every contact with a source but keeps its offers visible, so it is deliberately not part of this rule.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def settings_path_for(db_path: str | Path) -> Path:
    """The settings file that goes with a database: ``data/settings.json`` next to ``data/jobs.db``
    (the same convention as the pipeline's preflight and the API's ``app.state.settings_path``)."""
    return Path(db_path).parent / "settings.json"


def _sources_yaml_entry(config_dir: str | Path, source: str) -> dict | None:
    from src.discovery.registry import SourceRegistry

    path = Path(config_dir) / "sources.yaml"
    if not path.exists():
        return None
    try:
        entry = SourceRegistry(config_path=str(path)).get_source(source)
    except Exception as exc:  # noqa: BLE001 — reading the config must never raise here
        logger.warning("Source enablement: could not read %s: %s", path, exc)
        return None
    return entry if isinstance(entry, dict) else None


def _linkedin_configured(config_dir: str | Path) -> bool:
    import yaml

    path = Path(config_dir) / "linkedin.yaml"
    if not path.exists():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning("Source enablement: could not read %s: %s", path, exc)
        return False
    return isinstance(data, dict) and bool(data.get("enabled", False))


def source_configured(source: str, config_dir: str | Path) -> bool:
    """Whether the configuration provides ``source`` (its part of the rule, without the user's choice)."""
    if source == "linkedin":
        return _linkedin_configured(config_dir)
    entry = _sources_yaml_entry(config_dir, source)
    if entry is None:
        return False
    if source == "hellowork":
        return True  # listed is enough: its scanner reads the entry whatever ``active`` says
    return bool(entry.get("active", True))


def source_enabled(source: str, config_dir: str | Path, settings_path: str | Path | None) -> bool:
    """The single answer to "is ``source`` on", used by every consumer (see the module docstring)."""
    from src.app_settings import UNOFFICIAL_CHOICE_SOURCES, unofficial_source_enabled

    if not source_configured(source, config_dir):
        return False
    if source not in UNOFFICIAL_CHOICE_SOURCES:
        return True
    return unofficial_source_enabled(Path(settings_path) if settings_path else None, source)


def _known_sources(config_dir: str | Path) -> set[str]:
    """Every source name the configuration knows: the ``sources.yaml`` entries (retired types are
    set aside by the registry) plus the user-switchable sources, whatever their state."""
    from src.app_settings import UNOFFICIAL_CHOICE_SOURCES
    from src.discovery.registry import SourceRegistry

    names = set(UNOFFICIAL_CHOICE_SOURCES)
    path = Path(config_dir) / "sources.yaml"
    if path.exists():
        try:
            names |= {str(e["name"]) for e in SourceRegistry(config_path=str(path)).all_sources}
        except Exception as exc:  # noqa: BLE001 — reading the config must never raise here
            logger.warning("Source enablement: could not read %s: %s", path, exc)
    return names


def _removed_type_sources(config_dir: str | Path) -> set[str]:
    """``sources.yaml`` entries of a type removed in 2.0 (``REMOVED_SOURCE_TYPES``): never on."""
    from src.collectors.registry import REMOVED_SOURCE_TYPES
    from src.discovery.registry import SourceRegistry

    path = Path(config_dir) / "sources.yaml"
    if not path.exists():
        return set()
    try:
        return {str(e["name"]) for e in SourceRegistry(config_path=str(path)).all_sources
                if e.get("type") in REMOVED_SOURCE_TYPES}
    except Exception:  # noqa: BLE001
        return set()


def enabled_sources(config_dir: str | Path, settings_path: str | Path | None) -> set[str]:
    """The sources that are on now (issue #253): their stored active offers are carried into every
    run. Stored offers carry the source's name (``jobs.source``)."""
    removed = _removed_type_sources(config_dir)
    return {s for s in _known_sources(config_dir) - removed if source_enabled(s, config_dir, settings_path)}


def disabled_sources(config_dir: str | Path, settings_path: str | Path | None) -> set[str]:
    """The sources that are off now, whatever the reason (issue #253): a known source that is not
    on (inactive in ``sources.yaml``, a user-switchable source switched off or not configured), a
    ``sources.yaml`` entry of a removed type, and the retired types (RemoteOK, Jobicy, We Work
    Remotely). Their stored offers are hidden from the lists, except saved or tracked ones. A name
    the configuration does not know at all is not counted: its offers are never carried into a run,
    so they reach no list but the saved and tracked views."""
    from src.discovery.registry import RETIRED_SOURCE_TYPES

    known = _known_sources(config_dir)
    return (known - enabled_sources(config_dir, settings_path)) | set(RETIRED_SOURCE_TYPES) \
        | _removed_type_sources(config_dir)


def switched_off_sources(config_dir: str | Path, settings_path: str | Path | None) -> set[str]:
    """The user-switchable sources that are off now: their stored offers are hidden (D1).

    Stored offers carry the source's name (``jobs.source``), which for these sources is the same
    string as the switch (``linkedin``, ``hellowork``, ``indeed``). The list itself hides the offers
    of every source outside :func:`enabled_sources` (issue #253)."""
    from src.app_settings import UNOFFICIAL_CHOICE_SOURCES

    return {s for s in UNOFFICIAL_CHOICE_SOURCES if not source_enabled(s, config_dir, settings_path)}
