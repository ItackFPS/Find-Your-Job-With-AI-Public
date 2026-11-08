"""Local ROME index (src/rome/db.py): autocomplete for the Profile page's domain field."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.rome.db import RomeDb, RomeEntry

ENTRIES = [
    RomeEntry("A", "Agriculture et Pêche", "domain"),
    RomeEntry("C12", "Comptabilité et gestion", "domain"),
    RomeEntry("A1101", "Conducteur / Conductrice d'engins agricoles", "occupation",
              group="Engins agricoles et forestiers", aliases=["Conducteur de tracteur"]),
    RomeEntry("M1203", "Comptable", "occupation", group="Comptabilité et gestion",
              aliases=["Aide-comptable", "Collaborateur / Collaboratrice comptable"]),
    RomeEntry("M1855", "Développeur / Développeuse web", "occupation", group="Études et développement informatique",
              aliases=["Développeur front-end"]),
]
META = {"version": "62", "published": "19/10/2026"}


@pytest.fixture()
def db(tmp_path: Path) -> RomeDb:
    d = RomeDb(str(tmp_path / "rome.db"))
    d.replace_all(ENTRIES, META)
    return d


def test_missing_database_is_empty_not_an_error(tmp_path: Path) -> None:
    d = RomeDb(str(tmp_path / "absent.db"))
    assert d.search("comptable") == [] and d.count() == 0 and not d.is_ready() and d.meta() == {}


def test_prefix_search_ignores_accents_and_case(db: RomeDb) -> None:
    assert {e.code for e in db.search("COMPTAB")} == {"M1203", "C12"}
    assert {e.code for e in db.search("developpeur")} == {"M1855"}
    assert {e.code for e in db.search("Développeur")} == {"M1855"}


def test_every_typed_word_must_match(db: RomeDb) -> None:
    assert [e.code for e in db.search("conducteur engins")] == ["A1101"]
    assert db.search("conducteur comptable") == []


def test_an_alias_finds_its_entry(db: RomeDb) -> None:
    assert [e.code for e in db.search("tracteur")] == ["A1101"]
    assert [e.code for e in db.search("front-end")] == ["M1855"]


def test_label_match_ranks_above_alias_match(db: RomeDb) -> None:
    assert db.search("comptable")[0].code == "M1203"


def test_kind_filter_and_limit(db: RomeDb) -> None:
    assert {e.kind for e in db.search("compt", kind="domain")} == {"domain"}
    assert len(db.search("co", limit=1)) == 1


def test_special_characters_never_break_the_query(db: RomeDb) -> None:
    for q in ['"', "a*b", "comptable) OR (", "NEAR(", "-", "'"]:
        db.search(q)  # no exception
    assert db.search("   ") == []


def test_replace_all_replaces_and_keeps_meta(db: RomeDb) -> None:
    db.replace_all([RomeEntry("Z", "Zoologue", "occupation")], {"version": "63"})
    assert db.count() == 1 and db.search("comptable") == [] and db.meta() == {"version": "63"}
    assert db.is_ready()


def test_entry_round_trip(db: RomeDb) -> None:
    (e,) = db.search("aide-comptable")
    assert e == ENTRIES[3]


@pytest.mark.parametrize(("label", "keyword"), [
    ("Conducteur / Conductrice d'engins agricoles", "Conducteur d'engins agricoles"),
    ("Chef / Cheffe de cuisine", "Chef de cuisine"),
    ("Expert-comptable / Experte-comptable", "Expert-comptable"),
    ("Infirmier / Infirmière en Pratique Avancée", "Infirmier en Pratique Avancée"),
    ("Développeur / Développeuse web", "Développeur web"),
    ("Soigneur animalier / Soigneuse animalière", "Soigneur animalier"),
    ("Comptable", "Comptable"),
    ("Agriculture et Pêche, Espaces naturels", "Agriculture et Pêche, Espaces naturels"),
])
def test_keyword_drops_the_feminine_form(label: str, keyword: str) -> None:
    """The profile stores the keyword; the title filter reads it as a literal title word."""
    assert RomeEntry("X", label, "occupation").keyword == keyword
