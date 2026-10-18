"""Evidence builders (filter + gate + conflicts) — observation only, consistent with the real stages."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.decisions import evidence as ev
from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import frozen_clock, run_decisions
from src.models.job import Job
from src.pipeline.filter import (
    build_filter_config, explain_filter_checks, filter_jobs, load_filters, load_profile,
)

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


@pytest.fixture(scope="module")
def world():
    ds = load_dataset(DS)
    jobs = ds.build_jobs()
    profile = load_profile(str(DS / "profile.yaml"))
    cfg = build_filter_config(profile, load_filters(str(ROOT / "config" / "filters.yaml")))
    with frozen_clock(__import__("datetime").datetime.fromisoformat(ds.manifest.as_of)):
        passing, rejections = filter_jobs(jobs, cfg)
        _decisions, _t, _llm, extras = run_decisions(ds, StrategyRef(config_dir=str(ROOT / "config")), jobs)
    return {"jobs": jobs, "cfg": cfg, "passing": passing, "rejections": rejections, "gate": extras["gate_results"]}


def test_explain_filter_checks_mirrors_filter_jobs_exactly(world):
    """First failing check == the code filter_jobs reports; no failing check == passes."""
    from src.evaluation.runner import frozen_clock as fc
    import datetime as dt

    with fc(dt.datetime.fromisoformat(load_dataset(DS).manifest.as_of)):
        for job in world["jobs"]:
            outcomes = explain_filter_checks(job, world["cfg"], stop_at_first_failure=True)
            failing = [o.code for o in outcomes if not o.passed]
            assert (failing[0] if failing else None) == world["rejections"].get(job.id)


def test_full_evaluation_is_a_superset_and_has_facts(world):
    rejected = [j for j in world["jobs"] if j.id in world["rejections"]]
    assert rejected
    for job in rejected:
        first = explain_filter_checks(job, world["cfg"], stop_at_first_failure=True)
        full = explain_filter_checks(job, world["cfg"])
        assert [o.code for o in full[: len(first)]] == [o.code for o in first]
        assert len(full) == 8 and all(o.observed for o in full)
    assert any(len([o for o in explain_filter_checks(j, world["cfg"]) if not o.passed]) > 1 for j in rejected)


def test_filter_failure_items_label_the_deciding_check_and_optionally_the_rest(world):
    job = next(j for j in world["jobs"] if j.id in world["rejections"])
    code = world["rejections"][job.id]
    one = ev.filter_failure_items(job, world["cfg"], code, all_failing=False)
    assert len(one) == 1 and one[0].source.rule_id == f"filter.{code}" and one[0].state == "violated"
    assert one[0].observed and one[0].dimension == ev.FILTER_DIMENSION[code]
    many = ev.filter_failure_items(job, world["cfg"], code, all_failing=True)
    assert many[0] == one[0] and all("not the deciding check" in m.message for m in many[1:])


def _cfg(**over):
    base = {"locations": {"include": ["france"]}, "remote": {"allowed": []}, "work_modes": {"allowed": ["remote"]},
            "seniority": {"exclude": ["director"]}, "contract_type": {"exclude_title_keywords": ["freelance"]},
            "title_keywords": {"include": ["python"]}, "posted_within_days": 30, "salary_requirements": {}}
    base.update(over)
    return base


def _by_code(items):
    return {i.source.rule_id.removeprefix("filter."): i for i in items}


def test_pass_items_follow_the_labelling_policy():
    job = Job(id="a", title="Python dev", location="Paris", countries=["France"], remote=True,
              published_at=__import__("datetime").datetime.now(__import__("datetime").UTC))
    got = _by_code(ev.filter_pass_items(job, _cfg()))
    # `remote` has no allowed list and `salary` no bounds: those checks cannot reject → no evidence at all
    assert set(got) == {"location", "work_mode", "seniority", "published_age", "title_keywords", "contract_type"}
    assert all(i.state == "satisfied" and i.polarity == "positive" for i in got.values())


def test_missing_data_on_a_configured_check_is_unknown_not_satisfied():
    bare = Job(id="b", title="Python dev")                    # no location, no remote/work_mode, no date
    got = _by_code(ev.filter_pass_items(bare, _cfg()))
    assert got["location"].state == "unknown" and got["work_mode"].state == "unknown"
    assert got["published_age"].state == "unknown" and "unknown is not a rejection" in got["published_age"].message
    assert got["title_keywords"].state == "satisfied"         # the title IS present
    untitled = _by_code(ev.filter_pass_items(Job(id="c", description="x" * 300), _cfg()))
    assert untitled["seniority"].state == "unknown" and untitled["contract_type"].state == "unknown"


@pytest.mark.parametrize("code,cfg,job,expected", [
    ("location", _cfg(locations={"include": []}), Job(id="j", location="Paris"), "not_applicable"),
    ("location", _cfg(), Job(id="j", location="Berlin"), "satisfied"),
    ("location", _cfg(), Job(id="j", department="83"), "unknown"),              # the check never reads department
    ("remote", _cfg(), Job(id="j", remote=True), "not_applicable"),
    ("remote", _cfg(remote={"allowed": ["france"]}), Job(id="j"), "satisfied"),
    ("work_mode", _cfg(work_modes={"allowed": ["remote", "hybrid"]}), Job(id="j", work_mode="remote"), "not_applicable"),
    ("work_mode", _cfg(), Job(id="j", work_mode="remote"), "satisfied"),
    ("salary", _cfg(), Job(id="j", salary_min=1.0), "not_applicable"),
    ("salary", _cfg(salary_requirements={"min_salary": 30000}), Job(id="j", salary_max=40000.0), "unknown"),
    ("salary", _cfg(salary_requirements={"min_salary": 30000}), Job(id="j", salary_min=35000.0), "satisfied"),
    ("seniority", _cfg(seniority={}), Job(id="j", title="Dev"), "not_applicable"),
    ("title_keywords", _cfg(title_keywords={}), Job(id="j", title="Dev"), "not_applicable"),
    ("contract_type", _cfg(contract_type={}), Job(id="j", title="Dev"), "not_applicable"),
    ("contract_type", _cfg(), Job(id="j", employment_type="freelance"), "unknown"),   # title-only check
])
def test_applicability_matrix(code, cfg, job, expected):
    assert ev.applicability(code, job, cfg) == expected


def test_pass_items_never_rerun_the_checks_so_they_cannot_contradict_the_verdict(world, monkeypatch):
    import src.decisions.evidence as e

    def boom(*a, **k):
        raise AssertionError("pass evidence must not re-evaluate the filter")

    monkeypatch.setattr(e, "explain_filter_checks", boom)
    for job in world["passing"]:
        e.filter_pass_items(job, world["cfg"])           # the job already passed every check


def test_expected_is_valid_structured_text_not_a_truncated_fragment(world):
    import json

    for code, text in ev.expected_cache(world["cfg"]).items():
        if text is not None:
            json.loads(text)                              # parses: lists are summarised structurally, never cut mid-JSON
    big = {"locations": {"include": [f"city{i}" for i in range(80)]}}
    assert "+68 more" in ev.expected_cache(big)["location"]


def test_soft_penalties_are_never_labelled_satisfied():
    from src.requirements.models import ConstraintState as CS, EducationRequirement, GateResult, Importance, JobRequirements

    req = JobRequirements(education=EducationRequirement(level_bac_plus=5, level_label="Bac+5", importance=Importance.CORE))
    res = GateResult(passes=True, requirements=req, education_penalty=0.2, experience_penalty=0.2,
                     states={"education": CS.SATISFIED, "experience": CS.SATISFIED, "skills": CS.SATISFIED})
    items = ev.gate_items(res)
    soft = {i.dimension: i for i in items if i.source.rule_id.endswith(".soft")}
    assert set(soft) == {"education", "experience"} and all(i.polarity == "negative" and i.weight == 0.2 for i in soft.values())
    assert not [i for i in items if i.dimension in ("education", "experience") and i.state == "satisfied"]
    assert [i.dimension for i in items if i.state == "satisfied"] == ["skills"]


def test_skills_state_is_unknown_when_no_skill_gate_ran():
    from src.requirements.gate import HardConstraintGate
    from src.requirements.models import JobRequirements

    off = HardConstraintGate(user_max_education_level=2, user_max_years_experience=2,
                             principal_skill_gate=False, mandatory_skill_gate=False)
    on = HardConstraintGate(user_max_education_level=2, user_max_years_experience=2)
    assert off.check(JobRequirements()).states["skills"].value == "unknown"
    assert on.check(JobRequirements()).states["skills"].value == "satisfied"
    items = ev.gate_items(off.check(JobRequirements()))
    assert next(i for i in items if i.dimension == "skills").state == "unknown"


def test_gate_states_agree_with_the_gate_verdict(world):
    for jid, res in world["gate"].items():
        assert set(res.states) == {"education", "experience", "skills"}
        violated = [k for k, v in res.states.items() if v.value == "violated"]
        assert res.passes == (not violated), (jid, res.states, [r.type for r in res.reasons])


def test_gate_items_include_non_violated_states_without_duplicating_violations(world):
    saw_absent = saw_span = False
    for jid, res in world["gate"].items():
        items = ev.gate_items(res)
        viol_dims = {i.dimension for i in items if i.state == "violated"}
        for dim in ("education", "experience", "skills"):
            if dim in viol_dims:      # one violation item per reason, no extra "satisfied/unknown" for that dim
                assert not [i for i in items if i.dimension == dim and i.state != "violated"
                            and i.source.rule_id == f"gate.{dim}"]
        for i in items:
            assert i.source.stage == "requirements_gate" and i.source.rule_version
            saw_absent |= i.message.startswith("No ") and i.state == "satisfied"
            saw_span |= i.span is not None
    assert saw_absent and saw_span


CONTRADICTION = "Poste en télétravail. Présence obligatoire 4 jours sur site par semaine. " * 3


def test_work_mode_conflict_requires_a_real_metadata_claim():
    c = ev.work_mode_conflict(Job(id="c", title="Dev", remote=True, description=CONTRADICTION), keep_quote=True)
    assert c is not None and c.dimensions == ["work_mode"] and "on-site" in c.description.lower()
    quiet = ev.work_mode_conflict(Job(id="n", title="Dev", remote=True, description="Nous codons en Python. " * 20), keep_quote=True)
    assert quiet is None and ev.work_mode_conflict(Job(id="e", title="Dev"), keep_quote=True) is None
    # remote=False is the DEFAULT (unknown), not a claim of on-site: a "fully remote" description is not a conflict
    unknown = Job(id="u", title="Dev", description="Poste 100% télétravail, full remote partout. " * 5)
    assert ev.work_mode_conflict(unknown, keep_quote=True) is None
    explicit = unknown.model_copy(update={"work_mode": "on_site"})
    assert ev.work_mode_conflict(explicit, keep_quote=True) is not None


def test_conflict_quotes_are_withheld_when_job_text_is_not_stored():
    j = Job(id="c", title="Dev", remote=True, description=CONTRADICTION)
    kept = ev.work_mode_conflict(j, keep_quote=True)
    scrubbed = ev.work_mode_conflict(j, keep_quote=False)
    assert "jours sur site" in kept.description.lower() or "on-site" in kept.description.lower()
    assert scrubbed.description == ev.SCRUBBED_CONFLICT and "site" not in scrubbed.description.lower().replace("withheld", "")
