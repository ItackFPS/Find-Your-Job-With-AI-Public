"""Benchmark test suite for the requirements engine.

Covers the 15 representative cases from the architecture specification plus
additional edge cases discovered during implementation.

Each test is deliberately independent — no shared fixtures that could mask
real failures.  The tests exercise the full gate pipeline: extraction →
hard constraint check → gate result.

Candidate profile used throughout:
    max_education: bac+2
    skills: python, javascript, php, mysql, node.js, git, linux, html, css,
            bash, docker, n8n, ai, ia
"""

from __future__ import annotations

import pytest

from src.requirements.education import extract_education, parse_education_level
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import GateResult, Importance
from src.requirements.principal_skill import detect_missing_principal_skills

# ---------------------------------------------------------------------------
# Candidate profile constants
# ---------------------------------------------------------------------------

_CANDIDATE_MAX_EDU = 2       # Bac+2
_CANDIDATE_SKILLS = [
    "python", "javascript", "php", "MySQL", "Node.js",
    "Git", "Linux", "HTML", "CSS", "Bash", "Docker",
    "n8n", "AI", "IA",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_gate(
    principal_gate: bool = True,
    mandatory_gate: bool = True,
) -> HardConstraintGate:
    return HardConstraintGate(
        user_max_education_level=_CANDIDATE_MAX_EDU,
        principal_skill_gate=principal_gate,
        mandatory_skill_gate=mandatory_gate,
    )


def _make_job(title: str, description: str = "", job_id: str = "j1"):
    from src.models.job import Job
    return Job(id=job_id, source="test", title=title, description=description)


def _extract_and_gate(
    title: str,
    description: str = "",
    job_id: str = "j1",
) -> GateResult:
    job = _make_job(title, description, job_id)
    extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS)
    reqs = extractor.extract(job)
    gate = _make_gate()
    return gate.check(reqs)


# ===========================================================================
# Case 1: Bac+5 required / candidate Bac+2 → HARD REJECT
# ===========================================================================

class TestCase01_Bac5Required:
    """Mandatory Bac+5 must hard-reject a Bac+2 candidate."""

    def test_french_minimum_notation(self):
        desc = "Formation : Bac+5 minimum en informatique."
        result = _extract_and_gate("Software Engineer", desc)
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)

    def test_french_requis(self):
        desc = "Diplôme : Bac+5 requis (Master ou école d'ingénieur)."
        result = _extract_and_gate("Backend Developer", desc)
        assert not result.passes

    def test_english_master_required(self):
        desc = "Requirements: Master's degree required in Computer Science."
        result = _extract_and_gate("Software Engineer", desc)
        assert not result.passes

    def test_master_in_title_context(self):
        desc = "Profil : Bac+5 en informatique (obligatoire)."
        result = _extract_and_gate("Développeur Python", desc)
        assert not result.passes

    def test_rejection_reason_fields(self):
        desc = "Formation Bac+5 minimum."
        result = _extract_and_gate("Developer", desc)
        assert not result.passes
        edu_reasons = [r for r in result.reasons if r.type == "education"]
        assert edu_reasons
        r = edu_reasons[0]
        assert r.severity == "hard"
        assert "bac+2" in r.candidate_value.lower()


# ===========================================================================
# Case 2: Bac+5 preferred / candidate Bac+2 → NOT a hard reject
# ===========================================================================

class TestCase02_Bac5Preferred:
    """A desirable Bac+5 above the candidate's max is a wish, not a ceiling
    (ADR 0004, amended 2026-10): it passes, with the soft penalty applied by
    gate_jobs. Only a level stated as the bar ("Bac+5 ou équivalent") or a
    mandatory one hard-rejects — see test_education_requirement_strength.py."""

    def test_souhaite(self):
        desc = "Formation Bac+5 souhaitée en informatique."
        result = _extract_and_gate("Developer", desc)
        assert result.passes, f"Should pass but got: {result}"

    def test_idealement(self):
        desc = "Idéalement un Master ou équivalent."
        result = _extract_and_gate("Backend Engineer", desc)
        assert result.passes

    def test_de_preference(self):
        desc = "De préférence Bac+5 en informatique."
        result = _extract_and_gate("Software Engineer", desc)
        assert result.passes

    def test_education_penalty_injected_when_toggle_off(self):
        """With education_hard_constraint disabled, a PREFERRED mismatch
        falls back to the existing soft-penalty injection (unchanged)."""
        from src.models.job import Job
        job = Job(id="j2", source="test", title="Developer",
                  description="Formation Bac+5 souhaitée.")
        extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS)
        reqs = extractor.extract(job)
        gate = HardConstraintGate(
            user_max_education_level=_CANDIDATE_MAX_EDU,
            education_hard_constraint=False,
        )
        passing, gate_results = gate_jobs([job], {"j2": reqs}, gate, education_preferred_penalty=0.20)
        assert len(passing) == 1
        assert gate_results["j2"].education_penalty == pytest.approx(0.20)


