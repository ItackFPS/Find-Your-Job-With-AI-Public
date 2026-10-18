"""Tests for the job explanation generator.

Covers:
- Accepted jobs: positive reasons and concerns
- Rejected jobs: specific rejection reasons
- Unknown data is not treated as negative
- Language detection
- Seniority detection
- Remote/location logic
- Concerns are separate from blocking reasons
- Prima Junior Software Engineer regression test
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.models.job import Job
from src.models.profile import ExperiencePreferences, GeographyPreferences, JobSearchProfile
from src.pipeline.explain import generate_explanation


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def profile() -> JobSearchProfile:
    """A representative junior developer search profile."""
    return JobSearchProfile(
        target_domains=["software_development", "qa_testing", "data"],
        target_skills=["python", "javascript"],
        geography=GeographyPreferences(
            preferred_countries=["France"],
            allowed_regions=["Europe"],
        ),
        work_modes=["remote", "hybrid", "on_site"],
        contract_types=["cdi", "cdd", "internship", "alternance"],
        experience=ExperiencePreferences(
            preferred_levels=["junior", "entry_level", "internship"],
            max_years=3,
            excluded_seniority=["senior", "director", "vp", "c_suite"],
        ),
        languages=["French", "English"],
        freshness_days=30,
    )


@pytest.fixture
def domain_keywords() -> dict:
    """Minimal domain keyword vocabulary for testing."""
    return {
        "software_development": {
            "title": [
                "software engineer", "software developer", "developer",
                "développeur", "ingénieur logiciel",
            ],
            "description": ["software development", "python", "django"],
        },
        "qa_testing": {
            "title": ["qa engineer", "test engineer", "quality assurance"],
            "description": ["testing", "automation", "selenium"],
        },
        "data": {
            "title": ["data engineer", "data analyst", "data scientist"],
            "description": ["data pipeline", "pandas", "spark"],
        },
    }


def make_job(**kwargs) -> Job:
    defaults = dict(
        id="test-job-001",
        source="test",
        title="Software Engineer",
        company="Acme",
        description="We build software.",
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        discovered_at=datetime.now(UTC),
    )
    defaults.update(kwargs)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# Accepted jobs — positive reasons
# ---------------------------------------------------------------------------

class TestAcceptedJobReasons:
    def test_accepted_has_positive_reasons(self, profile, domain_keywords):
        job = make_job(title="Junior Software Engineer", remote=True, countries=["France"])
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert result.positive_reasons, "Accepted job should have at least one positive reason"

    def test_accepted_no_rejection_text(self, profile, domain_keywords):
        job = make_job(title="Junior Developer", remote=True, countries=["France"])
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons

    def test_role_match_in_positive(self, profile, domain_keywords):
        job = make_job(title="Junior Software Engineer")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("Software" in t or "software" in t for t in texts)

    def test_junior_in_positive(self, profile, domain_keywords):
        job = make_job(title="Junior Developer")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("junior" in t.lower() or "Junior" in t for t in texts)

    def test_remote_in_positive(self, profile, domain_keywords):
        job = make_job(remote=True)
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("remote" in t.lower() for t in texts)

    def test_france_in_positive(self, profile, domain_keywords):
        job = make_job(countries=["France"], remote=False)
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("France" in t for t in texts)

    def test_english_language_detected(self, profile, domain_keywords):
        job = make_job(title="Junior Software Engineer", source="arbeitnow")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("English" in t for t in texts)

    def test_python_skill_in_positive(self, profile, domain_keywords):
        job = make_job(description="We use Python extensively for data pipelines.")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("python" in t.lower() for t in texts)

    def test_emojis_present(self, profile, domain_keywords):
        job = make_job()
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        for r in result.positive_reasons:
            assert r.emoji, "Each reason must have an emoji"
            assert r.kind == "positive"


# ---------------------------------------------------------------------------
# Accepted jobs — concerns
# ---------------------------------------------------------------------------

class TestAcceptedJobConcerns:
    def test_unknown_country_is_concern_not_rejection(self, profile, domain_keywords):
        job = make_job(countries=["United Kingdom"], remote=False)
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons
        concern_texts = [r.text for r in result.concerns]
        assert any("UK" in t or "United Kingdom" in t for t in concern_texts)

    def test_not_remote_concern_when_profile_prefers_remote(self, profile, domain_keywords):
        # Non-French non-remote job: concern expected
        job = make_job(remote=False, countries=["Germany"])
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        concern_texts = [r.text.lower() for r in result.concerns]
        assert any("remote" in t for t in concern_texts)

    def test_no_remote_concern_for_french_onsite_job(self, profile, domain_keywords):
        """French on-site jobs must NOT generate a 'remote' concern.
        France is a preferred country; on-site there is geographically valid."""
        job = make_job(remote=False, countries=["France"])
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        concern_texts = [r.text.lower() for r in result.concerns]
        assert not any("remote" in t for t in concern_texts), (
            "French on-site jobs should not generate a 'verify remote' concern"
        )

    def test_concerns_have_correct_kind(self, profile, domain_keywords):
        job = make_job(remote=False, countries=["United Kingdom"])
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        for r in result.concerns:
            assert r.kind == "concern"

    def test_unknown_seniority_is_concern_not_rejection(self, profile, domain_keywords):
        job = make_job(title="Software Engineer")  # no 'junior' keyword
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        # Should not be rejected; may have a concern about unclear experience level
        assert not result.rejection_reasons


# ---------------------------------------------------------------------------
# Rejected jobs
# ---------------------------------------------------------------------------

class TestRejectedJobs:
    def test_rejected_has_rejection_reasons(self, profile, domain_keywords):
        job = make_job(location="Berlin, Germany", countries=["Germany"])
        result = generate_explanation(job, profile, "rejected", rejection_detail="location")
        assert result.rejection_reasons

    def test_seniority_rejection_has_no_positive_reasons(self, profile, domain_keywords):
        """Non-location rejections do not append positive highlights."""
        job = make_job(title="Senior Software Engineer")
        result = generate_explanation(job, profile, "rejected", rejection_detail="seniority")
        assert not result.positive_reasons

    def test_location_rejection_may_have_positive_highlights(self, profile, domain_keywords):
        """Location rejections surface matching signals as positive_reasons so the
        user understands the job was otherwise relevant."""
        job = make_job(
            location="London", countries=["United Kingdom"], remote=False,
            title="Junior Software Engineer",
        )
        result = generate_explanation(job, profile, "rejected", rejection_detail="location")
        assert result.rejection_reasons  # blocking reason present
        # positive_reasons may or may not be populated depending on signals found

    def test_location_rejection_mentions_country(self, profile, domain_keywords):
        job = make_job(countries=["Germany"])
        result = generate_explanation(job, profile, "rejected", rejection_detail="location")
        texts = [r.text for r in result.rejection_reasons]
        assert any("Germany" in t for t in texts)

    def test_seniority_rejection_mentions_seniority(self, profile, domain_keywords):
        job = make_job(title="Senior Software Engineer")
        result = generate_explanation(job, profile, "rejected", rejection_detail="seniority")
        texts = [r.text for r in result.rejection_reasons]
        assert any("senior" in t.lower() or "Senior" in t for t in texts)

    def test_title_keywords_rejection(self, profile, domain_keywords):
        job = make_job(title="Electrician")
        result = generate_explanation(job, profile, "rejected", rejection_detail="title_keywords")
        texts = [r.text for r in result.rejection_reasons]
        assert any("domain" in t.lower() or "target" in t.lower() for t in texts)

    def test_published_age_rejection(self, profile, domain_keywords):
        job = make_job()
        result = generate_explanation(job, profile, "rejected", rejection_detail="published_age")
        texts = [r.text for r in result.rejection_reasons]
        assert any("days" in t for t in texts)

    def test_rejection_reasons_have_red_emoji(self, profile, domain_keywords):
        job = make_job()
        result = generate_explanation(job, profile, "rejected", rejection_detail="location")
        for r in result.rejection_reasons:
            assert r.emoji == "🔴"
            assert r.kind == "rejection"

    def test_unknown_rejection_detail_does_not_crash(self, profile, domain_keywords):
        job = make_job()
        result = generate_explanation(job, profile, "rejected", rejection_detail="unknown_check")
        assert result.rejection_reasons

    def test_none_rejection_detail_handled(self, profile, domain_keywords):
        job = make_job()
        result = generate_explanation(job, profile, "rejected", rejection_detail=None)
        assert result.rejection_reasons


# ---------------------------------------------------------------------------
# Phase NOW / I2: requirements-gate structured evidence
#
# The requirements gate (src/requirements/gate.py) stores its rejection
# reasons as a JSON string via rejection_reasons_to_detail() — a list of
# HardRejectionReason.to_dict() dicts, e.g.:
#   [{"type": "education", "requirement": "Bac+5", "candidate_value": "bac+2",
#     "message": "Job requires Bac+5 (mandatory); candidate maximum is bac+2.",
#     "constraint_state": "violated", ...}]
# Before this fix, this JSON string fell through to the same catch-all as a
# genuinely unrecognized category and was shown to the user verbatim
# (f"Did not pass filter: {rejection_detail}") — a raw JSON dump.
# ---------------------------------------------------------------------------

def _gate_reason(
    message: str,
    *,
    type_: str = "education",
    span_text: str | None = None,
) -> dict:
    d = {
        "type": type_,
        "requirement": "Bac+5",
        "candidate_value": "bac+2",
        "severity": "hard",
        "message": message,
        "constraint_state": "violated",
        "rule_version": "hard-filter.v2",
        "evidence_source": "heuristic",
    }
    if span_text is not None:
        d["span"] = {"text": span_text, "start": 10, "end": 20, "field": "description", "extractor_id": "x"}
    return d


class TestGateEvidenceRejections:
    def test_normal_structured_gate_evidence_renders_message(self, profile):
        job = make_job()
        detail = json.dumps([_gate_reason(
            "Job requires Bac+5 (mandatory); candidate maximum is bac+2."
        )])
        result = generate_explanation(job, profile, "rejected", rejection_detail=detail)
        assert len(result.rejection_reasons) == 1
        r = result.rejection_reasons[0]
        assert r.text == "Job requires Bac+5 (mandatory); candidate maximum is bac+2."
        assert r.emoji == "🔴"
        assert r.kind == "rejection"
        # No raw JSON syntax leaked into the user-facing text.
        assert "{" not in r.text and "constraint_state" not in r.text

    def test_evidence_span_quoted_as_supporting_text(self, profile):
        job = make_job()
        detail = json.dumps([_gate_reason(
            "Job title centers on 'Rust' which is not in the candidate's profile.",
            type_="principal_skill",
            span_text="notre stack repose principalement sur Rust",
        )])
        result = generate_explanation(job, profile, "rejected", rejection_detail=detail)
        assert len(result.rejection_reasons) == 1
        text = result.rejection_reasons[0].text
        assert "Rust" in text
        assert "notre stack repose principalement sur Rust" in text

    def test_multiple_evidence_items_all_rendered_in_order(self, profile):
        job = make_job()
        detail = json.dumps([
            _gate_reason("Job requires Bac+5 (mandatory); candidate maximum is bac+2.", type_="education"),
            _gate_reason("Job title centers on 'Rust' which is not in the candidate's profile.", type_="principal_skill"),
        ])
        result = generate_explanation(job, profile, "rejected", rejection_detail=detail)
        assert len(result.rejection_reasons) == 2
        assert "Bac+5" in result.rejection_reasons[0].text
        assert "Rust" in result.rejection_reasons[1].text

    def test_missing_optional_fields_still_renders_from_message(self, profile):
        """A reason dict with only 'message' (no span, no candidate_value) —
        the schema's only truly required field — must still render cleanly."""
        job = make_job()
        detail = json.dumps([{"type": "experience", "message": "Requires 5+ years experience."}])
        result = generate_explanation(job, profile, "rejected", rejection_detail=detail)
        assert len(result.rejection_reasons) == 1
        assert result.rejection_reasons[0].text == "Requires 5+ years experience."

    def test_malformed_json_never_leaks_raw_text(self, profile):
        """Truncated/invalid JSON that still LOOKS like gate evidence (starts
        with '[') must fall back to a safe, generic sentence — never the raw
        broken JSON string."""
        job = make_job()
        broken = '[{"type": "education", "message": "Job requires Bac'  # truncated
        result = generate_explanation(job, profile, "rejected", rejection_detail=broken)
        assert len(result.rejection_reasons) == 1
        text = result.rejection_reasons[0].text
        assert "{" not in text
        assert '"type"' not in text
        assert text == "Did not meet one or more of your configured requirements."

    def test_empty_valid_evidence_list_never_leaks_raw_text(self, profile):
        """Valid JSON array, but no item has a usable message (e.g. every
        item is missing 'message' or it's blank) — same safe fallback as
        malformed JSON, not a raw dump of the parsed structure."""
        job = make_job()
        detail = json.dumps([{"type": "education", "message": ""}, {"type": "experience"}])
        result = generate_explanation(job, profile, "rejected", rejection_detail=detail)
        assert len(result.rejection_reasons) == 1
        assert result.rejection_reasons[0].text == "Did not meet one or more of your configured requirements."

    def test_legacy_plain_string_categories_unaffected(self, profile):
        """Preserve existing explanations for older rejection categories —
        a plain string (not JSON-shaped) must take the exact same path as
        before this change."""
        job = make_job(countries=["Germany"], remote=False)
        result = generate_explanation(job, profile, "rejected", rejection_detail="location")
        assert len(result.rejection_reasons) == 1
        assert "Germany" in result.rejection_reasons[0].text
        assert result.rejection_reasons[0].text != "Did not meet one or more of your configured requirements."

    def test_unknown_non_json_rejection_format_keeps_legacy_fallback(self, profile):
        """A plain-string category with no dedicated branch (e.g.
        'contract_type', or any future one) must keep today's exact
        catch-all behavior — only the JSON-shaped path changed."""
        job = make_job()
        result = generate_explanation(job, profile, "rejected", rejection_detail="contract_type")
        assert len(result.rejection_reasons) == 1
        assert result.rejection_reasons[0].text == "Did not pass filter: contract_type"


