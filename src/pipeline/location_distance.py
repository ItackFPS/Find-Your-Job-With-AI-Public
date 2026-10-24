"""Distance + remote-policy location matching (update plan §C2, item F4.3).

Active when the profile has a ``geography.home`` (see ``distance_config``). One
assessment per job, shared by the hard filter, scoring, the decision journal and
explanations, so they can never disagree:

* the offer's place: ``src.geo.places.resolve_place`` on its location text, falling
  back to the canonical ``department`` column (département centre, approximate);
* its remote eligibility: ``src.geo.remote.classify_remote`` (typed, with evidence);
* the road distance from home, estimated as great-circle × ``ROAD_FACTOR`` (1.25).

Hard filter (the ONLY location exclusions this model adds; ADR 0004's rule applies:
uncertainty never rejects):

* ``distance``: on-site work — an offer with no remote signal at all counts as on-site,
  as in v1 — (hybrid too when the policy does not accept hybrid),
  beyond ``max_distance_km``, and only when the user ticked ``strict_distance``.
  The place must be a resolved commune, or a département whose nearest plausible
  point (centre − ``DEPARTMENT_MARGIN_KM``) is still beyond the radius. Unknown
  places are never excluded.
* ``remote_policy``: an offer explicitly restricted to residents of another area
  ("must reside in the US") that is not itself placed in France.

Location factor (0–1), with R = ``max_distance_km`` and d = estimated road km:

* place known:  d ≤ R → ``1 − 0.35·(d/R)²`` (1.0 at home, 0.65 at the edge, flat
  near home and steeper towards the edge); d > R → ``max(0.30, 0.65 − 0.35·(d−R)/R)``
  (continuous at R, 0.30 from 2R on);
* place unknown: 0.50 when the offer is only known to be in France (country or region
  level), else 0.40 (the v1 "unknown" tier: conservative, not neutral);
* remote: a kind the policy accepts scores ``max(place score, 0.90)``; remote with
  ``unknown`` area scores ``max(place score, 0.55)`` ("worth a look", never a
  preferred match); a kind the policy does not tick, or an inferred restriction,
  gets the place score only;
* home-proximity bonus (replaces ADR 0005's top-location bonus in this model):
  ``top_location_bonus × (1 − d/R)`` for on-site/hybrid offers inside the radius,
  added before the dealbreaker gates exactly like the v1 bonus.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from src.geo.places import COMMUNE, COUNTRY, DEPARTMENT, REGION, Place, department_place, distance_km, resolve_place
from src.geo.remote import (
    EXPLICIT,
    HYBRID,
    ONSITE,
    REMOTE_EU,
    REMOTE_FRANCE,
    REMOTE_RESTRICTED,
    REMOTE_WORLDWIDE,
    UNKNOWN,
    RemoteEligibility,
    classify_remote,
)

ROAD_FACTOR = 1.25
DEPARTMENT_MARGIN_KM = 60.0
EDGE_SCORE = 0.65
FAR_FLOOR = 0.30
FRANCE_ONLY_SCORE = 0.50
UNKNOWN_PLACE_SCORE = 0.40
REMOTE_ACCEPTED_SCORE = 0.90
REMOTE_UNKNOWN_SCORE = 0.55
RULE_VERSION = "location-distance.v1"

_POLICY_KEY = {REMOTE_FRANCE: "remote_france", REMOTE_EU: "remote_eu", REMOTE_WORLDWIDE: "remote_worldwide"}


def distance_config(profile: Any) -> dict[str, Any] | None:
    """The distance model's settings for filter/scoring configs, or None (v1 model)."""
    geo = profile.geography
    if geo.home is None:
        return None
    return {
        "home": {"name": geo.home.name, "insee": geo.home.insee, "lat": geo.home.lat, "lon": geo.home.lon,
                 "department": geo.home.department, "precision": geo.home.precision},
        "max_distance_km": geo.max_distance_km,
        "strict_distance": geo.strict_distance,
        "remote_policy": geo.remote_policy.model_dump(),
    }


@dataclass(frozen=True)
class LocationAssessment:
    place: Place | None
    road_km: float | None            # None when the place has no usable coordinates
    within_radius: bool | None       # None when the distance is unknown
    remote: RemoteEligibility
    remote_accepted: bool            # the policy ticks this remote kind
    score: float
    proximity: float                 # 1 − d/R inside the radius, else 0 (drives the bonus)


def distance_score(road_km: float, radius_km: float) -> float:
    ratio = road_km / radius_km
    if ratio <= 1.0:
        return 1.0 - (1.0 - EDGE_SCORE) * ratio * ratio
    return max(FAR_FLOOR, EDGE_SCORE - (EDGE_SCORE - FAR_FLOOR) * (ratio - 1.0))


@lru_cache(maxsize=8192)
def _classify(title: str | None, description: str | None, location: str | None,
              countries: tuple[str, ...], remote: bool | None, work_mode: str | None) -> RemoteEligibility:
    return classify_remote(title=title, description=description, location=location,
                           countries=list(countries), remote=remote, work_mode=work_mode)