# ===========================================================================
# Case 3: Rust mandatory / Rust unknown → REJECT
# ===========================================================================

class TestCase03_RustMandatoryReject:
    """Jobs centered on Rust must be rejected when Rust is not in the profile."""

    def test_developpeur_rust_title(self):
        result = _extract_and_gate("Développeur Rust")
        assert not result.passes
        assert any(r.type == "principal_skill" for r in result.reasons)

    def test_rust_backend_engineer_title(self):
        result = _extract_and_gate("Rust Backend Engineer")
        assert not result.passes

    def test_rust_ingenieur_title(self):
        result = _extract_and_gate("Ingénieur Rust")
        assert not result.passes

    def test_rust_developer_english(self):
        result = _extract_and_gate("Rust Developer")
        assert not result.passes

    def test_rejection_reason_is_principal_skill(self):
        result = _extract_and_gate("Rust Developer")
        principal_reasons = [r for r in result.reasons if r.type == "principal_skill"]
        assert principal_reasons
        assert "rust" in principal_reasons[0].requirement.lower()


# ===========================================================================
# Case 4: Rust preferred / Rust unknown → score penalty, NO hard reject
# ===========================================================================

class TestCase04_RustPreferredNoReject:
    """Rust as a nice-to-have in description must NOT cause hard rejection."""

    def test_rust_nice_to_have_in_description(self):
        desc = (
            "Vous développerez des applications Python backend. "
            "Une connaissance de Rust serait un plus."
        )
        result = _extract_and_gate("Backend Developer Python", desc)
        assert result.passes, f"Should pass but rejected: {result.reasons}"

    def test_rust_optional_in_description(self):
        desc = "Tech stack: Python, Django, PostgreSQL. Rust (optionnel)."
        result = _extract_and_gate("Python Developer", desc)
        assert result.passes

    def test_rust_bonus_in_description(self):
        desc = "Skills: Python required. Rust: bonus."
        result = _extract_and_gate("Software Engineer", desc)
        assert result.passes


# ===========================================================================
# Case 5: Python mandatory / Python known → PASS
# ===========================================================================

class TestCase05_PythonMandatoryPass:
    """Python as mandatory skill must not cause rejection (user knows Python)."""

    def test_python_developer_title(self):
        result = _extract_and_gate("Python Developer")
        assert result.passes

    def test_python_required_in_description(self):
        desc = "Python est requis. Expérience en Django appréciée."
        result = _extract_and_gate("Backend Engineer", desc)
        assert result.passes

    def test_python_ingenieur_title(self):
        result = _extract_and_gate("Ingénieur Python")
        assert result.passes


# ===========================================================================
# Case 6: Python + optional Docker / Docker unknown concern → PASS
# ===========================================================================

class TestCase06_PythonPlusOptionalDocker:
    """Optional Docker should not cause rejection (and user has Docker anyway)."""

    def test_python_docker_optional(self):
        desc = "Python requis. Docker serait un plus."
        result = _extract_and_gate("Backend Developer", desc)
        assert result.passes

    def test_even_if_user_lacked_docker(self):
        # Even without Docker in profile, Docker-as-optional should not reject.
        # Test with a fresh extractor that has no Docker skill.
        from src.models.job import Job
        job = Job(id="j3", source="test", title="Backend Developer",
                  description="Python requis. Docker nice to have.")
        skills_without_docker = [s for s in _CANDIDATE_SKILLS if s.lower() != "docker"]
        extractor = RequirementExtractor(user_skills=skills_without_docker)
        reqs = extractor.extract(job)
        gate = _make_gate()
        result = gate.check(reqs)
        assert result.passes


