"""Discovery registry for companies, domains, and source configurations.

This module maintains a catalog of known job sources and their metadata.
It supports both manual configuration (via YAML) and future automated discovery.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _validate_source_entry(src: object, *, index: int, path: str) -> None:
    """Raise a clear, actionable error for a malformed sources.yaml entry.

    Phase NEXT / I7: before this, a malformed entry (missing 'name', or a
    non-mapping list item) surfaced as a raw KeyError/TypeError from deep
    inside whatever first happened to call ``self._sources[src["name"]]``
    or ``source_cfg["name"]`` — pointing nowhere near the actual mistake in
    sources.yaml. This validates just the envelope every collector relies on
    (name/active/type), not each collector's own ``config`` sub-dict shape,
    which legitimately varies per source.
    """
    where = f"{path}: sources[{index}]"
    if not isinstance(src, dict):
        raise TypeError(f"{where}: expected a mapping, got {type(src).__name__}")
    name = src.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}: 'name' is required and must be a non-empty string")
    if "active" in src and not isinstance(src["active"], bool):
        raise ValueError(
            f"{path}: source '{name}': 'active' must be true/false, "
            f"got {type(src['active']).__name__}"
        )
    if "type" in src and not isinstance(src["type"], str):
        raise ValueError(
            f"{path}: source '{name}': 'type' must be a string, "
            f"got {type(src['type']).__name__}"
        )


class SourceRegistry:
    """In-memory registry of job sources with persistence to disk."""

    def __init__(self, config_path: str = "config/sources.yaml"):
        self._sources: dict[str, dict] = {}
        self._load(config_path)

    def _load(self, path: str) -> None:
        p = Path(path)
        if not p.exists():
            return
        with open(p, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise TypeError(
                f"{path}: expected a mapping at the top level, got {type(data).__name__}"
            )
        entries = data.get("sources", [])
        if not isinstance(entries, list):
            raise TypeError(
                f"{path}: 'sources' must be a list, got {type(entries).__name__}"
            )
        for i, src in enumerate(entries):
            _validate_source_entry(src, index=i, path=path)
            self._sources[src["name"]] = src

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {"sources": list(self._sources.values())},
                f,
                sort_keys=False,
                allow_unicode=True,
            )

    def get_source(self, name: str) -> dict | None:
        return self._sources.get(name)

    def add_source(self, source: dict) -> None:
        self._sources[source["name"]] = source

    @property
    def all_sources(self) -> list[dict]:
        return list(self._sources.values())
