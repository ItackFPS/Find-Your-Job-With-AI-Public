"""The experiment's latency guardrail: speed on comparable work, with the aggregate cost kept visible.

A candidate that accepts more jobs also scores and assesses more of them, so total replay time ÷ dataset
size (the former measure) calls it slower without it being slower per job. Here each timed stage of the
replay (``filter``, ``requirements_gate``, ``scoring``, ``assessment_accepted``, ``assessment_rejected``) is
normalised by the jobs that stage actually processed (``RunResult.timings["stage_jobs"]``):

* ``expected_ms`` = Σ over stages of the baseline's per-job cost × the candidate's jobs at that stage —
  what the baseline's code would have taken for the candidate's workload;
* ``workload_adjusted_ratio`` = the candidate's actual stage time ÷ ``expected_ms``. The guardrail passes
  when it is within ``tolerance`` (+ ``floor_ms`` absolute, for timer granularity on tiny datasets);
* ``aggregate_ratio`` (candidate ÷ baseline total stage time) and the per-stage job counts are reported
  next to it, so a strategy that processes many more jobs never looks free — it is just not called slow.

Each side is measured on at least two replays and the per-stage minimum is used: the first replay in a
process pays imports and cold caches (seconds, against milliseconds warm) and, being the baseline's, would
otherwise make any candidate look fast. Results without ``stage_jobs`` (older runs) fall back to the former
per-job mean, labelled ``method: aggregate_per_job``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.evaluation.models import RunResult

STAGES = ("filter", "requirements_gate", "scoring", "assessment_accepted", "assessment_rejected")
DEFAULT_TOLERANCE = 1.5
FLOOR_MS = 2.0


def _best(runs: Sequence[RunResult]) -> tuple[dict[str, float], dict[str, int]] | None:
    """Per-stage minimum seconds over the runs and their common workload; ``None`` if the workloads differ."""
    seconds: dict[str, float] = {}
    jobs = dict(runs[0].timings["stage_jobs"])
    for run in runs:
        if run.timings["stage_jobs"] != jobs:
            return None
        for stage in STAGES:
            if stage in run.timings["stage_seconds"]:
                s = float(run.timings["stage_seconds"][stage])
                seconds[stage] = min(s, seconds.get(stage, s))
    return seconds, jobs


def _legacy(baseline: Sequence[RunResult], candidate: Sequence[RunResult], tolerance: float, floor_ms: float) -> dict[str, Any]:
    b_ms = min(r.timings["per_job_ms_mean"] for r in baseline)
    c_ms = min(r.timings["per_job_ms_mean"] for r in candidate)
    return {"passed": c_ms <= b_ms * tolerance + floor_ms, "method": "aggregate_per_job",
            "baseline_ms": b_ms, "candidate_ms": c_ms, "tolerance": tolerance}


def latency_guardrail(baseline: Sequence[RunResult], candidate: Sequence[RunResult], *,
                      tolerance: float = DEFAULT_TOLERANCE, floor_ms: float = FLOOR_MS) -> dict[str, Any]:
    """Compare replays of the same dataset; ``baseline`` / ``candidate`` are ≥1 replays of each strategy."""
    if not all("stage_jobs" in r.timings for r in (*baseline, *candidate)):
        return _legacy(baseline, candidate, tolerance, floor_ms)
    b_best, c_best = _best(baseline), _best(candidate)
    if b_best is None or c_best is None:          # the strategy is not deterministic: no comparable workload
        return {"passed": False, "method": "workload_adjusted_stages",
                "why": "replays of one strategy processed different workloads"}
    (b_s, b_n), (c_s, c_n) = b_best, c_best
    stages: dict[str, dict[str, Any]] = {}
    expected = actual = b_total = 0.0
    unmeasured = []
    for stage in STAGES:
        if stage not in b_s and stage not in c_s:
            continue
        bn, cn = b_n.get(stage, 0), c_n.get(stage, 0)
        bms, cms = b_s.get(stage, 0.0) * 1000, c_s.get(stage, 0.0) * 1000
        b_per = bms / bn if bn else None
        c_per = cms / cn if cn else None
        if b_per is None and cn:          # no baseline cost to compare with: count the candidate's work at par
            unmeasured.append(stage)
        expected += b_per * cn if b_per is not None else cms
        actual += cms
        b_total += bms
        stages[stage] = {"baseline_jobs": bn, "candidate_jobs": cn,
                         "baseline_ms_per_job": None if b_per is None else round(b_per, 4),
                         "candidate_ms_per_job": None if c_per is None else round(c_per, 4),
                         "per_job_ratio": round(c_per / b_per, 3) if b_per and c_per is not None else None}
    return {
        "passed": actual <= expected * tolerance + floor_ms, "method": "workload_adjusted_stages",
        "workload_adjusted_ratio": round(actual / expected, 3) if expected else None,
        "aggregate_ratio": round(actual / b_total, 3) if b_total else None,
        "baseline_ms": round(b_total, 3), "candidate_ms": round(actual, 3), "expected_ms": round(expected, 3),
        "tolerance": tolerance, "floor_ms": floor_ms, "stages": stages, "unmeasured_in_baseline": unmeasured,
        "replays": {"baseline": len(baseline), "candidate": len(candidate)},
        # end-to-end, including dataset loading and any cold start: visible, never the comparison itself
        "end_to_end_per_job_ms": {"baseline": [r.timings["per_job_ms_mean"] for r in baseline],
                                  "candidate": [r.timings["per_job_ms_mean"] for r in candidate]},
    }
