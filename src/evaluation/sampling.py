"""Sampling cohorts of an exported real dataset: which numbers describe the job stream, which do not.

``export`` draws from several strata with unequal selection probabilities (near a tier threshold, user
actioned, per source, top-scored, pipeline-rejected) plus ``random_control``, a uniform draw from the
stored jobs the other strata left. Pooled metrics over such a sample are dominated by the enriched strata
and are NOT rates of the production job stream. Two cohorts are uniform draws and can be read as such:

* ``random_control`` — the jobs the pipeline stored (accepted at the time), minus the few the other strata
  took first: its confusion metrics estimate what the gates let through;
* ``pipeline_rejected`` — a uniform draw from the jobs the pipeline rejected (``export --rejected``): its
  accept rate, times the pool size, estimates the suitable jobs the gates turned away.

The stratum of each job is kept in the private export sidecar (the sheet stays blind). It is read only on
request (``run --sampling``), and only its ``strata`` and ``rejected_pool`` keys; outputs are aggregates
by stratum, never ids, the id map or the salt.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

from src.evaluation.metrics import confusion, ranking_metrics
from src.evaluation.models import Decision, LabelRecord

SIDECAR = "PRIVATE_export_meta.json"
REPRESENTATIVE = "random_control"
REJECTED = "pipeline_rejected"
_UNIFORM = (REPRESENTATIVE, REJECTED)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """Wilson score interval for ``k`` successes out of ``n`` (``None`` when ``n`` is 0)."""
    if n == 0:
        return None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def load_sampling(dataset_path: str | Path) -> tuple[dict[str, str], dict[str, Any]] | None:
    """``(strata, {"rejected_pool": n})`` from the private sidecar, or ``None`` when there is none."""
    path = Path(dataset_path) / SIDECAR
    if not path.is_file():
        return None
    meta = json.loads(path.read_text(encoding="utf-8"))
    info = {"rejected_pool": meta["rejected_pool"]} if "rejected_pool" in meta else {}
    return dict(meta.get("strata") or {}), info


def sampling_metrics(
    decisions: Sequence[Decision], labels: dict[str, LabelRecord], strata: dict[str, str], info: dict[str, Any],
) -> dict[str, Any]:
    """Per-stratum label counts, metrics on the uniform cohorts, and what the pooled numbers mean."""
    by_stratum: dict[str, dict[str, int]] = {}
    for d in decisions:
        row = by_stratum.setdefault(strata.get(d.job_id, "unknown"),
                                    {"n": 0, "accept": 0, "reject": 0, "borderline": 0, "unlabelled": 0})
        row["n"] += 1
        row[labels[d.job_id].expected if d.job_id in labels else "unlabelled"] += 1

    rep = [d for d in decisions if strata.get(d.job_id) == REPRESENTATIVE]
    rep_lab = [d for d in rep if d.job_id in labels]
    representative = {
        "stratum": REPRESENTATIVE, "n_labelled": len(rep_lab),
        "overall": confusion((labels[d.job_id].expected, d.verdict) for d in rep_lab).to_dict(),
        "ranking": ranking_metrics(rep, labels),
    } if rep else None

    # every labelled job of the draw, borderline included: the pool it is scaled to holds them all
    rej = [d for d in decisions if strata.get(d.job_id) == REJECTED and d.job_id in labels]
    rejected_sample = None
    if rej:
        k = sum(labels[d.job_id].expected == "accept" for d in rej)
        ci = wilson(k, len(rej))
        pool = info.get("rejected_pool")
        rejected_sample = {
            "n_labelled": len(rej), "accept": k,
            "borderline": sum(labels[d.job_id].expected == "borderline" for d in rej),
            "accept_rate": k / len(rej), "accept_rate_ci95": ci,
            "pool": pool,
            "estimated_missed_accepts": ([round(ci[0] * pool), round(k / len(rej) * pool), round(ci[1] * pool)]
                                         if pool and ci else None),
            "still_rejected_now": sum(1 for d in rej if labels[d.job_id].expected == "accept"
                                      and d.verdict == "reject"),
        }

    enriched = sorted(s for s in by_stratum if s not in _UNIFORM and s != "unknown")
    return {
        "by_stratum": dict(sorted(by_stratum.items())),
        "representative": representative,
        "rejected_sample": rejected_sample,
        "enriched_strata": enriched,
        "warning": (f"strata {enriched} were drawn with unequal probabilities: pooled rates over-represent them "
                    f"and are not rates of the job stream; read `representative` ({REPRESENTATIVE}) for what "
                    f"the gates let through and `rejected_sample` for missed opportunities") if enriched else "",
    }
