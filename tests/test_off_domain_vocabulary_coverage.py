"""Pass 5.1 regression tests: off_domain_keywords vocabulary coverage audit.

Background: after Pass 5's evidence-hierarchy fix, several clearly
unsuitable sectors still weren't represented in off_domain_keywords
(config/filters.yaml's excluded_title_keywords + non_it_title_functions),
so a posting from one of those sectors landed at the neutral "undetermined"
domain score (0.45) instead of "confirmed wrong" (0.05) whenever an
incidental technology mention (a common target_skill like "Docker") let it
slip past the hard filter's description fallback. Demonstrated in the
2026-09-12 audit: "Ingénieur BTP Junior" + one incidental "Docker" sentence
scored domain_relevance=0.35, final=69/100 ("strong") -- clearly too high
for a civil-engineering posting.

Fix: added narrow, high-confidence vocabulary to THREE NEW exclusion groups
(construction, hospitality, agriculture) plus two existing groups
(healthcare: aide-soignant; sales: English account-executive/business-
developer/sales-representative variants). Every addition is a specific,
multi-word (or genuinely unambiguous single-word/acronym) phrase chosen
specifically to have zero overlap with any real IT job title -- see the
inline comments in config/filters.yaml for the false-positive analysis
behind each group.

One deliberate, documented gap remains: a bare "Serveur"/"Serveur Junior"
title (no restaurant/salle qualifier) is NOT covered, because real French
IT titles ("Administrateur Serveurs", "Technicien Serveurs et Réseaux")
would collide with a blanket "serveur" exclusion. This is asserted below as
current, intentional behavior -- not something this fix claims to solve.

No live network, no DB, no collectors -- pure function calls against real
config/profile.yaml + config/filters.yaml vocabulary and the real
_score_domain_relevance / matches_title_keywords implementations.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.models.job import Job
from src.pipeline.filter import build_filter_config, load_filters, load_profile, matches_title_keywords
from src.pipeline.scoring import _score_domain_relevance, score_job, score_to_tier
from tests.fixtures.profiles import TEST_PROFILE_PATH

_INCIDENTAL_TECH_MENTION = (
    "Nous utilisons Docker et AWS pour nos outils internes de planning."
)


def _job(title: str, description: str = "", **kwargs: Any) -> Job:
    defaults: dict[str, Any] = {
        "source": "test", "title": title, "description": description, "company": "TestCo",
        "remote": False, "location": "Toulon (83)", "countries": ["France"],
        "department": "83", "work_mode": "on_site", "discovered_at": datetime.now(UTC),
    }
    defaults.update(kwargs)
    return Job(**defaults)


def _real_filter_and_scoring_configs() -> tuple[dict[str, Any], dict[str, Any]]:
    """Build filter_cfg (hard filter) and a minimal domain_relevance-only
    scoring config from the REAL config/profile.yaml + config/filters.yaml,
    mirroring orchestrator.py's injection for the pieces this test needs."""
    profile = load_profile(TEST_PROFILE_PATH)
    engine_cfg = load_filters("config/filters.yaml")
    filter_cfg = build_filter_config(profile, engine_cfg)

    off_domain_kws: list[str] = []
    for group in engine_cfg.get("excluded_title_keywords", {}).values():
        off_domain_kws.extend(group)
    for group in engine_cfg.get("non_it_title_functions", {}).values():
        off_domain_kws.extend(group)

    domain_title_kws: list[str] = []
    domain_desc_kws: list[str] = []
    for domain in profile.target_domains:
        kws = engine_cfg.get("domain_keywords", {}).get(domain, {})
        domain_title_kws.extend(kws.get("title", []))
        domain_desc_kws.extend(kws.get("description", []))

    scoring_cfg = {
        "scoring": {
            "domain_relevance": {
                "domain_title_keywords": list(dict.fromkeys(domain_title_kws)),
                "domain_description_keywords": list(dict.fromkeys(domain_desc_kws)),
                "off_domain_keywords": list(dict.fromkeys(off_domain_kws)),
            },
            "location_fit": {"target_department": "83", "preferred_countries": ["france"]},
            "skills_match": {"target_skills": list(profile.target_skills)},
            "seniority_match": {"preferred_level_keywords": ["junior", "alternance", "stagiaire"]},
        }
    }
    return filter_cfg, scoring_cfg


