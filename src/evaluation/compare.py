"""Paired comparison of two run results on the same dataset."""

from __future__ import annotations

from typing import Any

from src.evaluation.metrics import mcnemar_exact_p
from src.evaluation.models import RunResult


def compare_runs(
    a: RunResult, b: RunResult, labels: dict[str, str] | None = None, provenance: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compare run ``a`` (baseline) with ``b`` (candidate).

    ``labels`` maps job_id → expected (``accept|reject|borderline``); when
    given, verdict flips are classified as fixed/broken and an exact
    McNemar p-value on discordant pairs is returned. ``provenance`` maps
    job_id → ``label_source``: fixed/broken are then also split by
    provenance, and a warning says when they rest on labels that are not
    independent (assistant-assisted or derived). No quality verdict is
    issued here (that is the experiment lab's job): this is descriptive.
    """
    if a.dataset["jobs_sha256"] != b.dataset["jobs_sha256"] or a.dataset["labels_sha256"] != b.dataset["labels_sha256"]:
        raise ValueError("runs were produced on different datasets; comparison refused")
    if a.dataset.get("as_of") != b.dataset.get("as_of"):
        raise ValueError("runs use different as_of reference times; comparison refused")
    da = {d.job_id: d for d in a.decisions}
    db = {d.job_id: d for d in b.decisions}
    if set(da) != set(db):
        raise ValueError("runs cover different job ids; comparison refused")
    flips = [
        {"job_id": j, "from": da[j].verdict, "to": db[j].verdict, "stage_from": da[j].stage, "stage_to": db[j].stage}
        for j in sorted(da) if da[j].verdict != db[j].verdict
    ]
    out: dict[str, Any] = {
        "n_jobs": len(da),
        "n_verdict_flips": len(flips),
        "flips": flips,
        "stage_changes": sum(1 for j in da if da[j].stage != db[j].stage),
        "same_fingerprint": a.strategy["fingerprint"] == b.strategy["fingerprint"],
    }
    if labels is not None:
        fixed = broken = 0
        by_source: dict[str, dict[str, int]] = {}
        for f in flips:
            exp = labels.get(f["job_id"])
            if exp in (None, "borderline"):
                continue
            kind = "fixed" if f["to"] == exp else "broken"
            fixed += kind == "fixed"
            broken += kind == "broken"
            if provenance is not None:
                bucket = by_source.setdefault(provenance.get(f["job_id"], "unknown"), {"fixed": 0, "broken": 0})
                bucket[kind] += 1
        out.update(fixed=fixed, broken=broken, mcnemar_exact_p=mcnemar_exact_p(broken, fixed))
        if provenance is not None:
            counts: dict[str, int] = {}
            for src in provenance.values():
                counts[src] = counts.get(src, 0) + 1
            out["label_provenance"] = dict(sorted(counts.items()))
            out["flips_by_provenance"] = dict(sorted(by_source.items()))
            ind = by_source.get("human", {"fixed": 0, "broken": 0})
            # the paired test over independent labels only: the figure to quote as evidence, if any
            out["independent"] = {**ind, "mcnemar_exact_p": mcnemar_exact_p(ind["broken"], ind["fixed"])}
            dependent = sum(v["fixed"] + v["broken"] for k, v in by_source.items() if k != "human")
            if dependent:
                out["warnings"] = [f"{dependent} of {fixed + broken} classified flip(s) rest on labels that are not "
                                   "independent human judgements (see flips_by_provenance)"]
    return out
