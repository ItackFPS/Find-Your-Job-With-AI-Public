"""Profile adaptability tests — the engine must be genuinely profile-driven.

CRITICAL INVARIANT: The same generic engine code must produce different
rejection decisions when given different profile data.  No occupation-specific
branch, no domain-specific predicate, and no user-specific hardcoded value
may appear in the engine itself.

Tests use the same engine instance with materially different profiles and the
same vacancy evidence, asserting that:

1. Profile A (software developer, Bac+2, 3yr max, Python skills)
   behaves correctly for software developer vacancies.

2. Profile B (gardener, Bac+5 accepted, no programming language skills)
   behaves correctly for gardening and software vacancies.

3. Profile C (accountant, Bac+3 accepted, Excel skills)
   behaves correctly for accounting and software vacancies.

4. The SAME job description evaluated against different education limits
   produces different outcomes (profile data, not engine logic, drives it).

5. The same engine works for any domain — no occupation-specific branches.

Also verifies:
- Changing education limit in profile → different outcome, same engine.
- Changing skill list → different outcome, same engine.
- Changing max_years → different outcome, same engine.
- Changing work modes / contract types does not alter engine code.
"""
from __future__ import annotations


from src.models.job import Job
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate
from src.requirements.models import GateResult
from src.requirements.principal_skill import detect_missing_principal_skills


# ---------------------------------------------------------------------------
# Profile factory (no hardcoded user-specific logic in engine)
# ---------------------------------------------------------------------------

def _profile(
    target_skills: list[str],
    max_education_level: int | None,  # Bac+N integer
    max_years: int | None = None,
) -> dict:
    """Return a plain-dict profile for parametrising the engine."""
    return {
        "target_skills": target_skills,
        "max_education_level": max_education_level,
        "max_years": max_years,
    }


def _gate_for_profile(p: dict) -> HardConstraintGate:
    return HardConstraintGate(
        user_max_education_level=p["max_education_level"],
        user_max_years_experience=p["max_years"],
        principal_skill_gate=True,
        mandatory_skill_gate=True,
    )


def _extractor_for_profile(p: dict) -> RequirementExtractor:
    return RequirementExtractor(user_skills=p["target_skills"])


def _check_job_against_profile(
    title: str,
    description: str,
    profile: dict,
) -> GateResult:
    job = Job(id="j1", source="test", title=title, description=description)
    extractor = _extractor_for_profile(profile)
    reqs = extractor.extract(job)
    gate = _gate_for_profile(profile)
    return gate.check(reqs)


# ---------------------------------------------------------------------------
# Profiles under test
# ---------------------------------------------------------------------------

# Profile A: Software developer (current user profile)
_PROFILE_SOFTWARE = _profile(
    target_skills=["python", "javascript", "php", "MySQL", "Node.js",
                   "Git", "Linux", "HTML", "CSS", "Bash", "Docker"],
    max_education_level=2,   # Bac+2
    max_years=3,
)

# Profile B: Gardener (no programming, higher education)
_PROFILE_GARDENER = _profile(
    target_skills=["horticulture", "permaculture", "botanique", "jardinage", "taille"],
    max_education_level=5,   # Bac+5 accepted
    max_years=None,          # No experience limit
)

# Profile C: Accountant (spreadsheets, no programming)
_PROFILE_ACCOUNTANT = _profile(
    target_skills=["excel", "comptabilité", "sage", "quickbooks", "trésorerie", "bilan"],
    max_education_level=3,   # Bac+3 accepted
    max_years=5,
)


# ===========================================================================
# 1. Same engine, different profiles → different education outcomes
# ===========================================================================

