"""Typed remote eligibility (src/geo/remote.py, update plan F4.2).

Texts are written for the tests, modelled on phrasing seen in collected offers.
"""

from __future__ import annotations

import pytest

from src.geo.remote import (
    EXPLICIT,
    HYBRID,
    ONSITE,
    REMOTE_EU,
    REMOTE_FRANCE,
    REMOTE_RESTRICTED,
    REMOTE_WORLDWIDE,
    UNKNOWN,
    classify_remote,
)


def test_one_home_day_abroad_is_hybrid_not_remote_france() -> None:
    # The v1 #1 false positive: a Hong Kong office job with one home-working day.
    r = classify_remote(
        title="Web Developer (Fresh graduate welcome, WFH policy)",
        location="Remote", remote=True,
        description=("Headquarters: Hong Kong, Hong Kong SAR, Hong Kong URL: http://example.com "
                     "Benefits: 5-day work week. Work From Home (WFH) option available one day "
                     "per week."))
    assert r.kind == HYBRID and r.hybrid_days == 4
    assert r.kind != REMOTE_FRANCE and r.open_from_france is None


@pytest.mark.parametrize("description,area", [
    ("Candidates must reside in the United States.", "US"),
    ("You must be located in Canada to apply.", "CA"),
    ("Applicants must be authorized to work in the USA without sponsorship.", "US"),
    ("This role is open to candidates based in the UK.", "UK"),
    ("Remote (US only). Great benefits.", "US"),
    ("We hire US-based candidates only.", "US"),
    ("You will work within US time zones.", "US"),
    ("You must live in a state where we are registered.", "US"),
])
def test_explicit_restriction_is_not_open_from_france(description: str, area: str) -> None:
    r = classify_remote(title="Engineer", location="Remote", remote=True, description=description)
    assert (r.kind, r.area, r.strength) == (REMOTE_RESTRICTED, area, EXPLICIT)
    assert r.open_from_france is False and r.evidence


def test_restriction_beats_worldwide_boilerplate() -> None:
    r = classify_remote(location="Remote", remote=True, description=(
        "We serve customers worldwide and you can work from anywhere. "
        "Candidates must reside in the United States."))
    assert r.kind == REMOTE_RESTRICTED and r.area == "US"


@pytest.mark.parametrize("description", [
    "For US based applicants, the salary range is $90,000 - $120,000.",
    "Our US based employees enjoy great benefits.",
    "We work in a hybrid Microsoft environment with Azure AD.",
    "Our company is the leader in hybrid cloud infrastructure.",
    "Headquarters: Remote (United States)",
])
def test_lookalike_phrases_are_not_restrictions_or_hybrid(description: str) -> None:
    r = classify_remote(location="Anywhere", remote=True, description=description)
    assert r.kind == REMOTE_WORLDWIDE


@pytest.mark.parametrize("location,kind,area", [
    ("Anywhere", REMOTE_WORLDWIDE, None),
    ("Worldwide", REMOTE_WORLDWIDE, None),
    ("Europe", REMOTE_EU, None),
    ("EMEA", REMOTE_EU, None),
    ("USA", REMOTE_RESTRICTED, "US"),
    ("Texas, USA", REMOTE_RESTRICTED, "US"),
    ("Canada, UK, USA", REMOTE_RESTRICTED, "CA/UK/US"),
    ("APAC, Middle East", REMOTE_RESTRICTED, "APAC/ME"),
    ("Slovakia", REMOTE_RESTRICTED, "SK"),
    ("France", REMOTE_FRANCE, None),
    ("Remote - France", REMOTE_FRANCE, None),
    ("Lyon, Auvergne-Rhône-Alpes, France", REMOTE_FRANCE, None),
])
def test_structured_location_of_a_remote_offer(location: str, kind: str, area: str | None) -> None:
    r = classify_remote(title="Engineer", location=location, remote=True, description="Join us.")
    assert (r.kind, r.area) == (kind, area)


@pytest.mark.parametrize("location", ["Regina,", "Allan,", "Macau,"])
def test_foreign_town_named_like_a_french_commune_is_not_france(location: str) -> None:
    # Remote boards list foreign towns that share a name with a small French commune.
    r = classify_remote(title="Team Lead", location=location, remote=True, description="Join us.")
    assert r.kind != REMOTE_FRANCE


def test_remote_without_any_area_is_unknown_not_france() -> None:
    r = classify_remote(title="Engineer", location="Remote", remote=True, description="Join us.")
    assert r.kind == UNKNOWN and r.open_from_france is None


def test_work_from_anywhere_against_a_foreign_location_field_is_unknown() -> None:
    r = classify_remote(location="USA", remote=True,
                        description="We are a distributed team; work from anywhere.")
    assert r.kind == UNKNOWN


def test_work_from_anywhere_with_a_europe_field_stays_eu() -> None:
    r = classify_remote(location="Europe", remote=True, description="You can work from anywhere.")
    assert r.kind == REMOTE_EU


@pytest.mark.parametrize("description,days", [
    ("2 jours de télétravail / semaine.", 3),
    ("Accord télétravail : 1 jour de télétravail par semaine.", 4),
    ("Télétravail partiel possible.", None),
    ("Hybrid working: three days a week in the office.", 3),
    ("Poste en hybride, avec deux jours sur site.", None),
])
def test_hybrid_wording(description: str, days: int | None) -> None:
    r = classify_remote(title="Développeur", location="69 - Lyon", countries=["France"],
                        remote=False, description=description)
    assert r.kind == HYBRID and r.hybrid_days == days


def test_french_full_remote() -> None:
    r = classify_remote(title="Développeur", location="Nantes, Pays de la Loire, FR",
                        countries=["France"], remote=True,
                        description="Poste en télétravail complet depuis la France.")
    assert r.kind == REMOTE_FRANCE and r.strength == EXPLICIT


def test_onsite_french_offer() -> None:
    r = classify_remote(title="Technicien", location="83 - Toulon", countries=["France"],
                        remote=False, description="Poste sur site.")
    assert r.kind == ONSITE


def test_nothing_known_is_unknown() -> None:
    assert classify_remote().kind == UNKNOWN
