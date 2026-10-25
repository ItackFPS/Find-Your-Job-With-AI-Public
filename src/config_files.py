"""Resolve personal configuration files against tracked defaults.

``config/profile.yaml`` is a tracked repository default. A developer's own
values live in the gitignored sibling
``config/<name>.local.yaml``: when it exists it replaces the default as a
whole file (no key-level merge — missing keys fall back to the model defaults
exactly as they would in the tracked file).

Readers call :func:`resolve_config_path` every time they load, so a local file
created while the backend is running is picked up. Writers always write
:func:`local_config_path`, seeding it from the file currently in effect, so
tracked defaults are never rewritten by the application.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Any

# One lock for every in-process write of the profile file (PUT /api/profile and the
# distance-model migration), so a save and a migration never interleave.
PROFILE_WRITE_LOCK = threading.RLock()


def write_yaml_atomic(path: str | Path, data: Any) -> None:
    """Write ``data`` as YAML to ``path`` atomically: a uniquely named temp file in the same
    directory, then a rename (concurrent writers never share a temp file)."""
    import yaml

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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
