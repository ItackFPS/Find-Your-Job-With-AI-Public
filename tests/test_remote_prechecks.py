"""The word prechecks of the remote patterns never skip a pattern that would match (Phase B B10, P-032)."""

from __future__ import annotations

import pytest

from src.geo import remote as R

# One phrase per pattern, in pattern order, that the pattern matches.
RESTRICTION_PHRASES = [
    "Candidates must be located in the United States.",
    "You must be authorized to work in the UK.",
    "This role is US only.",
    "We are only hiring in Canada.",
    "UK-based candidates only.",
    "Remote - Germany",
    "Work within US time zones.",
    "Overlap with EST business hours.",
]
HYBRID_PHRASES = [
    "Hybrid work from our Paris office.",
    "Poste en travail hybride.",
    "We work in a hybrid way.",
    "Organisation en hybride, avec deux jours au bureau.",
    "Remote / hybrid possible.",
    "Hybrid (2 days on site).",
    "Télétravail partiel possible.",
    "Deux jours de télétravail par semaine.",
    "Two days a week in the office.",
    "WFH option available one day per week.",
]


def test_one_word_per_pattern() -> None:
    assert len(R._RESTRICTION_NEEDS) == len(R._RESTRICTION_RES) == len(RESTRICTION_PHRASES)
    assert len(R._HYBRID_NEEDS) == len(R._HYBRID_RES) == len(HYBRID_PHRASES)


@pytest.mark.parametrize("i", range(len(RESTRICTION_PHRASES)))
def test_each_restriction_phrase_holds_its_word(i: int) -> None:
    for text in (RESTRICTION_PHRASES[i], RESTRICTION_PHRASES[i].upper()):
        assert R._RESTRICTION_RES[i].search(text), text
        assert R._RESTRICTION_NEEDS[i].search(text), text


@pytest.mark.parametrize("i", range(len(HYBRID_PHRASES)))
def test_each_hybrid_phrase_holds_its_word(i: int) -> None:
    for text in (HYBRID_PHRASES[i], HYBRID_PHRASES[i].upper()):
        assert R._HYBRID_RES[i].search(text), text
        assert R._HYBRID_NEEDS[i].search(text), text


def test_classification_is_unchanged_on_the_phrases() -> None:
    assert R.classify_remote(description=RESTRICTION_PHRASES[0], remote=True).kind == R.REMOTE_RESTRICTED
    assert R.classify_remote(description=HYBRID_PHRASES[7]).hybrid_days == 3