@lru_cache(maxsize=8192)
def _place(location: str | None, countries: tuple[str, ...], department: str | None) -> Place | None:
    place = resolve_place(location, list(countries) or None)
    if (place is None or place.precision in (REGION, COUNTRY)) and department:
        place = department_place(str(department)) or place
    return place


def _is_unknown_remote(r: RemoteEligibility) -> bool:
    """Remote with no usable area ("remote, no stated area", or openness wording the
    location field contradicts). A plain ``unknown`` with no evidence is no remote signal."""
    return r.kind == UNKNOWN and r.evidence is not None


def assess(job: Any, cfg: dict[str, Any]) -> LocationAssessment:
    """Assess one job against the distance config (``distance_config``). Never raises on job data."""
    countries = tuple(job.countries or ())
    place = _place(job.location, countries, job.department)
    remote = _classify(job.title, job.description, job.location, countries, job.remote, job.work_mode)
    radius = float(cfg["max_distance_km"])
    home = cfg["home"]

    road_km: float | None = None
    if place is not None and place.precision in (COMMUNE, DEPARTMENT) and place.lat is not None:
        road_km = distance_km((home["lat"], home["lon"]), (place.lat, place.lon)) * ROAD_FACTOR  # type: ignore[arg-type]

    if road_km is not None:
        place_score = distance_score(road_km, radius)
    elif place is not None:  # region or country: only known to be in France
        place_score = FRANCE_ONLY_SCORE
    else:
        place_score = UNKNOWN_PLACE_SCORE

    policy = cfg.get("remote_policy") or {}
    accepted = bool(policy.get(_POLICY_KEY.get(remote.kind, ""), False))
    if accepted:
        score = max(place_score, REMOTE_ACCEPTED_SCORE)
    elif _is_unknown_remote(remote):
        score = max(place_score, REMOTE_UNKNOWN_SCORE)
    else:
        score = place_score

    within = None if road_km is None else road_km <= radius
    proximity = 0.0
    if road_km is not None and within and remote.kind not in _POLICY_KEY:
        proximity = max(0.0, 1.0 - road_km / radius)
    return LocationAssessment(place, road_km, within, remote, accepted, round(score, 6), proximity)


def strict_distance_exclusion(a: LocationAssessment, cfg: dict[str, Any]) -> bool:
    """True only for on-site work beyond the radius with ``strict_distance`` ticked."""
    if not cfg.get("strict_distance") or a.road_km is None or a.place is None:
        return False
    hybrid_ok = bool((cfg.get("remote_policy") or {}).get("hybrid", True))
    if a.remote.kind == HYBRID and hybrid_ok:
        return False
    no_remote_signal = a.remote.kind == UNKNOWN and a.remote.evidence is None
    if a.remote.kind not in (ONSITE, HYBRID) and not no_remote_signal:
        return False
    radius = float(cfg["max_distance_km"])
    if a.place.precision == COMMUNE:
        return a.road_km > radius
    return a.road_km - DEPARTMENT_MARGIN_KM > radius


def remote_policy_exclusion(a: LocationAssessment) -> bool:
    """True only for an EXPLICIT restriction to another area, on an offer not placed in France."""
    return a.remote.kind == REMOTE_RESTRICTED and a.remote.strength == EXPLICIT and a.place is None


_STRUCTURED_EVIDENCE = ("location:", "countries:", "work_mode=", "remote, no stated area")


def describe(a: LocationAssessment, cfg: dict[str, Any]) -> dict[str, Any]:
    """Structured facts for the decision journal. A remote-evidence quote taken from the
    posting text is withheld (journal policy: observed/expected never carry job text)."""
    ev = a.remote.evidence
    quoted = ev if ev is None or ev.startswith(_STRUCTURED_EVIDENCE) else "(quoted from the posting)"
    return {
        "place": None if a.place is None else {"name": a.place.name, "precision": a.place.precision,
                                               "department": a.place.department, "insee": a.place.insee},
        "road_km": None if a.road_km is None else round(a.road_km, 1),
        "within_radius": a.within_radius,
        "max_distance_km": cfg["max_distance_km"],
        "home": cfg["home"]["name"],
        "remote_kind": a.remote.kind,
        "remote_area": a.remote.area,
        "remote_strength": a.remote.strength,
        "remote_evidence": quoted,
        "remote_accepted": a.remote_accepted,
        "score": a.score,
        "rule_version": RULE_VERSION,
    }


def ft_departments(cfg: dict[str, Any]) -> list[str]:
    """Départements the France Travail web collector should read: those whose centre or any
    commune lies within the radius (road estimate), plus the home département."""
    from src.geo.places import departments_within

    home = cfg["home"]
    codes = set(departments_within((home["lat"], home["lon"]), float(cfg["max_distance_km"]), ROAD_FACTOR))
    if home.get("department"):
        codes.add(str(home["department"]))
    return sorted(codes)
