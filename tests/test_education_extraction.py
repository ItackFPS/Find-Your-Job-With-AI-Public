"""Regression tests for education requirement extraction improvements.

Covers:
  - Fix: 5000-char description truncation (was 3000)
  - Fix: "titulaire d'" (already holds the diploma) is MANDATORY
  - Fix (2026-09-05): "vous préparez"/"en cours de préparation" (currently
    PURSUING the diploma, e.g. an alternance posting) is NOT equivalent to a
    completed mandatory requirement — must be UNKNOWN (soft penalty only,
    never a hard reject). Previously misclassified as MANDATORY, which
    hard-rejected real jobs solely because the posting targets students
    actively enrolled in a program — see the real-job benchmark's Job 39.
  - Regression: existing mandatory patterns (requis, obligatoire, exigé, minimum)
  - Regression: existing preferred patterns (souhaité, serait un plus)
  - Regression: ambiguous cases must NOT become hard rejects

Candidate profile used throughout: max_education = bac+2 (level 2).
"""

from __future__ import annotations

import pytest

from src.requirements.education import extract_education
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate
from src.requirements.models import GateResult, Importance

_CANDIDATE_MAX_EDU = 2
_CANDIDATE_SKILLS = ["python", "javascript", "php", "MySQL", "Node.js",
                     "Git", "Linux", "HTML", "CSS", "Bash", "Docker", "n8n"]


def _make_job(title: str, description: str = "", job_id: str = "j1"):
    from src.models.job import Job
    return Job(id=job_id, source="test", title=title, description=description)


def _extract_and_gate(title: str, description: str = "") -> GateResult:
    job = _make_job(title, description)
    extractor = RequirementExtractor(user_skills=_CANDIDATE_SKILLS)
    reqs = extractor.extract(job)
    gate = HardConstraintGate(user_max_education_level=_CANDIDATE_MAX_EDU)
    return gate.check(reqs)


# ===========================================================================
# Mandatory patterns — hard reject regression (existing patterns must not break)
# ===========================================================================

class TestMandatoryPatternsRegression:
    """Existing mandatory signals must still trigger hard rejection."""

    def test_bac5_requis_is_hard_rejected(self):
        result = _extract_and_gate("Dev", "Profil : Bac+5 requis en informatique.")
        assert not result.passes

    def test_bac5_obligatoire_is_hard_rejected(self):
        result = _extract_and_gate("Dev", "Formation Bac+5 obligatoire.")
        assert not result.passes

    def test_bac5_exige_is_hard_rejected(self):
        result = _extract_and_gate("Dev", "Niveau Bac+5 exigé pour ce poste.")
        assert not result.passes

    def test_bac5_minimum_is_hard_rejected(self):
        result = _extract_and_gate("Dev", "Bac+5 minimum en école d'ingénieur.")
        assert not result.passes

    def test_minimum_bac5_word_order_variant(self):
        result = _extract_and_gate("Dev", "De formation minimum Bac+5.")
        assert not result.passes


# ===========================================================================
# Student-targeting language ("vous préparez X") — NOT a completed requirement
# ===========================================================================

class TestVousPreparezIsNotHardRejected:
    """'vous préparez un diplôme X' means the candidate is CURRENTLY PURSUING
    it (typically an alternance posting targeting enrolled students) — this
    is NOT equivalent to already holding the diploma, so it must not hard-
    reject a candidate below that level (soft penalty only, at most)."""

    def test_vous_preparez_bac5(self):
        desc = "Vous préparez un diplôme Bac+5 en école d'ingénieur."
        result = _extract_and_gate("Stage Développeur", desc)
        assert result.passes, "Student-targeting Bac+5 language must not hard-reject a Bac+2 candidate"

    def test_vous_preparez_master(self):
        desc = "Vous préparez un Master en informatique ou un diplôme d'ingénieur."
        result = _extract_and_gate("Stage Data Engineer", desc)
        assert result.passes

    def test_vous_preparez_diplome_ingenieur(self):
        desc = "Profil : Vous préparez un diplôme d'ingénieur en systèmes embarqués."
        result = _extract_and_gate("Stage Ingénieur", desc)
        assert result.passes

    def test_vous_preparez_accent_variant(self):
        desc = "Vous préparez actuellement un Bac+5 en data science."
        result = _extract_and_gate("Stage AI", desc)
        assert result.passes

    def test_vous_preparez_combined_with_explicit_minimum_still_rejects(self):
        """A genuinely separate, explicit 'minimum' qualifier elsewhere in the
        same context must still independently trigger MANDATORY — the
        student-targeting phrase itself just no longer does so on its own."""
        desc = "Vous préparez un Bac+5 minimum en école d'ingénieur."
        result = _extract_and_gate("Stage Ingénieur", desc)
        assert not result.passes


