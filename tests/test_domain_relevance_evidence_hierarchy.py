"""Pass 5 regression tests: domain-relevance evidence-hierarchy fix.

Confirmed off-domain title evidence must always win over incidental
positive evidence — a generic role noun in the same title ("Ingénieur",
"Technicien", "Analyste", "Developer", "Engineer", ...) or a generic
technology word in the description ("Docker", "AWS", "Azure", ...).

Background (2026-09-12 audit + fix): _score_domain_relevance used to check
the broad-tech-title indicator and the description-keyword tier BEFORE the
off-domain title check, so a confirmed off-domain job could "rescue" itself
to a much higher domain_relevance purely from an unrelated generic word.
Live-code proof from the audit:
    "Commercial Junior"            -> domain_relevance = 0.05  (correct)
    "Ingénieur Commercial Junior"  -> domain_relevance = 0.35  (bug)
    "Ingénieur BTP Junior" + one incidental "Docker" sentence -> 0.70 (bug)
    "Serveur en Restaurant" + boilerplate "AWS, Azure" line   -> 0.70 (bug)

The fix has two parts:
  1. src/pipeline/scoring.py: _score_domain_relevance now checks
     off_domain_keywords FIRST, before any positive-evidence tier.
  2. config/filters.yaml: the devops/cloud domains' `description` keyword
     lists no longer contain bare generic tool/vendor names (docker, aws,
     azure, gcp, kubernetes, terraform, ansible, linux, microservices) —
     those already have a dedicated home in `domain_skill_vocabulary` for
     skills_match's fallback bonus, and are not unambiguous enough to
     confirm domain relevance on their own.

No live network, no DB, no collectors — pure function calls against
real config/profile.yaml + config/filters.yaml vocabulary (fix #2) and
minimal hand-built configs (fix #1, isolated from vocabulary drift).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.pipeline.filter import load_filters, load_profile
from src.pipeline.scoring import _score_domain_relevance
from tests.fixtures.profiles import TEST_PROFILE_PATH

_GENERIC_BUZZWORDS = {
    "docker", "kubernetes", "terraform", "ansible", "linux",
    "aws", "azure", "gcp", "microservices",
}


def _cfg(
    domain_title: list[str] | None = None,
    domain_desc: list[str] | None = None,
    off_domain: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "scoring": {
            "domain_relevance": {
                "domain_title_keywords": domain_title or [],
                "domain_description_keywords": domain_desc or [],
                "off_domain_keywords": off_domain or [],
            }
        }
    }


def _real_domain_desc_keywords() -> list[str]:
    """Flatten domain_description_keywords exactly as orchestrator.py
    injects them, for the current real profile.yaml target_domains."""
    profile = load_profile(TEST_PROFILE_PATH)
    engine_cfg = load_filters("config/filters.yaml")
    kws: list[str] = []
    for domain in profile.target_domains:
        kws.extend(engine_cfg.get("domain_keywords", {}).get(domain, {}).get("description", []))
    return list(dict.fromkeys(kws))


def _real_domain_title_keywords() -> list[str]:
    profile = load_profile(TEST_PROFILE_PATH)
    engine_cfg = load_filters("config/filters.yaml")
    kws: list[str] = []
    for domain in profile.target_domains:
        kws.extend(engine_cfg.get("domain_keywords", {}).get(domain, {}).get("title", []))
    return list(dict.fromkeys(kws))


# ---------------------------------------------------------------------------
# 1. Off-domain title evidence must win over the broad-tech-title fallback
# ---------------------------------------------------------------------------

class TestOffDomainTitleWinsOverBroadTechTitle:
    def test_bare_commercial_is_confirmed_wrong(self):
        assert _score_domain_relevance(
            "Commercial Junior", None, _cfg(off_domain=["commercial"])
        ) == 0.05

    def test_ingenieur_commercial_no_longer_bypasses_the_gate(self):
        """The exact regression demonstrated in the audit: same off-domain
        evidence ('commercial'), but with 'Ingénieur' prepended — previously
        scored 0.35 because the broad-tech check ran first."""
        score = _score_domain_relevance(
            "Ingénieur Commercial Junior", None, _cfg(off_domain=["commercial"])
        )
        assert score == 0.05, (
            f"'Ingénieur Commercial Junior' must be confirmed off-domain (0.05), "
            f"got {score} -- off-domain evidence must win over a broad-tech word"
        )

    @pytest.mark.parametrize(
        "broad_tech_word",
        ["Ingénieur", "Technicien", "Analyste", "Developer", "Engineer", "Architecte"],
    )
    def test_off_domain_wins_regardless_of_which_broad_tech_word_precedes_it(
        self, broad_tech_word: str
    ):
        title = f"{broad_tech_word} Commercial Junior"
        score = _score_domain_relevance(title, None, _cfg(off_domain=["commercial"]))
        assert score == 0.05, f"{title!r} must stay confirmed off-domain, got {score}"


# ---------------------------------------------------------------------------
# 2 & 3. Generic tech buzzwords cannot manufacture domain relevance via
#         an incidental description mention (real filters.yaml vocabulary)
# ---------------------------------------------------------------------------

class TestGenericTechBuzzwordsCannotConfirmDomainViaDescription:
    def test_btp_job_with_incidental_docker_mention_stays_weak(self):
        cfg = _cfg(domain_desc=_real_domain_desc_keywords())
        score = _score_domain_relevance(
            "Ingénieur BTP Junior",
            "Chantier, genie civil, batiment, junior debutant. "
            "Nous utilisons Docker pour notre outil de planning interne.",
            cfg,
        )
        assert score < 0.7, (
            f"An incidental 'Docker' mention in an unrelated BTP posting must "
            f"not confirm domain relevance, got {score}"
        )

    def test_restaurant_job_with_incidental_aws_azure_boilerplate_stays_weak(self):
        cfg = _cfg(domain_desc=_real_domain_desc_keywords())
        score = _score_domain_relevance(
            "Serveur Junior en Restaurant",
            "Service en salle, restauration, junior debutant. "
            "Stack technique du groupe: AWS, Azure.",
            cfg,
        )
        assert score < 0.7, (
            f"An incidental 'AWS'/'Azure' boilerplate line in a restaurant "
            f"posting must not confirm domain relevance, got {score}"
        )

    def test_generic_buzzwords_removed_from_real_devops_cloud_description_vocab(self):
        """Direct vocabulary-source check: these terms must no longer be
        used as domain_relevance description evidence for devops/cloud —
        they still exist for skills_match via domain_skill_vocabulary."""
        engine_cfg = load_filters("config/filters.yaml")
        for domain in ("devops", "cloud"):
            desc = engine_cfg.get("domain_keywords", {}).get(domain, {}).get("description", [])
            desc_lower = {d.lower() for d in desc}
            overlap = _GENERIC_BUZZWORDS & desc_lower
            assert not overlap, (
                f"domain_keywords.{domain}.description still contains generic "
                f"buzzwords that must live only in domain_skill_vocabulary: {overlap}"
            )


# ---------------------------------------------------------------------------
# 4 & 5. Legitimate domain matches must still work
# ---------------------------------------------------------------------------

class TestLegitimateDomainMatchesStillWork:
    def test_devops_title_still_scores_confirmed(self):
        cfg = _cfg(domain_title=_real_domain_title_keywords())
        score = _score_domain_relevance(
            "DevOps Engineer Junior",
            "Infrastructure as code, CI/CD pipelines, cloud automation.",
            cfg,
        )
        assert score == 1.0, (
            f"A genuinely titled DevOps job must still score confirmed (1.0), got {score}"
        )

    def test_cybersecurity_title_still_scores_confirmed(self):
        cfg = _cfg(domain_title=_real_domain_title_keywords())
        score = _score_domain_relevance(
            "Ingénieur Cybersécurité Junior", "SOC, SIEM, pentest.", cfg
        )
        assert score == 1.0

    def test_it_support_title_still_scores_confirmed(self):
        cfg = _cfg(domain_title=_real_domain_title_keywords())
        score = _score_domain_relevance(
            "Technicien Support Informatique Junior", "Helpdesk, maintenance.", cfg
        )
        assert score == 1.0

    def test_generic_engineer_title_with_no_domain_or_off_domain_evidence_is_still_broad_tech(self):
        """A genuinely off-domain job the vocabulary simply doesn't cover
        (no off_domain_keywords hit at all) still lands at the pre-existing
        broad-tech fallback (0.35) — this fix only reorders evidence, it does
        not expand what counts as off-domain."""
        assert _score_domain_relevance("Ingénieur Agronome", "Agriculture, sols.", _cfg()) == 0.35

    def test_generic_technicien_title_still_broad_tech_when_no_evidence_either_way(self):
        assert _score_domain_relevance(
            "Technicien de Maintenance Industrielle", None, _cfg()
        ) == 0.35


# ---------------------------------------------------------------------------
# 6. Existing false-positive protections remain intact
# ---------------------------------------------------------------------------

class TestExistingFalsePositiveProtectionsIntact:
    def test_domain_keyword_still_takes_priority_over_broad_tech(self):
        cfg = _cfg(domain_title=["cybersécurité", "cybersecurity"])
        assert _score_domain_relevance("Python Cybersecurity Engineer", None, cfg) == 1.0

    def test_off_domain_keyword_still_word_boundary_guarded(self):
        cfg = _cfg(off_domain=["vendeur"])
        assert _score_domain_relevance("Développeur Python", None, cfg) != 0.05

    def test_business_developer_context_qualifier_still_applies(self):
        """'Business Developer' must not earn a domain bonus from 'developer'
        alone (existing _BUSINESS_CONTEXT_QUALIFIER protection, unaffected
        by the evidence-hierarchy reorder)."""
        cfg = _cfg(domain_title=["developer"])
        score = _score_domain_relevance("Business Developer", None, cfg)
        assert score != 1.0

    def test_undetected_domain_still_lands_at_undetermined(self):
        cfg = _cfg(domain_title=["développeur", "cybersécurité"])
        score = _score_domain_relevance("Alternance Learning & Development H/F", None, cfg)
        assert score == 0.45

    def test_confirmed_off_domain_title_still_returns_min(self):
        cfg = _cfg(
            domain_title=["développeur", "cybersécurité"],
            off_domain=["commercial", "recruteur"],
        )
        assert _score_domain_relevance("Recruteur H/F", None, cfg) == 0.05
