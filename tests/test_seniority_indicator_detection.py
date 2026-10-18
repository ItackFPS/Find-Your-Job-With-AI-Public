"""Regression tests for ``find_seniority_indicators`` word-boundary and
third-party-context guards (src/pipeline/scoring.py).

Background (2026-09-05 live AI benchmark validation): the eligibility gate's
evidence assembly (``src/ai/eligibility.py::_assemble_evidence``) calls
``find_seniority_indicators`` to surface seniority/level words to the LLM
eligibility prompt. The previous implementation used a bare substring check
(``ind in text``), which let "lead" match inside "leader"/"leadership" and
"intern" match inside "interne"/"internes"/"international" — routine French
corporate-boilerplate text ("Thales est un leader mondial...", "communautés
internes") was misread as real candidate seniority/internship evidence,
defeating the eligibility gate's corroboration check (a REJECT is only
trusted when real evidence was assembled — see
``EligibilityGate.run()``) with false evidence. Three real jobs from the
benchmark are used directly below as regression fixtures.

These tests target ``find_seniority_indicators`` and
``_assemble_evidence`` directly (the actual evidence-assembly path
``EligibilityGate`` consumes), not a mocked ``EligibilityResult``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.ai.eligibility import _assemble_evidence
from src.models.job import Job
from src.pipeline.scoring import find_seniority_indicators

# ---------------------------------------------------------------------------
# Must NOT detect — substring collisions inside unrelated words
# ---------------------------------------------------------------------------


class TestSubstringCollisionsAreExcluded:
    def test_lead_does_not_match_leader(self):
        assert find_seniority_indicators("Notre entreprise est leader mondial") == []

    def test_lead_does_not_match_leader_europeen(self):
        assert find_seniority_indicators("Telespazio France est le leader europeen") == []

    def test_lead_does_not_match_leadership(self):
        assert find_seniority_indicators("Formation en leadership") == []

    def test_intern_does_not_match_internes(self):
        assert find_seniority_indicators("Rejoignez nos communautes internes") == []

    def test_intern_does_not_match_interne(self):
        assert find_seniority_indicators("Une communication interne efficace") == []

    def test_intern_does_not_match_international(self):
        assert find_seniority_indicators("Poste avec une portee internationale") == []

    def test_intern_does_not_match_internally(self):
        assert find_seniority_indicators("Data shared internally across teams") == []

    def test_systemes_internes_no_false_hit(self):
        assert find_seniority_indicators("Administration des systemes internes") == []

    def test_thales_backend_boilerplate_no_false_hit(self):
        """Real text from the benchmark: 'leader mondial' + 'communautes
        internes' in the same posting must not produce ANY indicator hit."""
        text = (
            "Thales est un leader mondial des hautes technologies. "
            "Rejoignez nos academies et communautes internes."
        )
        assert find_seniority_indicators(text) == []


# ---------------------------------------------------------------------------
# Must NOT detect — third-party/co-worker enumeration context
# ---------------------------------------------------------------------------


class TestThirdPartyCoordinationIsExcluded:
    def test_squad_lead_as_coworker_reference_isagri(self):
        """Real text from the ISAGRI benchmark job: 'lead' is a standalone
        word here (not a substring collision) but names a CO-WORKER'S role
        the candidate will support, not a requirement on the candidate."""
        text = (
            "Tu appuieras par ailleurs le Product Owner et le Squad Lead "
            "dans le pilotage du projet et le suivi des travaux techniques."
        )
        assert find_seniority_indicators(text) == []

    def test_generic_et_le_enumeration(self):
        assert find_seniority_indicators("aux cotes du chef de projet et le tech lead") == []

    def test_english_and_the_enumeration(self):
        assert find_seniority_indicators("alongside the product owner and the team lead") == []


# ---------------------------------------------------------------------------
# Must still detect — genuine candidate/job seniority requirements
# ---------------------------------------------------------------------------


class TestGenuineSeniorityStillDetected:
    def test_tech_lead(self):
        assert "lead" in find_seniority_indicators("Tech Lead Developer")

    def test_team_lead(self):
        assert "lead" in find_seniority_indicators("We are hiring a Team Lead")

    def test_lead_developer(self):
        assert "lead" in find_seniority_indicators("Lead Developer position")

    def test_senior_developer(self):
        assert "senior" in find_seniority_indicators("Senior Developer wanted")

    def test_poste_senior(self):
        result = find_seniority_indicators("poste Senior confirme")
        assert "senior" in result
        assert "confirme" in result

    def test_vous_etes_le_lead_developer_still_counts(self):
        """'vous serez le Lead Developer' genuinely states the candidate will
        BE the lead — 'le' here is not part of an 'et le'/'and the'
        enumeration, so it must still count as real evidence."""
        assert "lead" in find_seniority_indicators("Vous serez le Lead Developer de l'equipe")

    def test_poste_de_tech_lead(self):
        assert "lead" in find_seniority_indicators("Poste de Tech Lead pour notre equipe")

    def test_experience_en_tant_que_senior(self):
        assert "senior" in find_seniority_indicators("Experience en tant que Senior Developer requise")

    def test_internship(self):
        assert "internship" in find_seniority_indicators("Software Engineering Internship")

    def test_stagiaire(self):
        assert "stagiaire" in find_seniority_indicators("Developpeur Stagiaire")

    def test_alternance(self):
        assert "alternance" in find_seniority_indicators("Poste en alternance")

    def test_principal_staff_director_manager_still_detected(self):
        assert "principal" in find_seniority_indicators("Principal Engineer")
        assert "staff" in find_seniority_indicators("Staff Applied AI Engineer")
        assert "director" in find_seniority_indicators("Director of Engineering")
        assert "manager" in find_seniority_indicators("Engineering Manager")

    def test_junior_entry_graduate_still_detected(self):
        assert "junior" in find_seniority_indicators("Junior Developer")
        assert "entry" in find_seniority_indicators("Entry level position")
        assert "graduate" in find_seniority_indicators("Graduate Software Engineer")


