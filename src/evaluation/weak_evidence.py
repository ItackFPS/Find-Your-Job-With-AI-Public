"""Weak-evidence comparison: the pre-registered AD-2b test, offline (stage L5.4).

Runs one dataset twice through the evaluation path: the baseline strategy as it is, and the candidate
of ``docs/lab/preregistered/2026-11-05-ad2-weak-evidence.json`` (the same config with
``published_age``, ``title_keywords`` and ``seniority`` switched to ``rank``). It then computes
criteria 1-4 of that file:

1. relevant offers (``accept`` labels) reaching the main list or the "outside your criteria" lane,
   candidate vs baseline (pass: at least 2x the baseline count);
2. main-list precision (``accept`` labels in main / labelled in main, borderline left out), with a
   paired percentile bootstrap CI of candidate minus baseline (pass: the CI does not lie wholly below 0);
3. the outside lane holds at most ``outside_lane_size`` offers;
4. no STRONG change: every job whose verdict changes went from a filter rejection to an acceptance that
   carries a weak flag, and every other rejection is unchanged (stage and reasons).

Offline, a job's score is the deterministic score (no semantic blend), the proxy every evaluation
report uses for production's final score. The main list is the accepted, unflagged offers in the
``relevant`` tier or above, the floor the weekly top-N and notifications apply.

Criterion 5 (runtime on a replay of the owner's database) is not computed here: ``timings`` only
compares the two offline runs on the dataset.

Which split may be used is the caller's responsibility (``__main__`` refuses a held-out run: the
decisive measurement waits for the owner, as the pre-registration says).
"""

from __future__ import annotations

import random
import re
import shutil
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from src.evaluation.dataset import Dataset
from src.evaluation.models import Decision, LabelRecord, StrategyRef

PREREGISTRATION = "docs/lab/preregistered/2026-11-05-ad2-weak-evidence.json"
CANDIDATE_RANK = ("published_age", "title_keywords", "seniority")
MAIN, OUTSIDE, BELOW, REJECTED = "main", "outside_criteria", "below_threshold", "rejected"
N_BOOT = 2000
SEED = 20261105


@contextmanager
def candidate_config(config_dir: str | Path, checks: Sequence[str] = CANDIDATE_RANK) -> Iterator[Path]:
    """A temporary copy of ``config_dir`` with ``checks`` set to ``rank`` in ``weak_evidence_mode``."""
    with tempfile.TemporaryDirectory(prefix="fyj-weak-") as tmp:
        dst = Path(tmp) / "config"
        shutil.copytree(config_dir, dst)
        path = dst / "filters.yaml"
        text = path.read_text(encoding="utf-8")
        start = text.find("\nweak_evidence_mode:")
        if start < 0:
            raise ValueError(f"{path}: no weak_evidence_mode section")
        head, section = text[:start], text[start:]
        for code in checks:
            section, n = re.subn(rf"^(  {re.escape(code)}:)\s*reject\s*$", r"\1 rank", section, count=1,
                                 flags=re.MULTILINE)
            if n != 1:
                raise ValueError(f"{path}: weak_evidence_mode.{code} is not 'reject'")
        path.write_text(head + section, encoding="utf-8")
        yield dst


def _visible(score: float) -> bool:
    from src.pipeline.scoring import score_to_tier, tier_meets_minimum

    return tier_meets_minimum(score_to_tier(round(score * 100)), "relevant")


def lanes(decisions: Sequence[Decision], scoring_yaml: str | Path) -> dict[str, str]:
    """{job_id: lane} for one run: main, outside_criteria, below_threshold, or rejected."""
    from src.pipeline.lanes import assign_lanes, load_lane_params

    out: dict[str, str] = {}
    weak_rows = []
    for d in decisions:
        if d.verdict != "accept" or d.score is None:
            out[d.job_id] = REJECTED
        elif any(f.startswith("weak:") for f in d.flags):
            weak_rows.append((d.job_id, d.score, {**(d.factors or {}), "weak_flags": [f for f in d.flags
                                                                                     if f.startswith("weak:")]}))
        else:
            out[d.job_id] = MAIN if _visible(d.score) else BELOW
    out.update(assign_lanes(weak_rows, load_lane_params(scoring_yaml)))
    return out


def _by_lane(lane: Mapping[str, str], job_ids: Sequence[str]) -> dict[str, int]:
    return {k: sum(lane.get(j) == k for j in job_ids) for k in (MAIN, OUTSIDE, BELOW, REJECTED)}


def _precision(job_ids: Sequence[str], lane: Mapping[str, str], labels: Mapping[str, str]) -> float | None:
    shown = [labels[j] for j in job_ids if lane.get(j) == MAIN and labels[j] != "borderline"]
    return sum(e == "accept" for e in shown) / len(shown) if shown else None


def _percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    return values[int(q * (len(values) - 1))]


def _filter_code(d: Decision) -> str | None:
    """``filter:<code>[...]`` → ``<code>`` for a filter rejection, else ``None``."""
    if d.stage != "filter" or not d.reasons or not d.reasons[0].startswith("filter:"):
        return None
    return d.reasons[0].split(":")[1]