# ===========================================================================
# Case 7: Backend Python job + unrelated optional skills → PASS
# ===========================================================================

class TestCase07_BackendPythonUnrelatedOptionals:
    """Optional unrelated technologies must not cause rejection."""

    def test_backend_python_with_rust_mentioned_once(self):
        desc = (
            "Nous développons en Python/Django. PostgreSQL requis. "
            "Docker utilisé. Rust mentionné dans certains modules. "
            "Kotlin apprécié."
        )
        result = _extract_and_gate("Backend Developer", desc)
        assert result.passes

    def test_php_job_with_unrelated_tech_list(self):
        desc = (
            "Développeur PHP/Laravel. Connaissance de Go serait un plus. "
            "Swift, Kotlin: non requis."
        )
        result = _extract_and_gate("Développeur PHP", desc)
        assert result.passes


# ===========================================================================
# Case 8: Completely different occupation → pass gate (domain filter handles it)
# ===========================================================================

class TestCase08_DifferentOccupation:
    """The gate does not reject for occupation mismatch — that is the domain filter's job."""

    def test_accountant_title_passes_gate(self):
        # The keyword filter would have rejected this; the gate should not double-reject
        # (it only sees jobs that already passed the keyword filter).
        result = _extract_and_gate("Comptable Senior")
        # Gate should not add a rejection reason for occupation mismatch
        assert not any(r.type == "occupation" for r in result.reasons)


# ===========================================================================
# Case 9: Transferable backend-development job → PASS
# ===========================================================================

class TestCase09_TransferableBackend:
    """Backend developer role matching user's profile should pass."""

    def test_backend_nodejs_developer(self):
        result = _extract_and_gate("Backend Developer Node.js")
        assert result.passes

    def test_fullstack_php_js_developer(self):
        result = _extract_and_gate("Full Stack Developer PHP/JavaScript")
        assert result.passes

    def test_django_developer(self):
        result = _extract_and_gate("Développeur Django")
        assert result.passes


# ===========================================================================
# Case 10: Ambiguous education → UNKNOWN, soft penalty only (never hard-reject)
# ===========================================================================

class TestCase10_AmbiguousEducation:
    """Unqualified education mention becomes UNKNOWN — soft penalty, never hard-reject.

    User decision (2026-09-01): unqualified Bac+5 is ambiguous; prefer false-negative
    (job visible, ranked lower) over false-positive (good job permanently rejected).
    Only hard-reject when an explicit qualifier is present: "minimum", "requis", etc.
    """

    def test_unqualified_bac5_becomes_unknown(self):
        """Bac+5 without qualifier → UNKNOWN → passes gate with 20% score penalty."""
        desc = "Formation Bac+5 en informatique."
        edu = extract_education(desc)
        assert edu is not None
        assert edu.importance == Importance.UNKNOWN
        # Gate passes — soft penalty, no rejection
        result = _extract_and_gate("Developer", desc)
        assert result.passes

    def test_ou_equivalent_softens_to_preferred_but_still_rejects(self):
        """'ou équivalent' alone classifies PREFERRED but states the bar, so a
        level above the candidate's max still hard-rejects — generic "ou
        équivalent" names an explicit level, unlike the experience-substitution
        alternative ("ou expérience équivalente"), which remains exempt (see
        EducationRequirement.is_experience_alternative)."""
        desc = "Bac+5 ou équivalent."
        edu = extract_education(desc)
        assert edu is not None
        assert edu.importance == Importance.PREFERRED
        result = _extract_and_gate("Developer", desc)
        assert not result.passes

    def test_ou_equivalent_souhaite_is_a_wish(self):
        """Adding a desirability cue ("souhaité") makes it a wish (ADR 0004)."""
        desc = "Bac+5 ou équivalent souhaité."
        edu = extract_education(desc)
        assert edu is not None
        assert edu.importance == Importance.PREFERRED and edu.is_desirable
        assert _extract_and_gate("Developer", desc).passes


# ===========================================================================
# Case 11: French job / English skill terminology
# ===========================================================================