# ---------------------------------------------------------------------------
# Unknown data is not treated as negative
# ---------------------------------------------------------------------------

class TestUnknownDataNotNegative:
    def test_unknown_remote_not_rejection(self, profile, domain_keywords):
        job = make_job(remote=False)  # remote unknown (not marked True) but accepted
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        # Should be accepted, maybe with a concern — but not rejected
        assert not result.rejection_reasons

    def test_no_countries_not_rejection(self, profile, domain_keywords):
        job = make_job(countries=[])  # no location info
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons

    def test_no_seniority_keyword_not_rejection(self, profile, domain_keywords):
        job = make_job(title="Software Developer")  # no 'junior' or 'senior' keyword
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons

    def test_missing_salary_not_rejection(self, profile, domain_keywords):
        job = make_job()  # no salary info
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons


# ---------------------------------------------------------------------------
# French job descriptions
# ---------------------------------------------------------------------------

class TestFrenchJobMatching:
    def test_french_source_detected_as_french(self, profile, domain_keywords):
        job = make_job(source="france-travail", title="Développeur Logiciel")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("French" in t for t in texts)

    def test_french_title_accepted(self, profile, domain_keywords):
        job = make_job(title="Développeur logiciel junior")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons

    def test_french_title_role_match(self, profile, domain_keywords):
        job = make_job(title="Ingénieur logiciel junior")
        result = generate_explanation(job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        # Should match software_development domain
        assert any("Software" in t or "software" in t or "logiciel" in t.lower() for t in texts)


# ---------------------------------------------------------------------------
# Prima Junior Software Engineer regression test
# ---------------------------------------------------------------------------

class TestPrimaRegression:
    """Regression test based on the Prima Junior Software Engineer job.

    The job has:
    - Title: "Junior Software Engineer"
    - No explicit years-of-experience requirement
    - Python, pandas, scikit-learn, mlflow as nice-to-haves
    - Work from home / office / mix (remote-friendly)
    - No stated language requirement in description
    - Data pipelines and ML-driven services mentioned
    """

    PRIMA_JOB_DESCRIPTION = """
    Junior Software Engineer
    Software Engineering team.
    We are looking for someone with solid programming foundations and a passion for software development.

    What you will work on:
    - Building and maintaining data pipelines
    - Contributing to ML-driven services
    - Working with Python, pandas, scikit-learn, mlflow (nice to have)
    - Spark and Airflow experience a plus

    Work setup:
    - Work from home, office, or a mix — up to 30 days working from anywhere
    - No explicit years-of-experience requirement stated
    """

    @pytest.fixture
    def prima_job(self):
        return Job(
            id="prima-job-001",
            source="test",
            title="Junior Software Engineer",
            company="Prima",
            description=self.PRIMA_JOB_DESCRIPTION,
            location="Remote",
            countries=[],   # No explicit country
            remote=True,
            employment_type="full-time",
            discovered_at=datetime.now(UTC),
        )

    def test_prima_accepted_has_positive_reasons(self, prima_job, profile, domain_keywords):
        result = generate_explanation(prima_job, profile, "passed", domain_keywords=domain_keywords)
        assert result.positive_reasons, "Prima job should produce positive reasons when accepted"
        assert not result.rejection_reasons

    def test_prima_rejected_has_rejection_reasons(self, prima_job, profile, domain_keywords):
        result = generate_explanation(prima_job, profile, "rejected",
                                       rejection_detail="location", domain_keywords=domain_keywords)
        assert result.rejection_reasons
        # Location rejections may include positive highlights (seniority, language, skills)
        # to show the user the job was otherwise relevant.

    def test_prima_junior_detected(self, prima_job, profile, domain_keywords):
        result = generate_explanation(prima_job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("junior" in t.lower() or "Junior" in t for t in texts)

    def test_prima_remote_in_positive(self, prima_job, profile, domain_keywords):
        result = generate_explanation(prima_job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("remote" in t.lower() for t in texts)

    def test_prima_python_detected(self, prima_job, profile, domain_keywords):
        result = generate_explanation(prima_job, profile, "passed", domain_keywords=domain_keywords)
        texts = [r.text for r in result.positive_reasons]
        assert any("python" in t.lower() for t in texts)

    def test_prima_no_experience_not_treated_as_negative(self, prima_job, profile, domain_keywords):
        # No stated years of experience should not generate a rejection reason
        result = generate_explanation(prima_job, profile, "passed", domain_keywords=domain_keywords)
        assert not result.rejection_reasons

    def test_prima_decision_is_consistent(self, prima_job, profile, domain_keywords):
        """Accepted Prima job should have positive reasons; rejected should have rejection reasons."""
        accepted = generate_explanation(prima_job, profile, "passed", domain_keywords=domain_keywords)
        rejected = generate_explanation(prima_job, profile, "rejected",
                                         rejection_detail="title_keywords", domain_keywords=domain_keywords)

        assert accepted.positive_reasons
        assert not accepted.rejection_reasons
        assert rejected.rejection_reasons
        assert not rejected.positive_reasons


