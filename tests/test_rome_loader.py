"""ROME loader (src/rome/loader.py): builds the index from France Travail's open-data zip."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from src.rome import loader
from src.rome.db import RomeDb
from src.rome.loader import RomeSetupError, parse_rome_zip

# the real files' columns, with a few rows
_FILES = {
    "unix_grand_domaine_v462_utf8.csv": '"code_grand_domaine","libelle_grand_domaine"\n'
        '"A","Agriculture et Pêche, Espaces naturels et Espaces verts, Soins aux animaux"\n'
        '"M","Support à l\'entreprise"\n',
    "unix_domaine_professionnel_v462_utf8.csv": '"code_domaine_professionnel","libelle_domaine_professionnel"\n'
        '"A11","Engins agricoles et forestiers"\n"M12","Gestion administrative"\n',
    "unix_cr_gd_dp_v462_utf8.csv": '"code_rome","libelle_rome","code_grand_domaine","libelle_grand_domaine",'
        '"code_domaine_professionel","libelle_domaine_professionel"\n'
        '"A1101","Conducteur / Conductrice d\'engins agricoles","A","x","A11","Engins agricoles et forestiers"\n'
        '"M1203","Comptable","M","y","M12","Gestion administrative"\n',
    "unix_referentiel_appellation_v462_utf8.csv": '"code_ogr","libelle_appellation_long","libelle_appellation_court",'
        '"code_rome","classification","peu_usite"\n'
        '"1","Conducteur / Conductrice de tracteur","Conducteur de tracteur","A1101","SYNONYME","N"\n'
        '"2","Comptable","Comptable","M1203","SYNONYME","N"\n'
        '"3","Aide-comptable","Aide-comptable","M1203","SYNONYME","O"\n',
    "version.txt": "Numero de version : 62\n\nDate de publication : 19/10/2026\n\nTitre : ROME 4.0 version 62\n",
}


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text.encode("utf-8-sig"))
    return buf.getvalue()


def test_parse_builds_domains_occupations_and_aliases() -> None:
    entries, meta = parse_rome_zip(_zip(_FILES))
    by_code = {e.code: e for e in entries}
    assert meta == {"version": "62", "published": "19/10/2026", "source": "France Travail, ROME 4.0"}
    assert by_code["A"].kind == "domain" and by_code["A11"].kind == "domain"
    a1101 = by_code["A1101"]
    assert a1101.kind == "occupation" and a1101.group == "Engins agricoles et forestiers"
    assert a1101.aliases == ["Conducteur / Conductrice de tracteur"]
    # an alias equal to the label adds nothing; a title France Travail marks as rarely used is kept
    assert by_code["M1203"].aliases == ["Aide-comptable"]


def test_a_zip_missing_a_file_is_a_clear_error() -> None:
    with pytest.raises(RomeSetupError, match="unix_cr_gd_dp"):
        parse_rome_zip(_zip({k: v for k, v in _FILES.items() if "cr_gd_dp" not in k}))
    with pytest.raises(RomeSetupError, match="not a zip"):
        parse_rome_zip(b"<html>blocked</html>")


def test_the_file_version_suffix_does_not_matter() -> None:
    renamed = {k.replace("_v462_", "_v470_"): v for k, v in _FILES.items()}
    entries, _ = parse_rome_zip(_zip(renamed))
    assert len(entries) == 6


def test_load_fills_the_index(tmp_path: Path) -> None:
    db = RomeDb(str(tmp_path / "rome.db"))
    assert loader.load(db, _zip(_FILES)) == 6
    assert [e.code for e in db.search("tracteur")] == ["A1101"] and db.meta()["version"] == "62"


def test_download_errors_are_one_clear_message(monkeypatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(loader.httpx, "get", boom)
    with pytest.raises(RomeSetupError, match="download"):
        loader.download_rome_zip()

    monkeypatch.setattr(loader.httpx, "get", lambda *a, **k: httpx.Response(
        503, request=httpx.Request("GET", "https://x")))
    with pytest.raises(RomeSetupError, match="503"):
        loader.download_rome_zip()


@pytest.mark.network
def test_live_download_has_the_expected_files() -> None:
    entries, meta = parse_rome_zip(loader.download_rome_zip())
    assert len(entries) > 1900 and meta["version"]
