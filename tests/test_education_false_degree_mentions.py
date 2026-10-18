"""Issue #51: skill "maîtrise …" and role/data "Master" jargon are not degrees.

Examples are taken from real postings where the old patterns produced a
Bac+4/Bac+5 requirement and hard-rejected a Bac+2 profile.
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.requirements.education import extract_education
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate


def _gate_passes(description: str) -> bool:
    job = Job(id="j1", source="test", title="Technicien", description=description)
    requirements = RequirementExtractor(user_skills=["python"]).extract(job)
    return HardConstraintGate(user_max_education_level=2).check(requirements).passes


@pytest.mark.parametrize(
    "text",
    [
        "Maitrise obligatoire des langues françaises et anglaises.",
        "Maîtrise avancée de l'anglais (un niveau de français conversationnel).",
        "Maîtrise Cloud (Azure, GCP ou AWS) et de leurs services managés.",
        "Expérience DevOps, avec une maîtrise avérée de Kubernetes et Terraform.",
        "Langue(s) de travail\n\nAnglais (C-Maitrise professionnelle usuelle)",
        "Une maîtrise en Matlab ou Python serait un plus.",
        "Environnement technique\n\nMaîtrise requise\n\nWindows Server",
        "Vous justifiez d'un niveau de maîtrise avancé.",
        "Vous disposez d'une maîtrise avérée des outils BIM.",
        "Maîtrise en informatique requise.",
    ],
)
def test_skill_maitrise_is_not_a_degree(text):
    edu = extract_education(text)
    assert edu is None or edu.level_label != "Maîtrise", edu
    assert _gate_passes(text)


@pytest.mark.parametrize(
    "text",
    [
        "Titulaire d'une Maîtrise, minimum.",
        "Titulaire d’une maîtrise en droit requise.",
        "Diplômé d'un baccalauréat et d'une maitrise en informatique, requise.",
        "Diplômé(e) d'une Maîtrise en gestion requise.",
    ],
)
def test_maitrise_with_degree_cue_is_still_a_bac4_degree(text):
    edu = extract_education(text)
    assert edu is not None
    assert (edu.level_bac_plus, edu.level_label) == (4, "Maîtrise"), edu
    assert not _gate_passes(text)


@pytest.mark.parametrize(
    "text",
    [
        "Chef de Projet, Product Owner ou Scrum Master dans un contexte Data.",
        "Une expérience sur un outil de Master Data Management (MDM) est appréciée.",
    ],
)
def test_master_jargon_is_not_a_degree(text):
    assert extract_education(text) is None
    assert _gate_passes(text)


@pytest.mark.parametrize(
    "text",
    [
        "Bac+5 (école d'ingénieur, Master Data, Supply Chain) requis.",
        "Master en informatique requis.",
        "Titulaire d'un Master 2 en data science.",
    ],
)
def test_real_master_degrees_still_detected(text):
    edu = extract_education(text)
    assert edu is not None
    assert edu.level_bac_plus == 5, edu
