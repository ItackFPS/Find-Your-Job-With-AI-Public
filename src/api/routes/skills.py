"""Career Skill Graph API (read-only).

GET /api/skills/graph  — market demand for technologies, their co-occurrence, and the profile's gaps
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.config_files import resolve_config_path
from src.skillgraph.graph import build_skill_graph

router = APIRouter(prefix="/api/skills", tags=["skills"])


@router.get("/graph")
def skill_graph(
    request: Request,
    days: int = Query(default=90, ge=7, le=365),
    min_support: int = Query(default=2, ge=1, le=20),
) -> dict[str, Any]:
    from src.pipeline.filter import load_profile

    config_dir = Path(getattr(request.app.state, "config_dir", "config"))
    try:
        profile = load_profile(str(resolve_config_path(config_dir, "profile.yaml")))
    except Exception as exc:  # noqa: BLE001 — a broken profile is reported, not a 500 traceback
        raise HTTPException(status_code=422, detail=f"profile could not be loaded: {exc}") from exc
    return build_skill_graph(request.app.state.db_path, profile.target_skills, days=days, min_support=min_support)
