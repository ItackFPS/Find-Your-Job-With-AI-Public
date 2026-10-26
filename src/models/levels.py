"""Career levels and education levels of profile v2 (update plan §C3).

Career levels are what the candidate *targets*, in ascending order. Education levels are
the French national nomenclature levels 3–8, which share their numbers with EQF levels
3–8 (CAP 3, Bac 4, BTS/DUT 5, Licence/BUT 6, Master/ingénieur 7, Doctorat 8). The app
only ever compares level numbers; it never claims two qualifications are equivalent.
"""

from __future__ import annotations

from typing import Literal

CareerLevel = Literal["internship", "alternance", "graduate", "junior", "mid", "senior", "lead", "manager"]

# Ascending; internship and alternance share the lowest rank (both are contracts for
# people still in training).
CAREER_LEVELS: tuple[str, ...] = ("internship", "alternance", "graduate", "junior", "mid", "senior", "lead", "manager")
CAREER_RANK: dict[str, int] = {
    "internship": 0, "alternance": 0, "graduate": 1, "junior": 2, "mid": 3, "senior": 4, "lead": 5, "manager": 5,
}

# v1 ``experience.preferred_levels`` labels → career level.
V1_PREFERRED_TO_LEVEL: dict[str, str] = {
    "internship": "internship", "intern": "internship", "stage": "internship",
    "alternance": "alternance", "apprenticeship": "alternance",
    "entry_level": "graduate", "graduate": "graduate",
    "junior": "junior",
    "mid": "mid", "mid_level": "mid", "intermediate": "mid", "confirmed": "mid",
    "senior": "senior",
    "lead": "lead",
    "manager": "manager",
}

# v1 ``experience.excluded_seniority`` labels (config/filters.yaml seniority_exclusions)
# → the career level they rule out.
V1_EXCLUDED_TO_LEVEL: dict[str, str] = {
    "senior": "senior",
    "lead": "lead", "principal": "lead", "staff": "lead",
    "manager": "manager", "director": "manager", "vp": "manager", "executive": "manager", "c_suite": "manager",
}

EducationLevel = Literal[3, 4, 5, 6, 7, 8]
MIN_EDUCATION_LEVEL = 3
MAX_EDUCATION_LEVEL = 8


def bac_plus_to_level(bac_plus: int) -> int:
    """Map a Bac+N number (``src.requirements.education`` scale, Bac = 0, below Bac = -1)
    to a level 3–8.

    CAP/BEP 3, Bac 4, Bac+2 5, Bac+3 6, Bac+5 7, Bac+8 8. Bac+1 counts as Bac (no
    qualification at +1), Bac+4 as level 6 (a first-year master is not a level 7
    qualification), and Bac+6/+7 as level 7.
    """
    if bac_plus < 0:
        return 3
    if bac_plus <= 1:
        return 4
    if bac_plus == 2:
        return 5
    if bac_plus <= 4:
        return 6
    if bac_plus <= 7:
        return 7
    return 8


def level_to_max_bac_plus(level: int) -> int:
    """The highest Bac+N a level covers (the inverse of ``bac_plus_to_level``): an offer
    asking Bac+N is within a candidate's level exactly when N <= this value."""
    return {3: -1, 4: 1, 5: 2, 6: 4, 7: 7}.get(level, 8 if level >= 8 else -1)


def level_to_min_bac_plus(level: int) -> int:
    """The lowest Bac+N of a level: the years of study a holder of that level surely has."""
    return {3: -1, 4: 0, 5: 2, 6: 3, 7: 5}.get(level, 8 if level >= 8 else -1)


# Qualification names the profile offers (French national nomenclature, décret
# n° 2019-14). A foreign degree is entered with the level the user picks. Mirrored in
# frontend/src/pages/EducationEditor.tsx (tests/test_education_levels_v2.py checks it).
QUALIFICATION_LEVELS: dict[str, int] = {
    "CAP": 3, "BEP": 3,
    "Bac": 4, "Bac pro": 4, "Bac techno": 4,
    "BTS": 5, "DUT": 5, "DEUG": 5, "DEUST": 5,
    "BUT": 6, "Licence": 6, "Licence pro": 6, "Bachelor": 6,
    "Master": 7, "Diplôme d'ingénieur": 7, "MBA": 7,
    "Doctorat": 8,
}