# ---------------------------------------------------------------------------
# End-to-end: the actual evidence EligibilityGate consumes for the real
# benchmark jobs (not a mocked EligibilityResult)
# ---------------------------------------------------------------------------


def _job(title: str, description: str) -> Job:
    return Job(
        id="j1", source="test", title=title, company="Acme", description=description,
        location="Paris, France", countries=["France"], remote=False,
        employment_type="full-time", discovered_at=datetime.now(UTC),
    )


class TestAssembleEvidenceNoLongerFooledByBoilerplate:
    def test_isagri_squad_lead_produces_no_structured_evidence(self):
        job = _job(
            title="Développeur Full-Stack Node/VueJS H/F",
            description=(
                "Tu appuieras par ailleurs le Product Owner et le Squad Lead "
                "dans le pilotage du projet et le suivi des travaux techniques "
                "des membres de l'équipe."
            ),
        )
        evidence, has_evidence = _assemble_evidence(job, None)
        assert has_evidence is False
        assert "seniority/level words found" not in evidence

    def test_thales_leader_mondial_produces_no_structured_evidence(self):
        job = _job(
            title="Développeur BackEnd - Traitement de Données (F/H)",
            description=(
                "Thales est un leader mondial des hautes technologies. "
                "Rejoignez nos academies et communautes internes."
            ),
        )
        evidence, has_evidence = _assemble_evidence(job, None)
        assert has_evidence is False

    def test_telespazio_leader_europeen_produces_no_structured_evidence(self):
        job = _job(
            title="Ingénieur Cybersécurité et Opérations (F/H)",
            description="Telespazio France est le leader europeen des services satellitaires.",
        )
        evidence, has_evidence = _assemble_evidence(job, None)
        assert has_evidence is False

    def test_genuine_senior_title_still_produces_structured_evidence(self):
        job = _job(
            title="Senior Software Engineer",
            description="Looking for a senior engineer to lead the team.",
        )
        evidence, has_evidence = _assemble_evidence(job, None)
        assert has_evidence is True
        assert "senior" in evidence.lower()


# ---------------------------------------------------------------------------
# _score_seniority_match (src/pipeline/scoring.py) must use the SAME
# word-boundary-guarded find_seniority_indicators() the eligibility gate
# uses, not a duplicate bare substring/split check. Before this fix, the
# scoring path's own inline check (`ind in title_norm` /
# `ind in title_norm.split()`) let "lead" match inside "leader"/"leadership"
# and "intern" match inside "interne"/"international" — the exact same false
# positive find_seniority_indicators was built to close, but reintroduced by
# a second, un-synced implementation of the same rule.
# ---------------------------------------------------------------------------


class TestScoreSeniorityMatchUsesWordBoundaryDetector:
    """preferred_level_keywords is deliberately non-empty (but non-matching)
    in the "must be neutral" cases below — an EMPTY keyword list makes
    _score_seniority_match return 0.5 immediately without ever reaching the
    has_seniority_signal check this fix targets, which would make these
    regression tests pass vacuously."""

    def _score(self, title: str, preferred_keywords: list[str] | None = None) -> float:
        from src.pipeline.scoring import _score_seniority_match
        config = {
            "scoring": {
                "seniority_match": {
                    "preferred_level_keywords": (
                        ["junior"] if preferred_keywords is None else preferred_keywords
                    ),
                }
            }
        }
        return _score_seniority_match(title, config)

    def test_lead_substring_in_leader_is_not_a_wrong_seniority_penalty(self):
        """'Leader' in a title must not be misread as a 'lead' seniority
        signal and penalized to 0.0 — it isn't a seniority word at all here."""
        assert self._score("Leader Technique Full-Stack") == 0.5

    def test_intern_substring_in_international_is_not_penalized(self):
        assert self._score("Ingenieur International Support") == 0.5

    def test_genuine_lead_title_is_still_penalized_when_not_preferred(self):
        """A real seniority word must still trigger the 0.0 wrong-level penalty
        (this fix must not silently disable seniority scoring altogether)."""
        assert self._score("Tech Lead Developer") == 0.0

    def test_genuine_senior_title_still_penalized_when_junior_preferred(self):
        assert self._score("Senior Software Engineer", preferred_keywords=["junior"]) == 0.0

    def test_preferred_keyword_match_still_scores_a_bonus(self):
        assert self._score("Junior Developer", preferred_keywords=["junior"]) > 0.5

    def test_no_seniority_signal_at_all_is_neutral(self):
        assert self._score("AI Engineer") == 0.5
