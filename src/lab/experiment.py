"""Replay a candidate strategy against the baseline on a frozen dataset and issue a verdict.

Verdicts: ``improvement | regression | inconclusive | insufficient_evidence``. Nothing here changes
production; ``eligible_for_approval`` only says a human MAY approve (never that the strategy is
promoted). Guardrails (any failure forbids approval and forces ``regression``):

* ``no_new_unsafe_rejection`` — the candidate rejects no labelled-accept job the baseline accepted
  (absolute ``unsafe_rejection`` counts are reported; the baseline may already have some);
* ``reproducible`` — two runs of the candidate give identical decisions;
* ``latency`` — the candidate's stage time within ``latency_tolerance`` × what the baseline's per-job stage
  costs predict for the candidate's workload (``src/lab/latency.py``: comparable work, the aggregate cost
  and the jobs per stage reported beside it; minimum over interleaved replays, so no cold start counts).

Two tiers, never conflated: ``eligible_for_approval`` (improvement + green guardrails on all labels) says
the candidate is worth a human's review; ``promotion_evidence`` says what supports it —
``independently_supported`` only when the ``human`` labels alone (assistant-assisted and derived ones
excluded) clear the same evidence floor and CI test, else ``exploratory_only`` (``not_eligible`` when not
eligible at all). ``independently_validated`` is true only in the first case. No policy forbids approving
exploratory evidence.

Every result also carries ``comparison``: precision, recall, F1, unsafe-rejection rate, reject recall,
review (abstention) rate and confusion counts for both sides and their deltas, ``ranking`` (how the score
orders the accepted jobs: AUC, precision@k, the visibility floor, and a bootstrap CI on the AUC change —
the only place a scoring-only change shows), the label provenance and the limitations of the comparison.
These are reported, never used to grant approval on their own.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from src.evaluation.dataset import Dataset
from src.evaluation.metrics import evaluate, ranking_delta, ranking_metrics, review_metrics
from src.evaluation.models import RunResult, StrategyRef
from src.evaluation.runner import run_evaluation
from src.lab.bundle import verify_bundle
from src.lab.latency import DEFAULT_TOLERANCE, latency_guardrail

MIN_LABELLED = 30
FEW_ACCEPTS = 30          # below this many accept labels, recall / unsafe-rejection rest on very few jobs
_COMPARED = ("accuracy", "precision", "recall", "f1", "unsafe_rejection_rate", "reject_recall")
_COUNTS = ("tp", "tn", "fp", "fn", "borderline")
MIN_DISCORDANT = 5
N_BOOT = 2000


def _correct(result: RunResult, dataset: Dataset) -> dict[str, bool]:
    out = {}
    for d in result.decisions:
        lab = dataset.labels.get(d.job_id)
        if lab is not None and lab.expected != "borderline":
            out[d.job_id] = (d.verdict == "accept") == (lab.expected == "accept")
    return out


def paired_bootstrap(diffs: list[int], *, n_boot: int = N_BOOT, seed: int = 20260917) -> tuple[float, float]:
    """95 % percentile CI of the mean paired difference (+1 candidate fixes, -1 candidate breaks)."""
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(n_boot))
    return means[int(0.025 * (n_boot - 1))], means[int(0.975 * (n_boot - 1))]


def _side(run: RunResult, dataset: Dataset) -> dict[str, Any]:
    """Recomputed from the run's decisions (the same ones the verdict judges), never from cached metrics."""
    overall = evaluate(run.decisions, dataset.labels)["overall"]
    review = review_metrics(run.decisions, dataset.labels) if any(d.assessment for d in run.decisions) else {}
    out: dict[str, Any] = {k: overall.get(k) for k in (*_COMPARED, *_COUNTS)}
    out["accepted"] = sum(1 for d in run.decisions if d.verdict == "accept")
    out["review_rate"] = review.get("review_rate")
    out["n_review"] = review.get("n_review")
    return out


