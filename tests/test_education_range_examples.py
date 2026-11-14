"""A diploma named in the parenthesis of a range illustrates it; it does not raise it."""

from __future__ import annotations

import pytest

from src.requirements.education import extract_education


@pytest.mark.parametrize("text", [
    "De formation Bac à Bac +2 en informatique (BTS SIO, TSSR ou équivalent).",
    "Bac à Bac+2 (BTS, DUT) exigé.",
])
def test_parenthesis_illustrating_a_range_keeps_its_minimum(text: str) -> None:
    assert extract_education(text).level_bac_plus == 0


def test_a_higher_diploma_in_the_parenthesis_still_counts() -> None:
    assert extract_education("Bac à Bac+2 (ou Master apprécié)").level_bac_plus == 5


def test_a_separate_clause_keeps_the_highest_level_rule() -> None:
    assert extract_education("Bac à Bac+2. Vous avez un BTS (obligatoire) ou Bac+3.").level_bac_plus >= 2


def test_range_without_parenthesis_is_unchanged() -> None:
    assert extract_education("Bac+3 à Bac+5 requis").level_bac_plus == 3
