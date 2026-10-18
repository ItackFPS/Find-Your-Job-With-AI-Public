"""Issue #33: education patterns must accept typographic apostrophes.

Job postings copied from websites or word processors often use ’ (U+2019) or
‘ (U+2018) instead of the ASCII ' — the extracted requirement must not depend
on which one the author's editor produced.
"""

from __future__ import annotations

import pytest

from src.requirements.education import extract_education
from src.requirements.models import Importance

_APOSTROPHES = ["'", "’", "‘"]


@pytest.mark.parametrize("apos", _APOSTROPHES)
@pytest.mark.parametrize(
    "template, label",
    [
        ("Diplôme d{a}ingénieur requis.", "Diplôme d'ingénieur"),
        ("Diplome d{a}ingenieur requis.", "Diplôme d'ingénieur"),
        ("École d{a}ingénieurs exigée.", "École d'ingénieur"),
    ],
)
def test_engineering_degree_detected_with_any_apostrophe(apos, template, label):
    edu = extract_education(template.format(a=apos))
    assert edu is not None, template.format(a=apos)
    assert (edu.level_bac_plus, edu.level_label) == (5, label)


@pytest.mark.parametrize("apos", _APOSTROPHES)
def test_titulaire_is_mandatory_with_any_apostrophe(apos):
    edu = extract_education(f"Titulaire d{apos}un BTS en informatique.")
    assert edu is not None
    assert (edu.level_bac_plus, edu.importance) == (2, Importance.MANDATORY)


def test_ascii_and_typographic_apostrophes_extract_identically():
    ascii_text = "Diplôme d'ingénieur ou titulaire d'un Master, idéalement."
    for apos in _APOSTROPHES[1:]:
        edu_ascii = extract_education(ascii_text)
        edu_typo = extract_education(ascii_text.replace("'", apos))
        assert edu_ascii is not None and edu_typo is not None
        assert (edu_typo.level_bac_plus, edu_typo.importance, edu_typo.level_label) == (
            edu_ascii.level_bac_plus,
            edu_ascii.importance,
            edu_ascii.level_label,
        )