def compare_metrics(dataset: Dataset, baseline: RunResult, candidate: RunResult) -> dict[str, Any]:
    """Side-by-side metrics on the same dataset and labels, with what limits the comparison."""
    b, c = _side(baseline, dataset), _side(candidate, dataset)
    delta = {k: (c[k] - b[k]) if isinstance(b[k], (int, float)) and isinstance(c[k], (int, float)) else None
             for k in b}
    provenance = Counter(_sources(dataset).values())
    accepts = sum(1 for lab in dataset.labels.values() if lab.expected == "accept")
    limitations = []
    if set(provenance) - {"human"}:
        limitations.append(f"labels by provenance {dict(sorted(provenance.items()))}: only `human` labels are "
                           "independent evidence; the rest can share the assistant's or the system's blind spots")
    if accepts < FEW_ACCEPTS:
        limitations.append(f"only {accepts} accept label(s): recall and unsafe-rejection deltas rest on very few jobs")
    if any(("survival bias" in w) or ("pipeline-rejected" in w) for w in candidate.warnings):
        limitations.append("sampling of the dataset limits which false negatives can be seen (see dataset warnings)")
    # reported beside the verdict, never part of it: a scoring-only change flips no gate decision, so the
    # verdict cannot see it; how the score orders the accepted jobs can
    ranking = {"baseline": ranking_metrics(baseline.decisions, dataset.labels),
               "candidate": ranking_metrics(candidate.decisions, dataset.labels),
               "delta": ranking_delta(baseline.decisions, candidate.decisions, dataset.labels)}
    return {"baseline": b, "candidate": c, "delta": delta, "ranking": ranking,
            "label_provenance": dict(sorted(provenance.items())), "accept_labels": accepts,
            "limitations": limitations}


def _sources(dataset: Dataset) -> dict[str, str]:
    """Each label's provenance as evidence (``human`` over an assistant proposal on record counts as assisted)."""
    from src.evaluation.annotation_assist import assisted_job_ids
    from src.evaluation.metrics import effective_sources

    return effective_sources(dataset.labels, frozenset(assisted_job_ids(dataset.path)))


def _basis(ids: list[str], sources: Mapping[str, str]) -> str:
    n_human = sum(1 for j in ids if sources.get(j) == "human")
    return "none" if not ids else "independent" if n_human == len(ids) else "not_independent" if not n_human else "mixed"


def independent_support(ids: list[str], diffs: list[int], sources: Mapping[str, str]) -> dict[str, Any]:
    """The verdict test (same evidence floor, same paired bootstrap) on the independent ``human`` labels only."""
    ind = [x for j, x in zip(ids, diffs, strict=True) if sources.get(j) == "human"]
    fixed, broken = ind.count(1), ind.count(-1)
    out: dict[str, Any] = {"n_labelled": len(ind), "fixed": fixed, "broken": broken, "ci95": None}
    if len(ind) < MIN_LABELLED or fixed + broken < MIN_DISCORDANT:
        out.update(status="insufficient_evidence",
                   why=f"{len(ind)} independent labelled jobs (need {MIN_LABELLED}) and {fixed + broken} independent "
                       f"discordant pairs (need {MIN_DISCORDANT})")
    else:
        ci = paired_bootstrap(ind)
        out.update(ci95=ci, status="improvement" if ci[0] > 0 else "regression" if ci[1] < 0 else "inconclusive",
                   why=f"independent paired accuracy difference CI95 [{ci[0]:+.3f}, {ci[1]:+.3f}]")
    return out