class TestEnCoursPreparationIsNotHardRejected:
    """'en cours de préparation d'un Master' indicates a current student —
    same non-mandatory treatment as 'vous préparez'."""

    def test_en_cours_preparation_master(self):
        desc = "Candidat en cours de préparation d'un Master 2 en informatique."
        result = _extract_and_gate("Alternance", desc)
        assert result.passes

    def test_en_cours_de_preparation_bac5(self):
        desc = "Vous êtes en cours de préparation d'un Bac+5 en école d'ingénieur."
        result = _extract_and_gate("Stage", desc)
        assert result.passes


class TestTitulaireIsMandatory:
    """'titulaire d'un Bac+5' indicates the candidate must hold that diploma."""

    def test_titulaire_bac5(self):
        desc = "Vous êtes titulaire d'un Bac+5 en informatique."
        result = _extract_and_gate("Développeur Senior", desc)
        assert not result.passes

    def test_titulaire_master(self):
        desc = "Titulaire d'un Master (Bac+5) avec 3 ans d'expérience minimum."
        result = _extract_and_gate("Ingénieur", desc)
        assert not result.passes


# ===========================================================================
# Preferred patterns — now DO hard-reject when the level exceeds the
# candidate's max (behavior change, 2026-09: the hard education ceiling is
# no longer "mandatory only" — see HardConstraintGate._check_education's
# docstring). PREFERRED still names an explicit, attributable level (e.g.
# "Bac+5 souhaitée" is a real level mention, just softly worded) — it is
# UNKNOWN (genuinely ambiguous, e.g. "vous préparez un Bac+5") that remains
# exempt, covered by TestVousPreparezIsNotHardRejected below.
# ===========================================================================

class TestPreferredPatternsRegression:
    """Preferred Bac+5 above the candidate's max is a hard reject."""

    def test_bac5_souhaite_rejected(self):
        result = _extract_and_gate("Dev", "Formation Bac+5 souhaitée en informatique.")
        assert not result.passes, "PREFERRED Bac+5 above max must hard-reject (not mandatory-only)"

    def test_bac5_serait_un_plus_rejected(self):
        result = _extract_and_gate("Dev", "Bac+5 serait un plus, Bac+3 accepté.")
        assert not result.passes


# ===========================================================================
# Ambiguous / unknown — soft penalty, never hard reject
# ===========================================================================

class TestAmbiguousCasesNotHardRejected:
    """Genuinely UNKNOWN-importance education mentions (no explicit mandatory
    or preferred qualifier) get 20% soft penalty but must not hard-reject."""

    def test_de_formation_bac5_passes(self):
        desc = "De formation Bac+5 en informatique, vous avez une solide expérience."
        result = _extract_and_gate("Développeur", desc)
        assert result.passes, "'de formation Bac+5' alone must not hard-reject"

    def test_equipe_accueille_etudiants_passes(self):
        desc = "Notre équipe accueille également des étudiants Bac+5 pour des projets."
        result = _extract_and_gate("Développeur", desc)
        assert result.passes, "Generic mention of Bac+5 students must not hard-reject"


class TestExplicitPreferredEducationIsHardRejected:
    """'apprécié'/'ou équivalent' are PREFERRED patterns (extract_education
    classifies them as PREFERRED, not UNKNOWN) — they name an explicit level,
    just softly worded, so they now hard-reject like MANDATORY (see
    TestPreferredPatternsRegression above). This is distinct from genuinely
    ambiguous UNKNOWN mentions in TestAmbiguousCasesNotHardRejected."""

    def test_niveau_bac5_apprecie_is_rejected(self):
        desc = "Niveau Bac+5 apprécié pour ce poste en développement logiciel."
        result = _extract_and_gate("Développeur", desc)
        assert not result.passes

    def test_ou_equivalent_still_rejected(self):
        """'ou équivalent' softens MANDATORY to PREFERRED, but PREFERRED above
        the candidate's max is still a hard reject — 'ou équivalent' means
        "an equivalent level of formation", not "no requirement at all"."""
        desc = "Bac+5 en informatique ou équivalent selon l'expérience."
        result = _extract_and_gate("Dev", desc)
        assert not result.passes


