"""weak_evidence_mode: WEAK violations rank with a bounded penalty (ADR 0019 v5 AD-2b; stage L5.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from src.models.job import Job
from src.pipeline import filter as F
from src.pipeline import scoring as S

ROOT = Path(__file__).resolve().parents[1]
RANK_ALL = {c: "rank" for c in F.RANKABLE_CHECKS}


def _cfg(**over):
    base = {"locations": {"include": ["france"]}, "remote": {"allowed": []}, "work_modes": {"allowed": ["remote"]},
            "seniority": {"exclude": ["director"]}, "contract_type": {"exclude_title_keywords": ["freelance"]},
            "title_keywords": {"include": ["python"]}, "posted_within_days": 30, "salary_requirements": {}}
    base.update(over)
    return base


def _ago(days):
    return datetime.now(UTC) - timedelta(days=days)


JOBS = [
    Job(id="ok", title="Python dev", remote=True, published_at=_ago(1)),
    Job(id="senior", title="Python director", remote=True),                       # weak: seniority
    Job(id="old", title="Python dev", remote=True, published_at=_ago(60)),        # weak: age
    Job(id="two", title="Python director freelance", remote=True),                # weak x2
    Job(id="abroad", title="Python dev", remote=True, countries=["Germany"]),     # strong: location
    Job(id="onsite", title="Python dev", work_mode="on_site"),                    # strong: structured mode
    Job(id="pay", title="Python dev", remote=True, salary_min=1.0),               # salary: never ranks
]


def test_shipped_config_keeps_every_check_in_reject():
    engine = yaml.safe_load((ROOT / "config" / "filters.yaml").read_text(encoding="utf-8"))
    assert set(engine["weak_evidence_mode"].values()) == {"reject"}
    assert F.weak_evidence_modes(engine) == {}


def test_modes_accept_only_rankable_checks():
    got = F.weak_evidence_modes({"weak_evidence_mode": {
        "published_age": "rank", "seniority": "reject", "salary": "rank", "location": "rank", "title_keywords": "x"}})
    assert got == {"published_age": "rank"}
    assert F.weak_evidence_modes({"weak_evidence_mode": "rank"}) == {}
    assert "salary" not in F.RANKABLE_CHECKS


def test_reject_mode_is_the_historical_filter():
    flags: dict = {}
    cfg = _cfg(salary_requirements={"min_salary": 30000})
    valid, reasons = F.filter_jobs(JOBS, cfg, weak_flags=flags)
    assert [j.id for j in valid] == ["ok"] and flags == {}
    assert reasons == {"senior": "seniority", "old": "published_age", "two": "seniority",
                       "abroad": "location", "onsite": "work_mode", "pay": "salary"}
    assert F.filter_jobs(JOBS, dict(cfg, weak_evidence_mode={})) == (valid, reasons)


def test_rank_mode_passes_only_weak_violations():
    cfg = _cfg(weak_evidence_mode=RANK_ALL, salary_requirements={"min_salary": 30000})
    flags: dict = {}
    valid, reasons = F.filter_jobs(JOBS, cfg, weak_flags=flags)
    assert {j.id for j in valid} == {"ok", "senior", "old", "two"}
    assert reasons == {"abroad": "location", "onsite": "work_mode", "pay": "salary"}   # strong + salary still reject
    assert {k: [f.code for f in v] for k, v in flags.items()} == {
        "senior": ["seniority"], "old": ["published_age"], "two": ["seniority", "contract_type"]}
    assert flags["senior"][0].observed and flags["senior"][0].severity == 1.0
    plain, _ = F.filter_jobs(JOBS, cfg)                     # without the out-parameter: same verdicts
    assert [j.id for j in plain] == [j.id for j in valid]


def test_a_strong_violation_rejects_even_after_weak_ones():
    job = Job(id="x", title="Python director", countries=["Germany"], remote=True)
    _, reasons = F.filter_jobs([job], _cfg(weak_evidence_mode=RANK_ALL))
    assert reasons == {"x": "location"}


@pytest.mark.parametrize("age,want", [(30, 0.0), (45, 0.25), (60, 0.5), (90, 1.0), (400, 1.0)])
def test_age_severity_ramps_from_one_to_three_times_the_limit(age, want):
    job = Job(id="a", published_at=_ago(age))
    assert F.weak_severity("published_age", job, {"posted_within_days": 30}) == pytest.approx(want, abs=1e-3)
    assert F.weak_severity("seniority", job, {}) == 1.0


def test_penalty_is_bounded():
    assert S.weak_evidence_penalty([], {}) == 0.0
    assert S.weak_evidence_penalty([1.0], {}) == pytest.approx(0.10)
    assert S.weak_evidence_penalty([0.5, 1.0], {}) == pytest.approx(0.15)
    assert S.weak_evidence_penalty([1.0] * 5, {}) == pytest.approx(0.25)
    cfg = {"scoring": {"weak_evidence": {"penalty_per_flag": 0.2, "penalty_total_max": 0.3}}}
    assert S.weak_evidence_penalty([1.0, 1.0], cfg) == pytest.approx(0.3)


def test_penalty_applies_before_the_gates():
    factors = {"location_fit": 0.9, "domain_relevance": 0.9, "skills_match": 0.9,
               "seniority_match": 0.9, "experience_fit": 0.9}
    cfg: dict = {}
    assert S._apply_weights(factors, cfg, weak_penalty=0.1) == pytest.approx(S._apply_weights(factors, cfg) - 0.1)
    gated = dict(factors, domain_relevance=0.05)            # domain gate fires: the cap still bounds it
    assert S._apply_weights(gated, cfg, weak_penalty=0.1) <= 0.40
    assert S._apply_weights(gated, cfg, weak_penalty=0.1) == S._apply_weights(gated, cfg)   # above the cap


# -- pipeline ------------------------------------------------------------------------------------

def _rank_all_filters(cfg_dir: Path) -> None:
    path = cfg_dir / "filters.yaml"
    text = path.read_text(encoding="utf-8")
    head, sep, tail = text.partition("weak_evidence_mode:")
    path.write_text(head + sep + tail.replace(": reject", ": rank"), encoding="utf-8")


def test_pipeline_default_has_no_weak_flags_and_rank_scores_more(tmp_path):
    import sqlite3

    from tests.test_decision_journal_orchestrator import _config_dir
    from tests.test_learning_model_shadow import _pipeline

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    base = _pipeline(a / "jobs.db", _config_dir(a, None))
    assert all(r.weak_flags == [] and r.weak_penalty == 0.0 for r in base.scored_jobs)

    cfg = _config_dir(b, None)
    _rank_all_filters(cfg)
    ranked = _pipeline(b / "jobs.db", cfg)
    base_ids = {r.job_id for r in base.scored_jobs}
    flagged = {r.job_id: r for r in ranked.scored_jobs if r.weak_flags}
    assert flagged and base_ids <= {r.job_id for r in ranked.scored_jobs}
    assert set(flagged).isdisjoint(base_ids)                        # only previously rejected offers get flags
    for rec in flagged.values():
        assert {f["code"] for f in rec.weak_flags} <= set(F.RANKABLE_CHECKS)
        assert 0.0 <= rec.weak_penalty <= 0.25
    conn = sqlite3.connect(b / "jobs.db")
    rows = conn.execute("SELECT job_id, evidence_json FROM job_decisions WHERE run_id = 'run'").fetchall()
    conn.close()
    weak = {jid for jid, ev in rows if '"strength": "weak"' in ev and "ranked instead of rejected" in ev}
    assert weak == set(flagged)
