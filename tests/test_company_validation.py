"""Regression tests for company validation and normalization.

Root cause that was fixed:
  normalize_job() was converting absent company to "" (empty string).
  validate_job() then rejected every job that had no company name, which
  caused repeated "Validation: Company is empty" spam in the pipeline.

Fix:
  - normalize_job() now produces company=None when the source omits the field.
  - validate_job() accepts company=None (legitimately unknown) but still rejects
    company="" (explicit empty string, which should not reach validation after the fix).
"""

from __future__ import annotations


from src.models.job import Job
from src.pipeline.normalize import normalize_job
from src.pipeline.validate import validate_job, validate_jobs


# ---------------------------------------------------------------------------
# normalize_job — company field
# ---------------------------------------------------------------------------

class TestNormalizeCompany:
    def test_company_present(self):
        raw = {"title": "Dev", "company": "Acme Corp", "description": "x"}
        job = normalize_job("test", raw)
        assert job.company == "Acme Corp"

    def test_company_absent_is_none(self):
        """When the source has no company field, normalize to None (not '')."""
        raw = {"title": "Dev", "description": "some description here"}
        job = normalize_job("test", raw)
        assert job.company is None

    def test_company_empty_string_is_none(self):
        """An empty string from the source is treated as absent."""
        raw = {"title": "Dev", "company": "", "description": "some description"}
        job = normalize_job("test", raw)
        assert job.company is None

    def test_company_whitespace_only_is_none(self):
        """A whitespace-only string from the source is treated as absent."""
        raw = {"title": "Dev", "company": "   ", "description": "some description"}
        job = normalize_job("test", raw)
        assert job.company is None

    def test_company_organization_fallback(self):
        """'organization' is used as fallback when 'company' is absent."""
        raw = {"title": "Dev", "organization": "Open Source Project", "description": "x"}
        job = normalize_job("test", raw)
        assert job.company == "Open Source Project"

    def test_company_strips_whitespace(self):
        raw = {"title": "Dev", "company": "  Trimmed Corp  ", "description": "x"}
        job = normalize_job("test", raw)
        assert job.company == "Trimmed Corp"


# ---------------------------------------------------------------------------
# validate_job — company field
# ---------------------------------------------------------------------------

class TestValidateCompany:
    def _make_job(self, **kwargs) -> Job:
        defaults = {
            "title": "Software Developer",
            "description": "This is a job description with enough detail.",
        }
        defaults.update(kwargs)
        return Job(**defaults)

    def test_company_present_passes(self):
        job = self._make_job(company="Acme Corp")
        result = validate_job(job)
        assert result.is_valid
        company_errors = [e for e in result.errors if "company" in e.lower()]
        assert not company_errors

    def test_company_none_passes(self):
        """company=None (unknown/anonymous posting) must pass validation."""
        job = self._make_job(company=None)
        result = validate_job(job)
        assert result.is_valid
        company_errors = [e for e in result.errors if "company" in e.lower()]
        assert not company_errors

    def test_company_explicit_empty_string_rejected(self):
        """company='' (explicit empty, should not reach here after normalization)
        is still rejected as a safeguard."""
        job = self._make_job(company="")
        result = validate_job(job)
        assert not result.is_valid
        company_errors = [e for e in result.errors if "company" in e.lower()]
        assert company_errors


# ---------------------------------------------------------------------------
# France Travail anonymous posting — end-to-end regression
# ---------------------------------------------------------------------------

class TestFranceTravailAnonymous:
    """Regression: France Travail anonymous postings must not produce
    'Validation: Company is empty' errors."""

    def test_anonymous_posting_passes_validation(self):
        """Simulate a France Travail anonymous posting where entreprise is absent."""
        from src.collectors.france_travail import FranceTravailCollector

        raw = {
            "id": "FT-12345",
            "intitule": "Développeur Python",
            "description": "Nous recherchons un développeur Python expérimenté.",
            # entreprise is absent — anonymous posting
            "lieuTravail": {"libelle": "75 - Paris"},
            "typeContrat": "CDI",
        }
        normalized = FranceTravailCollector._normalize_fields(raw)
        job = normalize_job("france_travail", normalized)
        assert job.company is None

        result = validate_job(job)
        company_errors = [e for e in result.errors if "company" in e.lower()]
        assert not company_errors, (
            f"Anonymous France Travail posting should not fail company validation. "
            f"Errors: {company_errors}"
        )

    def test_anonymous_posting_batch_produces_no_company_errors(self):
        """Batch validation of anonymous postings should not emit company errors."""
        from src.collectors.france_travail import FranceTravailCollector

        raw_jobs = [
            {
                "id": f"FT-{i}",
                "intitule": f"Poste {i}",
                "description": "Description détaillée du poste informatique.",
                "lieuTravail": {"libelle": "69 - Lyon"},
                "typeContrat": "CDI",
            }
            for i in range(5)
        ]

        jobs = [
            normalize_job("france_travail", FranceTravailCollector._normalize_fields(r))
            for r in raw_jobs
        ]
        valid, errors = validate_jobs(jobs)

        company_errors = [e for e in errors if "company" in e.lower()]
        assert not company_errors, (
            f"Anonymous France Travail postings should not emit company errors. "
            f"Got: {company_errors}"
        )
        # All 5 jobs should pass (they have title, description, and location)
        assert len(valid) == 5

    def test_named_company_posting_preserved(self):
        """A posting with an explicit company name must still surface it."""
        from src.collectors.france_travail import FranceTravailCollector

        raw = {
            "id": "FT-99999",
            "intitule": "Ingénieur Logiciel",
            "description": "Rejoignez notre équipe tech.",
            "entreprise": {"nom": "TechCorp France"},
            "lieuTravail": {"libelle": "31 - Toulouse"},
            "typeContrat": "CDI",
        }
        normalized = FranceTravailCollector._normalize_fields(raw)
        job = normalize_job("france_travail", normalized)
        assert job.company == "TechCorp France"

