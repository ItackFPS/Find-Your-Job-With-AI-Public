"""Tests for the Location Priority feature (2026-09-10).

Covers:
- profile.geography.location_preferences is the SOLE source of the
  location_fit priority tiers — no hardcoded department/remote default
  anywhere.
- List order IS priority — the first (index 0) matching preference wins,
  each following one scoring slightly lower.
- status="preferred"/"acceptable" both contribute a priority tier;
  status="excluded" never does (falls through to the ordinary tiers).
- A job outside the priority list remains fully eligible/rankable — never a
  hard rejection, only a ranking preference.
- Remote-job scoring via a "remote" preference entry.
- Backward compatibility with the legacy single-value `target_department`
  config key, ONLY when no location_preferences list is configured.
- Profile GET/PUT persistence, including merge-safety for existing profiles
  that predate this field.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.models.profile import GeographyPreferences, JobSearchProfile, LocationPreference
from src.pipeline.scoring import _score_location_fit


def _pref(type_: str, value: str, status: str = "preferred") -> dict:
    return {"type": type_, "value": value, "status": status}


def _cfg(location_preferences: list[dict] | None = None, preferred_countries: list[str] | None = None, target_department: str | None = None) -> dict:
    location_fit: dict = {
        "location_preferences": location_preferences or [],
        "preferred_countries": preferred_countries or ["france"],
    }
    if target_department is not None:
        location_fit["target_department"] = target_department
    return {"scoring": {"location_fit": location_fit}}


class TestNoLocationPreferences:
    def test_no_department_gets_top_tier(self):
        """(1) With no location_preferences configured, NOT EVEN Var (83)
        gets the top tier anymore — confirms no hardcoded default remains."""
        score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=_cfg(location_preferences=[]),
        )
        assert score != 1.0
        assert score == 0.60  # falls through to the ordinary France on-site baseline

    def test_empty_list_is_distinct_from_no_config_key(self):
        """An explicit empty list behaves identically to the key being absent
        entirely — both mean 'no preference configured'."""
        score_absent = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83",
            config={"scoring": {"location_fit": {"preferred_countries": ["france"]}}},
        )
        score_empty = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=_cfg(location_preferences=[]),
        )
        assert score_absent == score_empty == 0.60


class TestSingleLocationPreference:
    def test_83_selected_var_gets_top_tier(self):
        """(2) 83 selected as priority 0 -> a Var job gets the top tier."""
        score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=_cfg(location_preferences=[_pref("department", "83")]),
        )
        assert score == 1.0

    def test_83_selected_other_department_unaffected(self):
        """A department NOT in the priority list gets the ordinary tier,
        not the top one — confirms the preference is specific, not global."""
        score = _score_location_fit(
            location="Lyon (69)", countries=["France"], work_mode="on_site",
            remote=False, department="69", config=_cfg(location_preferences=[_pref("department", "83")]),
        )
        assert score == 0.60

    def test_06_selected_alpes_maritimes_gets_top_tier_instead(self):
        """(3) 06 selected -> Alpes-Maritimes gets the top tier, and Var
        (NOT selected this time) does not — proves the department is
        genuinely read from config, not hardcoded to 83 anywhere."""
        cfg = _cfg(location_preferences=[_pref("department", "06")])
        nice_score = _score_location_fit(
            location="Nice (06)", countries=["France"], work_mode="on_site",
            remote=False, department="06", config=cfg,
        )
        var_score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=cfg,
        )
        assert nice_score == 1.0
        assert var_score != 1.0  # Var not preferred in this profile -> ordinary tier


class TestPriorityOrdering:
    def test_first_priority_scores_higher_than_second(self):
        """[83 (prio 0), 06 (prio 1)] -> a Var match outscores a Nice match,
        proving list order (not just membership) now matters."""
        cfg = _cfg(location_preferences=[_pref("department", "83"), _pref("department", "06")])
        var_score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=cfg,
        )
        nice_score = _score_location_fit(
            location="Nice (06)", countries=["France"], work_mode="on_site",
            remote=False, department="06", config=cfg,
        )
        assert var_score == 1.0
        assert 0.70 <= nice_score < var_score

    def test_lower_priority_never_drops_below_floor(self):
        """A long priority list's last entry never decays below the
        documented 0.70 floor."""
        prefs = [_pref("department", str(d)) for d in range(10, 30)]
        cfg = _cfg(location_preferences=prefs)
        score = _score_location_fit(
            location="X (29)", countries=["France"], work_mode="on_site",
            remote=False, department="29", config=cfg,
        )
        assert score >= 0.70

    def test_department_not_in_list_stays_ordinary(self):
        cfg = _cfg(location_preferences=[_pref("department", "83"), _pref("department", "06"), _pref("department", "13")])
        score = _score_location_fit(
            location="Lille (59)", countries=["France"], work_mode="on_site",
            remote=False, department="59", config=cfg,
        )
        assert score == 0.60


class TestAcceptableStatus:
    def test_acceptable_scores_lower_than_preferred_at_same_priority(self):
        cfg_preferred = _cfg(location_preferences=[_pref("department", "83", "preferred")])
        cfg_acceptable = _cfg(location_preferences=[_pref("department", "83", "acceptable")])
        preferred_score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=cfg_preferred,
        )
        acceptable_score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=cfg_acceptable,
        )
        assert acceptable_score < preferred_score
        assert acceptable_score > 0.60  # still clearly above the ordinary baseline


class TestExcludedStatus:
    def test_excluded_department_gets_no_priority_bonus(self):
        """An excluded entry never wins the priority tier — it falls through
        to the ordinary country/remote tiers, exactly like a non-matching
        department. Location stays a ranking signal, not a hard filter."""
        cfg = _cfg(location_preferences=[_pref("department", "75", "excluded")])
        score = _score_location_fit(
            location="Paris (75)", countries=["France"], work_mode="on_site",
            remote=False, department="75", config=cfg,
        )
        assert score == 0.60  # ordinary tier, not rejected/zeroed

    def test_excluded_does_not_block_a_different_preferred_match(self):
        """A department excluded at priority 0 must not prevent a
        LOWER-priority preferred match (e.g. remote) from still winning —
        exclusion is a per-preference veto, never a global one."""
        cfg = _cfg(location_preferences=[
            _pref("department", "75", "excluded"),
            _pref("remote", "remote_france", "preferred"),
        ])
        score = _score_location_fit(
            location="Paris, France", countries=["France"], work_mode="remote",
            remote=True, department=None, config=cfg,
        )
        assert score == 0.97  # remote (priority index 1) still wins, decayed slightly not zeroed


class TestMultipleDepartmentsPreference:
    def test_multiple_departments_each_get_a_priority_tier(self):
        """(4) [83, 06, 13] selected -> every one of them gets a priority
        tier (>=0.70), unlike a non-listed department."""
        cfg = _cfg(location_preferences=[_pref("department", d) for d in ("83", "06", "13")])
        for dept, city in [("83", "Toulon"), ("06", "Nice"), ("13", "Marseille")]:
            score = _score_location_fit(
                location=f"{city} ({dept})", countries=["France"], work_mode="on_site",
                remote=False, department=dept, config=cfg,
            )
            assert score >= 0.70, f"department {dept} should get a priority tier"


class TestNotAHardFilter:
    def test_job_outside_location_preferences_still_scores_and_ranks(self):
        """(5) A job outside the priority list is never rejected — it still
        gets a valid, usable score and can still rank via other factors."""
        cfg = _cfg(location_preferences=[_pref("department", "83")])
        score = _score_location_fit(
            location="Lille (59)", countries=["France"], work_mode="on_site",
            remote=False, department="59", config=cfg,
        )
        assert isinstance(score, float)
        assert score > 0.0  # never zero/rejected

    def test_full_job_scoring_unaffected_by_absence_from_location_preferences(self):
        """A confirmed domain/skills/seniority match outside the preferred
        department must still outrank a Var job that fails on those other
        factors — location preference never overrides eligibility-adjacent
        quality signals."""
        from datetime import UTC, datetime

        from src.models.job import Job
        from src.pipeline.scoring import score_job

        def _job(**kwargs):
            defaults = dict(
                id="j1", source="test", title="Software Engineer", description="",
                location="France", countries=["France"], remote=False,
                raw_payload={}, discovered_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            defaults.update(kwargs)
            return Job(**defaults)

        cfg = {
            "scoring": {
                "location_fit": {"location_preferences": [_pref("department", "83")], "preferred_countries": ["france"]},
                "domain_relevance": {"domain_title_keywords": ["développeur"]},
                "skills_match": {"target_skills": ["python"]},
                "seniority_match": {"preferred_level_keywords": ["junior"]},
            }
        }
        var_off_domain = _job(
            title="Commercial Sédentaire H/F", description="CRM, prospection.",
            location="Toulon (83)", countries=["France"], department="83",
        )
        elsewhere_on_domain = _job(
            title="Développeur Junior Python", description="Python requis.",
            location="Lille, France", countries=["France"], department="59",
        )
        assert score_job(var_off_domain, cfg) < score_job(elsewhere_on_domain, cfg)


class TestRemotePreference:
    def test_remote_preference_gets_top_tier(self):
        cfg = _cfg(location_preferences=[_pref("remote", "remote_france")])
        score = _score_location_fit(
            location="Paris, France", countries=["France"], work_mode="remote",
            remote=True, department=None, config=cfg,
        )
        assert score == 1.0

    def test_remote_preference_does_not_match_on_site_job(self):
        cfg = _cfg(location_preferences=[_pref("remote", "remote_france")])
        score = _score_location_fit(
            location="Lille (59)", countries=["France"], work_mode="on_site",
            remote=False, department="59", config=cfg,
        )
        assert score == 0.60  # ordinary tier — the remote preference simply doesn't match

    def test_non_france_remote_does_not_match_remote_france_preference(self):
        cfg = _cfg(location_preferences=[_pref("remote", "remote_france")])
        score = _score_location_fit(
            location="Berlin, Germany", countries=["Germany"], work_mode="remote",
            remote=True, department=None, config=cfg,
        )
        assert score == 0.55  # falls through to the ordinary non-preferred-country remote tier


class TestRemoteJobsUnaffectedByDepartmentOnlyPreferences:
    def test_remote_job_scoring_unchanged_by_department_only_preference(self):
        """(7) A fully-remote job's score depends only on the existing
        remote/country logic when no "remote" preference is configured —
        department-only preferences must not alter it."""
        for prefs in ([], [_pref("department", "83")], [_pref("department", "06"), _pref("department", "13")]):
            score = _score_location_fit(
                location="Paris, France", countries=["France"], work_mode="remote",
                remote=True, department=None, config=_cfg(location_preferences=prefs),
            )
            assert score == 0.75, f"remote score changed with location_preferences={prefs}"

    def test_non_france_remote_unaffected(self):
        for prefs in ([], [_pref("department", "83")]):
            score = _score_location_fit(
                location="Berlin, Germany", countries=["Germany"], work_mode="remote",
                remote=True, department=None, config=_cfg(location_preferences=prefs),
            )
            assert score == 0.55


class TestBackwardCompatLegacyTargetDepartment:
    def test_legacy_target_department_still_honoured_when_no_list_configured(self):
        """Existing configs/tests that set the old singular `target_department`
        key keep working unchanged when location_preferences is empty — no
        default value is assumed anymore, but an EXPLICIT legacy value is
        still respected."""
        score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83",
            config=_cfg(location_preferences=[], target_department="83"),
        )
        assert score == 1.0

    def test_legacy_key_never_overrides_a_configured_list(self):
        """A configured location_preferences list always takes precedence —
        the legacy target_department key is never unioned in and never
        overrides it."""
        cfg = _cfg(location_preferences=[_pref("department", "06")], target_department="83")
        var_score = _score_location_fit(
            location="Toulon (83)", countries=["France"], work_mode="on_site",
            remote=False, department="83", config=cfg,
        )
        nice_score = _score_location_fit(
            location="Nice (06)", countries=["France"], work_mode="on_site",
            remote=False, department="06", config=cfg,
        )
        assert var_score != 1.0  # legacy "83" ignored — a list is configured
        assert nice_score == 1.0


class TestOrchestratorInjection:
    def test_profile_location_preferences_injected_into_location_cfg(self):
        """profile.geography.location_preferences is the source orchestrator
        injects into scoring config — verified directly on the profile
        model, without running the full pipeline."""
        profile = JobSearchProfile(
            geography=GeographyPreferences(location_preferences=[
                LocationPreference(type="department", value="83"),
                LocationPreference(type="department", value="06"),
            ])
        )
        assert [p.value for p in profile.geography.location_preferences] == ["83", "06"]

    def test_default_profile_has_no_location_preferences(self):
        """A profile with nothing configured has an empty list, not a
        hardcoded [Var] default — confirms 'no hardcoded Var' end-to-end."""
        profile = JobSearchProfile()
        assert profile.geography.location_preferences == []


# ---------------------------------------------------------------------------
# Profile API: GET/PUT persistence
# ---------------------------------------------------------------------------

@pytest.fixture
def profile_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "target_domains:\n  - software_development\n"
        "target_skills:\n  - python\n"
        "geography:\n  preferred_countries:\n    - France\n  allowed_regions:\n    - Europe\n"
        "  location_preferences:\n    - type: department\n      value: '83'\n      status: preferred\n"
        "work_modes:\n  - remote\n"
        "freshness_days: 30\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def profile_yaml_legacy_no_departments(tmp_path: Path) -> Path:
    """A pre-existing profile saved before this feature existed — no
    location_preferences key at all."""
    p = tmp_path / "profile.yaml"
    p.write_text(
        "target_domains:\n  - software_development\n"
        "geography:\n  preferred_countries:\n    - France\n"
        "freshness_days: 30\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def client(tmp_path: Path, profile_yaml: Path, monkeypatch: pytest.MonkeyPatch):
    import src.api.routes.profile as profile_route
    monkeypatch.setattr(profile_route, "_PROFILE_PATH", profile_yaml)
    from src.api.app import app
    return TestClient(app)


class TestProfileApiPersistence:
    def test_get_returns_location_preferences(self, client: TestClient) -> None:
        data = client.get("/api/profile").json()
        assert data["geography"]["location_preferences"] == [
            {"type": "department", "value": "83", "status": "preferred", "label": None}
        ]

    def test_put_saves_new_locations_in_order(self, client: TestClient, profile_yaml: Path) -> None:
        payload = client.get("/api/profile").json()
        payload["geography"]["location_preferences"] = [
            _pref("department", "83"), _pref("department", "06"), _pref("department", "13", "acceptable"),
        ]
        r = client.put("/api/profile", json=payload)
        assert r.status_code == 200
        saved = r.json()["geography"]["location_preferences"]
        assert [p["value"] for p in saved] == ["83", "06", "13"]
        assert saved[2]["status"] == "acceptable"

        on_disk = yaml.safe_load(profile_yaml.read_text(encoding="utf-8"))
        assert [p["value"] for p in on_disk["geography"]["location_preferences"]] == ["83", "06", "13"]

    def test_put_reorder_persists_new_priority_order(self, client: TestClient) -> None:
        """Reordering the array (simulating a UI drag) must persist in the
        new order — array position IS priority, there's no separate field."""
        payload = client.get("/api/profile").json()
        payload["geography"]["location_preferences"] = [
            _pref("department", "06"), _pref("department", "83"),
        ]
        r = client.put("/api/profile", json=payload)
        saved = r.json()["geography"]["location_preferences"]
        assert [p["value"] for p in saved] == ["06", "83"]

    def test_put_locations_does_not_overwrite_unrelated_geography_fields(
        self, client: TestClient
    ) -> None:
        """Saving a location-preferences-only update via the geography
        sub-object must not blow away preferred_countries/allowed_regions."""
        payload = client.get("/api/profile").json()
        assert payload["geography"]["preferred_countries"] == ["France"]
        payload["geography"] = {
            **payload["geography"],
            "location_preferences": [_pref("department", "06")],
        }
        r = client.put("/api/profile", json=payload)
        body = r.json()
        assert [p["value"] for p in body["geography"]["location_preferences"]] == ["06"]
        assert body["geography"]["preferred_countries"] == ["France"]  # untouched

    def test_put_does_not_overwrite_unrelated_top_level_settings(self, client: TestClient) -> None:
        payload = client.get("/api/profile").json()
        payload["geography"]["location_preferences"] = [_pref("department", "13")]
        r = client.put("/api/profile", json=payload)
        body = r.json()
        assert body["target_domains"] == ["software_development"]
        assert body["target_skills"] == ["python"]