FILTER_CFG, SCORING_CFG = _real_filter_and_scoring_configs()


# ---------------------------------------------------------------------------
# Newly-covered sectors: confirmed-wrong domain + hard-filter rejection,
# including the exact "incidental tech mention" scenario from the audit.
# ---------------------------------------------------------------------------

_NEWLY_COVERED_TITLES = [
    "Conducteur de Travaux",
    "Chef de Chantier",
    "Ingénieur BTP Junior",
    "Commis de Cuisine",
    "Chef de Rang",
    "Employé de Restaurant",
    "Serveur de Restaurant",
    "Serveur en Salle",
    "Agriculteur",
    "Exploitant Agricole",
    "Aide-Soignant",
    "Account Executive",
    "Sales Representative",
    "Business Developer",
]


class TestNewlyCoveredSectorsAreConfirmedOffDomain:
    @pytest.mark.parametrize("title", _NEWLY_COVERED_TITLES)
    def test_confirmed_wrong_with_empty_description(self, title: str):
        score = _score_domain_relevance(title, "", SCORING_CFG)
        assert score == 0.05, f"{title!r} must be confirmed off-domain (0.05), got {score}"

    @pytest.mark.parametrize("title", _NEWLY_COVERED_TITLES)
    def test_confirmed_wrong_survives_incidental_tech_mention(self, title: str):
        """The exact audit scenario: an off-domain job that happens to
        mention a generic tech tool once must still be confirmed off-domain,
        not rescued to 'undetermined' or higher."""
        score = _score_domain_relevance(title, _INCIDENTAL_TECH_MENTION, SCORING_CFG)
        assert score == 0.05, (
            f"{title!r} + incidental tech mention must stay confirmed "
            f"off-domain (0.05), got {score}"
        )

    @pytest.mark.parametrize("title", _NEWLY_COVERED_TITLES)
    def test_final_score_stays_at_the_gated_floor(self, title: str):
        job = _job(title, _INCIDENTAL_TECH_MENTION)
        final = score_job(job, SCORING_CFG)
        assert round(final * 100) == 40, (
            f"{title!r} + incidental tech mention must land at the domain-"
            f"gate floor (40/100), got {round(final * 100)}"
        )
        assert score_to_tier(round(final * 100)) == "low_priority"

    @pytest.mark.parametrize("title", _NEWLY_COVERED_TITLES)
    def test_hard_filter_rejects_even_with_incidental_tech_mention(self, title: str):
        """Defense in depth: the hard filter's exclude/non_it_functions
        check must reject these titles even when a description fallback
        term (like 'docker', a real target_skill) would otherwise pass."""
        job = _job(title, _INCIDENTAL_TECH_MENTION)
        assert matches_title_keywords(job, FILTER_CFG) is False, (
            f"{title!r} + incidental tech mention must still be hard-rejected"
        )


# ---------------------------------------------------------------------------
# Documented, intentional gap: bare "Serveur" is NOT covered (false-positive
# risk against real IT server-infrastructure titles).
# ---------------------------------------------------------------------------