class TestCase11_FrenchEnglishSkills:
    """French job postings mentioning English skill names work correctly."""

    def test_python_in_french_job(self):
        desc = "Compétences requises: Python, Git, Linux. Docker souhaité."
        result = _extract_and_gate("Développeur Backend", desc)
        assert result.passes

    def test_javascript_react_in_french_job(self):
        result = _extract_and_gate("Développeur React.js")
        assert result.passes  # React → JS family → user has JS


# ===========================================================================
# Case 12: Synonyms and spelling variants
# ===========================================================================

class TestCase12_SynonymsAndVariants:
    """Golang/Go, C++/cpp, Node.js/nodejs etc. are handled correctly."""

    def test_golang_title_rejected(self):
        result = _extract_and_gate("Golang Developer")
        assert not result.passes  # Go/Golang not in profile

    def test_go_title_rejected(self):
        result = _extract_and_gate("Go Developer")
        assert not result.passes

    def test_nodejs_developer_passes(self):
        result = _extract_and_gate("Node.js Developer")
        assert result.passes  # Node.js is in profile

    def test_nodejs_variant_passes(self):
        result = _extract_and_gate("NodeJS Developer")
        assert result.passes

    def test_cpp_title_rejected(self):
        result = _extract_and_gate("C++ Developer")
        assert not result.passes  # C++ not in profile

    def test_dotnet_title_rejected(self):
        result = _extract_and_gate(".NET Developer")
        assert not result.passes  # .NET not in profile

    def test_csharp_title_rejected(self):
        result = _extract_and_gate("C# Developer")
        assert not result.passes  # C# not in profile


# ===========================================================================
# Case 13: Remote European job vs non-remote (gate does not handle location)
# ===========================================================================

class TestCase13_LocationNotHandledByGate:
    """Location/remote filtering is handled by the keyword filter, not the gate."""

    def test_gate_does_not_reject_for_location(self):
        result = _extract_and_gate("Software Engineer", "Remote EU")
        # Gate should not have location-based rejection reasons
        assert not any(r.type == "location" for r in result.reasons)


# ===========================================================================
# Case 14: France department / location constraints (gate scope)
# ===========================================================================

class TestCase14_FranceDepartmentScope:
    """France department constraints are outside gate scope."""

    def test_gate_ignores_location_constraints(self):
        result = _extract_and_gate("Developer", "Paris 75")
        assert not any(r.type == "location" for r in result.reasons)


# ===========================================================================
# Case 15: Experience requirement edge cases
# ===========================================================================

class TestCase15_ExperienceEdgeCases:
    """Experience requirements — gate does not re-implement the keyword filter's
    experience_years check, but principal skill gate and education still apply."""

    def test_high_experience_and_bac5_both_flagged(self):
        desc = "5 ans d'expérience requis. Bac+5 minimum."
        result = _extract_and_gate("Developer", desc)
        assert not result.passes  # Bac+5 mandatory → reject

    def test_alternance_job_no_education_passes(self):
        result = _extract_and_gate("Alternant Développeur Python")
        assert result.passes


# ===========================================================================
# Education level parsing tests
# ===========================================================================

class TestEducationLevelParsing:
    """Unit tests for the education level normalization hierarchy."""

    @pytest.mark.parametrize("label,expected_level", [
        ("bac+2", 2),
        ("bac+3", 3),
        ("bac+5", 5),
        ("bac+8", 8),
        ("master", 5),
        ("master 2", 5),
        ("licence", 3),
        ("bts", 2),
        ("dut", 2),
        ("phd", 8),
        ("doctorat", 8),
        ("bachelor", 3),
        ("msc", 5),
    ])
    def test_parse_education_level(self, label, expected_level):
        result = parse_education_level(label)
        assert result == expected_level, f"{label!r} → expected {expected_level}, got {result}"

    def test_unknown_label_returns_none(self):
        assert parse_education_level("super school") is None


# ===========================================================================
# Principal skill detection unit tests
# ===========================================================================

