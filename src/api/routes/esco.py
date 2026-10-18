"""ESCO autocomplete API routes.

GET /api/esco/skills?q=<query>&lang=<en|fr>&limit=<n>
    Fast local autocomplete from the SQLite FTS index.
    Returns an empty list when the ESCO database has not been set up yet.

GET /api/esco/occupations?q=<query>&lang=<en|fr>&limit=<n>
    Same lookup restricted to ESCO occupation concepts (e.g. "Seller",
    "Cleaner") — used to power the Profile page's domain search so it isn't
    limited to the small curated IT domain list.

GET /api/esco/status
    Reports whether the local ESCO index is available and how many concepts
    are indexed.

The index is populated by running::

    python scripts/setup_esco.py
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

from src.esco.db import EscoDb

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/esco", tags=["esco"])

_DB_PATH = "data/esco.db"


def _get_db() -> EscoDb:
    return EscoDb(db_path=_DB_PATH)


# ---------------------------------------------------------------------------
# Response schemas (inline — too small to warrant a shared schemas.py entry)
# ---------------------------------------------------------------------------

def _concept_to_response(concept: Any) -> dict:
    label_en = concept.preferred_label_en
    label_fr = concept.preferred_label_fr

    # Short display label: strip parenthetical annotations.
    keyword_en = concept.keyword  # e.g. "Python (programming language)" → "Python"

    return {
        "concept_uri": concept.concept_uri,
        "label": label_en,          # full canonical English label
        "label_fr": label_fr,        # full canonical French label
        "keyword": keyword_en,       # simplified keyword for pipeline matching
        "alt_labels_en": concept.alt_labels_en,
        "alt_labels_fr": concept.alt_labels_fr,
        "concept_type": concept.concept_type,
        "is_esco": not concept.concept_uri.startswith("local:"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/skills")
def search_skills(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    lang: str = Query("en", pattern="^(en|fr)$", description="Preferred language for results"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results"),
) -> list[dict]:
    """Search the local ESCO skill index.

    Returns up to ``limit`` matching concepts, ranked by relevance.
    Results include both English and French labels so the frontend can display
    the label in the user's language.

    Returns an empty list if the ESCO database has not been set up yet.
    Run ``python scripts/setup_esco.py`` to populate it.
    """
    db = _get_db()
    concepts = db.search(q, lang=lang, limit=limit, concept_type="skill")
    return [_concept_to_response(c) for c in concepts]


@router.get("/occupations")
def search_occupations(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    lang: str = Query("en", pattern="^(en|fr)$", description="Preferred language for results"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results"),
) -> list[dict]:
    """Search the local ESCO occupation index (e.g. "Seller", "Cleaner").

    Same shape and behaviour as ``/skills`` above, restricted to
    ``concept_type="occupation"`` so the Profile page's domain field can
    search the full ESCO occupation taxonomy rather than a fixed IT list.
    """
    db = _get_db()
    concepts = db.search(q, lang=lang, limit=limit, concept_type="occupation")
    return [_concept_to_response(c) for c in concepts]


@router.get("/status")
def esco_status() -> dict:
    """Return the status of the local ESCO index.

    Response fields:
      - ``ready``:   True when at least one concept is indexed.
      - ``count``:   Number of concepts in the index.
      - ``setup_command``: Command to run to populate the index.
    """
    db = _get_db()
    count = db.count()
    return {
        "ready": count > 0,
        "count": count,
        "setup_command": "python scripts/setup_esco.py",
        "message": (
            f"{count} skills indexed — autocomplete is active."
            if count > 0
            else "ESCO index not populated. Run: python scripts/setup_esco.py"
        ),
    }
