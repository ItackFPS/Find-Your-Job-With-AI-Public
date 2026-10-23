#!/usr/bin/env python3
"""Regenerate src/geo/data/ from the official commune and département lists.

Source: geo.api.gouv.fr (Etalab / IGN / INSEE, Licence Ouverte 2.0), two requests:
every current French commune with its INSEE code, name, postcodes, département,
region, population and centroid (communes.csv.gz), and the départements with their
names and regions (departements.csv). The app bundles the result so that resolving
a place never needs the network.

Usage:
    python scripts/build_geo_data.py            # writes src/geo/data/
    python scripts/build_geo_data.py --check    # only prints counts

Run it when communes change (mergers happen every January), then run
tests/test_geo_places.py. Rows are sorted by INSEE code and written without a
timestamp, so an unchanged source gives a byte-identical file.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import sys
import urllib.request
from pathlib import Path

URL = (
    "https://geo.api.gouv.fr/communes"
    "?fields=nom,code,codesPostaux,centre,codeDepartement,codeRegion,population&format=json"
)
DEPT_URL = "https://geo.api.gouv.fr/departements?fields=nom,code,codeRegion"
DATA = Path(__file__).resolve().parents[1] / "src" / "geo" / "data"
OUT = DATA / "communes.csv.gz"
DEPT_OUT = DATA / "departements.csv"
FIELDS = ("insee", "name", "postcodes", "department", "region", "population", "lat", "lon")


def fetch(url: str = URL) -> list[dict]:
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https URL
        return list(json.load(resp))


def rows(communes: list[dict]) -> list[tuple[str, ...]]:
    out = []
    for c in communes:
        centre = (c.get("centre") or {}).get("coordinates")
        if not centre or not c.get("codeDepartement"):
            continue
        lon, lat = centre
        out.append((
            c["code"], c["nom"], " ".join(sorted(c.get("codesPostaux") or [])),
            c["codeDepartement"], c.get("codeRegion") or "", str(c.get("population") or 0),
            f"{lat:.4f}", f"{lon:.4f}",
        ))
    return sorted(out)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="print counts, write nothing")
    args = p.parse_args()
    data = rows(fetch())
    depts = sorted((d["code"], d["nom"], d.get("codeRegion") or "") for d in fetch(DEPT_URL))
    print(f"{len(data)} communes, {len(depts)} départements")
    if args.check:
        return 0
    with DEPT_OUT.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(("code", "name", "region"))
        writer.writerows(depts)
    text = io.StringIO()
    writer = csv.writer(text, lineterminator="\n")
    writer.writerow(FIELDS)
    writer.writerows(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(text.getvalue().encode("utf-8"))
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
