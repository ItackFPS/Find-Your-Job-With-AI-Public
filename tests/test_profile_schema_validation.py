"""Phase NEXT / I7 regression tests: config/profile.yaml (and the PUT
/api/profile round-trip) must reject unknown/typo'd keys instead of
silently dropping them, while still accepting every valid shape that
existed before this change — including the real, committed
config/profile.yaml.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.models.profile import (
    EscoEntry,
    ExperiencePreferences,
    GeographyPreferences,
    JobSearchProfile,
    LocationPreference,
    MatchingPreferences,
)


class TestRealConfigStillLoads:
    def test_real_profile_yaml_loads_without_error(self) -> None:
        # Deliberately the tracked default, not the frozen test profile: the
        # shipped config/profile.yaml must always validate.
        with open("config/profile.yaml", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        JobSearchProfile(**data)  # must not raise

    def test_frozen_test_profile_loads_without_error(self) -> None:
        from tests.fixtures.profiles import TEST_PROFILE_PATH

        with open(TEST_PROFILE_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        JobSearchProfile(**data)  # must not raise

    def test_minimal_profile_with_defaults_only(self) -> None:
        profile = JobSearchProfile()
        assert profile.target_domains == []
        assert profile.freshness_days == 30


class TestTypoDetection:
    def test_top_level_typo_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JobSearchProfile(target_skil=["python"])

    def test_geography_typo_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GeographyPreferences(preferred_countrie=["France"])

    def test_experience_typo_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ExperiencePreferences(max_year=3)

    def test_matching_typo_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MatchingPreferences(principal_skil_gate=True)

    def test_location_preference_typo_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LocationPreference(tpye="department", value="83")

    def test_esco_entry_typo_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EscoEntry(labl="Python")

    def test_nested_typo_inside_full_profile_is_rejected(self) -> None:
        """A typo several levels deep must still be caught, not just at the
        top level — the whole model tree is strict, not just the outer one."""
        with pytest.raises(ValidationError):
            JobSearchProfile(matching={"principal_skil_gate": True})


class TestValidConfigurationStillAccepted:
    def test_all_documented_top_level_fields_accepted(self) -> None:
        JobSearchProfile(
            target_domains=["software_development"],
            focus_domain=True,
            target_skills=["python"],
            target_skills_esco=[{"label": "Python"}],
            geography={"preferred_countries": ["France"], "allowed_regions": [], "location_preferences": []},
            work_modes=["remote"],
            contract_types=["cdi"],
            experience={"preferred_levels": ["junior"], "max_years": 3, "excluded_seniority": []},
            languages=["French"],
            freshness_days=30,
            max_education="bac+3",
            matching={
                "education_hard_constraint": True,
                "education_preferred_penalty": 0.2,
                "principal_skill_gate": True,
                "mandatory_central_skill_gate": True,
                "llm_extraction_enabled": False,
                "llm_eligibility_enabled": False,
                "llm_eligibility_reject_confidence": 0.75,
            },
            looking_for="something",
        )  # must not raise

    def test_missing_optional_fields_still_default(self) -> None:
        """Backward compatibility: an older profile.yaml saved before a field
        (e.g. target_skills_esco) existed must still load fine."""
        profile = JobSearchProfile(target_domains=["software_development"])
        assert profile.target_skills_esco == []
        assert profile.matching.principal_skill_gate is True


@pytest.fixture
def profile_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "target_domains:\n  - software_development\n"
        "target_skills:\n  - python\n"
        "freshness_days: 30\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def client(tmp_path: Path, profile_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient with profile route pointing at a temp profile.yaml."""
    import src.api.routes.profile as profile_route

    monkeypatch.setattr(profile_route, "_PROFILE_PATH", profile_yaml)

    from src.api.app import app
    return TestClient(app)


class TestApiRejectsTypoInOnDiskProfile:
    def test_put_surfaces_clear_422_for_stale_typo_key_already_on_disk(
        self, client: TestClient, profile_yaml: Path
    ) -> None:
        """If the on-disk file already has a typo'd key (e.g. from manual
        editing outside the UI), the next PUT must surface a clear 422
        instead of silently merging around it forever."""
        profile_yaml.write_text(
            "target_domains:\n  - software_development\n"
            "target_skil:\n  - python\n"  # typo, pre-existing on disk
            "freshness_days: 30\n",
            encoding="utf-8",
        )
        r = client.put("/api/profile", json={"freshness_days": 14})
        assert r.status_code == 422

    def test_put_with_only_valid_fields_still_succeeds(self, client: TestClient) -> None:
        r = client.put("/api/profile", json={"freshness_days": 14})
        assert r.status_code == 200
        assert r.json()["freshness_days"] == 14
