"""Issue #41: an experience clause's qualifier is not the degree's qualifier.

"Issu(e) d'une formation supérieure (école d'ingénieurs ou université), vous
justifiez d'au moins 3 ans d'expérience" — "au moins" qualifies the 3 years,
not the school. Like a neighbouring education mention (#19), a numeric
experience-duration mention across a clause separator bounds the window.
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.requirements.education import extract_education
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate
from src.requirements.models import Importance


def _education_gate_passes(description: str) -> bool:
    job = Job(id="j1", source="test", title="Ingénieur qualité", description=description)
    requirements = RequirementExtractor(user_skills=["python"]).extract(job)
    # No experience ceiling: only the education decision is under test here.
    return HardConstraintGate(user_max_education_level=2).check(requirements).passes


@pytest.mark.parametrize(
    "text",
    [
        "Votre profil : Issu(e) d'une formation supérieure (école d'ingénieurs ou université), "
        "vous justifiez d'au moins 3 ans d'expérience en qualité.",
        "Bac+5 en informatique, vous justifiez de 5 ans d'expérience minimum.",
        "Diplôme d'ingénieur ; 2 années d'expérience souhaitée.",
        "Master in Computer Science, at least 4 years of experience required.",
    ],
)
def test_experience_qualifier_is_not_attributed_to_the_degree(text):
    edu = extract_education(text)
    assert edu is not None
    assert edu.importance == Importance.UNKNOWN, edu
    assert _education_gate_passes(text)


@pytest.mark.parametrize(
    "text, importance",
    [
        # Qualifier in the degree's own clause still applies.
        ("Bac+5 minimum, 3 ans d'expérience.", Importance.MANDATORY),
        ("Bac+5 exigé. 2 ans d'expérience souhaitée.", Importance.MANDATORY),
        ("Master idéalement, 3 ans d'expérience requis.", Importance.PREFERRED),
        # No clause separator between them: still shared (ambiguous, unchanged).
        ("Bac+5 et 3 ans d'expérience minimum.", Importance.MANDATORY),
        # No experience mention: unchanged.
        ("Bac+5 requis pour ce poste.", Importance.MANDATORY),
        # The degree's own list and softener stay with it; only the clause
        # that contains the experience duration is cut off.
        ("Bac+5 en électronique, systèmes embarqués, ou équivalent.\n\nExpérience de 3 à 5 ans.", Importance.PREFERRED),
        ("Formation d'ingénieur (Bac+5) en informatique, santé, ou équivalent. Expérience de 3 ans minimum.", Importance.PREFERRED),
        ("Bac+5 en informatique, idéalement en data, et 3 ans d'expérience.", Importance.PREFERRED),
    ],
)
def test_qualifiers_in_the_degree_clause_are_unchanged(text, importance):
    edu = extract_education(text)
    assert edu is not None
    assert edu.importance == importance, edu
