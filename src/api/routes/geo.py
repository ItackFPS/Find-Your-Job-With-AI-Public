"""Geo API routes.

GET /api/geo/places?q=  — offline autocomplete over French communes (home town picker).

Answered from the bundled communes table (src/geo/data): nothing is sent over the network.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from src.api.schemas import PlaceOut
from src.geo.places import department_name, search_communes

router = APIRouter(prefix="/api/geo", tags=["geo"])


@router.get("/places", response_model=list[PlaceOut])
def search_places(
    q: str = Query("", max_length=80, description="town name or postcode prefix"),
    limit: int = Query(10, ge=1, le=50),
) -> list[PlaceOut]:
    return [
        PlaceOut(name=p.name, insee=p.insee or "", department=p.department,
                 department_name=department_name(p.department) if p.department else None,
                 lat=p.lat or 0.0, lon=p.lon or 0.0)
        for p in search_communes(q, limit)
    ]
