"""France Travail salary reaches the stored offer (Phase B B6-B, finding P-007)."""

from __future__ import annotations

import pytest

from src.collectors.france_travail import FranceTravailCollector, _annual_salary
from src.pipeline.normalize import normalize_job


@pytest.mark.parametrize("label,expected", [
    ("Annuel de 40000.0 Euros à 50000.0 Euros sur 12.0 mois", (40000.0, 50000.0)),
    ("Annuel de 30000.0 Euros", (30000.0, None)),
    ("Mensuel de 1900.0 Euros à 3000.0 Euros", (22800.0, 36000.0)),            # 12 months when unstated
    ("Mensuel de 2000.0 Euros sur 13.0 mois", (26000.0, None)),
    ("Horaire de 12.5 Euros sur 12.0 mois", (None, None)),                     # no working time stated
    ("Selon profil", (None, None)),
])
def test_annual_salary(label, expected) -> None:  # noqa: ANN001
    assert _annual_salary({"libelle": label}) == expected


@pytest.mark.parametrize("salaire", [None, {}, {"libelle": None}, {"libelle": 42}, "Annuel de 1 Euros"])
def test_missing_or_malformed_salary_is_empty_never_raises(salaire) -> None:  # noqa: ANN001
    assert _annual_salary(salaire) == (None, None)


def test_the_stored_offer_carries_the_salary() -> None:
    raw = {"id": "215X", "intitule": "Technicien support", "description": "Poste.",
           "lieuTravail": {"libelle": "83 - Toulon"}, "typeContrat": "CDI",
           "salaire": {"libelle": "Annuel de 26000.0 Euros à 28000.0 Euros"}}
    job = normalize_job("france-travail", FranceTravailCollector._normalize_fields(raw))
    assert (job.salary_min, job.salary_max, job.salary_currency) == (26000.0, 28000.0, "EUR")


def test_an_offer_without_salary_stays_without() -> None:
    raw = {"id": "215Y", "intitule": "Technicien support", "description": "Poste.",
           "lieuTravail": {"libelle": "83 - Toulon"}, "typeContrat": "CDI"}
    job = normalize_job("france-travail", FranceTravailCollector._normalize_fields(raw))
    assert job.salary_min is None and job.salary_max is None
