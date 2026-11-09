#!/usr/bin/env python
"""Download France Travail's ROME open data and build the local domain index.

Fills ``data/rome.db``: the professional domains and job sheets of ROME 4.0 (French
labels, with each sheet's other job titles), which the Profile page's domain field
suggests from. One download of about 4 MB; run it once, and again after a new ROME
version (France Travail publishes two a year).

Usage::

    python scripts/setup_rome.py                 # download, then index
    python scripts/setup_rome.py --zip rome.zip  # index a zip you already downloaded
    python scripts/setup_rome.py --db data/custom.db

Data: ROME, France Travail, Licence Ouverte ROME. Reuse requires naming France Travail
and the date of the last update, which the index keeps (see ``/api/rome/status``).
Source: https://www.data.gouv.fr/datasets/repertoire-operationnel-des-metiers-et-des-emplois-rome
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.rome.db import RomeDb
from src.rome.loader import RomeSetupError, download_rome_zip, load

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local ROME index (data/rome.db).")
    parser.add_argument("--db", default="data/rome.db", help="ROME SQLite database (default: data/rome.db)")
    parser.add_argument("--zip", dest="zip_path", help="Index this ROME open-data zip instead of downloading")
    args = parser.parse_args()
    try:
        data = Path(args.zip_path).read_bytes() if args.zip_path else download_rome_zip()
        db = RomeDb(args.db)
        count = load(db, data)
    except (RomeSetupError, OSError) as exc:
        logging.error("%s", exc)
        return 1
    meta = db.meta()
    print(f"ROME: {count} entries indexed in {args.db} "
          f"(version {meta.get('version', '?')}, published {meta.get('published', '?')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