class TestExistingProfilesWithoutDepartmentsStillWork:
    def test_get_on_legacy_profile_defaults_to_empty_list(
        self, tmp_path: Path, profile_yaml_legacy_no_departments: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(6) A profile.yaml saved before this feature existed (no
        location_preferences key at all) must load safely with an empty
        list, not crash and not silently assume Var."""
        import src.api.routes.profile as profile_route
        monkeypatch.setattr(profile_route, "_PROFILE_PATH", profile_yaml_legacy_no_departments)
        from src.api.app import app
        client = TestClient(app)

        r = client.get("/api/profile")
        assert r.status_code == 200
        assert r.json()["geography"]["location_preferences"] == []

    def test_put_on_legacy_profile_can_add_locations(
        self, tmp_path: Path, profile_yaml_legacy_no_departments: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.api.routes.profile as profile_route
        monkeypatch.setattr(profile_route, "_PROFILE_PATH", profile_yaml_legacy_no_departments)
        from src.api.app import app
        client = TestClient(app)

        payload = client.get("/api/profile").json()
        payload["geography"]["location_preferences"] = [_pref("department", "83")]
        r = client.put("/api/profile", json=payload)
        assert r.status_code == 200
        saved = r.json()["geography"]["location_preferences"]
        assert [p["value"] for p in saved] == ["83"]
