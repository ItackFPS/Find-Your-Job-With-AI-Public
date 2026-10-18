"""The evaluation runner must reproduce what the REAL orchestrator does.

Runs ``run_pipeline()`` on the same 50 synthetic jobs (injected through the
pre-collected-jobs hook, so no collector/network) with the dataset's pinned
profile and the repository's filters/scoring, captures what the scoring stage
received/returned, and compares it with the runner's decisions. This guards the
runner's mirrored wiring (requirements gate, scoring config) against drift.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


class _StopAfterScoring(Exception):
    pass


@pytest.fixture(scope="module")
def captured(tmp_path_factory):
    from src.pipeline import scoring
    from src.pipeline.orchestrator import run_pipeline

    tmp = tmp_path_factory.mktemp("equiv")
    cfg = tmp / "config"
    cfg.mkdir()
    shutil.copy(DS / "profile.yaml", cfg / "profile.yaml")
    for name in ("filters.yaml", "scoring.yaml"):
        shutil.copy(ROOT / "config" / name, cfg / name)

    ds = load_dataset(DS)
    out: dict = {}
    real = scoring.score_jobs_with_breakdown

    def spy(jobs, config, *a, **k):
        res = real(jobs, config, *a, **k)
        out["scored"] = {job.id: (score, factors) for job, score, factors in res}
        raise _StopAfterScoring            # nothing after scoring matters here

    with patch.object(scoring, "score_jobs_with_breakdown", spy), \
            patch.object(scoring, "compute_esco_synonym_map", lambda skills, **k: {}):
        run_pipeline(db_path=str(tmp / "t.db"), config_dir=str(cfg),
                     prefetched_linkedin_jobs=ds.build_jobs())
    assert "scored" in out, "orchestrator never reached the scoring stage"
    return out["scored"]


def test_runner_accepts_exactly_the_jobs_the_orchestrator_scores(captured):
    res = run_evaluation(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config")))
    accepted = {d.job_id for d in res.decisions if d.verdict == "accept"}
    assert len(accepted) >= 20 and accepted == set(captured)


def test_runner_scores_and_factors_equal_the_orchestrators(captured):
    res = run_evaluation(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config")))
    got = {d.job_id: (d.score, d.factors) for d in res.decisions if d.verdict == "accept"}
    assert len(got) >= 20 and got == captured
    assert list(got) == sorted(got)       # runner order is by job id (stable, independent of scoring sort)
