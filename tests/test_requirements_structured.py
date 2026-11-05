"""Structured evidence from France Travail fields (v5 L2) and the extractor-agreement report."""

from __future__ import annotations

import pytest

from src.evaluation.extractor_agreement import extractor_agreement
from src.models.job import Job
from src.requirements.structured import (
    parse_education_level, parse_experience_years, parse_salary_label, structured_evidence,
)
from src.storage.sqlite import Storage


def _ft(**fields) -> dict:
    return {"_france_travail_raw": {"id": "1", "intitule": "x", **fields}}


@pytest.mark.parametrize("label,years", [
    ("Débutant accepté", 0.0), ("1 An(s)", 1.0), ("5 An(s)", 5.0), ("6 Mois", 0.5),
    ("Expérience exigée", None), ("", None),
])
def test_experience_label(label, years):
    assert parse_experience_years(label) == years


@pytest.mark.parametrize("label,levels", [
    ("Bac+5 et plus ou équivalents", (5, None)), ("Bac+3, Bac+4 ou équivalents", (3, 4)),
    ("Bac+2 ou équivalents", (2, 2)), ("Bac ou équivalent", (0, 0)), ("CAP, BEP et équivalents", (-1, -1)),
    ("Autre", (None, None)),
])
def test_education_label(label, levels):
    assert parse_education_level(label) == levels


def test_salary_label():
    s = parse_salary_label("Annuel de 40000.0 Euros à 50000.0 Euros sur 12.0 mois")
    assert (s.period, s.minimum, s.maximum, s.months, s.currency) == ("annual", 40000.0, 50000.0, 12.0, "EUR")
    h = parse_salary_label("Horaire de 11.88 Euros")
    assert (h.period, h.minimum, h.maximum, h.months) == ("hourly", 11.88, 11.88, None)
    assert parse_salary_label("Selon profil") is None


def test_france_travail_payload_to_typed_evidence():
    ev = structured_evidence("france_travail", _ft(
        romeCode="m1805", romeLibelle="Études et développement informatique", appellationlibelle="Développeur",
        experienceExige="E", experienceLibelle="3 An(s)",
        formations=[{"niveauLibelle": "Bac+5 et plus ou équivalents", "exigence": "E"}, {"niveauLibelle": ""}, "x"],
        salaire={"libelle": "Mensuel de 2500.0 Euros sur 12 mois"}))
    assert (ev.occupation.code, ev.occupation.appellation) == ("M1805", "Développeur")
    assert (ev.experience.required, ev.experience.min_years) == (True, 3.0)
    assert len(ev.education) == 1 and ev.education[0].exigence == "required" and ev.education[0].level_min == 5
    assert (ev.salary.period, ev.salary.minimum) == ("monthly", 2500.0)
    beginner = structured_evidence("france_travail", _ft(experienceExige="D"))
    assert (beginner.experience.required, beginner.experience.min_years) == (False, 0.0)
    empty = structured_evidence("france_travail", _ft(romeCode="bogus"))
    assert empty.occupation is None and empty.experience is None and empty.education == () and empty.salary is None


def test_other_sources_have_no_structured_evidence():
    assert structured_evidence("linkedin", {"romeCode": "M1805"}) is None
    assert structured_evidence("france_travail", None) is None


def _db(tmp_path, jobs):
    path = tmp_path / "jobs.db"
    st = Storage(db_path=str(path))
    st.init_db()
    for job in jobs:
        st.upsert_job(job)
    return path


def _job(i, desc, **ft):
    return Job(id=f"j{i}", source="france_travail", source_job_id=str(i), title="Développeur", description=desc,
               raw_payload=_ft(**ft))


def test_agreement_report_against_structured_fields(tmp_path):
    jobs = [
        _job(1, "Expérience de 3 ans minimum exigée.", romeCode="M1805", experienceExige="E", experienceLibelle="3 An(s)"),
        _job(2, "Vous avez au moins 5 ans d'expérience.", romeCode="M1805", experienceExige="E", experienceLibelle="2 An(s)"),
        _job(3, "Rejoignez une équipe.", romeCode="M1805", experienceExige="E", experienceLibelle="2 An(s)"),
        _job(4, "Rejoignez une équipe.", experienceExige="D", experienceLibelle="Débutant accepté",
             formations=[{"niveauLibelle": "Bac+5 et plus ou équivalents", "exigence": "E"}]),
        Job(id="other", source="linkedin", title="Dev", description="5 ans d'expérience"),
    ]
    r = extractor_agreement(_db(tmp_path, jobs))
    assert r["status"] == "measured" and r["offers"] == 4
    assert (r["rome_coverage"]["k"], r["rome_coverage"]["n"]) == (3, 4)
    e = r["experience"]
    assert (e["recall"]["k"], e["recall"]["n"]) == (2, 3)          # job 3 states 2 years, extractor silent
    assert (e["precision"]["k"], e["precision"]["n"]) == (1, 2)    # 5 extracted vs 2 stated disagrees
    assert e["disagreement"]["value"] == pytest.approx(0.5)
    assert (r["education"]["recall"]["n"]) == 1


def test_agreement_report_without_france_travail_offers(tmp_path):
    r = extractor_agreement(_db(tmp_path, [Job(id="a", source="linkedin", title="t")]))
    assert r["status"] == "no_france_travail_offers" and r["rome_coverage"]["value"] is None


def test_api_reports_extractor_agreement(tmp_path):
    from fastapi.testclient import TestClient

    from src.api.app import app

    path = _db(tmp_path, [_job(1, "x", romeCode="M1805")])
    saved = app.state.db_path
    app.state.db_path = str(path)
    try:
        r = TestClient(app).get("/api/quality/extractor-agreement").json()
    finally:
        app.state.db_path = saved
    assert r["status"] == "measured" and r["rome_coverage"]["value"] == 1.0
