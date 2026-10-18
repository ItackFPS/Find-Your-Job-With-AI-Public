#!/usr/bin/env python
"""One-time ESCO data setup script.

Populates the local SQLite FTS database (data/esco.db) with IT-relevant
ESCO skills.  Run this once before using the Profile autocomplete feature.

What it does:
  1. Initialises the database schema.
  2. Loads the bundled seed data (works offline, instant).
  3. Optionally downloads additional concepts from the public ESCO REST API
     (free, no authentication required — requires internet).

Usage::

    # From the project root:
    python scripts/setup_esco.py

    # Seed data only (no network):
    python scripts/setup_esco.py --no-api

    # Custom database location:
    python scripts/setup_esco.py --db data/custom.db

The ESCO REST API is public and free but please be polite — a short delay
between requests is enforced by default.  Do not run this script repeatedly
in a tight loop.

ESCO dataset: https://esco.ec.europa.eu/en/use-esco/esco-data/download-esco
ESCO API docs: https://ec.europa.eu/esco/api/doc/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure src/ is importable when running from the project root.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.esco.db import EscoDb
from src.esco.loader import load_from_api, load_seed_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("setup_esco")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate the local ESCO skills index.",
    )
    parser.add_argument(
        "--db",
        default="data/esco.db",
        help="Path to the ESCO SQLite database (default: data/esco.db)",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Skip the ESCO REST API download (seed data only, works offline)",
    )
    parser.add_argument(
        "--api-delay",
        type=float,
        default=0.3,
        help="Seconds to wait between ESCO API requests (default: 0.3)",
    )
    args = parser.parse_args()

    db = EscoDb(db_path=args.db)

    # 1. Initialise schema.
    logger.info("Initialising ESCO database at: %s", args.db)
    db.init_db()

    # 2. Load bundled seed data (always available, no network needed).
    logger.info("Loading bundled seed data...")
    seed_count = load_seed_data(db)
    logger.info("Seed data: %d concepts loaded.", seed_count)

    # 3. Optionally fetch additional concepts from the ESCO REST API.
    if not args.no_api:
        logger.info(
            "Fetching concepts from ESCO REST API (this may take a few minutes)..."
        )
        logger.info(
            "  Tip: use --no-api to skip this step and work offline with seed data only."
        )
        try:
            skill_count = load_from_api(db, delay_seconds=args.api_delay, concept_type="skill")
            logger.info("ESCO API: %d skill concepts fetched.", skill_count)
        except Exception as exc:
            logger.warning(
                "ESCO API skill download failed (continuing with seed data): %s", exc
            )
        try:
            occupation_count = load_from_api(db, delay_seconds=args.api_delay, concept_type="occupation")
            logger.info("ESCO API: %d occupation concepts fetched.", occupation_count)
        except Exception as exc:
            logger.warning(
                "ESCO API occupation download failed (continuing without it): %s", exc
            )
    else:
        logger.info("Skipping ESCO API download (--no-api specified).")

    # Summary
    total = db.count()
    logger.info("Setup complete. Total concepts in index: %d", total)

    if total == 0:
        logger.error("No concepts were indexed. Check the database path and permissions.")
        sys.exit(1)

    logger.info(
        "Profile autocomplete is now active. The backend API serves "
        "GET /api/esco/skills?q=<query> from the local index."
    )


if __name__ == "__main__":
    main()