# ===========================================================================
# Truncation fix — education at position > 3000 must now be detected
# ===========================================================================

class TestTruncationFix:
    """Education requirements beyond position 3000 in a long description must be detected."""

    def _long_desc_with_bac5_at(self, position: int, signal: str = "requis") -> str:
        filler = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 60
        requirement = f"Votre profil : Bac+5 {signal} en informatique."
        desc = filler[:position] + requirement
        return desc

    def test_bac5_requis_at_position_3100_is_detected(self):
        desc = self._long_desc_with_bac5_at(3100)
        result = _extract_and_gate("Développeur", desc)
        assert not result.passes, "Mandatory Bac+5 at position 3100 must now be detected and hard-rejected"

    def test_bac5_requis_at_position_4000_is_detected(self):
        desc = self._long_desc_with_bac5_at(4000)
        result = _extract_and_gate("Ingénieur", desc)
        assert not result.passes, "Mandatory Bac+5 at position 4000 must now be detected and hard-rejected"

    def test_vous_preparez_at_position_3200_is_detected(self):
        """Detection (extraction reaching beyond the old 3000-char cutoff)
        still works at this position — but student-targeting language is not
        a hard-reject signal, so the job must still pass."""
        filler = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 57
        requirement = "\nVous préparez un diplôme Bac+5 en école d'ingénieur."
        desc = filler[:3200] + requirement
        result = _extract_and_gate("Stage", desc)
        assert result.passes, "Student-targeting Bac+5 must not hard-reject regardless of position"

    def test_education_within_3000_still_detected(self):
        desc = "Formation : Bac+5 minimum.\n" + "Texte supplémentaire. " * 200
        result = _extract_and_gate("Dev", desc)
        assert not result.passes, "Education within first 3000 chars must still be detected"


# ===========================================================================
# Extract_education unit tests — importance classification
# ===========================================================================

class TestExtractEducationImportance:
    """Direct unit tests for extract_education() importance classification."""

    def test_vous_preparez_bac5_is_unknown_not_mandatory(self):
        text = "Vous préparez un diplôme Bac+5 en école d'ingénieur ou Master."
        edu = extract_education(text)
        assert edu is not None
        assert edu.importance == Importance.UNKNOWN

    def test_titulaire_bac5_is_mandatory(self):
        text = "Vous êtes titulaire d'un Bac+5 en informatique."
        edu = extract_education(text)
        assert edu is not None
        assert edu.importance == Importance.MANDATORY

    def test_bac5_ou_equivalent_is_preferred(self):
        text = "De formation Bac+5 ou équivalent en informatique."
        edu = extract_education(text)
        assert edu is not None
        assert edu.importance == Importance.PREFERRED

    def test_bac5_souhaite_is_preferred(self):
        text = "Bac+5 souhaité pour ce poste."
        edu = extract_education(text)
        assert edu is not None
        assert edu.importance == Importance.PREFERRED