class TestPrincipalSkillDetection:
    """Unit tests for detect_missing_principal_skills."""

    def test_rust_developer_detected(self):
        missing = detect_missing_principal_skills("Rust Developer", _CANDIDATE_SKILLS)
        assert "rust" in missing

    def test_java_developer_detected(self):
        missing = detect_missing_principal_skills("Java Developer", _CANDIDATE_SKILLS)
        assert "java" in missing

    def test_python_developer_not_missing(self):
        missing = detect_missing_principal_skills("Python Developer", _CANDIDATE_SKILLS)
        assert "python" not in missing

    def test_react_developer_not_missing(self):
        # React is in the JavaScript language family
        missing = detect_missing_principal_skills("React Developer", _CANDIDATE_SKILLS)
        assert "javascript" not in missing
        # react itself might be in missing if not recognized — let's verify
        assert not missing, f"React should be covered by JS family, got: {missing}"

    def test_django_developer_not_missing(self):
        missing = detect_missing_principal_skills("Django Developer", _CANDIDATE_SKILLS)
        assert not missing, f"Django is Python family, got: {missing}"

    def test_nodejs_developer_not_missing(self):
        missing = detect_missing_principal_skills("Node.js Developer", _CANDIDATE_SKILLS)
        assert not missing

    def test_golang_developer_detected(self):
        missing = detect_missing_principal_skills("Golang Developer", _CANDIDATE_SKILLS)
        assert "go" in missing

    def test_backend_developer_no_tech_token(self):
        missing = detect_missing_principal_skills("Backend Developer", _CANDIDATE_SKILLS)
        assert not missing

    def test_software_engineer_no_tech_token(self):
        missing = detect_missing_principal_skills("Software Engineer", _CANDIDATE_SKILLS)
        assert not missing

    def test_fullstack_php_js_no_missing(self):
        missing = detect_missing_principal_skills("Full Stack Developer PHP/JavaScript", _CANDIDATE_SKILLS)
        assert not missing

    def test_dotnet_developer_detected(self):
        missing = detect_missing_principal_skills(".NET Developer", _CANDIDATE_SKILLS)
        assert ".net" in missing or "c#" in missing or any("net" in m for m in missing)

    def test_php_laravel_not_missing(self):
        missing = detect_missing_principal_skills("PHP/Laravel Developer", _CANDIDATE_SKILLS)
        assert not missing

    def test_kotlin_developer_detected(self):
        missing = detect_missing_principal_skills("Kotlin Developer", _CANDIDATE_SKILLS)
        assert "kotlin" in missing

    def test_ruby_developer_detected(self):
        missing = detect_missing_principal_skills("Ruby Developer", _CANDIDATE_SKILLS)
        assert "ruby" in missing

    def test_typescript_developer_not_missing(self):
        # TypeScript is in the JS language family
        missing = detect_missing_principal_skills("TypeScript Developer", _CANDIDATE_SKILLS)
        assert not missing, f"TypeScript should be covered by JS family, got: {missing}"

    def test_rd_ingenieur_not_flagged_as_r_language(self):
        # Regression: "Ingénieur R&D" must NOT be mistaken for the R programming language.
        # The 'R' in 'R&D' (recherche et développement) should not trigger the R-lang detector.
        missing = detect_missing_principal_skills("Ingénieur R&D en IA embarquée", _CANDIDATE_SKILLS)
        assert "r" not in missing, f"'R&D' must not be confused with R language, got: {missing}"

    def test_rd_gate_passes(self):
        # End-to-end: "Ingénieur R&D" job must pass the gate (no R-language rejection).
        result = _extract_and_gate("Ingénieur R&D en IA embarquée et TinyML en alternance")
        assert result.passes, f"R&D job falsely rejected: {[r.message for r in result.reasons]}"


# ===========================================================================
# Behavioral regression tests (10 specified rules)
# ===========================================================================

