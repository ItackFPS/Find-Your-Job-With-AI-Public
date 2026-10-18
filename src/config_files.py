"""Resolve personal configuration files against tracked defaults.

``config/profile.yaml`` and ``config/notifications.yaml`` are tracked
repository defaults. A developer's own values live in the gitignored sibling
``config/<name>.local.yaml``: when it exists it replaces the default as a
whole file (no key-level merge — missing keys fall back to the model defaults
exactly as they would in the tracked file).

Readers call :func:`resolve_config_path` every time they load, so a local file
created while the backend is running is picked up. Writers always write
:func:`local_config_path`, seeding it from the file currently in effect, so
tracked defaults are never rewritten by the application.
"""

from __future__ import annotations

from pathlib import Path


def local_config_path(config_dir: str | Path, name: str) -> Path:
    """Path of the gitignored personal override for ``name`` (e.g.
    ``profile.yaml`` → ``<config_dir>/profile.local.yaml``). May not exist."""
    default = Path(config_dir) / name
    return default.with_name(f"{default.stem}.local{default.suffix}")


def resolve_config_path(config_dir: str | Path, name: str) -> Path:
    """The file to read for ``name``: the local override if it exists,
    otherwise the tracked default (which may itself not exist)."""
    local = local_config_path(config_dir, name)
    if local.is_file():
        return local
    return Path(config_dir) / name
