"""Tests for matches_contract_type — title-only freelance exclusion.

Deliberately does NOT test rejecting on the structured employment_type
field: job boards conflate CDD and freelance under the same generic
"contract" bucket (see src/pipeline/explain.py's _EMP_MAP), so this check
only ever looks at the job title, never employment_type. See
build_filter_config()'s contract_type section in src/pipeline/filter.py.

No Internet access. No collectors called. No real database touched.
"""

from __future__ import annotations

from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.filter import build_filter_config, matches_contract_type


def _job(**kwargs) -> Job:
    defaults: dict = {
        "source": "test",
        "title": "Software Engineer",
        "company": "Acme",
        "remote": False,
        "location": "Toulon, France",
        "countries": ["France"],
    }
    defaults.update(kwargs)
    return Job(**defaults)


class TestMatchesContractTypeUnit:
    def test_no_exclusion_list_accepts_everything(self):
        job = _job(title="DevOps - Freelance (H/F)")
        assert matches_contract_type(job, {"contract_type": {"exclude_title_keywords": []}}) is True

    def test_title_containing_freelance_is_rejected(self):
        job = _job(title="DevOps / Software Factory - Build & CI/CD - Freelance (H/F)")
        config = {"contract_type": {"exclude_title_keywords": ["freelance"]}}
        assert matches_contract_type(job, config) is False

    def test_title_containing_independant_is_rejected_accent_insensitive(self):
        job = _job(title="Consultant Indépendant DevOps")
        config = {"contract_type": {"exclude_title_keywords": ["indépendant"]}}
        assert matches_contract_type(job, config) is False

    def test_unrelated_title_is_accepted(self):
        job = _job(title="Ingénieur DevOps F/H")
        config = {"contract_type": {"exclude_title_keywords": ["freelance", "indépendant"]}}
        assert matches_contract_type(job, config) is True

    def test_employment_type_alone_is_never_checked(self):
        """A job with employment_type='contract' but a clean title must NOT
        be rejected — the ambiguity with CDD is exactly why this check is
        title-only."""
        job = _job(title="Ingénieur DevOps CDD", employment_type="contract")
        config = {"contract_type": {"exclude_title_keywords": ["freelance", "indépendant"]}}
        assert matches_contract_type(job, config) is True


class TestBuildFilterConfigContractTypeSection:
    def _profile(self, contract_types: list[str]) -> JobSearchProfile:
        return JobSearchProfile(contract_types=contract_types)

    def test_freelance_excluded_when_not_in_accepted_contract_types(self):
        profile = self._profile(["cdi", "cdd", "full-time"])
        engine_cfg = {"contract_keywords": {"freelance": {"title": ["freelance", "indépendant"]}}}
        config = build_filter_config(profile, engine_cfg)
        assert config["contract_type"]["exclude_title_keywords"] == ["freelance", "indépendant"]

    def test_no_exclusion_when_freelance_is_explicitly_accepted(self):
        profile = self._profile(["cdi", "freelance"])
        engine_cfg = {"contract_keywords": {"freelance": {"title": ["freelance", "indépendant"]}}}
        config = build_filter_config(profile, engine_cfg)
        assert config["contract_type"]["exclude_title_keywords"] == []

    def test_no_exclusion_when_contract_types_is_empty(self):
        """Empty contract_types means 'accept any contract type' — must not
        suddenly start rejecting freelance jobs for a profile that never
        opted into any restriction at all."""
        profile = self._profile([])
        engine_cfg = {"contract_keywords": {"freelance": {"title": ["freelance"]}}}
        config = build_filter_config(profile, engine_cfg)
        assert config["contract_type"]["exclude_title_keywords"] == []


class TestRealWorldRegression:
    def test_the_reported_job_is_now_rejected(self):
        """Reproduces the exact job that slipped through in production:
        'DevOps / Software Factory - Build & CI/CD - Freelance (H/F)',
        employment_type='contract', with the user's real contract_types
        (internship, alternance, cdi, cdd, full-time, part-time)."""
        job = _job(
            title="DevOps / Software Factory - Build & CI/CD - Freelance (H/F)",
            employment_type="contract",
        )
        profile = JobSearchProfile(
            contract_types=["internship", "alternance", "cdi", "cdd", "full-time", "part-time"]
        )
        engine_cfg = {
            "contract_keywords": {
                "freelance": {
                    "title": ["freelance", "indépendant", "independant", "auto-entrepreneur", "portage salarial"]
                }
            }
        }
        config = build_filter_config(profile, engine_cfg)
        assert matches_contract_type(job, config) is False