class TestBehavioralRegression:
    """10 regression fixtures for all gate behavioral rules.

    Rule:
      1. Unknown principal tech in title → reject
      2. Unknown principal tech only in description (clearly principal, centrality≥5) → reject
      3. Unknown secondary mandatory tech in description (centrality 2-4) → penalty, not reject
      4. Unknown preferred tech → no penalty
      5. Ambiguous tech (mandatory signal, centrality≤1) → flag only
      6. "X ou Y" where Y is known → don't reject for X alone
      7. Unqualified Bac+5 → UNKNOWN + 20% penalty, not rejected
      8. Bac+5 with "minimum"/"requis" → hard reject
      9. R&D must not be detected as R language
      10. Long multi-tech backend description → don't reject for one missing secondary
    """

    # Rule 1: Unknown principal tech in title → hard reject
    def test_rule1_unknown_principal_tech_in_title(self):
        result = _extract_and_gate("Développeur Java Senior")
        assert not result.passes
        assert any(r.type == "principal_skill" for r in result.reasons)

    # Rule 2: Unknown principal tech clearly in description (centrality≥5) → reject
    def test_rule2_unknown_principal_tech_in_description(self):
        # Java: in title +3, description 3+ mentions +2 = 5 → principal+MANDATORY → reject
        title = "Ingénieur de développement Java"
        desc = (
            "Vous développerez des applications Java. "
            "Expérience Java requise. "
            "Notre équipe Java cherche un expert. "
        )
        result = _extract_and_gate(title, desc)
        assert not result.passes
        reject_types = {r.type for r in result.reasons}
        assert "principal_skill" in reject_types or "mandatory_skill" in reject_types

    # Rule 3: Unknown secondary mandatory tech → penalty, not reject
    def test_rule3_secondary_mandatory_tech_penalty_not_reject(self):
        # Kotlin mentioned 2 times with mandatory signal but NOT in title → centrality=2-3 → penalty only
        title = "Développeur Mobile"
        desc = (
            "Vous travaillerez principalement sur Android. "
            "La maîtrise de Kotlin est requise. "
            "Une connaissance de Kotlin est indispensable pour ce poste."
        )
        from src.models.job import Job
        job = Job(id="j_r3", source="test", title=title, description=desc)
        job.raw_payload = {}
        extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS)
        reqs = extractor.extract(job)
        gate = _make_gate()
        passing, gate_results = gate_jobs([job], {"j_r3": reqs}, gate)
        result = gate_results["j_r3"]
        # Must pass (not hard reject) AND have a score penalty
        assert result.passes, f"Should pass with penalty, got: {[r.message for r in result.reasons]}"
        assert result.score_penalties, "Expected score penalty for secondary mandatory Kotlin"

    # Rule 4: Unknown preferred tech → no penalty (PREFERRED importance)
    def test_rule4_preferred_tech_no_penalty(self):
        title = "Développeur Backend Python"
        desc = (
            "Stack principale : Python/FastAPI. "
            "Une expérience en Go serait un atout. "
            "Go est apprécié mais non obligatoire."
        )
        result = _extract_and_gate(title, desc)
        assert result.passes, f"Preferred tech should not reject: {[r.message for r in result.reasons]}"
        # Should not have a skill penalty (only education penalty possible)
        skill_penalties = [p for p in result.score_penalties if p.type != "education"]
        assert not skill_penalties, f"Preferred Go should not penalize, got: {skill_penalties}"

    # Rule 5: Ambiguous mandatory signal, low centrality → flag only, no penalty
    def test_rule5_ambiguous_tech_flag_only(self):
        # Kotlin mentioned once with "requis" nearby, but low centrality → flag, no penalty/reject
        title = "Développeur Fullstack"
        desc = (
            "Nous cherchons un développeur fullstack. "
            "Expérience requise en développement mobile. "
            "Des notions de Kotlin seraient un plus."
        )
        result = _extract_and_gate(title, desc)
        assert result.passes
        # No skill penalty (centrality too low)
        skill_penalties = [p for p in result.score_penalties if "kotlin" in p.requirement.lower()]
        assert not skill_penalties, f"Ambiguous Kotlin should not penalize, got: {skill_penalties}"

    # Rule 6: "X ou Y" where Y is known → don't reject for X alone
    def test_rule6_ou_alternative_not_rejected(self):
        # "Java ou Node.js" — user knows Node.js → Java is OR alternative → no hard reject
        result = _extract_and_gate("Développeur Backend Java ou Node.js")
        assert result.passes, (
            f"'Java ou Node.js' should not reject when user knows Node.js, "
            f"got: {[r.message for r in result.reasons]}"
        )
        # Java should appear as an OR-alternative flag, not a rejection
        or_flags = [f for f in result.flags if "OR-alternative" in f and "java" in f.lower()]
        assert or_flags, f"Expected OR-alternative flag for Java, got flags: {result.flags}"

    def test_rule6_or_english_alternative_not_rejected(self):
        # English "or" variant
        result = _extract_and_gate("Backend Engineer Java or Node.js")
        assert result.passes, f"Java or Node.js should not reject: {[r.message for r in result.reasons]}"

    def test_rule6_slash_tech_still_rejects(self):
        # "Java / React" — "/" is AND, not OR → Java is still hard-rejected
        result = _extract_and_gate("Tech Lead Java / React")
        assert not result.passes, "Java/React (AND) should reject when user lacks Java"

    # Rule 7: Unqualified Bac+5 (no modifier) → UNKNOWN, NOT rejected, NOT penalized
    # (2026-09-11 audit fix: UNKNOWN is an ambiguous mention with no explicit
    # qualifier, not evidence of a real requirement — it must pass cleanly,
    # with no soft penalty either. See gate.py's gate_jobs docstring.)
    def test_rule7_unqualified_bac5_unknown_no_penalty(self):
        from src.models.job import Job
        job = Job(id="j_r7", source="test", title="Developer",
                  description="Formation Bac+5 en informatique.")
        job.raw_payload = {}
        extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS)
        reqs = extractor.extract(job)
        assert reqs.education is not None
        assert reqs.education.importance == Importance.UNKNOWN, (
            f"Unqualified Bac+5 should be UNKNOWN, got {reqs.education.importance}"
        )
        gate = _make_gate()
        passing, gate_results = gate_jobs([job], {"j_r7": reqs}, gate, education_preferred_penalty=0.20)
        assert len(passing) == 1, "Unqualified Bac+5 must pass gate"
        assert gate_results["j_r7"].education_penalty == 0.0, (
            "UNKNOWN education importance must not be penalized at all"
        )

    # Rule 8: Bac+5 with "minimum"/"requis" → hard reject
    def test_rule8_bac5_minimum_hard_reject(self):
        desc = "Formation Bac+5 minimum en informatique."
        result = _extract_and_gate("Developer", desc)
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)

    def test_rule8_bac5_requis_hard_reject(self):
        desc = "Diplôme Bac+5 requis."
        result = _extract_and_gate("Engineer", desc)
        assert not result.passes
        assert any(r.type == "education" for r in result.reasons)

    # Rule 9: R&D must not be detected as R language (regression guard)
    def test_rule9_rd_not_r_language(self):
        from src.requirements.principal_skill import detect_missing_principal_skills
        missing = detect_missing_principal_skills("Ingénieur R&D", _CANDIDATE_SKILLS)
        assert "r" not in missing, f"R&D must not match R language, got: {missing}"

    def test_rule9_rd_full_title_passes(self):
        result = _extract_and_gate("Ingénieur R&D - IA embarquée")
        assert result.passes, f"R&D job should pass, got: {[r.message for r in result.reasons]}"

    # Rule 10: Long multi-tech description → don't reject for one missing secondary
    def test_rule10_multitech_one_missing_secondary_passes(self):
        # Stack mentions Python (known), JavaScript (known), and Rust (unknown, secondary)
        # The job should pass with a penalty for Rust, not be hard-rejected
        title = "Développeur Backend"
        desc = (
            "Notre stack backend utilise Python et JavaScript. "
            "Nous utilisons également Rust pour les modules critiques. "
            "Rust est requis pour ce module spécifique. "
            "Expérience Python requise. "
        )
        from src.models.job import Job
        job = Job(id="j_r10", source="test", title=title, description=desc)
        job.raw_payload = {}
        extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS)
        reqs = extractor.extract(job)
        gate = _make_gate()
        passing, gate_results = gate_jobs([job], {"j_r10": reqs}, gate)
        result = gate_results["j_r10"]
        # Rust in generic "Développeur Backend" title → not principal from title
        # Description centrality for Rust: freq=2 +1, no title +0 = 1-2 → penalty or flag
        # Result: passes (Rust is secondary/ambiguous in a multi-tech stack)
        assert result.passes, (
            f"Missing one secondary tech in a multi-tech stack should not reject, "
            f"got: {[r.message for r in result.reasons]}"
        )


