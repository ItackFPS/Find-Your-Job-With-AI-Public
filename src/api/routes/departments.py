"""French department API routes.

GET /api/departments?q=<query>&limit=<n>
    Autocomplete: returns departments matching a query (name, code, or city).
    Always available — data is embedded statically, no setup required.

GET /api/departments/all
    Returns the complete list of all French departments, sorted by code.
    Used by the Jobs page dropdown to populate the full department selector.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from src.pipeline.departments import Department, all_departments, search_departments

router = APIRouter(prefix="/api/departments", tags=["departments"])


@router.get("/all")
def list_all_departments() -> list[dict]:
    """Return all French departments sorted by code (metropolitan first, then overseas).

    This endpoint is used by the Jobs page to populate the department selector
    dropdown in one request rather than building it from autocomplete queries.
    """
    depts = all_departments()

    def _sort_key(d: Department) -> tuple[int, int | str]:
        try:
            return (0, int(d.code))
        except ValueError:
            return (1, d.code)

    return [{"code": d.code, "name": d.name} for d in sorted(depts, key=_sort_key)]


@router.get("")
def search_french_departments(
    q: str = Query(..., min_length=1, max_length=100, description="Department name, code, or city"),
    limit: int = Query(10, ge=1, le=50, description="Maximum results"),
) -> list[dict]:
    """Search French departments by name, code, or major city.

    Examples:
      - ``?q=var``   → Var (83)
      - ``?q=83``    → Var (83)
      - ``?q=lyon``  → Rhône (69)
      - ``?q=paris`` → Paris (75)
    """
    results = search_departments(q.strip(), limit=limit)
    return [{"code": d.code, "name": d.name} for d in results]