class TestEducationProfileAdaptability:

    def test_bac5_mandatory_rejects_bac2_profile(self):
        """Bac+5 mandatory REJECTS Profile A (max=Bac+2)."""
        desc = "Bac+5 minimum en informatique requis."
        result = _check_job_against_profile("Dev", desc, _PROFILE_SOFTWARE)
        assert not result.passes, "Bac+5 must reject Bac+2 software profile"
        assert any(r.type == "education" for r in result.reasons)

    def test_bac5_mandatory_passes_bac5_profile(self):
        """Same Bac+5 mandatory PASSES Profile B (max=Bac+5)."""
        desc = "Bac+5 minimum en informatique requis."
        result = _check_job_against_profile("Dev", desc, _PROFILE_GARDENER)
        assert result.passes, "Bac+5 must pass Bac+5 gardener profile (education satisfied)"

    def test_bac3_mandatory_rejects_bac2_profile(self):
        """Bac+3 mandatory REJECTS Profile A (max=Bac+2)."""
        desc = "Licence professionnelle requise."
        result = _check_job_against_profile("Dev", desc, _PROFILE_SOFTWARE)
        assert not result.passes

    def test_bac3_mandatory_passes_bac3_profile(self):
        """Same Bac+3 mandatory PASSES Profile C (max=Bac+3)."""
        desc = "Licence professionnelle requise."
        result = _check_job_against_profile("Comptable", desc, _PROFILE_ACCOUNTANT)
        assert result.passes

    def test_bac5_mandatory_rejects_bac3_profile(self):
        """Bac+5 mandatory REJECTS Profile C (max=Bac+3)."""
        desc = "Master requis."
        result = _check_job_against_profile("Comptable senior", desc, _PROFILE_ACCOUNTANT)
        assert not result.passes

    def test_no_education_mentioned_passes_all_profiles(self):
        """No education mention → passes every profile (UNKNOWN → no rejection)."""
        desc = "Rejoignez notre équipe dynamique."
        for profile in (_PROFILE_SOFTWARE, _PROFILE_GARDENER, _PROFILE_ACCOUNTANT):
            result = _check_job_against_profile("Any", desc, profile)
            assert result.passes, "No education mention must pass for all profiles"

    def test_same_engine_code_for_all_profiles(self):
        """The engine applies the same generic logic; only profile data changes."""
        # All three profiles use exactly the same HardConstraintGate class.
        # Verify there are no occupation-specific code paths by confirming that
        # altering ONLY the profile dict changes the outcome.
        desc = "Master requis."
        result_a = _check_job_against_profile("Dev", desc, _PROFILE_SOFTWARE)
        result_b = _check_job_against_profile("Dev", desc, _PROFILE_GARDENER)
        # Profile A rejects (max=Bac+2 < master=Bac+5)
        # Profile B passes (max=Bac+5 ≥ master=Bac+5)
        assert not result_a.passes
        assert result_b.passes
        # The gate classes themselves are generic — their __class__ is the same.
        gate_a = _gate_for_profile(_PROFILE_SOFTWARE)
        gate_b = _gate_for_profile(_PROFILE_GARDENER)
        assert gate_a.__class__ is gate_b.__class__, "Same engine class must be used for all profiles"


# ===========================================================================
# 2. Same engine, different skill profiles → different principal-skill outcomes
# ===========================================================================

class TestSkillProfileAdaptability:

    def test_rust_job_rejects_software_profile_without_rust(self):
        """Rust Developer title REJECTS a profile without Rust."""
        result = _check_job_against_profile("Rust Developer", "", _PROFILE_SOFTWARE)
        assert not result.passes, "Rust not in software profile → reject"

    def test_rust_job_passes_rust_profile(self):
        """Same Rust title passes when profile includes Rust."""
        rust_profile = _profile(
            target_skills=["rust", "python", "linux"],
            max_education_level=None,
        )
        result = _check_job_against_profile("Rust Developer", "", rust_profile)
        assert result.passes, "Rust in profile → pass"

    def test_java_job_rejects_software_profile(self):
        """Java Developer title REJECTS the current software profile (no Java)."""
        result = _check_job_against_profile("Java Developer", "", _PROFILE_SOFTWARE)
        assert not result.passes

    def test_java_job_passes_java_profile(self):
        """Same Java title passes when profile includes Java."""
        java_profile = _profile(
            target_skills=["java", "spring", "maven"],
            max_education_level=None,
        )
        result = _check_job_against_profile("Java Developer", "", java_profile)
        assert result.passes

    def test_gardener_job_no_tech_token_passes_any_profile(self):
        """'Jardinier paysagiste' has no programming tech token — passes all profiles."""
        for profile in (_PROFILE_SOFTWARE, _PROFILE_GARDENER, _PROFILE_ACCOUNTANT):
            mismatches = detect_missing_principal_skills(
                "Jardinier paysagiste", profile["target_skills"]
            )
            assert mismatches == [], (
                f"Gardening title must not trigger tech mismatch for any profile: {mismatches}"
            )

    def test_python_developer_passes_software_profile(self):
        """Python Developer passes the software profile (Python in profile)."""
        result = _check_job_against_profile("Python Developer", "", _PROFILE_SOFTWARE)
        assert result.passes

    def test_python_developer_passes_all_profiles(self):
        """Python Developer passes all profiles.

        Python is NOT in _CORE_LANGUAGES (it is a common language, not specialized).
        The hard gate only rejects on languages in _CORE_LANGUAGES (Rust, Java, Go, etc.).
        Domain filtering (gardening vs software) happens in filter.py, not the hard gate.
        """
        for profile in (_PROFILE_SOFTWARE, _PROFILE_GARDENER, _PROFILE_ACCOUNTANT):
            result = _check_job_against_profile("Python Developer", "", profile)
            assert result.passes, (
                "Python Developer should pass at the hard gate for any profile "
                "(Python is not in _CORE_LANGUAGES; domain filtering is done in filter.py)"
            )

    def test_java_developer_rejects_non_java_profiles(self):
        """Java Developer rejects profiles without Java (Java IS in _CORE_LANGUAGES)."""
        for profile in (_PROFILE_GARDENER, _PROFILE_ACCOUNTANT):
            result = _check_job_against_profile("Java Developer", "", profile)
            assert not result.passes, (
                "Java Developer should be rejected when Java is not in the profile"
            )

    def test_generic_backend_title_passes_gardener_profile(self):
        """'Backend Engineer' with no specific tech token passes all profiles."""
        for profile in (_PROFILE_SOFTWARE, _PROFILE_GARDENER, _PROFILE_ACCOUNTANT):
            mismatches = detect_missing_principal_skills(
                "Backend Engineer", profile["target_skills"]
            )
            assert mismatches == [], "Generic title must not trigger principal-skill rejection"