# ===========================================================================
# .NET normalization regression tests
# ===========================================================================

class TestDotNetNormalization:
    """.NET variant detection — all common forms must be caught.

    The candidate profile lacks .NET/C# — all of these title forms must
    trigger a hard reject.  Secondary mentions in a compatible-tech description
    must NOT trigger rejection.
    """

    # --- Forms that MUST reject ---

    def test_dotnet_space_rejects(self):
        # Standard spaced form: "Développeur .NET"
        result = _extract_and_gate("Développeur .NET (H/F)")
        assert not result.passes
        assert any(r.type == "principal_skill" and ".net" in r.requirement for r in result.reasons)

    def test_dotnet_compound_rejects(self):
        # No-space compound: "Développeur.net" — the false-negative case found in audit
        result = _extract_and_gate("Développeur.net (H/F) - Clermont-Fd")
        assert not result.passes, (
            "Développeur.net must be rejected (word.net compound pattern)"
        )
        assert any(r.type == "principal_skill" for r in result.reasons)

    def test_dotnet_dotnet_word_rejects(self):
        # "DotNet" word form
        result = _extract_and_gate("Senior DotNet Developer")
        assert not result.passes
        assert any(r.type == "principal_skill" for r in result.reasons)

    def test_dotnet_aspnet_rejects(self):
        # "ASP.NET" framework form
        result = _extract_and_gate("Développeur(se) Confirmé(e) ASP.NET (H/F)")
        assert not result.passes
        assert any(r.type == "principal_skill" for r in result.reasons)

    def test_dotnet_csharp_slash_dotnet_rejects(self):
        # "C#/.NET" combined form
        result = _extract_and_gate("Développeur C# / .NET Senior")
        assert not result.passes
        # Both C# and .NET should be detected
        detected = {r.requirement for r in result.reasons if r.type == "principal_skill"}
        assert "c#" in detected or ".net" in detected, f"Expected c# or .net in {detected}"

    def test_dotnet_core_rejects(self):
        # ".NET Core" form
        result = _extract_and_gate("Ingénieur .NET Core / Microservices")
        assert not result.passes
        assert any(r.type == "principal_skill" and ".net" in r.requirement for r in result.reasons)

    def test_dotnet_versioned_rejects(self):
        # ".NET 8" versioned form
        result = _extract_and_gate("Développeur .NET 8 (H/F)")
        assert not result.passes
        assert any(r.type == "principal_skill" for r in result.reasons)

    # --- Forms that must NOT create false positives ---

    def test_python_job_dotnet_secondary_passes(self):
        # .NET appears once in description as an existing system — not the principal tech
        title = "Développeur Python — API REST"
        desc = (
            "Vous développerez des API Python/FastAPI. "
            "L'existant repose en partie sur une base .NET que vous n'aurez pas à modifier."
        )
        result = _extract_and_gate(title, desc)
        assert result.passes, (
            f"Python job with .NET mentioned as existing system must pass, "
            f"got: {[r.message for r in result.reasons]}"
        )

    def test_network_word_no_false_positive(self):
        # "network" and "networking" must NOT trigger .NET detection
        result = _extract_and_gate("Network Engineer — Cloud & Networking")
        assert result.passes, (
            f"'network'/'networking' must not match .NET, "
            f"got: {[r.message for r in result.reasons]}"
        )

    def test_internet_no_false_positive(self):
        # "internet" must NOT trigger .NET detection
        result = _extract_and_gate("Développeur Web — Applications Internet")
        assert result.passes

    def test_aspnet_in_description_only_secondary_passes(self):
        # ASP.NET appears once in description without mandatory signal — not rejected
        title = "Développeur Full Stack PHP / JavaScript"
        desc = (
            "Stack principale : PHP/Laravel + Vue.js. "
            "L'environnement serveur inclut quelques composants ASP.NET legacy."
        )
        result = _extract_and_gate(title, desc)
        assert result.passes, (
            f"ASP.NET as secondary description mention must not reject, "
            f"got: {[r.message for r in result.reasons]}"
        )

    def test_domain_name_dot_net_no_false_positive(self):
        # "planet.net" as a company domain in the title must NOT match — "planet" is
        # not a role-function word, so the compound pattern does not fire.
        result = _extract_and_gate("Ingénieur Cloud — planet.net Infrastructure")
        assert result.passes, (
            f"Company domain 'planet.net' in title must not trigger .NET detection, "
            f"got: {[r.message for r in result.reasons]}"
        )
