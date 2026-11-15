"""Line-of-work vocabulary lives in config/presets, not in code.

Scoring's mid-tier role hints and the Stats page groupings come from the preset that
applies to the profile: an IT profile keeps them, any other profile gets neutral ones.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.discovery.presets import load_presets
from src.models.job import Job
from src.models.profile import JobSearchProfile
from src.pipeline.filter import load_filters
from src.pipeline.scoring import _score_domain_relevance, build_scoring_config, load_scoring_config
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

CONFIG = Path(__file__).parent.parent / "config"
FILTERS = load_filters(str(CONFIG / "filters.yaml"))
IT = next(p for p in load_presets() if p.name == "it")


def _scoring_cfg(profile: JobSearchProfile) -> dict:
    return build_scoring_config(
        load_scoring_config(str(CONFIG / "scoring.yaml")), profile, FILTERS, esco_synonyms={}
    )


def test_the_it_preset_carries_the_vocabulary_that_left_the_code() -> None:
    assert "ingenieur" in IT.broad_title_indicators and "devops" in IT.broad_title_indicators
    assert IT.technologies_only is True
    assert "docker" in IT.tool_keywords
    assert "Cybersecurity" in {name for name, _ in IT.title_categories}


@pytest.mark.parametrize("title,expected_it,expected_nurse", [
    ("Ingénieur calcul", 0.35, 0.45),
    ("Technicien de maintenance", 0.35, 0.45),
    ("Boulanger", 0.45, 0.45),
])
def test_mid_tier_role_hint_applies_only_to_an_it_profile(title, expected_it, expected_nurse) -> None:
    it_cfg = _scoring_cfg(JobSearchProfile(target_domains=["devops"]))
    nurse_cfg = _scoring_cfg(JobSearchProfile(target_domains=["infirmier"]))
    assert _score_domain_relevance(title, None, it_cfg) == expected_it
    assert _score_domain_relevance(title, None, nurse_cfg) == expected_nurse


def test_a_profile_without_domains_gets_no_role_hint() -> None:
    cfg = _scoring_cfg(JobSearchProfile())
    assert cfg["scoring"]["domain_relevance"]["broad_title_indicators"] == []


@pytest.fixture
def stats_storages(tmp_path: Path):
    db = str(tmp_path / "t.db")
    s = Storage(db_path=db)
    s.init_db()
    for job in (
        Job(id="n1", title="Infirmier DE", source="test", skills=["soins infirmiers", "python"]),
        Job(id="n2", title="Aide-soignant", source="test", skills=[
            "soins infirmiers", "Configurer le poste de travail aux besoins de l'utilisateur"]),
    ):
        s.upsert_job(job)
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.create_run("run-1")
    rs.complete_run("run-1", {})
    rs.store_job_scores("run-1", [{"job_id": "n1", "final_score": 0.9}, {"job_id": "n2", "final_score": 0.8}])
    yield s, rs
    s.close()
    rs.close()


def test_stats_for_a_non_it_profile_keep_its_skills_and_drop_sentence_noise(stats_storages) -> None:
    from src.api.routes.jobs import compute_job_stats

    s, rs = stats_storages
    stats = compute_job_stats(s, rs, JobSearchProfile(target_domains=["infirmier"]))
    names = {f.name: f.count for f in stats.skills}
    assert names == {"soins infirmiers": 2, "python": 1}
    assert stats.tools == []
    assert stats.title_categories == []


def test_stats_for_an_it_profile_keep_the_technology_filter(stats_storages) -> None:
    from src.api.routes.jobs import compute_job_stats

    s, rs = stats_storages
    stats = compute_job_stats(s, rs, JobSearchProfile(target_domains=["devops"]))
    assert {f.name for f in stats.skills} == {"python"}


def test_stats_without_a_profile_are_neutral(stats_storages) -> None:
    from src.api.routes.jobs import compute_job_stats

    s, rs = stats_storages
    stats = compute_job_stats(s, rs)
    assert "soins infirmiers" in {f.name for f in stats.skills}
    assert stats.title_categories == []


def _linkedin_config(tmp_path: Path, linkedin_yaml: str, profile_yaml: str | None):
    from src.linkedin.scanner import LinkedInConfig

    (tmp_path / "linkedin.yaml").write_text(linkedin_yaml, encoding="utf-8")
    if profile_yaml is not None:
        (tmp_path / "profile.yaml").write_text(profile_yaml, encoding="utf-8")
    return LinkedInConfig.load(str(tmp_path))


def test_linkedin_without_queries_uses_the_it_preset_for_an_it_profile(tmp_path: Path) -> None:
    config = _linkedin_config(tmp_path, "enabled: true\n", "target_domains: [devops]\n")
    assert "devops engineer" in [q.search_term for q in config.queries]
    assert all(q.location == "France" for q in config.queries)


def test_linkedin_without_queries_searches_the_profile_itself_for_other_work(tmp_path: Path) -> None:
    config = _linkedin_config(
        tmp_path, "enabled: true\n",
        "target_domains: [infirmier]\n"
        "target_occupations:\n  - {label: electrician, keyword: electrician, label_fr: électricien}\n",
    )
    assert [q.search_term for q in config.queries] == ["infirmier", "électricien"]


def test_linkedin_listed_queries_win_over_the_profile(tmp_path: Path) -> None:
    config = _linkedin_config(
        tmp_path, "enabled: true\nqueries:\n  - {search_term: mine, location: Lyon}\n",
        "target_domains: [devops]\n")
    assert [(q.search_term, q.location) for q in config.queries] == [("mine", "Lyon")]


def test_linkedin_without_a_profile_searches_nothing(tmp_path: Path) -> None:
    assert _linkedin_config(tmp_path, "enabled: true\n", None).queries == []


def test_apply_preset_collector_config_never_overrides_the_sources_own_keys() -> None:
    from src.discovery.query_expansion import apply_preset_collector_config

    profile = JobSearchProfile(target_domains=["devops"])
    assert apply_preset_collector_config({"domains": ["sante"]}, profile, "hellowork") == {
        "domains": ["sante"]}
    assert apply_preset_collector_config({}, profile, "hellowork") == {
        "domains": ["informatique", "data-et-ia"]}
    assert apply_preset_collector_config({}, JobSearchProfile(target_domains=["infirmier"]),
                                         "hellowork") == {}
