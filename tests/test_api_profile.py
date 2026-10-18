"""Tests for GET/PUT /api/profile."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


@pytest.fixture
def profile_yaml(tmp_path: Path) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "target_domains:\n  - software_development\n"
        "target_skills:\n  - python\n"
        "geography:\n  preferred_countries:\n    - France\n  allowed_regions:\n    - Europe\n"
        "work_modes:\n  - remote\n"
        "contract_types:\n  - cdi\n"
        "experience:\n  preferred_levels:\n    - junior\n  max_years: 3\n  excluded_seniority:\n    - senior\n"
        "languages:\n  - French\n  - English\n"
        "freshness_days: 30\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def client(tmp_path: Path, profile_yaml: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient with profile route pointing at a temp profile.yaml."""
    import src.api.routes.profile as profile_route

    monkeypatch.setattr(profile_route, "_PROFILE_PATH", profile_yaml)

    from src.api.app import app
    return TestClient(app)


class TestGetProfile:
    def test_returns_200(self, client: TestClient) -> None:
        r = client.get("/api/profile")
        assert r.status_code == 200

    def test_contains_target_domains(self, client: TestClient) -> None:
        data = client.get("/api/profile").json()
        assert "software_development" in data["target_domains"]

    def test_contains_target_skills(self, client: TestClient) -> None:
        data = client.get("/api/profile").json()
        assert "python" in data["target_skills"]

    def test_contains_geography(self, client: TestClient) -> None:
        data = client.get("/api/profile").json()
        assert "France" in data["geography"]["preferred_countries"]

    def test_contains_freshness_days(self, client: TestClient) -> None:
        data = client.get("/api/profile").json()
        assert data["freshness_days"] == 30


class TestPutProfile:
    def _valid_payload(self) -> dict:
        return {
            "target_domains": ["software_development", "web_development"],
            "target_skills": ["python", "typescript"],
            "geography": {
                "preferred_countries": ["France", "Germany"],
                "allowed_regions": ["Europe"],
            },
            "work_modes": ["remote", "hybrid"],
            "contract_types": ["cdi"],
            "experience": {
                "preferred_levels": ["junior"],
                "max_years": 3,
                "excluded_seniority": ["senior", "director"],
            },
            "languages": ["French", "English"],
            "freshness_days": 14,
        }

    def test_valid_update_returns_200(self, client: TestClient) -> None:
        r = client.put("/api/profile", json=self._valid_payload())
        assert r.status_code == 200

    def test_valid_update_returns_profile(self, client: TestClient) -> None:
        payload = self._valid_payload()
        r = client.put("/api/profile", json=payload)
        data = r.json()
        assert data["freshness_days"] == 14
        assert "Germany" in data["geography"]["preferred_countries"]

    def test_roundtrip_get_after_put(self, client: TestClient) -> None:
        payload = self._valid_payload()
        client.put("/api/profile", json=payload)
        data = client.get("/api/profile").json()
        assert data["freshness_days"] == 14
        assert "typescript" in data["target_skills"]

    def test_invalid_freshness_days_rejected(self, client: TestClient) -> None:
        payload = self._valid_payload()
        payload["freshness_days"] = "not-a-number"
        r = client.put("/api/profile", json=payload)
        assert r.status_code == 422

    def test_empty_lists_accepted(self, client: TestClient) -> None:
        payload = self._valid_payload()
        payload["target_skills"] = []
        r = client.put("/api/profile", json=payload)
        assert r.status_code == 200
        assert r.json()["target_skills"] == []

    def test_looking_for_roundtrips(self, client: TestClient) -> None:
        payload = self._valid_payload()
        payload["looking_for"] = "Remote backend role, Python and cloud infra."
        r = client.put("/api/profile", json=payload)
        assert r.status_code == 200
        assert r.json()["looking_for"] == "Remote backend role, Python and cloud infra."
        assert client.get("/api/profile").json()["looking_for"] == "Remote backend role, Python and cloud infra."

    def test_looking_for_defaults_to_null(self, client: TestClient) -> None:
        data = client.get("/api/profile").json()
        assert data["looking_for"] is None


# ---------------------------------------------------------------------------
# Regression: profile save must not silently wipe max_education / matching
# ---------------------------------------------------------------------------
#
# Root cause (see audit): ProfileIn/ProfileOut previously had no fields for
# max_education / matching, even though JobSearchProfile (the model actually
# consumed by the requirements gate) defines them. GET therefore dropped them
# from the JSON response, and PUT reconstructed JobSearchProfile straight
# from the request body — so any save reset them to the Pydantic field
# defaults (None / MatchingPreferences()), silently disabling the education
# hard-constraint gate. This is fixed by (1) adding both fields to the API
# schema and (2) making PUT merge onto the on-disk profile instead of fully
# replacing it, so fields a request omits are preserved.

