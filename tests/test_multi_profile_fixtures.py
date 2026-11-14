"""Phase B B3-P: the four evaluation profiles P1-P4 exist, validate, and differ where they must."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
from src.pipeline.normalize import normalize_job
from tests.fixtures.profiles import MULTI_PROFILES, TEST_PROFILE_PATH

ROOT = Path(__file__).resolve().parents[1]
IT_JOBS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50" / "jobs.jsonl"
IT_DOMAINS = {"software_development", "web_development", "cybersecurity", "data", "devops", "cloud",
              "it_support", "systems", "networking", "qa_testing", "automation"}


def test_four_distinct_profiles_that_are_not_the_personal_one() -> None:
    assert set(MULTI_PROFILES) == {"P1", "P2", "P3", "P4"}
    paths = {Path(p).resolve() for p in MULTI_PROFILES.values()}
    assert len(paths) == 4 and Path(TEST_PROFILE_PATH).resolve() not in paths
    for p in paths:
        assert p.is_file() and p.name != "profile.local.yaml"
        assert "Not a real person" in p.read_text(encoding="utf-8") or "not a real person" in p.read_text(encoding="utf-8")


@pytest.mark.parametrize("key", sorted(MULTI_PROFILES))
def test_each_profile_validates_and_uses_the_distance_model(key: str) -> None:
    profile = load_profile(MULTI_PROFILES[key])
    assert profile.target_domains
    assert profile.geography.home is not None


def test_roles() -> None:
    p1, p2, p3, p4 = (load_profile(MULTI_PROFILES[k]) for k in ("P1", "P2", "P3", "P4"))
    assert set(p1.target_domains) <= IT_DOMAINS and p1.experience.max_years == 2
    assert {"systems", "networking"} <= set(p1.target_domains)
    assert set(p2.target_domains) <= IT_DOMAINS and not set(p2.target_domains) & set(p1.target_domains)
    assert not set(p3.target_domains) & IT_DOMAINS                     # the non-IT profile
    assert len(p4.target_domains) >= 4 and not set(p4.target_domains) & IT_DOMAINS


def _accepted(key: str) -> int:
    jobs = [normalize_job("s", json.loads(x)) for x in IT_JOBS.read_text(encoding="utf-8").splitlines() if x.strip()]
    cfg = build_filter_config(load_profile(MULTI_PROFILES[key]), load_filters(str(ROOT / "config" / "filters.yaml")))
    kept, _ = filter_jobs(jobs, cfg)
    return len(kept)


def test_an_it_dataset_is_not_accepted_alike_by_the_it_and_non_it_profiles() -> None:
    """Diagnostic shape: the IT synthetic offers pass P1's hard filter more often than P3's."""
    assert _accepted("P1") > _accepted("P3")