def judge(
    dataset: Dataset, baseline: RunResult, candidate: RunResult, candidate_again: RunResult,
    *, latency_tolerance: float = DEFAULT_TOLERANCE, baseline_again: RunResult | None = None,
) -> dict[str, Any]:
    cb, cc = _correct(baseline, dataset), _correct(candidate, dataset)
    ids = sorted(cb)
    diffs = [int(cc[j]) - int(cb[j]) for j in ids]
    fixed, broken = diffs.count(1), diffs.count(-1)
    bd = {d.job_id: d.verdict for d in baseline.decisions}
    cd = {d.job_id: d.verdict for d in candidate.decisions}

    def unsafe(dec: Mapping[str, str]) -> list[str]:
        return sorted(j for j in ids if dataset.labels[j].expected == "accept" and dec[j] == "reject")

    new_unsafe = sorted(set(unsafe(cd)) - set(unsafe(bd)))
    guardrails: dict[str, dict[str, Any]] = {
        "no_new_unsafe_rejection": {"passed": not new_unsafe, "new_unsafe_job_ids": new_unsafe,
                                    "baseline_unsafe": len(unsafe(bd)), "candidate_unsafe": len(unsafe(cd))},
        "reproducible": {"passed": [d.model_dump() for d in candidate.decisions]
                         == [d.model_dump() for d in candidate_again.decisions]},
        "latency": latency_guardrail([baseline] + ([baseline_again] if baseline_again else []),
                                     [candidate, candidate_again], tolerance=latency_tolerance),
    }
    guard_ok = all(g["passed"] for g in guardrails.values())
    n = len(ids)
    ci: tuple[float, float] | None = None
    if n < MIN_LABELLED or (fixed + broken) < MIN_DISCORDANT:
        verdict, why = "insufficient_evidence", (
            f"{n} labelled non-borderline jobs (need {MIN_LABELLED}) and {fixed + broken} discordant "
            f"pairs (need {MIN_DISCORDANT})")
    else:
        ci = paired_bootstrap(diffs)
        verdict = "improvement" if ci[0] > 0 else "regression" if ci[1] < 0 else "inconclusive"
        why = f"paired accuracy difference CI95 [{ci[0]:+.3f}, {ci[1]:+.3f}]"
    if not guard_ok and verdict != "insufficient_evidence":
        verdict, why = "regression", why + "; a guardrail failed"
    if not guard_ok and verdict == "insufficient_evidence":
        why += "; a guardrail failed"
    sources = _sources(dataset)
    discordant = Counter(sources.get(j, "unknown") for j, x in zip(ids, diffs, strict=True) if x)
    dependent = sum(n for k, n in discordant.items() if k != "human")
    if dependent:   # say so in the verdict itself; approval stays a human decision either way
        why += (f"; {dependent} of {fixed + broken} discordant pair(s) rest on labels that are not independent "
                f"human judgements {dict(sorted(discordant.items()))}")
    eligible = verdict == "improvement" and guard_ok
    independent = independent_support(ids, diffs, sources)
    tier = ("not_eligible" if not eligible
            else "independently_supported" if independent["status"] == "improvement" else "exploratory_only")
    if tier == "exploratory_only":
        why += f"; EXPLORATORY ONLY, not independently validated: {independent['why']}"
    return {
        "verdict": verdict, "explanation": why, "eligible_for_approval": eligible,
        "promotion_evidence": tier, "independently_validated": tier == "independently_supported",
        "evidence_basis": _basis(ids, sources), "independent_evidence": independent,
        "n_labelled": n, "fixed": fixed, "broken": broken, "paired_accuracy_diff_ci95": ci,
        "discordant_by_provenance": dict(sorted(discordant.items())),
        "accuracy": {"baseline": sum(cb.values()) / n if n else None, "candidate": sum(cc.values()) / n if n else None},
        "guardrails": guardrails,
        "baseline_fingerprint": baseline.strategy["fingerprint"],
        "candidate_fingerprint": candidate.strategy["fingerprint"],
        "dataset_warnings": candidate.warnings,
        "comparison": compare_metrics(dataset, baseline, candidate),
    }


def run_experiment(dataset: Dataset, baseline: StrategyRef, bundle_dir: str | Path, *, name: str = "candidate") -> dict[str, Any]:
    if dataset.profile_path is None and baseline.profile_path is None:
        raise ValueError("dataset pins no profile.yaml: replays must not depend on a personal config "
                         "(rebuild it with --profile, or pass a profile explicitly)")
    if "status=insufficient_evidence" in dataset.manifest.notes:
        raise ValueError("dataset is flagged insufficient_evidence (below the P4 label thresholds): "
                         "no experiment may be run on it")
    verify_bundle(bundle_dir, Path(bundle_dir).name)          # a bundle's directory name IS its content hash
    cand = baseline.model_copy(update={"name": name, "config_dir": str(bundle_dir)})
    # interleaved, two replays a side: the latency guardrail keeps each stage's fastest, so the process's
    # cold start (paid by whichever replay runs first) is never attributed to either strategy
    base_run = run_evaluation(dataset, baseline, assess=True)
    cand_run = run_evaluation(dataset, cand, assess=True)
    base_again = run_evaluation(dataset, baseline, assess=True)
    cand_again = run_evaluation(dataset, cand, assess=True)
    result = judge(dataset, base_run, cand_run, cand_again, baseline_again=base_again)
    result["bundle_hash"] = Path(bundle_dir).name
    return result