@pytest.fixture
def profile_yaml_with_education(tmp_path: Path) -> Path:
    p = tmp_path / "profile.yaml"
    p.write_text(
        "target_domains:\n  - software_development\n"
        "target_skills:\n  - python\n"
        "geography:\n  preferred_countries:\n    - France\n  allowed_regions:\n    - Europe\n"
        "work_modes:\n  - remote\n"
        "contract_types:\n  - cdi\n"
        "experience:\n  preferred_levels:\n    - junior\n  max_years: 3\n  excluded_seniority:\n    - senior\n"
        "languages:\n  - French\n  - English\n"
        "freshness_days: 30\n"
        "max_education: bac+2\n"
        "matching:\n"
        "  education_hard_constraint: true\n"
        "  education_preferred_penalty: 0.2\n"
        "  principal_skill_gate: true\n"
        "  mandatory_central_skill_gate: true\n"
        "  llm_extraction_enabled: false\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def edu_client(tmp_path: Path, profile_yaml_with_education: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient with profile route pointing at a profile.yaml that has
    max_education / matching set, mirroring the real config/profile.yaml."""
    import src.api.routes.profile as profile_route
    from src.api import routes  # noqa: F401 — ensures route registration

    monkeypatch.setattr(profile_route, "_PROFILE_PATH", profile_yaml_with_education)

    from src.api.app import app
    return TestClient(app)


class TestProfileSaveDataIntegrity:
    """Reproduces the exact reported bug: GET -> tweak an unrelated field ->
    PUT -> GET must leave max_education / matching untouched."""

    def test_get_profile_includes_education_fields(self, edu_client: TestClient) -> None:
        data = edu_client.get("/api/profile").json()
        assert data["max_education"] == "bac+2"
        assert data["matching"]["education_hard_constraint"] is True

    def test_max_education_survives_unrelated_field_save(self, edu_client: TestClient) -> None:
        # This mirrors exactly what the real Profile UI does: load the full
        # profile, mutate one unrelated field via object-spread, PUT the
        # whole object back.
        payload = edu_client.get("/api/profile").json()
        payload["freshness_days"] = 45  # unrelated change

        r = edu_client.put("/api/profile", json=payload)
        assert r.status_code == 200

        after = edu_client.get("/api/profile").json()
        assert after["freshness_days"] == 45
        assert after["max_education"] == "bac+2"
        assert after["matching"]["education_hard_constraint"] is True
        assert after["matching"]["education_preferred_penalty"] == 0.2
        assert after["matching"]["principal_skill_gate"] is True
        assert after["matching"]["mandatory_central_skill_gate"] is True

    def test_max_education_survives_put_that_omits_it_entirely(self, edu_client: TestClient) -> None:
        """Simulates an old/partial client that never learned about these
        fields: the raw request body has no `max_education` / `matching` key
        at all (not even `null`). Omitted must mean 'leave alone', not
        'clear'."""
        raw_payload = {
            "target_domains": ["software_development", "web_development"],
            "target_skills": ["python", "typescript"],
            "geography": {"preferred_countries": ["France"], "allowed_regions": ["Europe"]},
            "work_modes": ["remote"],
            "contract_types": ["cdi"],
            "experience": {"preferred_levels": ["junior"], "max_years": 3, "excluded_seniority": ["senior"]},
            "languages": ["French", "English"],
            "freshness_days": 14,
            # max_education / matching intentionally absent
        }
        r = edu_client.put("/api/profile", json=raw_payload)
        assert r.status_code == 200

        after = edu_client.get("/api/profile").json()
        assert after["freshness_days"] == 14  # the intended change did apply
        assert after["max_education"] == "bac+2"  # but this was NOT wiped
        assert after["matching"]["education_hard_constraint"] is True

    def test_explicit_null_does_clear_max_education(self, edu_client: TestClient) -> None:
        """Explicitly setting max_education to null IS a real, intended
        clear — omitted vs. explicit-null must remain distinguishable."""
        payload = edu_client.get("/api/profile").json()
        payload["max_education"] = None

        r = edu_client.put("/api/profile", json=payload)
        assert r.status_code == 200
        assert r.json()["max_education"] is None

        after = edu_client.get("/api/profile").json()
        assert after["max_education"] is None


class TestProfileSaveFilteringConsequence:
    """The real risk isn't the JSON round-trip in isolation — it's that a
    silently-wiped max_education used to disable a live hard-reject rule.
    This proves the actual gate decision is unaffected by a normal profile
    save, using the exact same wiring orchestrator.py uses in production."""

    @staticmethod
    def _build_gate_from_profile_yaml(profile_path: Path):
        import yaml as _yaml

        from src.models.profile import JobSearchProfile
        from src.requirements.education import parse_education_level
        from src.requirements.gate import HardConstraintGate

        with open(profile_path, encoding="utf-8") as f:
            data = _yaml.safe_load(f) or {}
        profile = JobSearchProfile(**data)

        user_max_edu_level = None
        if profile.max_education:
            user_max_edu_level = parse_education_level(profile.max_education)

        return HardConstraintGate(
            user_max_education_level=user_max_edu_level,
            education_hard_constraint=profile.matching.education_hard_constraint,
            principal_skill_gate=profile.matching.principal_skill_gate,
            mandatory_skill_gate=profile.matching.mandatory_central_skill_gate,
        )

    @staticmethod
    def _bac_plus_5_mandatory_requirements():
        from src.requirements.models import (
            EducationRequirement,
            Importance,
            JobRequirements,
        )

        return JobRequirements(
            education=EducationRequirement(
                level_bac_plus=5,
                level_label="Master",
                importance=Importance.MANDATORY,
            )
        )

    def test_bac5_job_rejected_before_and_after_unrelated_profile_save(
        self, edu_client: TestClient, profile_yaml_with_education: Path
    ) -> None:
        reqs = self._bac_plus_5_mandatory_requirements()

        # --- BEFORE: profile.yaml has max_education=bac+2, hard constraint on ---
        gate_before = self._build_gate_from_profile_yaml(profile_yaml_with_education)
        result_before = gate_before.check(reqs)
        assert not result_before.passes, "Bac+5 job must be rejected under max_education=bac+2"

        # --- Perform the exact UI save round-trip: GET, tweak an unrelated
        # field, PUT (this is what used to silently null out max_education) ---
        payload = edu_client.get("/api/profile").json()
        payload["work_modes"] = ["remote", "hybrid"]  # unrelated change
        r = edu_client.put("/api/profile", json=payload)
        assert r.status_code == 200

        # --- AFTER: rebuild the gate from the now-saved profile.yaml ---
        gate_after = self._build_gate_from_profile_yaml(profile_yaml_with_education)
        result_after = gate_after.check(reqs)
        assert not result_after.passes, (
            "Bac+5 job must STILL be rejected after an unrelated profile save — "
            "max_education must not have been silently wiped"
        )

    # -----------------------------------------------------------------------
    # Real jobs from the audit (data/jobs.db, source=france-travail), embedded
    # verbatim so the test doesn't depend on that local, non-versioned DB file
    # being present. Both require a mandatory Bac+5 ("de type Bac + 5 en
    # informatique" / "école d'ingénieur ou BAC+5 en informatique").
    # -----------------------------------------------------------------------
    _REAL_JOB_DEV_LOGICIEL = (
        "0268dc5ff144a791ddf5544fe1dfc781",
        "Développeur logiciel (H/F)",
        "Profil requis :\n\n- De formation supérieure de type Bac + 5 en informatique\n"
        "- Expérience en React, Python, FastAPI, Rust, Material UI, Node.js et "
        "Typescript, API REST, MongoDB, GitLab / CI/CD",
    )
    _REAL_JOB_INGENIEUR_QA = (
        "09413846fdedd8065676848f37c8bf91",
        "Ingénieur QA (H/F)",
        "Votre profil :\n\n- Diplômé-e d'une école d'ingénieur ou BAC+5 en informatique\n"
        "- Anglais niveau B2 minimum\n- Minimum 5 ans d'expérience en test logiciel",
    )

    @staticmethod
    def _extract_real_job_requirements(job_id: str, title: str, description: str):
        from datetime import UTC, datetime

        from src.models.job import Job as JobModel
        from src.requirements.extractor import RequirementExtractor

        job = JobModel(
            id=job_id, source="france-travail", title=title, description=description,
            discovered_at=datetime.now(UTC),
            raw_payload={},
        )
        extractor = RequirementExtractor(user_skills=["python"])
        return job, extractor.extract(job)

    @pytest.mark.parametrize(
        "real_job", [_REAL_JOB_DEV_LOGICIEL, _REAL_JOB_INGENIEUR_QA],
        ids=["developpeur_logiciel", "ingenieur_qa"],
    )
    def test_real_job_rejected_when_hard_constraint_on(
        self, edu_client: TestClient, profile_yaml_with_education: Path, real_job
    ) -> None:
        job_id, title, description = real_job
        job, requirements = self._extract_real_job_requirements(job_id, title, description)

        # Fixture already has education_hard_constraint: true.
        gate = self._build_gate_from_profile_yaml(profile_yaml_with_education)
        result = gate.check(requirements)
        assert not result.passes, f"{title!r} (Bac+5 mandatory) must be rejected under max_education=bac+2"

    @pytest.mark.parametrize(
        "real_job", [_REAL_JOB_DEV_LOGICIEL, _REAL_JOB_INGENIEUR_QA],
        ids=["developpeur_logiciel", "ingenieur_qa"],
    )
    def test_real_job_passes_with_penalty_after_disabling_hard_constraint_via_api(
        self, edu_client: TestClient, profile_yaml_with_education: Path, real_job
    ) -> None:
        """Disable the constraint through the real API (not by hand-building
        a gate), then prove the same real job now passes with the existing
        soft education penalty instead of being hard-rejected."""
        from src.requirements.gate import gate_jobs

        job_id, title, description = real_job

        # Disable the setting exactly as the UI would: GET, flip the toggle, PUT.
        payload = edu_client.get("/api/profile").json()
        assert payload["matching"]["education_hard_constraint"] is True  # sanity: fixture default
        payload["matching"]["education_hard_constraint"] = False
        r = edu_client.put("/api/profile", json=payload)
        assert r.status_code == 200
        assert r.json()["matching"]["education_hard_constraint"] is False

        gate = self._build_gate_from_profile_yaml(profile_yaml_with_education)
        job, requirements = self._extract_real_job_requirements(job_id, title, description)

        passing, results = gate_jobs([job], {job.id: requirements}, gate, education_preferred_penalty=0.20)
        assert len(passing) == 1, f"{title!r} must NOT be rejected once education_hard_constraint=False"
        assert results[job.id].passes
        assert results[job.id].education_penalty == 0.20, (
            "the mandatory-but-disabled mismatch must still apply the existing "
            "soft education penalty, not silently pass with no signal at all"
        )

        # And the setting must survive a subsequent, unrelated profile save.
        payload2 = edu_client.get("/api/profile").json()
        payload2["freshness_days"] = 45
        edu_client.put("/api/profile", json=payload2)
        after = edu_client.get("/api/profile").json()
        assert after["matching"]["education_hard_constraint"] is False


class TestPersonalProfileOverride:
    """Issue #8: saves go to config/profile.local.yaml; the tracked default
    config/profile.yaml is only ever read."""

    @pytest.fixture
    def config_client(self, tmp_path: Path, profile_yaml: Path, monkeypatch: pytest.MonkeyPatch):
        import src.api.routes.profile as profile_route

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "profile.yaml").write_text(
            profile_yaml.read_text(encoding="utf-8"), encoding="utf-8"
        )
        monkeypatch.setattr(profile_route, "_PROFILE_PATH", None)
        monkeypatch.setattr(profile_route, "_CONFIG_DIR", config_dir)

        from src.api.app import app
        return TestClient(app), config_dir

    def test_get_reads_default_when_no_local_file(self, config_client) -> None:
        client, config_dir = config_client
        assert client.get("/api/profile").json()["freshness_days"] == 30
        assert not (config_dir / "profile.local.yaml").exists()

    def test_put_writes_local_file_and_leaves_default_untouched(self, config_client) -> None:
        client, config_dir = config_client
        default_before = (config_dir / "profile.yaml").read_bytes()

        payload = client.get("/api/profile").json()
        payload["freshness_days"] = 45
        assert client.put("/api/profile", json=payload).status_code == 200

        assert (config_dir / "profile.yaml").read_bytes() == default_before
        local = yaml.safe_load((config_dir / "profile.local.yaml").read_text(encoding="utf-8"))
        assert local["freshness_days"] == 45
        assert client.get("/api/profile").json()["freshness_days"] == 45

    def test_first_partial_put_seeds_local_file_from_default(self, config_client) -> None:
        client, config_dir = config_client
        assert client.put("/api/profile", json={"freshness_days": 7}).status_code == 200

        local = yaml.safe_load((config_dir / "profile.local.yaml").read_text(encoding="utf-8"))
        assert local["freshness_days"] == 7
        assert "software_development" in local["target_domains"]

    def test_get_prefers_existing_local_file(self, config_client) -> None:
        client, config_dir = config_client
        data = yaml.safe_load((config_dir / "profile.yaml").read_text(encoding="utf-8"))
        data["freshness_days"] = 12
        (config_dir / "profile.local.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        assert client.get("/api/profile").json()["freshness_days"] == 12
