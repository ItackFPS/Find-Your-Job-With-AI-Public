"""The lab agent: exact attribution of missed accepts, data-only hypotheses, experiments, memory,
recommendations for a human — and nothing outside the lab."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation
from src.lab.agent import AgentError, investigate, run_agent
from src.lab.memory import LabMemory
from src.lab.store import LabStore

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
PROFILE = ROOT / "tests" / "fixtures" / "ai_filter_benchmark" / "profile.yaml"


def _bad_config(tmp_path: Path) -> Path:
    """The real config plus a harmful seniority keyword: 'junior' now rejects wanted jobs."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("filters.yaml", "scoring.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)
    data = yaml.safe_load((cfg / "filters.yaml").read_text(encoding="utf-8"))
    data["seniority_exclusions"]["senior"] = [*data["seniority_exclusions"]["senior"], "junior"]
    (cfg / "filters.yaml").write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=True), encoding="utf-8")
    return cfg


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DS)


def test_investigate_attributes_missed_accepts_to_the_exact_keyword(dataset, tmp_path):
    strategy = StrategyRef(config_dir=str(_bad_config(tmp_path)), profile_path=str(PROFILE))
    run = run_evaluation(dataset, strategy)
    findings = investigate(dataset, run, strategy)
    rec = [f for f in findings if f.kind == "recoverable_rejection"]
    assert len(rec) == 1 and rec[0].details["keyword"] == "junior"
    assert rec[0].details["path"] == ["seniority_exclusions", "senior"]
    new_list = rec[0].overrides["filters.yaml"]["seniority_exclusions"]["senior"]
    assert "junior" not in new_list and len(rec[0].job_ids) >= 5
    for f in findings:                                      # every finding names the jobs it rests on
        assert f.job_ids and f.statement


def test_findings_without_a_data_lever_are_left_to_a_human(dataset):
    strategy = StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(PROFILE))
    findings = investigate(dataset, run_evaluation(dataset, strategy), strategy)
    no_lever = [f for f in findings if f.kind == "no_lever_rejection"]
    assert no_lever and all(f.overrides is None for f in no_lever)          # the baseline's 1 missed accept


def test_the_agent_refuses_to_run_unguarded(dataset, tmp_path):
    store = LabStore(tmp_path / "lab.db")
    try:
        with pytest.raises(AgentError, match="write guard"):
            run_agent(store, dataset, StrategyRef(profile_path=str(PROFILE)), bundle_store=tmp_path / "s")
    finally:
        store.close()


AGENT = textwrap.dedent('''
    import json, sys
    from src.lab.__main__ import main
    rc = main(["--lab-db", sys.argv[1], "agent", sys.argv[2], "--base-config", sys.argv[3], "--profile", sys.argv[4],
               "--store", sys.argv[5], "--json"])
    sys.exit(rc)
''')


def _agent(tmp_path: Path, cfg: Path) -> dict:
    out = subprocess.run([sys.executable, "-c", AGENT, str(tmp_path / "lab.db"), str(DS), str(cfg), str(PROFILE),
                          str(tmp_path / "store")], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def test_full_cycle_recommends_a_fix_for_human_review_and_remembers_it(tmp_path):
    cfg = _bad_config(tmp_path)
    report = _agent(tmp_path, cfg)
    assert report["baseline"]["fn"] >= 5
    [h] = report["hypotheses"]
    assert h["verdict"] == "improvement" and h["eligible"] is True
    assert h["delta"]["fn"] < 0 and h["delta"]["unsafe_rejection_rate"] < 0
    assert "approve " + h["experiment_id"] in h["recommendation"] and "HUMAN" in h["recommendation"]

    store = LabStore(tmp_path / "lab.db")
    try:
        mem = LabMemory(store)
        kinds = {m["kind"]: m for m in mem.recall()}
        assert set(kinds) == {"evidence", "hypothesis", "observation", "conclusion"}
        hyp = mem.recall(kind="hypothesis")[0]
        assert hyp["status"] == "confirmed" and [s["status"] for s in hyp["history"]] == ["active", "confirmed"]
        assert store.get_experiment(h["experiment_id"])["eligible_for_approval"] == 1
        assert store._conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0     # never approves
    finally:
        store.close()

    again = _agent(tmp_path, cfg)                         # same inputs: nothing re-tested
    assert again["hypotheses"] == [] and "already tested" in again["skipped"][0]["why"]


def test_memory_is_immutable_and_staleness_follows_the_inputs(tmp_path):
    store = LabStore(tmp_path / "lab.db")
    try:
        mem = LabMemory(store)
        inputs = {"dataset_id": "d", "dataset_jobs_sha256": "j", "dataset_labels_sha256": "l", "strategy_fingerprint": "f"}
        item = mem.add("evidence", "x rejects 2 wanted jobs", inputs=inputs, key="k")
        assert mem.recall(current=inputs)[0]["stale"] is False
        assert mem.recall(current={**inputs, "dataset_labels_sha256": "l2"})[0]["stale"] is True
        mem.set_status(item, "retracted", "relabelled")
        assert mem.recall()[0]["status"] == "retracted"
        import sqlite3

        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            store._conn.execute("UPDATE memory_items SET statement = 'y'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            store._conn.execute("DELETE FROM memory_status")
        with pytest.raises(ValueError):
            mem.set_status(item, "confirmed", " ")
    finally:
        store.close()


def test_mined_exclusions_spare_forms_of_wanted_vocabulary_and_carry_a_caution(dataset):
    from src.evaluation.models import Decision
    from src.lab.agent import MINED_CAUTION

    strategy = StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(PROFILE))
    run = run_evaluation(dataset, strategy)
    jobs = {j["id"]: j for j in dataset.job_dicts}
    rejects = [jid for jid, lab in dataset.labels.items() if lab.expected == "reject"][:4]
    # pretend the baseline accepted 4 reject-labelled jobs whose titles share two tokens
    titled = dataset.__class__(dataset.path, dataset.manifest,
                               [{**j, "title": "Ingenieure Vendangeuse"} if j["id"] in rejects else j
                                for j in dataset.job_dicts], dataset.labels, dataset.unlabeled_ids)
    fake = run.model_copy(update={"decisions": [
        Decision(job_id=d.job_id, verdict="accept" if d.job_id in rejects else d.verdict, stage=d.stage)
        for d in run.decisions]})
    mined = {f.details["token"]: f for f in investigate(titled, fake, strategy) if f.kind == "false_positive_token"}
    assert "vendangeuse" in mined and "ingenieure" not in mined     # a form of the IT title "ingenieur"
    assert mined["vendangeuse"].details["caution"] == MINED_CAUTION
    assert jobs                                                     # dataset untouched
