"""Discovery vocabulary loaded from configuration, not from code.

Two kinds of files under ``config/``:

* ``discovery.yaml`` — profession-neutral vocabulary used for every profile.
* ``presets/*.yaml`` — search vocabulary for one line of work (``it.yaml`` ships
  with the project). A preset applies to a profile when the profile's
  ``target_domains`` contains one of the preset's ``applies_to_domains``.

A missing or malformed file never raises: discovery degrades to the profile's own
terms and the problem is logged (collectors must not crash the pipeline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def normalize_domain(value: str) -> str:
    """Case- and whitespace-insensitive key for comparing domain names."""
    return " ".join(value.casefold().split())


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, (str, int)) and str(v).strip()]


def _list_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _str_list(v) for k, v in value.items()}


def _nested_list_map(value: Any) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, dict):
        return {}
    return {str(k): _list_map(v) for k, v in value.items()}


@dataclass(frozen=True)
class DiscoveryPreset:
    """Search vocabulary for one line of work."""

    name: str
    domains: frozenset[str]
    baseline: dict[str, list[str]] = field(default_factory=dict)
    domain_queries: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    contract_queries: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    jobicy_industries: dict[str, list[str]] = field(default_factory=dict)
    partition_vocabulary: list[str] = field(default_factory=list)
    partition_affinity: dict[str, list[str]] = field(default_factory=dict)
    noisy_replacements: dict[str, str | None] = field(default_factory=dict)
    # Per source type: collector config keys (``queries``, ``categories``,
    # ``occupations`` …) the preset supplies when the user's own source config
    # leaves them out.
    collector_config: dict[str, dict[str, Any]] = field(default_factory=dict)

    def owns(self, domain: str) -> bool:
        return normalize_domain(domain) in self.domains


@dataclass(frozen=True)
class NeutralDiscovery:
    """Profession-neutral vocabulary from ``config/discovery.yaml``."""

    contract_terms: dict[str, dict[str, str]] = field(default_factory=dict)
    partition_vocabulary: dict[str, list[str]] = field(default_factory=dict)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Discovery config %s unreadable (%s) — ignoring it", path, exc)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _collector_config(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {str(k): dict(v) for k, v in value.items() if isinstance(v, dict)}


def _parse_preset(path: Path, raw: dict[str, Any]) -> DiscoveryPreset | None:
    domains = frozenset(normalize_domain(d) for d in _str_list(raw.get("applies_to_domains")))
    if not domains:
        logger.warning("Discovery preset %s has no applies_to_domains — ignoring it", path)
        return None
    d = raw.get("discovery") if isinstance(raw.get("discovery"), dict) else {}
    noisy_raw = d.get("noisy_replacements")
    noisy: dict[str, str | None] = {}
    if isinstance(noisy_raw, dict):
        for k, v in noisy_raw.items():
            noisy[str(k).casefold()] = None if v is None else str(v)
    return DiscoveryPreset(
        name=str(raw.get("name") or path.stem),
        domains=domains,
        baseline=_list_map(d.get("baseline")),
        domain_queries=_nested_list_map(d.get("domain_queries")),
        contract_queries=_nested_list_map(d.get("contract_queries")),
        jobicy_industries=_list_map(d.get("jobicy_industries")),
        partition_vocabulary=_str_list(d.get("partition_vocabulary")),
        partition_affinity=_list_map(d.get("partition_affinity")),
        noisy_replacements=noisy,
        collector_config=_collector_config(d.get("collector_config")),
    )


@lru_cache(maxsize=8)
def _load_presets_cached(config_dir: str) -> tuple[DiscoveryPreset, ...]:
    presets_dir = Path(config_dir) / "presets"
    if not presets_dir.is_dir():
        logger.warning("No discovery presets folder at %s", presets_dir)
        return ()
    presets = []
    for path in sorted(presets_dir.glob("*.yaml")):
        preset = _parse_preset(path, _read_yaml(path))
        if preset is not None:
            presets.append(preset)
    return tuple(presets)


@lru_cache(maxsize=8)
def _load_neutral_cached(config_dir: str) -> NeutralDiscovery:
    raw = _read_yaml(Path(config_dir) / "discovery.yaml")
    terms: dict[str, dict[str, str]] = {}
    if isinstance(raw.get("contract_terms"), dict):
        for lang, table in raw["contract_terms"].items():
            if isinstance(table, dict):
                terms[str(lang)] = {
                    str(k): str(v).strip() for k, v in table.items() if str(v).strip()
                }
    return NeutralDiscovery(
        contract_terms=terms,
        partition_vocabulary=_list_map(raw.get("partition_vocabulary")),
    )


def load_presets(config_dir: str | Path | None = None) -> tuple[DiscoveryPreset, ...]:
    """Every preset found in ``<config_dir>/presets`` (cached per directory)."""
    return _load_presets_cached(str(config_dir or DEFAULT_CONFIG_DIR))


def load_neutral(config_dir: str | Path | None = None) -> NeutralDiscovery:
    """The profession-neutral vocabulary (cached per directory)."""
    return _load_neutral_cached(str(config_dir or DEFAULT_CONFIG_DIR))


def matching_presets(
    presets: tuple[DiscoveryPreset, ...], target_domains: list[str]
) -> list[DiscoveryPreset]:
    """Presets that apply to a profile with these ``target_domains``."""
    return [p for p in presets if any(p.owns(d) for d in target_domains)]
