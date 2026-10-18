"""The strategy-bundle format, shared by the lab (which writes bundles) and production (which runs one).

A bundle is a directory named by its content hash holding ``filters.yaml`` and ``scoring.yaml``: the base
configuration deep-merged with data overrides (``overrides.json``), rendered with ``yaml.safe_dump`` and
written LF. Production must not import ``src.lab`` (ADR 0011), so the format lives here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

BUNDLE_FILES = ("filters.yaml", "scoring.yaml")


def merge_overrides(base: Any, over: Any) -> Any:
    """Deep-merge ``over`` into ``base`` (mappings merge key by key; anything else is replaced)."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge_overrides(base[k], v) if k in base else v
        return out
    return over


def render_bundle(base_config_dir: str | Path, overrides: dict[str, Any]) -> dict[str, str]:
    """``{file: text}`` of the bundle ``base + overrides`` (raises ``FileNotFoundError`` on a missing base file)."""
    rendered: dict[str, str] = {}
    for name in BUNDLE_FILES:
        data = yaml.safe_load((Path(base_config_dir) / name).read_text(encoding="utf-8")) or {}
        if name in overrides:
            data = merge_overrides(data, overrides[name])
        rendered[name] = yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
    return rendered


def rendered_hash(rendered: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in BUNDLE_FILES:
        h.update(name.encode() + b"\0" + rendered[name].encode() + b"\0")
    return h.hexdigest()


def bundle_hash(directory: str | Path) -> str:
    """Content hash of a written bundle (equal to :func:`rendered_hash` of what was rendered into it)."""
    h = hashlib.sha256()
    for name in BUNDLE_FILES:
        h.update(name.encode() + b"\0" + (Path(directory) / name).read_bytes() + b"\0")
    return h.hexdigest()
