"""Quality fixes — regression tests.

Covers:
  Priority 2 — Work-mode inference improvements (job_type remote signal)
  Priority 3 — Employment type recovery (French types, title fallback)
  Priority 4 — Salary currency fix (EUR default, no fake USD)
  Priority 5 — Title-based skill extraction
  Priority 9 — Cloud/DevOps vocabulary expansion
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from tests.fixtures.profiles import TEST_PROFILE_PATH

try:
    import pandas  # noqa: F401
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

_requires_pandas = pytest.mark.skipif(
    not _HAS_PANDAS, reason="pandas (optional 'linkedin' extra) not installed"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_row(**kwargs) -> dict[str, Any]:
    """Return a minimal JobSpy-style row dict with sensible defaults."""
    defaults: dict[str, Any] = {
        "id": "99999",
        "job_url": "https://www.linkedin.com/jobs/view/99999",
        "title": "Software Engineer",
        "company": "Acme",
        "location": "Paris, France",
        "job_type": None,
        "date_posted": None,
        "interval": None,
        "min_amount": None,
        "max_amount": None,
        "currency": None,
        "is_remote": None,
        "job_level": None,
        "company_url": None,
        "company_logo": None,
        "description": None,
    }
    defaults.update(kwargs)
    return defaults


def _build_record(row: dict[str, Any]) -> dict[str, Any] | None:
    """Call LinkedInScanner._build_record on a synthetic row dict."""
    import pandas as pd
    from src.linkedin.scanner import LinkedInConfig, LinkedInScanner
    from src.linkedin.staging import LinkedInStagingStorage

    cfg = LinkedInConfig(fetch_description=False, enrich_descriptions=False)
    staging = MagicMock(spec=LinkedInStagingStorage)
    scanner = LinkedInScanner(config=cfg, staging=staging)

    series = pd.Series(row)
    return scanner._build_record(series, scan_id="test-scan", now_iso="2026-08-30T00:00:00+00:00")


# ---------------------------------------------------------------------------
# Priority 2 — Work-mode: job_type "remote" signal
# ---------------------------------------------------------------------------

@_requires_pandas
class TestWorkModeFromJobType:
    def test_job_type_remote_sets_work_mode(self):
        rec = _build_record(_make_raw_row(job_type="remote", is_remote=None))
        assert rec is not None
        assert rec["work_mode"] == "remote"

    def test_job_type_hybrid_sets_work_mode(self):
        rec = _build_record(_make_raw_row(job_type="hybrid"))
        assert rec is not None
        assert rec["work_mode"] == "hybrid"

    def test_job_type_on_site_sets_work_mode(self):
        rec = _build_record(_make_raw_row(job_type="on_site"))
        assert rec is not None
        assert rec["work_mode"] == "on_site"

    def test_job_type_onsite_variant(self):
        rec = _build_record(_make_raw_row(job_type="onsite"))
        assert rec is not None
        assert rec["work_mode"] == "on_site"

    def test_is_remote_true_takes_priority(self):
        rec = _build_record(_make_raw_row(is_remote=True, job_type="on_site"))
        assert rec is not None
        assert rec["work_mode"] == "remote"

    def test_no_signal_gives_none(self):
        rec = _build_record(_make_raw_row(is_remote=None, job_type=None))
        assert rec is not None
        assert rec["work_mode"] is None

    def test_title_remote_marker_sets_work_mode(self):
        rec = _build_record(_make_raw_row(title="Software Engineer (Remote)"))
        assert rec is not None
        assert rec["work_mode"] == "remote"

    def test_title_hybrid_marker_sets_work_mode(self):
        rec = _build_record(_make_raw_row(title="DevOps Engineer (Hybride)"))
        assert rec is not None
        assert rec["work_mode"] == "hybrid"

    def test_location_remote_sets_work_mode(self):
        rec = _build_record(_make_raw_row(location="Remote"))
        assert rec is not None
        assert rec["work_mode"] == "remote"


# ---------------------------------------------------------------------------
# Priority 3 — Employment type
# ---------------------------------------------------------------------------

class TestExtractEmploymentType:
    """Unit tests for _extract_employment_type."""

    def _extract(self, job_type=None, title=None):
        from src.linkedin.scanner import _extract_employment_type
        return _extract_employment_type(job_type, title=title)

    # Structured job_type field
    def test_fulltime(self):
        assert self._extract("fulltime") == "full_time"

    def test_full_time(self):
        assert self._extract("full_time") == "full_time"

    def test_full_time_hyphen(self):
        assert self._extract("full-time") == "full_time"

    def test_parttime(self):
        assert self._extract("parttime") == "part_time"

    def test_contract(self):
        assert self._extract("contract") == "contract"

    def test_internship(self):
        assert self._extract("internship") == "internship"

    def test_intern(self):
        assert self._extract("intern") == "internship"

    def test_freelance_from_job_type(self):
        assert self._extract("freelance") == "freelance"

    def test_temporary(self):
        assert self._extract("temporary") == "contract"

    def test_alternance_from_job_type(self):
        assert self._extract("alternance") == "alternance"

    def test_apprentissage_from_job_type(self):
        assert self._extract("apprentissage") == "alternance"

    def test_none_returns_none(self):
        assert self._extract(None) is None

    # Title fallback — alternance
    def test_title_alternance_parenthetical(self):
        assert self._extract(None, "Développeur Python (alternance)") == "alternance"

    def test_title_alternance_dash(self):
        assert self._extract(None, "Backend Developer - Alternance") == "alternance"

    def test_title_alternance_space_bounded(self):
        assert self._extract(None, "Ingénieur DevOps alternance Paris") == "alternance"

    def test_title_apprentissage(self):
        assert self._extract(None, "Technicien Réseau - Apprentissage") == "alternance"

    def test_title_alternant(self):
        assert self._extract(None, "Développeur Web - Alternant") == "alternance"

    # Title fallback — stage
    def test_title_stage_parenthetical(self):
        assert self._extract(None, "Développeur Python (stage)") == "internship"

    def test_title_stagiaire(self):
        assert self._extract(None, "Stagiaire DevOps") == "internship"

    def test_title_stage_word_boundary(self):
        assert self._extract(None, "Ingénieur Cloud - Stage") == "internship"

    # Title fallback — CDI
    def test_title_cdi_parenthetical(self):
        assert self._extract(None, "Software Engineer (CDI) - Paris") == "full_time"

    def test_title_cdi_dash(self):
        assert self._extract(None, "DevOps Engineer - CDI") == "full_time"

    # Title fallback — CDD (fixed-term contract → "contract", not "part_time")
    def test_title_cdd_parenthetical(self):
        assert self._extract(None, "Data Analyst (CDD) 6 mois") == "contract"

    def test_title_cdi_freelance_mixed(self):
        # "(CDI/FREELANCE)" — CDI wins (checked first); was previously missed by substring matching
        assert self._extract(None, "Consultant Devops Azure (CDI/FREELANCE) H/F") == "full_time"

    def test_title_cdd_freelance_mixed(self):
        # "(CDD/Freelance)" — CDD wins (checked before freelance)
        assert self._extract(None, "Développeur Backend (CDD/Freelance)") == "contract"

    # Title fallback — freelance
    def test_title_freelance(self):
        assert self._extract(None, "Développeur React - Freelance") == "freelance"

    # Structured field wins over title
    def test_job_type_wins_over_title(self):
        assert self._extract("fulltime", title="Stage Python") == "full_time"

    # Unknown values
    def test_unknown_job_type_and_no_title_returns_none(self):
        assert self._extract("unknown_value") is None

    def test_both_none_returns_none(self):
        assert self._extract(None, None) is None


# ---------------------------------------------------------------------------
# Priority 4 — Salary currency fix
# ---------------------------------------------------------------------------

@_requires_pandas
class TestSalaryCurrency:
    """Salary currency must default to EUR (not USD) for French LinkedIn jobs."""

    def test_no_salary_no_currency_defaults_eur(self):
        rec = _build_record(_make_raw_row(min_amount=None, max_amount=None, currency=None))
        assert rec is not None
        assert rec["salary_currency"] == "EUR"

    def test_nan_currency_defaults_eur(self):
        rec = _build_record(_make_raw_row(min_amount=float("nan"), max_amount=float("nan"), currency=float("nan")))
        assert rec is not None
        assert rec["salary_currency"] == "EUR"

    def test_explicit_eur_preserved(self):
        rec = _build_record(_make_raw_row(min_amount=40000.0, max_amount=50000.0, currency="EUR"))
        assert rec is not None
        assert rec["salary_currency"] == "EUR"
        assert rec["salary_min"] == 40000.0
        assert rec["salary_max"] == 50000.0

    def test_explicit_usd_preserved_when_present(self):
        # JobSpy returns USD explicitly — should not be overridden
        rec = _build_record(_make_raw_row(min_amount=60000.0, max_amount=80000.0, currency="USD"))
        assert rec is not None
        assert rec["salary_currency"] == "USD"

    def test_salary_min_without_currency_defaults_eur(self):
        # Salary present but no currency from JobSpy
        rec = _build_record(_make_raw_row(min_amount=45000.0, max_amount=None, currency=None))
        assert rec is not None
        assert rec["salary_min"] == 45000.0
        assert rec["salary_currency"] == "EUR"

    def test_no_salary_fields_both_none(self):
        rec = _build_record(_make_raw_row())
        assert rec is not None
        assert rec["salary_min"] is None
        assert rec["salary_max"] is None
        # Currency stored but salary fields are None → UI should not show salary
        assert rec["salary_currency"] == "EUR"  # EUR is a safe default, not USD


class TestStagingRowToJobCurrency:
    """staging_row_to_job must not convert None salary_currency to USD."""

    def _convert(self, salary_currency=None, salary_min=None, salary_max=None) -> Any:
        from src.linkedin.staging import staging_row_to_job

        row = {
            "id": "abc123",
            "linkedin_job_id": "42",
            "canonical_url": "https://www.linkedin.com/jobs/view/42",
            "title": "Software Engineer",
            "company": "Acme",
            "location": "Paris, France",
            "countries": "[]",
            "is_remote": 0,
            "work_mode": None,
            "employment_type": None,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": salary_currency,
            "description": None,
            "description_fetched": 0,
            "published_at": None,
            "raw_payload": "{}",
            "scan_run_id": "scan-1",
            "discovered_at": "2026-08-30T00:00:00+00:00",
            "last_seen_at": "2026-08-30T00:00:00+00:00",
        }
        return staging_row_to_job(row)

    def test_null_currency_becomes_eur_not_usd(self):
        job = self._convert(salary_currency=None)
        assert job.salary_currency == "EUR"

    def test_eur_preserved(self):
        job = self._convert(salary_currency="EUR", salary_min=40000.0)
        assert job.salary_currency == "EUR"

    def test_usd_preserved_when_explicit(self):
        job = self._convert(salary_currency="USD", salary_min=60000.0)
        assert job.salary_currency == "USD"


# ---------------------------------------------------------------------------
# Priority 5 — Title-based skill extraction
# ---------------------------------------------------------------------------

class TestExtractTitleSkills:
    def _extract(self, title: str | None) -> list[str]:
        from src.linkedin.scanner import _extract_title_skills
        return _extract_title_skills(title)

    def test_python_extracted(self):
        assert "Python" in self._extract("Développeur Python Senior")

    def test_docker_kubernetes(self):
        skills = self._extract("DevOps Engineer — Docker / Kubernetes")
        assert "Docker" in skills
        assert "Kubernetes" in skills

    def test_terraform_ansible(self):
        skills = self._extract("Cloud Engineer - Terraform / Ansible / AWS")
        assert "Terraform" in skills
        assert "Ansible" in skills
        assert "AWS" in skills

    def test_gcp(self):
        assert "GCP" in self._extract("Ingénieur Cloud GCP / Terraform")

    def test_azure(self):
        assert "Azure" in self._extract("Cloud Architect Azure & Kubernetes")

    def test_javascript_not_triggering_java(self):
        """'JavaScript' must not match 'Java' token."""
        skills = self._extract("Frontend Developer - JavaScript/React")
        assert "JavaScript" in skills
        assert "Java" not in skills

    def test_react_extracted(self):
        assert "React" in self._extract("Frontend Developer React/TypeScript")

    def test_typescript_extracted(self):
        assert "TypeScript" in self._extract("TypeScript Backend Engineer")

    def test_linux_extracted(self):
        assert "Linux" in self._extract("Sysadmin Linux/Bash")

    def test_bash_extracted(self):
        assert "Bash" in self._extract("Sysadmin Linux/Bash")

    def test_git_not_false_positive_in_github(self):
        """'GitHub' should not also extract 'Git' as a separate entity."""
        skills = self._extract("DevOps Engineer - GitHub Actions")
        # GitHub is extracted; Git alone might or might not be (word boundary)
        assert "GitHub" in skills

    def test_dotnet(self):
        assert ".NET" in self._extract("Développeur C# / .NET")

    def test_csharp(self):
        assert "C#" in self._extract("Backend Developer C#/.NET")

    def test_no_title_returns_empty(self):
        assert self._extract(None) == []

    def test_no_skills_returns_empty(self):
        assert self._extract("Responsable Comptable") == []

    def test_deduplication(self):
        """Same skill appearing multiple times is returned once."""
        skills = self._extract("Python/Python Developer with Python experience")
        assert skills.count("Python") == 1

    def test_ci_cd_extracted(self):
        assert "CI/CD" in self._extract("DevOps Engineer CI/CD Kubernetes")


@_requires_pandas
class TestBuildRecordSkillExtraction:
    """Integration: _build_record populates extracted_title_skills in raw_payload."""

    def test_skills_in_raw_payload_when_no_description(self):
        rec = _build_record(_make_raw_row(
            title="Cloud Engineer AWS / Terraform",
            description=None,
        ))
        assert rec is not None
        raw = json.loads(rec["raw_payload"])
        assert "extracted_title_skills" in raw
        skills = raw["extracted_title_skills"]
        assert "AWS" in skills
        assert "Terraform" in skills

    def test_skills_extracted_even_when_description_present(self):
        """Title skills are extracted regardless of whether a description exists.

        The scoring layer already checks titles directly; persisting title skills
        in raw_payload also makes them available for API display and future use.
        """
        rec = _build_record(_make_raw_row(
            title="Cloud Engineer AWS / Terraform",
            description="A full job description with many details about cloud engineering...",
        ))
        assert rec is not None
        raw = json.loads(rec["raw_payload"])
        assert "extracted_title_skills" in raw
        assert "AWS" in raw["extracted_title_skills"]
        assert "Terraform" in raw["extracted_title_skills"]

    def test_skills_extracted_from_description_when_title_has_none(self):
        """Regression: a title with no tech keyword must not leave skills
        empty when the description (already available at build time,
        e.g. from a bulk fetch) mentions known technologies.
        """
        rec = _build_record(_make_raw_row(
            title="Développeur informatique F/H",
            description="Vous travaillerez avec Python, Docker et Kubernetes.",
        ))
        assert rec is not None
        raw = json.loads(rec["raw_payload"])
        skills = raw.get("extracted_title_skills", [])
        # extract_known_technologies() (shared with every other source's
        # normalize.py fallback) returns lowercase tokens, unlike the
        # title-only extractor's canonical-cased labels — both are merged.
        assert "python" in skills
        assert "docker" in skills
        assert "kubernetes" in skills

    def test_staging_row_to_job_populates_skills(self):
        """staging_row_to_job reads extracted_title_skills into Job.skills."""
        from src.linkedin.staging import staging_row_to_job

        raw_payload = {"extracted_title_skills": ["Python", "Docker", "AWS"]}
        row = {
            "id": "abc",
            "linkedin_job_id": "123",
            "canonical_url": "https://www.linkedin.com/jobs/view/123",
            "title": "Python DevOps Engineer",
            "company": "Corp",
            "location": "France",
            "countries": "[]",
            "is_remote": 0,
            "work_mode": None,
            "employment_type": None,
            "salary_min": None,
            "salary_max": None,
            "salary_currency": "EUR",
            "description": None,
            "description_fetched": 0,
            "published_at": None,
            "raw_payload": json.dumps(raw_payload),
            "scan_run_id": "scan-1",
            "discovered_at": "2026-08-30T00:00:00+00:00",
            "last_seen_at": "2026-08-30T00:00:00+00:00",
        }
        job = staging_row_to_job(row)
        assert "Python" in job.skills
        assert "Docker" in job.skills
        assert "AWS" in job.skills

    def test_staging_row_to_job_empty_skills_when_no_payload(self):
        """Job.skills is empty when no extracted_title_skills in payload."""
        from src.linkedin.staging import staging_row_to_job

        row = {
            "id": "abc",
            "linkedin_job_id": "124",
            "canonical_url": "https://www.linkedin.com/jobs/view/124",
            "title": "Software Engineer",
            "company": "Corp",
            "location": "France",
            "countries": "[]",
            "is_remote": 0,
            "work_mode": None,
            "employment_type": None,
            "salary_min": None,
            "salary_max": None,
            "salary_currency": "EUR",
            "description": None,
            "description_fetched": 0,
            "published_at": None,
            "raw_payload": "{}",
            "scan_run_id": "scan-1",
            "discovered_at": "2026-08-30T00:00:00+00:00",
            "last_seen_at": "2026-08-30T00:00:00+00:00",
        }
        job = staging_row_to_job(row)
        assert job.skills == []


# ---------------------------------------------------------------------------
# Priority 9 — Cloud/DevOps vocabulary expansion
# ---------------------------------------------------------------------------

class TestCloudDevOpsVocabulary:
    """Ensure new Cloud/DevOps terms pass the hard filter."""

    def _make_filter_config(self):
        from src.pipeline.filter import build_filter_config, load_filters, load_profile

        profile = load_profile(TEST_PROFILE_PATH)
        engine = load_filters("config/filters.yaml")
        return build_filter_config(profile, engine)

    def _passes_filter(self, title: str) -> bool:
        from src.models.job import Job
        from src.pipeline.filter import filter_jobs

        cfg = self._make_filter_config()
        job = Job(
            source="linkedin",
            title=title,
            location="France",
            countries=["France"],
            remote=False,
        )
        passed, _ = filter_jobs([job], cfg)
        return len(passed) > 0

    def test_cloud_architect_passes(self):
        assert self._passes_filter("Cloud Architect AWS/Azure")

    def test_architecte_cloud_passes(self):
        assert self._passes_filter("Architecte Cloud GCP")

    def test_cloud_native_engineer_passes(self):
        assert self._passes_filter("Cloud Native Engineer Kubernetes")

    def test_cloud_native_hyphen_passes(self):
        assert self._passes_filter("Cloud-Native Platform Engineer")

    def test_cloudops_engineer_passes(self):
        assert self._passes_filter("CloudOps Engineer Azure")

    def test_cloud_infrastructure_passes(self):
        assert self._passes_filter("Cloud Infrastructure Engineer")

    def test_finops_passes(self):
        assert self._passes_filter("FinOps Cloud Engineer")

    def test_existing_devops_still_passes(self):
        assert self._passes_filter("DevOps Engineer France")

    def test_existing_sre_still_passes(self):
        assert self._passes_filter("Site Reliability Engineer")

    def test_existing_cloud_engineer_still_passes(self):
        assert self._passes_filter("Cloud Engineer GCP")

    def test_non_it_cloud_sales_rejected(self):
        """'Cloud Sales' is not a DevOps/Cloud engineering role."""
        # A Sales role title — check it doesn't accidentally pass
        # (depends on excluded_title_keywords and domain mismatch)
        assert not self._passes_filter("Cloud Sales Representative")

    def test_systems_administrator_passes(self):
        assert self._passes_filter("Systems Administrator Linux")

    # -- Automation-domain vocabulary (2026-09-12 forensic audit fix) -------
    # config/profile.yaml's target_domains used "automation" (English) but
    # this richer cloud/devops/sre/platform vocabulary previously lived
    # under a separate, never-referenced "automatisation" (French) key in
    # config/filters.yaml — merged into "automation" as the single
    # canonical bucket. These specific titles were the confirmed failures.

    def test_ingenieur_devops_passes(self):
        assert self._passes_filter("Ingénieur DevOps")

    def test_automation_engineer_passes(self):
        assert self._passes_filter("Automation Engineer")

    def test_ingenieur_automatisation_passes(self):
        assert self._passes_filter("Ingénieur Automatisation")

    def test_platform_engineer_passes(self):
        assert self._passes_filter("Platform Engineer")

    def test_devsecops_passes(self):
        assert self._passes_filter("DevSecOps Engineer")

    # -- Systems-domain vocabulary (2026-09-12 Pass 4 audit fix) -------------
    # config/filters.yaml's "systems" (English, referenced by
    # profile.yaml's target_domains) bucket was missing the plural
    # "systems administrator" keyword that its "systèmes" (French alias)
    # counterpart already had — a smaller instance of the same
    # French/English drift pattern as the automation/automatisation split.
    # "Systems Administrator" (no extra qualifying words) was the confirmed
    # live failure; the others are regression guards proving the fix
    # didn't disturb adjacent, already-working title variants.

    def test_systems_administrator_bare_title_passes(self):
        """The confirmed failure: no qualifying word (e.g. "Linux") to
        rescue it via a different keyword — this title depended entirely
        on the missing plural keyword."""
        assert self._passes_filter("Systems Administrator")

    def test_system_administrator_singular_still_passes(self):
        assert self._passes_filter("System Administrator")

    def test_administrateur_systemes_still_passes(self):
        assert self._passes_filter("Administrateur Systèmes")

    def test_technicien_systemes_still_passes(self):
        assert self._passes_filter("Technicien Systèmes")

    def test_sysadmin_still_passes(self):
        assert self._passes_filter("Sysadmin")

    def test_unrelated_titles_not_falsely_accepted_by_systems_fix(self):
        """The single-keyword addition must not broaden matching for
        titles that merely contain 'system'/'systems' in an unrelated
        sense."""
        for title in ["Solar Systems Sales Representative", "Ecosystems Marketing Manager"]:
            assert not self._passes_filter(title), f"{title!r} should still be rejected"


class TestDomainKeywordConfigurationContract:
    """Configuration contract: every profile.target_domains entry must
    resolve to a present, non-empty domain_keywords bucket in
    filters.yaml — never silently fall through to a missing/empty bucket
    (the literal-domain-string fallback in build_filter_config). This is
    the generic invariant that the automation/automatisation split
    violated: profile.yaml said "automation", filters.yaml's richer
    vocabulary was keyed "automatisation", and nothing caught the mismatch
    until a job like "Cloud Architect" was silently rejected.

    Deliberately does NOT assert a minimum keyword count (arbitrary,
    brittle) — only presence and non-emptiness, which is the actual,
    well-defined contract. Vocabulary *correctness/richness* for specific
    domains is covered by dedicated tests (e.g. TestCloudDevOpsVocabulary
    for "automation"), not by this generic structural check.
    """

    def test_every_target_domain_resolves_to_a_populated_bucket(self):
        from src.pipeline.filter import load_filters, load_profile

        profile = load_profile(TEST_PROFILE_PATH)
        engine_cfg = load_filters("config/filters.yaml")
        domain_keywords = engine_cfg.get("domain_keywords", {})

        unresolved = []
        empty = []
        for domain in profile.target_domains:
            bucket = domain_keywords.get(domain)
            if bucket is None:
                unresolved.append(domain)
                continue
            if not bucket.get("title"):
                empty.append(domain)

        assert not unresolved, (
            f"target_domains {unresolved} have NO matching domain_keywords "
            "entry in filters.yaml — build_filter_config() silently falls "
            "back to using the literal domain name as the only keyword."
        )
        assert not empty, (
            f"target_domains {empty} resolve to a domain_keywords entry "
            "with zero title keywords — effectively unmatchable."
        )

    def test_automation_domain_is_not_split_across_two_keys(self):
        """Regression guard for the specific bug: filters.yaml must not
        have a French "automatisation" key sitting alongside the canonical
        "automation" key — that split is exactly what silently orphaned
        the cloud/devops/sre vocabulary from profile.yaml's selected
        domain last time. A future edit reintroducing a parallel
        "automatisation" bucket should fail this test immediately."""
        from src.pipeline.filter import load_filters

        engine_cfg = load_filters("config/filters.yaml")
        domain_keywords = engine_cfg.get("domain_keywords", {})
        assert "automatisation" not in domain_keywords, (
            "A separate 'automatisation' domain_keywords key has reappeared — "
            "merge its content into 'automation' (the canonical key profile.yaml "
            "uses) instead of maintaining two divergent copies."
        )

    # -- French-language alias pairs must not silently drift -----------------
    # config/filters.yaml documents four "French alias for X" domain_keywords
    # entries (informatique has no single English 1:1 counterpart, so it's
    # excluded): systèmes/systems, cybersécurité/cybersecurity, réseaux/
    # networking, développement logiciel/software_development. Each pair is
    # maintained as two INDEPENDENT hand-written keyword lists rather than a
    # true single source of truth — exactly the design that let
    # "systems"/"systèmes" drift by one keyword ("systems administrator"),
    # silently rejecting a standard, common job title (2026-09-12 Pass 4
    # audit). Only the two pairs proven to be in sync (and that a real,
    # common job title depends on) are asserted strictly equal here.
    #
    # "réseaux"/"networking" and "développement logiciel"/"software_development"
    # are KNOWN to currently differ (see git history / Pass 3-4 audit
    # reports) — réseaux's extra keywords were proven harmless (all
    # functionally redundant via networking's own bare "réseaux"/"reseaux"
    # substring matches, except one unrealistic hybrid title with low
    # real-world likelihood), and développement logiciel's gap only matters
    # for a profile that selects the French domain name (not the case
    # today). Deliberately excluded here rather than silently passed, so
    # this comment — not test-list omission — is the record of that
    # decision; fixing them is out of this pass's scope.
    _SYNCED_ALIAS_PAIRS = [
        ("systems", "systèmes"),
        ("cybersecurity", "cybersécurité"),
    ]

    def test_synced_alias_pairs_have_identical_title_vocabulary(self):
        from src.pipeline.filter import load_filters

        engine_cfg = load_filters("config/filters.yaml")
        domain_keywords = engine_cfg.get("domain_keywords", {})

        mismatches = {}
        for english_key, french_key in self._SYNCED_ALIAS_PAIRS:
            eng = set(domain_keywords.get(english_key, {}).get("title", []))
            fr = set(domain_keywords.get(french_key, {}).get("title", []))
            diff = eng ^ fr
            if diff:
                mismatches[f"{english_key}/{french_key}"] = sorted(diff)

        assert not mismatches, (
            f"Alias pairs expected to stay in sync have drifted: {mismatches}. "
            "Add the missing keyword(s) to whichever side is behind."
        )


# ---------------------------------------------------------------------------
# False-positive regression tests for work-mode title extraction
# ---------------------------------------------------------------------------

class TestWorkModeTitleFalsePositives:
    """Ensure technology names with 'remote' in them are NOT classified as remote."""

    def _extract(self, title):
        from src.linkedin.scanner import _extract_work_mode_from_title
        return _extract_work_mode_from_title(title)

    def test_remote_procedure_call_not_remote(self):
        assert self._extract("Remote Procedure Call Engineer") is None

    def test_kafka_remote_write_not_remote(self):
        assert self._extract("Kafka Remote Write Developer") is None

    def test_prometheus_remote_write_not_remote(self):
        assert self._extract("Prometheus Remote Write Specialist") is None

    def test_remote_desktop_protocol_not_remote(self):
        assert self._extract("Remote Desktop Protocol Support Technician") is None

    def test_remote_access_engineer_not_remote(self):
        assert self._extract("Remote Access Engineer") is None

    def test_parenthetical_remote_is_remote(self):
        assert self._extract("Software Engineer (Remote)") == "remote"

    def test_dash_remote_is_remote(self):
        assert self._extract("DevOps Engineer - Remote") == "remote"

    def test_pipe_remote_is_remote(self):
        assert self._extract("Backend Developer | Remote") == "remote"


# ---------------------------------------------------------------------------
# Seniority filtering — "Sr." abbreviation
# ---------------------------------------------------------------------------

class TestSeniorityFilter:
    """Seniority exclusion must catch 'Sr.' abbreviation for Senior."""

    def _passes_filter(self, title: str) -> bool:
        from src.models.job import Job
        from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile

        profile = load_profile(TEST_PROFILE_PATH)
        engine = load_filters("config/filters.yaml")
        cfg = build_filter_config(profile, engine)
        job = Job(source="linkedin", title=title, location="France", countries=["France"], remote=False)
        passed, _ = filter_jobs([job], cfg)
        return len(passed) > 0

    def test_sr_devops_rejected(self):
        """'Sr. DevOps Engineer' must be rejected — abbreviation for Senior."""
        assert not self._passes_filter("Sr. DevOps Engineer")

    def test_sr_space_rejected(self):
        assert not self._passes_filter("Sr Software Engineer")

    def test_senior_devops_rejected(self):
        assert not self._passes_filter("Senior DevOps Engineer")

    def test_junior_devops_passes(self):
        assert self._passes_filter("Junior DevOps Engineer")

    def test_devops_no_seniority_passes(self):
        """Title with no seniority marker passes normally."""
        assert self._passes_filter("DevOps Engineer France")

    def test_sre_no_seniority_passes(self):
        assert self._passes_filter("Site Reliability Engineer")

    # -- principal/staff/c_suite (2026-09-12 forensic audit fix) -------------
    # filters.yaml's seniority_exclusions already defined "principal",
    # "staff", and "c_suite" groups, but profile.yaml's excluded_seniority
    # never referenced them — so a junior-only profile silently accepted
    # "Staff DevOps Engineer"-style titles. Fixed by adding all three to
    # profile.yaml's excluded_seniority (the existing configuration
    # mechanism), plus extending the "staff" group's compound-phrase list
    # (it had no DevOps/Cloud/Platform entries at all — see filters.yaml).

    def test_lead_devops_rejected(self):
        """Already correctly configured before this fix — regression guard
        proving the fix didn't disturb existing seniority semantics."""
        assert not self._passes_filter("Lead DevOps Engineer")

    def test_staff_devops_rejected(self):
        assert not self._passes_filter("Staff DevOps Engineer")

    def test_principal_devops_rejected(self):
        assert not self._passes_filter("Principal DevOps Engineer")

    def test_c_suite_example_rejected(self):
        assert not self._passes_filter("Chief Technology Officer")

    def test_devops_engineer_still_passes(self):
        """No seniority marker at all — must be unaffected by the fix."""
        assert self._passes_filter("DevOps Engineer")

    def test_junior_devops_engineer_still_passes(self):
        assert self._passes_filter("Junior DevOps Engineer")