class TestQualifiersStayInTheirOwnClause:
    """Issue #19: a qualifier belongs to the education mention in its own
    clause. Before the fix, both mentions saw every qualifier within ±120
    chars, so a "souhaité" on one level turned an adjacent "minimum requis"
    level into PREFERRED too — and a PREFERRED level above max_education
    hard-rejects."""

    @pytest.mark.parametrize(
        "text, level, importance",
        [
            ("Bac+5 souhaité, Bac+2 minimum requis.", 2, Importance.MANDATORY),
            ("Bac+2 minimum, idéalement Bac+5.", 2, Importance.MANDATORY),
            ("Master souhaité ; Licence requise.", 3, Importance.MANDATORY),
            ("Bac+5 apprécié. Bac+3 exigé.", 3, Importance.MANDATORY),
        ],
    )
    def test_mandatory_clause_is_not_softened_by_neighbouring_preferred_clause(
        self, text, level, importance
    ):
        edu = extract_education(text)
        assert edu is not None
        assert (edu.level_bac_plus, edu.importance) == (level, importance), edu

    @pytest.mark.parametrize(
        "text",
        [
            "Bac+5 souhaité, Bac+2 minimum requis.",
            "Bac+2 minimum, idéalement Bac+5.",
        ],
    )
    def test_bac2_candidate_is_not_hard_rejected(self, text):
        result = _extract_and_gate("Développeur", text)
        assert result.passes, result.reasons

    @pytest.mark.parametrize(
        "text, level, importance",
        [
            # Mentions in the same clause still share qualifiers (unchanged).
            ("BTS ou DUT requis", 2, Importance.MANDATORY),
            ("Master (Bac+5) souhaité", 5, Importance.PREFERRED),
            # An adjacent held range counts at its lowest level (#58).
            ("Bac+3/Bac+5 souhaité", 3, Importance.PREFERRED),
            # A single mention still sees qualifiers across punctuation (unchanged).
            ("Bac+5. Ce diplôme est obligatoire.", 5, Importance.MANDATORY),
            ("Bac+5 en informatique, idéalement.", 5, Importance.PREFERRED),
        ],
    )
    def test_behaviour_without_a_clause_boundary_between_mentions_is_unchanged(
        self, text, level, importance
    ):
        edu = extract_education(text)
        assert edu is not None
        assert (edu.level_bac_plus, edu.importance) == (level, importance), edu

    def test_punctuation_inside_a_neighbouring_mention_is_not_a_clause_separator(self):
        # The dot in "B.Sc" must not split BTS from the trailing "requis".
        edu = extract_education("BTS ou B.Sc ou Master requis")
        assert edu is not None
        assert (edu.level_bac_plus, edu.importance) == (5, Importance.MANDATORY)
        from src.requirements.education import _EDUCATION_PATTERNS, _extract_importance

        text = "BTS ou B.Sc ou Master requis"
        spans = [
            (m.start(), m.end()) for pattern, _l, _lab in _EDUCATION_PATTERNS
            for m in pattern.finditer(text)
        ]
        bts = next(s for s in spans if text[s[0]:s[1]] == "BTS")
        others = [s for s in spans if s != bts]
        assert _extract_importance(text, *bts, others)[0] == Importance.MANDATORY


class TestMaitriseSkillPhrasingIsNotADegree:
    """Issue #31: "maîtrise d'/du/des X" means mastery of a skill, not the
    Bac+4 "Maîtrise" diploma. Only "maîtrise de" was excluded, so
    "Maîtrise d'Excel requise." hard-rejected a Bac+2 candidate."""

    @pytest.mark.parametrize(
        "text",
        [
            "Maîtrise d'Excel requise.",
            "Maîtrise d’Excel exigée.",
            "Maîtrise du Pack Office indispensable.",
            "Maitrise du francais obligatoire.",
            "Maîtrise des outils bureautiques requise.",
            "Maîtrise de Python requise.",  # already excluded before #31
        ],
    )
    def test_skill_mastery_is_not_an_education_requirement(self, text):
        assert extract_education(text) is None

    @pytest.mark.parametrize(
        "text",
        ["Maîtrise d'Excel requise.", "Maîtrise du Pack Office requise."],
    )
    def test_bac2_candidate_is_not_hard_rejected(self, text):
        result = _extract_and_gate("Assistant administratif", text)
        assert result.passes, result.reasons

    def test_skill_mastery_does_not_hide_a_real_degree_in_the_same_text(self):
        edu = extract_education("Maîtrise du Pack Office indispensable, BTS requis.")
        assert edu is not None
        assert (edu.level_label, edu.importance) == ("BTS", Importance.MANDATORY)

    @pytest.mark.parametrize(
        "text",
        [
            # #51: without a degree cue, "Maîtrise en informatique requise." is
            # skill wording in practice and is no longer a Bac+4 requirement.
            "Titulaire d'une Maîtrise en informatique requise.",
            "Titulaire d'une Maîtrise, minimum.",
        ],
    )
    def test_degree_usage_still_detected(self, text):
        edu = extract_education(text)
        assert edu is not None
        assert (edu.level_bac_plus, edu.importance) == (4, Importance.MANDATORY)