# ===========================================================================
# 3. Experience limit adaptability
# ===========================================================================

class TestExperienceProfileAdaptability:

    def test_5yr_mandatory_rejects_3yr_profile(self):
        """Minimum 5 years mandatory → hard rejection for 3-year-max profile."""
        job = Job(id="j1", source="test", title="Dev",
                  description="Au moins 5 ans d'expérience professionnelle.")
        job.raw_payload = {}
        extractor = _extractor_for_profile(_PROFILE_SOFTWARE)
        reqs = extractor.extract(job)
        gate = _gate_for_profile(_PROFILE_SOFTWARE)  # max_years=3
        result = gate.check(reqs)
        # MANDATORY experience > profile max → hard gate rejection
        assert not result.passes, "MANDATORY experience > max must be a hard gate rejection"
        assert any(r.type == "experience" for r in result.reasons)

    def test_5yr_mandatory_passes_no_limit_profile(self):
        """Same minimum 5 years passes a profile with no max_years limit."""
        job = Job(id="j1", source="test", title="Dev",
                  description="Au moins 5 ans d'expérience professionnelle.")
        job.raw_payload = {}
        extractor = _extractor_for_profile(_PROFILE_GARDENER)  # max_years=None
        reqs = extractor.extract(job)
        gate = _gate_for_profile(_PROFILE_GARDENER)  # max_years=None
        result = gate.check(reqs)
        assert result.passes

    def test_2yr_mandatory_passes_3yr_profile(self):
        """Minimum 2 years passes a 3-year-max profile (within range)."""
        job = Job(id="j1", source="test", title="Dev",
                  description="Au moins 2 ans d'expérience.")
        job.raw_payload = {}
        extractor = _extractor_for_profile(_PROFILE_SOFTWARE)
        reqs = extractor.extract(job)
        gate = _gate_for_profile(_PROFILE_SOFTWARE)
        result = gate.check(reqs)
        assert result.passes


# ===========================================================================
# 4. Engine has no hardcoded domain branches
# ===========================================================================

class TestNoDomainSpecificBranches:
    """Verify the engine contains no occupation/domain-specific code paths."""

    def test_changing_target_skills_changes_outcome_without_code_change(self):
        """The same vacancy title produces different outcomes by changing profile data only."""
        title = "Java Developer"
        desc = ""

        # Without Java in skills → reject
        profile_no_java = _profile(["python", "docker"], max_education_level=None)
        result_no_java = _check_job_against_profile(title, desc, profile_no_java)

        # With Java in skills → pass
        profile_has_java = _profile(["java", "spring"], max_education_level=None)
        result_has_java = _check_job_against_profile(title, desc, profile_has_java)

        assert not result_no_java.passes
        assert result_has_java.passes

    def test_changing_education_limit_changes_outcome_without_code_change(self):
        """Same Bac+5 requirement; only profile education limit changes outcome."""
        desc = "Master requis."

        profile_bac2 = _profile([], max_education_level=2)
        profile_bac5 = _profile([], max_education_level=5)

        result_bac2 = _check_job_against_profile("Dev", desc, profile_bac2)
        result_bac5 = _check_job_against_profile("Dev", desc, profile_bac5)

        assert not result_bac2.passes, "Bac+2 profile must be rejected by Master requirement"
        assert result_bac5.passes, "Bac+5 profile must pass with Master requirement"

    def test_engine_class_is_same_for_all_domains(self):
        """All profiles use the same HardConstraintGate class — no subclass per domain."""
        gates = [
            _gate_for_profile(_PROFILE_SOFTWARE),
            _gate_for_profile(_PROFILE_GARDENER),
            _gate_for_profile(_PROFILE_ACCOUNTANT),
        ]
        class_set = {g.__class__.__name__ for g in gates}
        assert class_set == {"HardConstraintGate"}, (
            "All domains must use the same engine class"
        )

    def test_no_occupation_name_in_rejection_reasons(self):
        """Rejection reasons must not contain domain-specific terms like 'software'."""
        desc = "Master requis."
        result = _check_job_against_profile("Dev", desc, _PROFILE_SOFTWARE)
        assert not result.passes
        for reason in result.reasons:
            message_lower = reason.message.lower()
            assert "software" not in message_lower, (
                f"Rejection reason must not mention domain 'software': {reason.message}"
            )
            assert "gardening" not in message_lower
            assert "accounting" not in message_lower

    def test_rejection_reason_mentions_only_generic_values(self):
        """Rejection reason message contains only the extracted requirement and profile value."""
        desc = "Bac+5 requis."
        result = _check_job_against_profile("Dev", desc, _PROFILE_SOFTWARE)
        assert not result.passes
        reason = next(r for r in result.reasons if r.type == "education")
        # Message should mention the level labels, not domain names
        assert "bac+5" in reason.requirement.lower() or "master" in reason.requirement.lower() or "bac" in reason.requirement.lower()
        assert "bac+2" in reason.candidate_value.lower()