def compare(baseline: Sequence[Decision], candidate: Sequence[Decision], labels: Mapping[str, LabelRecord],
            scoring_yaml: str | Path, *, ranked: Sequence[str] = CANDIDATE_RANK, n_boot: int = N_BOOT,
            seed: int = SEED) -> dict[str, Any]:
    """Criteria 1-4 of the pre-registration for one dataset (counts only: no job content).

    ``ranked``: the checks the candidate switched to ``rank``."""
    from src.pipeline.lanes import load_lane_params

    lb, lc = lanes(baseline, scoring_yaml), lanes(candidate, scoring_yaml)
    expected = {j: lab.expected for j, lab in labels.items()}
    relevant = sorted(j for j, e in expected.items() if e == "accept")

    def reached(lane: Mapping[str, str]) -> int:
        return sum(lane.get(j) in (MAIN, OUTSIDE) for j in relevant)

    rb, rc = reached(lb), reached(lc)
    c1 = {"relevant_labelled": len(relevant), "baseline": rb, "candidate": rc,
          "candidate_main": sum(lc.get(j) == MAIN for j in relevant),
          "candidate_outside": sum(lc.get(j) == OUTSIDE for j in relevant),
          "relevant_by_lane": {"baseline": _by_lane(lb, relevant), "candidate": _by_lane(lc, relevant)},
          "passes": rc >= 2 * rb and rc > rb}

    ids = sorted(expected)
    pb, pc = _precision(ids, lb, expected), _precision(ids, lc, expected)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        sample = [rng.choice(ids) for _ in ids] if ids else []
        b, c = _precision(sample, lb, expected), _precision(sample, lc, expected)
        if b is not None and c is not None:
            diffs.append(c - b)
    ci = [_percentile(diffs, 0.025), _percentile(diffs, 0.975)] if len(diffs) >= n_boot // 2 else None
    shown = {"baseline": sum(lb.get(j) == MAIN and expected[j] != "borderline" for j in ids),
             "candidate": sum(lc.get(j) == MAIN and expected[j] != "borderline" for j in ids)}
    c2 = {"baseline": pb, "candidate": pc, "labelled_shown": shown, "diff_ci95": ci,
          "passes": None if ci is None else ci[1] >= 0.0}

    size = load_lane_params(scoring_yaml).size
    outside = sum(v == OUTSIDE for v in lc.values())
    c3 = {"outside_size": outside, "max": size, "passes": outside <= size}

    db, dc = {d.job_id: d for d in baseline}, {d.job_id: d for d in candidate}
    changed = sorted(j for j in db if db[j].verdict != dc[j].verdict)
    weak_only = [j for j in changed if db[j].verdict == "reject" and db[j].stage == "filter"
                 and dc[j].verdict == "accept" and any(f.startswith("weak:") for f in dc[j].flags)]
    still_rejected = [j for j in db if db[j].verdict == dc[j].verdict == "reject"
                      and (db[j].stage, db[j].reasons) != (dc[j].stage, dc[j].reasons)]
    # a job the baseline rejected on a now-ranked check may still be rejected by a later check: that is the
    # filter's first-failure order, not a change to a STRONG rejection
    weak_first = [j for j in still_rejected if _filter_code(db[j]) in ranked]
    other_rejections_changed = [j for j in still_rejected if j not in weak_first]
    c4 = {"verdict_changes": len(changed), "weak_flagged_acceptances": len(weak_only),
          "rejected_by_a_later_check": len(weak_first),
          "strong_or_other_changes": len(changed) - len(weak_only) + len(other_rejections_changed),
          "passes": len(weak_only) == len(changed) and not other_rejections_changed}

    return {"lanes": {"baseline": _by_lane(lb, list(lb)), "candidate": _by_lane(lc, list(lc))},
            "criterion_1": c1, "criterion_2": c2, "criterion_3": c3, "criterion_4": c4}


def run(dataset: Dataset, strategy: StrategyRef) -> dict[str, Any]:
    """Baseline vs pre-registered candidate on ``dataset`` → the comparison plus run timings."""
    from src.evaluation.heldout import describe
    from src.evaluation.runner import _parse_as_of, frozen_clock, run_decisions

    jobs = dataset.build_jobs()
    as_of = _parse_as_of(dataset.manifest.as_of)
    timings: dict[str, float] = {}
    with frozen_clock(as_of):
        t = time.perf_counter()
        base, _, _, _ = run_decisions(dataset, strategy, jobs)
        timings["baseline"] = time.perf_counter() - t
        with candidate_config(strategy.config_dir) as cand_dir:
            t = time.perf_counter()
            cand, _, _, _ = run_decisions(dataset, strategy.model_copy(update={"config_dir": str(cand_dir)}),
                                          dataset.build_jobs())
            timings["candidate"] = time.perf_counter() - t
    result = compare(base, cand, dataset.labels, Path(strategy.config_dir) / "scoring.yaml")
    return {"preregistration": PREREGISTRATION, "dataset_id": dataset.manifest.dataset_id,
            "split": describe(dataset), "candidate_rank": list(CANDIDATE_RANK), "jobs": len(jobs),
            "score": "deterministic (offline proxy for final_score)", **result,
            "timings_seconds": timings,
            "criterion_5": "not computed offline: needs a replay of a copy of the owner's database"}
