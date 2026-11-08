"""ROME autocomplete API routes (the Profile page's domain field).

GET /api/rome/occupations?q=<query>&limit=<n>
    ROME professional domains and job sheets starting with the typed words
    (French labels, accents ignored). Empty until ``python scripts/setup_rome.py``
    has built the local index.

GET /api/rome/status
    Whether the index exists, how many entries it holds, the setup command, and the
    attribution the ROME licence asks reusers to quote (France Travail + date).
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from src.rome.db import RomeDb, RomeEntry

router = APIRouter(prefix="/api/rome", tags=["rome"])

_DB_PATH = "data/rome.db"


def _db() -> RomeDb:
    return RomeDb(db_path=_DB_PATH)


def _entry_to_response(entry: RomeEntry) -> dict:
    return {
        "code": entry.code,
        "label": entry.label,
        "keyword": entry.keyword,  # what the profile stores (no feminine form)
        "kind": entry.kind,
        "group": entry.group,
        "is_rome": True,
    }


@router.get("/occupations")
def search_occupations(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results"),
) -> list[dict]:
    return [_entry_to_response(e) for e in _db().search(q, limit=limit)]


@router.get("/status")
def rome_status() -> dict:
    db = _db()
    count = db.count()
    meta = db.meta()
    attribution = None
    if count and meta:
        attribution = (f"{meta.get('source', 'France Travail, ROME 4.0')} "
                       f"(version {meta.get('version', '?')}, published {meta.get('published', '?')})")
    return {
        "ready": count > 0,
        "count": count,
        "setup_command": "python scripts/setup_rome.py",
        "attribution": attribution,
    }
