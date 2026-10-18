"""Issue #58: a HELD education range resolves to its lowest level.

Policy approved by the owner (2026-09-18): when a requirement states an
adjacent range of held degrees ("Bac+2 à Bac+3", "Bac+3 à Bac+5",
"Bac+2/Bac+3", "Bac+3 ou Bac+4", "entre Bac+3 et Bac+5"), the minimum held
education required is the LOWEST level of the range — consistent with the
enrolment-range rule of #40/#54. Separately stated mentions keep the
existing rule (the highest explicitly qualified level wins), and explicit
single-level requirements ("Bac+5 minimum") are unchanged.
"""

from __future__ import annotations

import pytest

from src.models.job import Job
from src.requirements.education import extract_education, parse_education_level
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import EducationKind, Importance


def _passes(description: str, held: str) -> bool:
    job = Job(id="j1", source="test", title="Développeur", description=description)
    requirements = RequirementExtractor(user_skills=["python"]).extract(job)
    gate = HardConstraintGate(user_max_education_level=parse_education_level(held))
    passing, _results = gate_jobs([job], {job.id: requirements}, gate)
    return passing == [job]


# ---------------------------------------------------------------------------
# Held ranges → lowest level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, level, importance",
    [
        ("Formation Bac+2 à Bac+3 souhaitée.", 2, Importance.PREFERRED),
        ("Bac+3 à Bac+5 requis.", 3, Importance.MANDATORY),
        ("Titulaire d'un Bac+3 à Bac+5 en informatique.", 3, Importance.MANDATORY),
        ("Bac+2/Bac+3 en informatique, idéalement.", 2, Importance.PREFERRED),
        ("Formation Bac +3 / Bac +5 minimum.", 3, Importance.MANDATORY),
        ("Bac+3 ou Bac+4 exigé.", 3, Importance.MANDATORY),
        ("Diplôme de niveau Bac+2 - Bac+3 requis.", 2, Importance.MANDATORY),
        ("Formation de Bac+2 au Bac+5 souhaitée.", 2, Importance.PREFERRED),
        ("Niveau d'études entre Bac+3 et Bac+5 requis.", 3, Importance.MANDATORY),
        ("Vous êtes diplômé(e) d'un Bac+3 à Bac+5 en informatique, obligatoire.", 3, Importance.MANDATORY),
    ],
)
def test_held_range_resolves_to_its_lowest_level(text, level, importance):
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.HELD
    assert (edu.level_bac_plus, edu.importance) == (level, importance), edu


@pytest.mark.parametrize(
    "text, rejects, accepts",
    [
        ("Formation Bac+2 à Bac+3 souhaitée.", None, "bac+2"),
        ("Formation Bac+2 à Bac+5, requis.", None, "bac+2"),
        ("Bac+3 à Bac+5 requis.", "bac+2", "bac+3"),
        ("Bac+3/Bac+5 obligatoire.", "bac+2", "bac+3"),
    ],
)
def test_held_range_gate_uses_the_lower_bound(text, rejects, accepts):
    if rejects is not None:
        assert not _passes(text, rejects)
    assert _passes(text, accepts)


# ---------------------------------------------------------------------------
# Unchanged: explicit single-level requirements and separate mentions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["Bac+5 minimum.", "Bac+5 obligatoire.", "Bac+5 requis.", "Titulaire d'un Bac+5."])
def test_explicit_single_level_requirement_is_unchanged(text):
    edu = extract_education(text)
    assert edu is not None and edu.level_bac_plus == 5
    assert not _passes(text, "bac+4")
    assert _passes(text, "bac+5")


@pytest.mark.parametrize(
    "text, level",
    [
        # Separately qualified mentions: the highest explicit requirement wins.
        ("Bac+2 souhaité, Bac+5 exigé.", 5),
        # Not a range: "et" without "entre" describes a team, not a requirement.
        ("Notre équipe compte des profils Bac+5 et Bac+3. Bac+5 requis.", 5),
        # Alternatives carrying their own extra condition are not adjacent.
        ("Bac+5 avec 3 ans d'expérience, ou Bac+3 avec 8 ans d'expérience, requis.", 5),
    ],
)
def test_non_adjacent_mentions_keep_the_highest_level(text, level):
    edu = extract_education(text)
    assert edu is not None and edu.level_bac_plus == level, edu


# ---------------------------------------------------------------------------
# Enrolment and preparing ranges keep their own semantics (#40 / #54 / #63)
# ---------------------------------------------------------------------------

def test_enrolment_range_is_unchanged():
    edu = extract_education("Étudiant(e) en Bac+3 à Bac+4.")
    assert edu is not None and edu.kind == EducationKind.ENROLLED
    assert (edu.level_bac_plus, edu.enrolment_level_min) == (4, 3)
    assert _passes("Étudiant(e) en Bac+3 à Bac+4.", "bac+2")
    assert not _passes("Étudiant(e) en Bac+3 à Bac+4.", "bac+1")


@pytest.mark.parametrize(
    "text",
    ["Vous préparez un diplôme Bac+3 à Bac+5 en automatisme.", "Tu prépares un Bac+2/Bac+3 en alternance."],
)
def test_preparing_range_is_not_a_held_requirement(text):
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.PREPARING
    assert _passes(text, "bac+1")


@pytest.mark.parametrize(
    "text, level",
    [
        # The noun "diplôme" is neither a held nor a preparing cue (#63): the
        # default held reading applies, and the range rule with it.
        ("Diplôme Bac+2 à Bac+3 requis.", 2),
        ("Diplôme d'ingénieur ou Master requis.", 5),
    ],
)
def test_unrelated_diplome_wording_stays_held(text, level):
    edu = extract_education(text)
    assert edu is not None and edu.kind == EducationKind.HELD
    assert edu.level_bac_plus == level
