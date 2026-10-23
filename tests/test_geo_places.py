"""Offline place resolution and distances (src/geo/places.py, update plan F4.1)."""

from __future__ import annotations

import pytest

from src.geo.places import (
    COMMUNE,
    COUNTRY,
    DEPARTMENT,
    REGION,
    distance_km,
    normalize,
    resolve_place,
)


@pytest.mark.parametrize("text,name,dept", [
    # France Travail
    ("13 - Aix-en-Provence", "Aix-en-Provence", "13"),
    ("75 - PARIS", "Paris", "75"),
    ("83 - TOULON", "Toulon", "83"),
    # LinkedIn / HelloWork
    ("Lyon, Auvergne-Rhône-Alpes, France", "Lyon", "69"),
    ("Valbonne, Provence-Alpes-Côte d'Azur, France", "Valbonne", "06"),
    ("Rennes, Bretagne, FR", "Rennes", "35"),
    # Indeed
    ("29200 Brest", "Brest", "29"),
    ("Bagneux (92)", "Bagneux", "92"),
    ("75002 Paris", "Paris", "75"),
    # Arrondissements, plain names
    ("Paris 01", "Paris", "75"),
    ("Lyon 3e", "Lyon", "69"),
    ("Marseille 8ème", "Marseille", "13"),
    ("Voisins-le-Bretonneux", "Voisins-le-Bretonneux", "78"),
    ("St-Étienne", "Saint-Étienne", "42"),
])
def test_resolves_real_formats_to_a_commune(text: str, name: str, dept: str) -> None:
    place = resolve_place(text)
    assert place is not None and place.precision == COMMUNE
    assert (place.name, place.department) == (name, dept)
    assert place.lat is not None and place.lon is not None


@pytest.mark.parametrize("text,dept", [
    ("Saint-Denis (974)", "974"),
    ("97400 Saint-Denis", "974"),
    ("Saint-Denis, La Réunion", "974"),
    ("Saint-Denis (93)", "93"),
])
def test_homonyms_follow_explicit_evidence(text: str, dept: str) -> None:
    place = resolve_place(text)
    assert place is not None and place.department == dept and place.confidence == "exact"


def test_stated_department_never_resolves_to_another_departments_commune() -> None:
    # "Port" exists only in the Ain (01); La Réunion's town is "Le Port".
    place = resolve_place("974 - Port")
    assert place is not None and place.precision == DEPARTMENT and place.department == "974"


def test_english_region_names_and_canton_prefix() -> None:
    place = resolve_place("Canton of Mont-Saint-Aignan, Normandy, France")
    assert place is not None and (place.name, place.department) == ("Mont-Saint-Aignan", "76")


def test_cedex_postcode_falls_back_to_the_town_name() -> None:
    place = resolve_place("31057 Toulouse")
    assert place is not None and (place.name, place.precision) == ("Toulouse", COMMUNE)


def test_ambiguous_homonym_is_unknown_not_guessed() -> None:
    # Saint-Denis (93) and Saint-Denis (974) have similar populations.
    assert resolve_place("Saint-Denis") is None


def test_dominant_homonym_is_reported_as_likely() -> None:
    place = resolve_place("Bagneux")  # 92 (44k) against five villages
    assert place is not None and place.department == "92" and place.confidence == "likely"


def test_department_and_region_fall_back_to_their_centre() -> None:
    var = resolve_place("Var")
    assert var is not None and var.precision == DEPARTMENT and var.department == "83"
    idf = resolve_place("Île-de-France")
    assert idf is not None and idf.precision == REGION and idf.region == "11"
    unknown_town = resolve_place("Nowhereville (83)")
    assert unknown_town is not None and unknown_town.precision == DEPARTMENT


def test_country_only_has_no_coordinates() -> None:
    place = resolve_place("France")
    assert place is not None and place.precision == COUNTRY and place.lat is None


@pytest.mark.parametrize("text,countries", [
    ("London, United Kingdom", None),
    ("Valencia, Spain", None),
    ("Hong Kong", None),
    ("Remote", None),
    ("", None),
    (None, None),
    ("Paris", ["United States"]),  # Paris, Texas
])
def test_non_french_or_empty_is_unknown(text: str | None, countries: list[str] | None) -> None:
    assert resolve_place(text, countries) is None


def test_distance_km_is_great_circle() -> None:
    paris = resolve_place("Paris")
    marseille = resolve_place("Marseille")
    assert paris and marseille
    d = distance_km((paris.lat, paris.lon), (marseille.lat, marseille.lon))  # type: ignore[arg-type]
    assert 655 < d < 665  # ~660 km between the two centroids
    assert distance_km((43.1, 5.9), (43.1, 5.9)) == 0.0


def test_normalize() -> None:
    assert normalize("Saint-Étienne-du-Rouvray") == "saint etienne du rouvray"
    assert normalize("St-Tropez") == normalize("Saint Tropez")
    assert normalize("L'Haÿ-les-Roses") == "l hay les roses"
