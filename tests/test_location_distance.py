"""Distance + remote-policy location matching (F4.3): filter, scoring, evidence, FT départements."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.decisions import evidence as ev
from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline import location_distance as ld
from src.pipeline.filter import build_filter_config, explain_filter_checks, filter_jobs, load_filters
from src.pipeline.scoring import (
    build_scoring_config,
    cap_personalization_at_gates,
    explain_score,
    load_scoring_config,
    score_jobs_with_breakdown,
)

ROOT = Path(__file__).resolve().parents[1]
TOULON = {"name": "Toulon", "insee": "83137", "lat": 43.1361, "lon": 5.9334, "department": "83",
          "precision": "commune"}


def _profile(**geo) -> JobSearchProfile:
    geography = {"preferred_countries": ["France"], "allowed_regions": ["Europe"], "home": TOULON,
                 "max_distance_km": 60, **geo}
    return JobSearchProfile(target_domains=["software_development"], target_skills=["python"],
                            geography=geography)


def _cfg(**geo) -> dict:
    cfg = ld.distance_config(_profile(**geo))
    assert cfg is not None
    return cfg


def _job(jid="j", **kw) -> Job:
    kw.setdefault("title", "Développeur Python")
    return Job(id=jid, source="test", **kw)


# The v1 #1-ranked false positive (plan §A2): an office job in Hong Kong with one home-working
# day a week, remote flag set, and an EMPTY countries list — which v1 read as "Remote France".
HONG_KONG = dict(title="IT Support Engineer", location="Hong Kong", countries=[], remote=True,
                 description="Office based in Kowloon. Work From Home (WFH) option available one day per week.")


# -- formula ---------------------------------------------------------------------------------------


def test_distance_score_is_smooth_and_bounded():
    assert ld.distance_score(0, 50) == 1.0
    assert ld.distance_score(50, 50) == pytest.approx(ld.EDGE_SCORE)
    assert ld.distance_score(50.001, 50) == pytest.approx(ld.EDGE_SCORE, abs=1e-3)   # continuous at R
    assert ld.distance_score(100, 50) == pytest.approx(ld.FAR_FLOOR)
    assert ld.distance_score(1000, 50) == ld.FAR_FLOOR
    values = [ld.distance_score(d, 50) for d in range(0, 120, 5)]
    assert values == sorted(values, reverse=True)                                       # never increases


def test_road_distance_is_haversine_times_1_25():
    a = ld.assess(_job(location="13 - Marseille"), _cfg())
    assert a.place is not None and a.place.name == "Marseille"
    from src.geo.places import distance_km
    straight = distance_km((TOULON["lat"], TOULON["lon"]), (a.place.lat, a.place.lon))
    assert a.road_km == pytest.approx(straight * 1.25)


# -- remote eligibility ----------------------------------------------------------------------------


def test_hong_kong_hybrid_office_job_is_not_remote_france():
    a = ld.assess(_job(**HONG_KONG), _cfg())
    assert a.remote.kind != "remote_france" and not a.remote_accepted
    assert a.score < ld.REMOTE_UNKNOWN_SCORE                        # not even "worth a look" as remote
    scoring = build_scoring_config(load_scoring_config(str(ROOT / "config" / "scoring.yaml")), _profile(),
                                   load_filters(str(ROOT / "config" / "filters.yaml")), esco_synonyms={})
    remote_fr = _job("fr", title="IT Support Engineer", location="France", remote=True, work_mode="remote",
                     description="Poste en télétravail complet, France.")
    ranked = score_jobs_with_breakdown([_job("hk", **HONG_KONG), remote_fr], scoring)
    by_id = {j.id: (s, f) for j, s, f in ranked}
    assert by_id["hk"][1]["location_fit"] < by_id["fr"][1]["location_fit"]
    assert by_id["fr"][1]["location_fit"] == pytest.approx(ld.REMOTE_ACCEPTED_SCORE)


@pytest.mark.parametrize("fields", [
    HONG_KONG,
    dict(location="Remote - US", remote=True, description="Must reside in the US."),
    dict(location="London, United Kingdom", remote=True, description="You must have the right to work in the UK."),
    dict(location="Hong Kong", remote=True),
    dict(location="", remote=True, countries=["Singapore"]),
])
def test_offers_restricted_elsewhere_are_never_remote_france(fields):
    a = ld.assess(_job(**fields), _cfg())
    assert a.remote.kind != "remote_france"
    assert a.score < ld.REMOTE_ACCEPTED_SCORE


def test_unknown_remote_is_never_a_preferred_match():
    a = ld.assess(_job(remote=True, work_mode="remote"), _cfg(remote_policy={"remote_france": True,
                                                                             "remote_eu": True,
                                                                             "remote_worldwide": True}))
    assert a.remote.kind == "unknown" and not a.remote_accepted
    assert a.score == pytest.approx(ld.REMOTE_UNKNOWN_SCORE)
    assert a.score < ld.REMOTE_ACCEPTED_SCORE


def test_remote_kinds_score_per_policy():
    eu = _job(location="Europe", remote=True, work_mode="remote", description="Fully remote - EU timezone.")
    assert ld.assess(eu, _cfg()).score < ld.REMOTE_ACCEPTED_SCORE                       # EU not ticked
    ticked = _cfg(remote_policy={"remote_france": True, "remote_eu": True})
    assert ld.assess(eu, ticked).score == pytest.approx(ld.REMOTE_ACCEPTED_SCORE)


# -- filter ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("strict,fields,excluded", [
    (False, dict(location="69 - Lyon"), False),                                # not strict: never excluded
    (True, dict(location="69 - Lyon"), True),                                  # on-site, commune, beyond 60 km
    (True, dict(location="06 - Nice"), True),                                  # ~150 km road > 60 km
    (True, dict(location="83 - Hyères"), False),                               # inside the radius
    (True, dict(location="Lyon", description="Rythme hybride : 2 jours de télétravail."), False),  # hybrid OK
    (True, dict(location="Somewhere unknown"), False),                         # unknown place: never excluded
    (True, dict(location="Auvergne-Rhône-Alpes"), False),                      # region only: distance unknown
    (True, dict(location="France", remote=True, work_mode="remote"), False),   # remote
])
def test_strict_distance_is_the_only_distance_exclusion(strict, fields, excluded):
    cfg = _cfg(strict_distance=strict)
    assert ld.strict_distance_exclusion(ld.assess(_job(**fields), cfg), cfg) is excluded


def test_hybrid_is_excluded_when_the_policy_refuses_hybrid():
    cfg = _cfg(strict_distance=True, remote_policy={"hybrid": False})
    job = _job(location="Lyon", description="Rythme hybride : 2 jours de télétravail.")
    assert ld.strict_distance_exclusion(ld.assess(job, cfg), cfg) is True


def test_department_precision_needs_a_margin_before_excluding():
    cfg = _cfg(strict_distance=True)
    near = ld.assess(_job(department="13"), cfg)            # Bouches-du-Rhône centre, ~90 km road
    assert near.place is not None and near.place.precision == "department"
    assert ld.strict_distance_exclusion(near, cfg) is False   # the département reaches into the radius
    far = ld.assess(_job(department="69"), cfg)
    assert ld.strict_distance_exclusion(far, cfg) is True


@pytest.mark.parametrize("fields,excluded", [
    (dict(location="Remote - US", remote=True, description="Must reside in the US."), True),   # explicit
    (dict(location="USA", remote=True, work_mode="remote"), False),                           # inferred only
    (dict(location="75 - Paris", remote=True, description="Must reside in the US."), False),  # placed in France
    (dict(location="France", remote=True, description="Télétravail complet"), False),
])
def test_remote_policy_excludes_only_explicit_restrictions_elsewhere(fields, excluded):
    assert ld.remote_policy_exclusion(ld.assess(_job(**fields), _cfg())) is excluded


def test_filter_jobs_uses_the_new_checks_and_v1_profiles_are_unchanged():
    engine = load_filters(str(ROOT / "config" / "filters.yaml"))
    strict = build_filter_config(_profile(strict_distance=True), engine)
    jobs = [_job("lyon", location="69 - Lyon"), _job("us", location="Remote - US", remote=True,
                                                      description="Python developer. Must reside in the US.")]
    _ok, rejected = filter_jobs(jobs, strict)
    assert rejected == {"lyon": "distance", "us": "remote_policy"}
    v1 = build_filter_config(JobSearchProfile(target_domains=["software_development"]), engine)
    assert v1["location_distance"] is None
    for job in jobs:
        codes = {o.code: o for o in explain_filter_checks(job, v1)}
        assert codes["distance"].passed and codes["remote_policy"].passed


# -- evidence --------------------------------------------------------------------------------------


def test_filter_evidence_names_the_place_and_distance():
    cfg = build_filter_config(_profile(strict_distance=True), load_filters(str(ROOT / "config" / "filters.yaml")))
    items = ev.filter_failure_items(_job(location="69 - Lyon"), cfg, "distance", all_failing=False)
    assert items[0].dimension == "location" and items[0].source.rule_id == "filter.distance"
    assert "Lyon" in items[0].observed and "km by road" in items[0].observed
    import json
    assert json.loads(items[0].expected) == {"home": "Toulon", "max_distance_km": 60, "strict_distance": True}
    assert json.loads(ev.expected_cache(cfg)["remote_policy"])["remote_accepted"] == ["remote_france", "hybrid"]
    passed = {i.source.rule_id: i for i in ev.filter_pass_items(_job(location="83 - Hyères"), cfg)}
    assert passed["filter.distance"].state == "satisfied"
    assert "filter.remote_policy" not in passed               # on-site offer: the remote policy does not apply


def test_score_evidence_has_distance_and_remote_items_without_job_text():
    scoring = build_scoring_config(load_scoring_config(str(ROOT / "config" / "scoring.yaml")), _profile(),
                                   load_filters(str(ROOT / "config" / "filters.yaml")), esco_synonyms={})
    items = {i.source.rule_id: i for i in ev.score_items(explain_score(_job(**HONG_KONG), scoring))}
    assert items["score.location.distance"].state == "unknown"
    remote = items["score.location.remote_eligibility"]
    assert remote.dimension == "location" and remote.observed.startswith("hybrid")
    assert "Kowloon" not in remote.observed                   # the posting quote is withheld
    near = {i.source.rule_id: i for i in ev.score_items(explain_score(_job(location="83 - Hyères"), scoring))}
    assert near["score.location.distance"].state == "satisfied"
    assert near["score.top_location_bonus"].message.startswith("Close to home")


def test_proximity_bonus_decays_to_zero_at_the_edge():
    cfg = _cfg(max_distance_km=50)
    home = ld.assess(_job(location="83 - Toulon"), cfg)
    beyond = ld.assess(_job(location="13 - Marseille"), cfg)      # ~59 km road > 50 km
    assert home.proximity > 0.9 and beyond.proximity == 0.0


def test_personalization_cap_is_unchanged_by_the_location_factor():
    """No new scoring gate: a job whose only weakness is distance keeps its personalization boost."""
    job = _job("far", location="69 - Lyon")
    factors = {"far": {"location_fit": ld.FAR_FLOOR, "domain_relevance": 1.0, "skills_match": 1.0,
                       "seniority_match": 1.0, "experience_fit": 0.5}}
    out = cap_personalization_at_gates([(job, 0.5)], [(job, 0.6)], factors, {})
    assert out == [(job, 0.6)]


# -- France Travail départements -------------------------------------------------------------------


def test_ft_departments_cover_home_and_radius():
    codes = ld.ft_departments(_cfg())
    assert "83" in codes and "13" in codes and "06" not in codes and "69" not in codes
    wide = ld.ft_departments(_cfg(max_distance_km=150))
    assert {"04", "06", "13", "83", "84"} <= set(wide)
    centre_only = ld.ft_departments(_cfg(max_distance_km=1))
    assert centre_only == ["83"]                              # the home département is always read
