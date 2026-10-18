"""Characterisation of the scoring-config assembly performed by ``run_pipeline``.

The orchestrator injects profile/filters-derived values (preferred seniority
keywords, countries, ordered location preferences, target skills, ESCO synonyms,
domain vocabulary, off-domain keywords, languages) into the loaded
``scoring.yaml`` before scoring. This test pins the exact resulting dictionary
for a frozen profile + frozen vocabularies, captured from a real
``run_pipeline()`` call, so the logic can be extracted into a function without
any behaviour change (see ``build_scoring_config`` tests below).
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from unittest.mock import patch

import yaml

from tests.fixtures.profiles import TEST_PROFILE_PATH

GOLDEN = Path(__file__).parent / "fixtures" / "scoring_config" / "assembled_scoring_config.json"

_FILTERS = textwrap.dedent("""
    domain_keywords:
      software_development: {title: [developer, développeur, python], description: [backend, api]}
      cybersecurity: {title: [security, sécurité], description: [pentest]}
      data: {title: [data analyst], description: [sql]}
    domain_skill_vocabulary:
      software_development: [django, flask, pytest]
      data: [pandas, sql]
    excluded_title_keywords:
      sales: [commercial, vendeur]
      finance: [comptable]
    non_it_title_functions:
      hr: [recruteur]
""")
_SCORING = textwrap.dedent("""
    scoring:
      weights: {location_fit: 0.3, domain_relevance: 0.25, skills_match: 0.2, seniority_match: 0.15, experience_fit: 0.1}
      seniority_match: {neutral_score: 0.5}
      location_fit: {default_score: 0.4}
      skills_match: {denominator_cap: 5}
      domain_relevance: {confirmed_wrong_score: 0.05}
      language: {penalty_required: 0.15}
""")
_ESCO = {"python": ["python programming"], "git": ["version control"]}


def _capture(tmp_path: Path) -> dict:
    """Run the real orchestrator (no sources → no network) and capture the scoring config."""
    from src.pipeline import scoring
    from src.pipeline.orchestrator import run_pipeline

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(TEST_PROFILE_PATH, config_dir / "profile.yaml")
    (config_dir / "filters.yaml").write_text(_FILTERS, encoding="utf-8")
    (config_dir / "scoring.yaml").write_text(_SCORING, encoding="utf-8")

    captured: dict = {}
    esco_calls: list = []
    real = scoring.score_jobs_with_breakdown
    real_build = scoring.build_scoring_config

    def spy(jobs, config, *a, **k):
        captured["config"] = json.loads(json.dumps(config))     # deep, JSON-safe snapshot
        captured["scored_with"] = config
        return real(jobs, config, *a, **k)

    def build_spy(*a, **k):
        captured["built"] = real_build(*a, **k)
        return captured["built"]

    def esco(skills):
        esco_calls.append(list(skills))
        return dict(_ESCO)

    with patch.object(scoring, "score_jobs_with_breakdown", spy), \
            patch.object(scoring, "build_scoring_config", build_spy), \
            patch.object(scoring, "compute_esco_synonym_map", esco):
        run_pipeline(db_path=str(tmp_path / "t.db"), config_dir=str(config_dir))
    assert "config" in captured, "scoring stage was never reached"
    # ESCO is consulted exactly once per run, with the profile's target skills (never per job)
    assert len(esco_calls) == 1
    # the very object returned by the assembly is the one scoring uses (and later stages reuse)
    assert captured["scored_with"] is captured["built"]
    return captured["config"]


def test_orchestrator_assembles_the_pinned_scoring_config(tmp_path):
    config = _capture(tmp_path)
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert config == expected, "scoring-config assembly changed; this must be a pure refactor"


def test_golden_reflects_the_inputs():
    """Sanity: the pinned file really contains the injected, profile-derived values."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    s = expected["scoring"]
    prof = yaml.safe_load(Path(TEST_PROFILE_PATH).read_text(encoding="utf-8"))
    assert s["skills_match"]["target_skills"] == prof["target_skills"]
    assert s["skills_match"]["esco_synonyms"] == _ESCO
    assert s["seniority_match"]["neutral_score"] == 0.5          # file value preserved
    keywords = s["seniority_match"]["preferred_level_keywords"]
    assert {"internship", "stagiaire", "intern", "trainee"} <= set(keywords)
    assert s["domain_relevance"]["off_domain_keywords"][:2] == ["commercial", "vendeur"]


def _direct_inputs():
    from src.pipeline.filter import load_profile

    return (
        yaml.safe_load(_SCORING),
        load_profile(TEST_PROFILE_PATH),
        yaml.safe_load(_FILTERS),
    )


def test_build_scoring_config_equals_the_orchestrator_assembly():
    from src.pipeline import scoring

    scoring_cfg, profile, engine_cfg = _direct_inputs()
    with patch.object(scoring, "compute_esco_synonym_map", lambda skills: dict(_ESCO)):
        built = scoring.build_scoring_config(scoring_cfg, profile, engine_cfg)
    assert json.loads(json.dumps(built)) == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_build_scoring_config_enriches_in_place_and_leaves_profile_and_vocabulary_alone():
    """Same contract as the original inline code: the loaded config is enriched in place and
    returned; the profile and the filters vocabulary are never modified."""
    import copy

    from src.pipeline import scoring

    scoring_cfg, profile, engine_cfg = _direct_inputs()
    before = (copy.deepcopy(engine_cfg), profile.model_dump())
    with patch.object(scoring, "compute_esco_synonym_map", lambda skills: dict(_ESCO)):
        built = scoring.build_scoring_config(scoring_cfg, profile, engine_cfg)
    assert built is scoring_cfg and "esco_synonyms" in scoring_cfg["scoring"]["skills_match"]
    assert (engine_cfg, profile.model_dump()) == before


def test_profile_lists_are_copied_not_aliased():
    from src.pipeline import scoring

    scoring_cfg, profile, engine_cfg = _direct_inputs()
    with patch.object(scoring, "compute_esco_synonym_map", lambda skills: dict(_ESCO)):
        built = scoring.build_scoring_config(scoring_cfg, profile, engine_cfg)
    assert built["scoring"]["skills_match"]["target_skills"] is not profile.target_skills
    assert built["scoring"]["language"]["profile_languages"] is not profile.languages
