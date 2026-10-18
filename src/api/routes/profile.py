"""Profile API routes.

GET  /api/profile  — read the profile in effect as JSON
PUT  /api/profile  — validate via JobSearchProfile, write profile.local.yaml

The profile in effect is config/profile.local.yaml when it exists, otherwise
the tracked default config/profile.yaml (see src/config_files.py). Saves
always go to the personal profile.local.yaml, so the tracked default is never
rewritten by the UI.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

from src.api.schemas import ProfileIn, ProfileOut
from src.config_files import local_config_path, resolve_config_path
from src.models.profile import JobSearchProfile

router = APIRouter(prefix="/api/profile", tags=["profile"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
# Tests monkeypatch this to a single file used for both reads and writes.
# None (production) = read the resolved profile, write the local override.
_PROFILE_PATH: Path | None = None


def _read_path() -> Path:
    return _PROFILE_PATH or resolve_config_path(_CONFIG_DIR, "profile.yaml")


def _write_path() -> Path:
    return _PROFILE_PATH or local_config_path(_CONFIG_DIR, "profile.yaml")


@router.get("", response_model=ProfileOut)
def get_profile() -> ProfileOut:
    read_path = _read_path()
    if not read_path.exists():
        raise HTTPException(status_code=404, detail="profile.yaml not found")
    with open(read_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return ProfileOut(**data)


def _merge_profile_updates(existing: dict, updates: dict) -> dict:
    """Overlay ``updates`` onto ``existing``, merging one level into any
    dict-valued fields (e.g. ``geography``, ``experience``, ``matching``) so a
    request that only sends part of a nested section doesn't blow away the
    rest of it. Scalars and lists in ``updates`` replace the existing value
    outright — that already matches how the UI edits those fields (it always
    sends the full current list/value for anything it touches).
    """
    merged = dict(existing)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_profile_updates(merged[key], value)
        else:
            merged[key] = value
    return merged


@router.put("", response_model=ProfileOut)
def update_profile(payload: ProfileIn) -> ProfileOut:
    """Validate the incoming payload via JobSearchProfile, then write profile.local.yaml.

    Using a Pydantic round-trip ensures the file always contains valid data
    and that the frontend cannot write arbitrary content.

    This is a merge, not a full replace: fields the request body omits
    entirely are preserved from the on-disk profile instead of being reset to
    schema defaults. A field must be explicitly present in the request body
    (even as ``null``) to be cleared — this is what ``exclude_unset`` checks.
    Without this, any client (UI build, script, or future field) that isn't
    yet aware of a given profile field would silently wipe it on every save.
    """
    existing_data: dict = {}
    read_path = _read_path()
    if read_path.exists():
        with open(read_path, encoding="utf-8") as f:
            existing_data = yaml.safe_load(f) or {}

    updates = payload.model_dump(exclude_unset=True)
    merged = _merge_profile_updates(existing_data, updates)

    try:
        # Validate through the canonical domain model.
        profile = JobSearchProfile(**merged)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Atomic write: write to a temp file in the same directory, then rename.
    # The first save seeds profile.local.yaml from the profile in effect
    # (``existing_data`` above), so no current value is lost.
    write_path = _write_path()
    write_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = write_path.with_suffix(".yaml.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                profile.model_dump(),
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        tmp_path.replace(write_path)
    except OSError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Could not write {write_path.name}: {exc}") from exc

    return ProfileOut(**profile.model_dump())