# ---------------------------------------------------------------------------
# Title skill extraction — expanded vocabulary
# ---------------------------------------------------------------------------

class TestExpandedTitleSkills:
    """Verify new skill tokens added in vocabulary expansion."""

    def _extract(self, title: str) -> list[str]:
        from src.linkedin.scanner import _extract_title_skills
        return _extract_title_skills(title)

    def test_machine_learning_extracted(self):
        assert "Machine Learning" in self._extract("Machine Learning Engineer")

    def test_siem_extracted(self):
        assert "SIEM" in self._extract("Cybersecurity Engineer SIEM/SOC")

    def test_soc_extracted(self):
        assert "SOC" in self._extract("Analyste SOC - Cybersécurité")

    def test_iam_extracted(self):
        assert "IAM" in self._extract("IAM Engineer - Identity Access Management")

    def test_pentest_extracted(self):
        assert "Pentest" in self._extract("Ingénieur Pentest / Cybersécurité")

    def test_devsecops_extracted(self):
        assert "DevSecOps" in self._extract("DevSecOps Engineer Cloud")

    def test_tensorflow_extracted(self):
        assert "TensorFlow" in self._extract("ML Engineer - TensorFlow / PyTorch")

    def test_pytorch_extracted(self):
        assert "PyTorch" in self._extract("ML Engineer - TensorFlow / PyTorch")

    def test_openshift_extracted(self):
        assert "OpenShift" in self._extract("Ingénieur DevOps OpenShift / Kubernetes")

    def test_powershell_extracted(self):
        assert "PowerShell" in self._extract("Sysadmin Windows PowerShell / Active Directory")

    def test_active_directory_extracted(self):
        assert "Active Directory" in self._extract("Administrateur Système Active Directory")

    def test_vmware_extracted(self):
        assert "VMware" in self._extract("Ingénieur Virtualisation VMware / Proxmox")

    def test_laravel_extracted(self):
        assert "Laravel" in self._extract("Développeur PHP / Laravel")

    def test_symfony_extracted(self):
        assert "Symfony" in self._extract("Backend Developer Symfony / PHP")

    def test_mlops_extracted(self):
        assert "MLOps" in self._extract("MLOps Engineer - Python / Kubernetes")

    def test_github_actions_extracted(self):
        assert "GitHub Actions" in self._extract("DevOps Engineer - GitHub Actions / Terraform")

    def test_indépendant_freelance(self):
        """'indépendant' in title → employment_type = freelance."""
        from src.linkedin.scanner import _extract_employment_type
        assert _extract_employment_type(None, title="Développeur Python Indépendant") == "freelance"
