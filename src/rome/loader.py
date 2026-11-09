"""Build the ROME index from France Travail's open-data zip (Licence Ouverte ROME).

The zip (``ROME_OPEN_DATA_URL``, about 4 MB) holds one CSV per ROME table. Only four
are read: broad domains, professional domains, job sheets with their domain, and job
titles (appellations), plus ``version.txt`` for the date France Travail asks reusers to
quote. Run by ``scripts/setup_rome.py``; never at application start.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import zipfile

import httpx

from src.rome.db import RomeDb, RomeEntry

logger = logging.getLogger(__name__)

ROME_OPEN_DATA_URL = "https://api.francetravail.fr/api-nomenclatureemploi/v1/open-data/csv"
SOURCE = "France Travail, ROME 4.0"
_USER_AGENT = "FindYourJobWithAI (personal, local; ROME open data download)"
_TABLES = ("grand_domaine", "domaine_professionnel", "cr_gd_dp", "referentiel_appellation")


class RomeSetupError(RuntimeError):
    """The ROME data could not be downloaded or is not in the expected shape."""


def download_rome_zip(url: str = ROME_OPEN_DATA_URL, timeout: float = 120.0) -> bytes:
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": _USER_AGENT})
    except httpx.HTTPError as exc:
        raise RomeSetupError(f"ROME download failed: {exc}") from exc
    if resp.status_code != 200:
        raise RomeSetupError(f"ROME download failed: HTTP {resp.status_code} from {url}")
    return resp.content


def _rows(z: zipfile.ZipFile, table: str) -> list[dict[str, str]]:
    # the file name carries the ROME version ("unix_<table>_v462_utf8.csv"): match without it
    pattern = re.compile(rf"^unix_{table}_v\d+_utf8\.csv$")
    name = next((n for n in z.namelist() if pattern.match(n)), None)
    if name is None:
        raise RomeSetupError(f"ROME zip has no unix_{table}_v*_utf8.csv file")
    return list(csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig"))))


def parse_rome_zip(data: bytes) -> tuple[list[RomeEntry], dict[str, str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise RomeSetupError("ROME download is not a zip file") from exc
    with z:
        tables = {t: _rows(z, t) for t in _TABLES}
        version_txt = z.read("version.txt").decode("utf-8", "replace") if "version.txt" in z.namelist() else ""
    meta = {"source": SOURCE}
    for key, label in (("version", "version"), ("published", "publication")):
        m = re.search(rf"{label}\s*:\s*(\S+)", version_txt, re.I)
        if m:
            meta[key] = m.group(1)

    entries = [RomeEntry(r["code_grand_domaine"], r["libelle_grand_domaine"].strip(), "domain")
               for r in tables["grand_domaine"]]
    entries += [RomeEntry(r["code_domaine_professionnel"], r["libelle_domaine_professionnel"].strip(), "domain")
                for r in tables["domaine_professionnel"]]
    titles: dict[str, list[str]] = {}
    for r in tables["referentiel_appellation"]:
        title = (r.get("libelle_appellation_long") or "").strip()
        if title:
            titles.setdefault(r["code_rome"], []).append(title)
    for r in tables["cr_gd_dp"]:
        code, label = r["code_rome"], r["libelle_rome"].strip()
        aliases = [t for t in dict.fromkeys(titles.get(code, [])) if t.casefold() != label.casefold()]
        entries.append(RomeEntry(code, label, "occupation", r["libelle_domaine_professionel"].strip(), aliases))
    return entries, meta


def load(db: RomeDb, data: bytes) -> int:
    """Replace the index with the ROME zip's content; returns the number of entries."""
    entries, meta = parse_rome_zip(data)
    db.replace_all(entries, meta)
    logger.info("ROME: indexed %d entries (version %s)", len(entries), meta.get("version", "?"))
    return len(entries)
