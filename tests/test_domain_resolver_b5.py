"""Phase B B5-A: typed line-of-work labels reach the curated vocabulary, and the vocabulary no longer
admits the other lines of work that share IT words (utility networks, embedded and safety engineering,
functional information-system roles). Titles from the B5-0 classified sample, shipped config, test
profiles only (never the personal profile)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.explain import _title_matched_domain
from src.pipeline.filter import build_filter_config, load_profile, matches_title_keywords
from src.pipeline.scoring import _score_domain_relevance, build_scoring_config
from tests.fixtures.profiles import MULTI_PROFILES

ROOT = Path(__file__).resolve().parents[1]
ENGINE = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
TYPED = JobSearchProfile(target_domains=["Administration systèmes", "Support informatique", "Réseaux informatiques",
                                          "Cybersécurité", "Maintenance informatique"], focus_domain=True)
KEYED = load_profile(MULTI_PROFILES["P1"])

IN_FIELD = [
    "Technicien Helpdesk H/F", "Technicien de proximité CDI (H/F)", "Administrateur réseaux (H/F)",
    "Technicien Système et Réseaux (H/F)", "Ingénieur Réseaux LAN / WiFi / WAN (H/F)",
    "Expert Senior Réseaux & Télécoms / ToIP - Freelance (H/F)", "Ingénieur sécurité infra H/F",
    "Responsable Sécurité des Systèmes d'Information (RSSI) (H/F)", "Technicien réseau datacenter CDI (H/F)",
]
OTHER_LINE_OF_WORK = [
    "Technicien(ne) Détection Réseaux (H/F)", "Monteur réseaux électriques B2T (H/F)",
    "CHARGE D'AFFAIRES RESEAUX HUMIDES (H/F)", "Ingénieur d'études réseaux de chaleur urbain/biomasse (H/F)",
    "Ingénieur systèmes embarqués (H/F)", "Avionics Systems Engineer (H/F)",
    "Ingénieur Sécurité des Procédés (H/F) – Études PSS", "Stage Ingénieur Sécurité Environnement (H/F)",
    "Architecte fonctionnel / fonctionnelle de système d'information (H/F)", "Commercial Cybersécurité H/F",
]


def _job(title: str) -> Job:
    return Job(id="j", source="test", title=title, description="", canonical_url="https://example.test/1")


@pytest.mark.parametrize("profile", [TYPED, KEYED], ids=["typed labels", "curated keys"])
@pytest.mark.parametrize("title", IN_FIELD)
def test_in_field_titles_pass_the_hard_filter(profile, title) -> None:
    assert matches_title_keywords(_job(title), build_filter_config(profile, ENGINE)), title


@pytest.mark.parametrize("profile", [TYPED, KEYED], ids=["typed labels", "curated keys"])
@pytest.mark.parametrize("title", OTHER_LINE_OF_WORK)
def test_other_lines_of_work_sharing_it_words_stay_rejected(profile, title) -> None:
    assert not matches_title_keywords(_job(title), build_filter_config(profile, ENGINE)), title


def test_an_exclusion_group_never_applies_to_its_own_line_of_work() -> None:
    embedded = JobSearchProfile(target_domains=["Systèmes embarqués"], focus_domain=True)
    assert matches_title_keywords(_job("Ingénieur systèmes embarqués (H/F)"), build_filter_config(embedded, ENGINE))


def test_filter_scorer_and_explanation_read_a_typed_label_the_same_way() -> None:
    """The consumers share one resolver (src/pipeline/domain_vocabulary.py)."""
    scoring = build_scoring_config({}, TYPED, ENGINE, esco_synonyms={})
    filt = build_filter_config(TYPED, ENGINE)
    for title in IN_FIELD[:5]:
        assert matches_title_keywords(_job(title), filt)
        assert _score_domain_relevance(title, "", scoring) == 1.0, title
        assert _title_matched_domain(title, ENGINE["domain_keywords"], list(TYPED.target_domains)) is not None, title


def test_discovery_reads_the_resolved_domains() -> None:
    from src.discovery.query_expansion import DiscoveryQueryExpander

    domains = DiscoveryQueryExpander(config_dir=str(ROOT / "config"))._domains(TYPED)
    assert domains[:5] == list(TYPED.target_domains)
    assert {"systems", "it_support", "networking"} <= set(domains)