class TestBareServeurRemainsIntentionallyUncovered:
    def test_bare_serveur_junior_is_not_confirmed_off_domain(self):
        """This is NOT a bug: a blanket 'serveur' exclusion would also
        hard-reject legitimate IT titles like 'Administrateur Serveurs
        Windows' or 'Technicien Serveurs et Réseaux'. See the Pass 5.1
        report for the full false-positive analysis."""
        score = _score_domain_relevance("Serveur Junior", "", SCORING_CFG)
        assert score != 0.05, (
            "Documenting current (intentional) behavior: bare 'Serveur' "
            "titles are not in off_domain_keywords by design"
        )

    def test_ingenieur_serveur_would_have_collided_with_a_blanket_ban(self):
        """Proves WHY bare 'serveur' was rejected as a candidate: a title
        containing the exact singular word 'serveur' as IT infrastructure
        vocabulary (e.g. 'Ingénieur Serveur Unix') must never be confirmed
        off-domain. Off-domain matching is word-boundary guarded, so a
        bare 'serveur' entry would NOT catch the more common PLURAL form
        ('Administrateur Serveurs') -- but it would still catch this
        entirely realistic singular phrasing, which is reason enough to
        keep it out of the vocabulary."""
        cfg_with_bare_serveur = {
            "scoring": {
                "domain_relevance": {
                    "domain_title_keywords": SCORING_CFG["scoring"]["domain_relevance"][
                        "domain_title_keywords"
                    ],
                    "off_domain_keywords": ["serveur"],
                }
            }
        }
        score = _score_domain_relevance("Ingénieur Serveur Unix", "", cfg_with_bare_serveur)
        assert score == 0.05, (
            "This demonstrates the false-positive risk a bare 'serveur' "
            "entry would have created for a legitimate IT title -- exactly "
            "why it was deliberately excluded from this fix"
        )


# ---------------------------------------------------------------------------
# False-positive guards: legitimate IT jobs must be completely unaffected.
# ---------------------------------------------------------------------------

_LEGITIMATE_TITLES = [
    "Technicien Support Informatique",
    "Technicien Réseaux",
    "Technicien Systèmes",
    "Technicien Maintenance Informatique",
    "Administrateur Systèmes",
    "Analyste Cybersécurité",
    "Ingénieur Réseaux",
    "Ingénieur DevOps",
    "Développeur Junior",
    "Chef de Projet IT",
    "Responsable Infrastructure Informatique",
    "Ingénieur Systèmes et Réseaux",
    "Administrateur Serveurs Windows",
    "Technicien Serveurs et Réseaux",
    "Ingénieur Cloud AWS",
    "Alternance Développeur Web",
]


class TestLegitimateITJobsUnaffected:
    @pytest.mark.parametrize("title", _LEGITIMATE_TITLES)
    def test_domain_relevance_not_dragged_down_to_confirmed_wrong(self, title: str):
        score = _score_domain_relevance(title, "", SCORING_CFG)
        assert score > 0.05, (
            f"{title!r} must never be confirmed off-domain, got {score}"
        )

    @pytest.mark.parametrize(
        "title",
        [
            "Technicien Support Informatique", "Technicien Réseaux", "Technicien Systèmes",
            "Administrateur Systèmes", "Analyste Cybersécurité", "Ingénieur Réseaux",
            "Ingénieur DevOps", "Développeur Junior",
        ],
    )
    def test_domain_confirmed_titles_still_score_1_0(self, title: str):
        """The titles explicitly named in the Pass 5.1 self-audit checklist
        must remain fully confirmed (1.0), not merely 'not off-domain'."""
        score = _score_domain_relevance(title, "", SCORING_CFG)
        assert score == 1.0, f"{title!r} must still score 1.0, got {score}"

    @pytest.mark.parametrize("title", _LEGITIMATE_TITLES)
    def test_hard_filter_unaffected(self, title: str):
        """Titles that already passed the hard filter before this change
        must still pass it (titles that were already rejected due to a
        separate, pre-existing inclusion-vocabulary gap -- e.g. 'Administrateur
        Serveurs Windows' not matching any configured include keyword -- are
        out of scope for this off-domain-vocabulary fix and are not asserted
        here)."""
        job = _job(title, "")
        # Only assert for titles this profile's real filter_cfg actually
        # accepted before touching anything -- a pre-existing inclusion gap
        # is a different, unrelated issue (see the Pass 5.1 report).
        pre_existing_inclusion_gaps = {
            "Administrateur Serveurs Windows", "Technicien Serveurs et Réseaux",
            "Chef de Projet IT", "Ingénieur Systèmes et Réseaux",
        }
        if title in pre_existing_inclusion_gaps:
            pytest.skip(f"{title!r}: pre-existing, unrelated inclusion-vocabulary gap")
        assert matches_title_keywords(job, FILTER_CFG) is True, (
            f"{title!r} must still pass the hard filter"
        )
