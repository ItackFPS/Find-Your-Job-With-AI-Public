"""Self-check: replay the current strategy on frozen datasets and compare it with a recorded baseline.

One repeatable command (``python -m src.evaluation self-check``) that covers steps 1-6 of the
quality loop and hands steps 7-10 to the experiment lab (ADR 0011):

1. select reproducible datasets (frozen, hash-pinned; the committed synthetic benchmark by default);
2. evaluate the current strategy on each (the REAL production decision path, see ``runner``);
3. measure eligibility safety and quality, kept apart from ranking diagnostics;
4. compare with the recorded baseline — the dataset's golden decisions, the same file the golden
   change detector uses, so there is one source of truth for "what the system decided before";
5. group the current errors into failure patterns (first failing check for false negatives, label
   category for false positives, borderline jobs rejected without a review flag);
6. write a report saying what was measured, on what evidence (dataset kind, size, label provenance)
   and what limits it; then 7. point each actionable pattern at the lab, which proposes a bounded
   candidate, tests it in isolation and leaves approval to a human.

Everything here is deterministic and computed from decisions and labels: no model, no network, no
write outside the report directory, and nothing read from ``jobs.db`` except (optionally, read-only)
the feedback and decision journals. Suggestions are marked ``kind: "suggestion"`` and never count as
measured results. A green self-check says the benchmark did not move the wrong way — never that the
system is accurate on real postings.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evaluation.dataset import Dataset, load_dataset
from src.evaluation.metrics import evaluate, rejection_reason, review_metrics
from src.evaluation.models import Decision, GoldenDecisions, RunResult, StrategyRef
from src.evaluation.runner import run_evaluation
from src.evaluation.store import diff_golden, read_golden, to_golden

SELF_CHECK_SCHEMA_VERSION = 1
DEFAULT_REPORTS_DIR = "data/benchmarks/self-check"

# Worst first. ``baseline_mismatch``: the baseline was recorded on other data, nothing can be compared.
STATUS_ORDER = ("safety_violation", "baseline_mismatch", "regression", "changed", "pass")

_QUALITY = ("accuracy", "precision", "recall", "f1", "reject_recall", "unsafe_rejection_rate")
_COUNTS = ("tp", "tn", "fp", "fn", "borderline", "scored")


def _worst(statuses: list[str]) -> str:
    return min(statuses, key=STATUS_ORDER.index) if statuses else "pass"


def _without_assessment(decisions: list[Decision]) -> list[Decision]:
    """Golden decisions are recorded without the (shadow) assessment: compare like with like."""
    return [d.model_copy(update={"assessment": None}) for d in decisions]


def _quality(decisions: list[Decision], dataset: Dataset) -> dict[str, Any]:
    overall = evaluate(decisions, dataset.labels)["overall"]
    return {k: overall.get(k) for k in (*_COUNTS, *_QUALITY)}


def _score_separation(decisions: list[Decision], dataset: Dataset) -> dict[str, Any]:
    """Among jobs that PASSED eligibility, how often an accept-labelled job outscores a reject-labelled one.

    A diagnostic, not a ranking metric: the labels say eligible / not eligible, not how relevant a job is.
    """
    good: list[float] = []
    bad: list[float] = []
    for d in decisions:
        lab = dataset.labels.get(d.job_id)
        if d.verdict != "accept" or d.score is None or lab is None or lab.expected == "borderline":
            continue
        (good if lab.expected == "accept" else bad).append(d.score)
    if not good or not bad:
        return {"status": "not_measured", "why": "needs both accept- and reject-labelled jobs among the passed ones",
                "n_accept": len(good), "n_reject": len(bad)}
    wins = sum((g > b) + 0.5 * (g == b) for g in good for b in bad)
    return {"status": "diagnostic", "pairwise_auc": wins / (len(good) * len(bad)),
            "n_accept": len(good), "n_reject": len(bad)}


def _patterns(decisions: list[Decision], dataset: Dataset) -> list[dict[str, Any]]:
    """Current errors grouped by what they have in common, largest group first."""
    groups: dict[tuple[str, str], list[Decision]] = {}
    for d in decisions:
        lab = dataset.labels.get(d.job_id)
        if lab is None:
            continue
        if lab.expected == "accept" and d.verdict == "reject":
            key = ("unsafe_rejection", rejection_reason(d) or d.stage)
        elif lab.expected == "reject" and d.verdict == "accept":
            key = ("false_positive", lab.category or "uncategorised")
        elif (lab.expected == "borderline" and d.verdict == "reject"
              and (d.assessment or {}).get("state") != "review"):
            key = ("borderline_rejected_without_review", rejection_reason(d) or d.stage)
        else:
            continue
        groups.setdefault(key, []).append(d)
    out = []
    for (kind, pattern), ds in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if kind == "false_positive" or pattern.startswith("filter:"):
            # the lab agent attributes it (which keyword, or that the profile/code is the cause) and tests a fix
            lever = {"kind": "lab", "detail": "the lab agent can attribute this pattern and test a bounded candidate"}
        else:
            lever = {"kind": "code", "detail": "no configuration lever: needs a code change with a regression test"}
        out.append({"kind": kind, "key": pattern, "count": len(ds), "job_ids": sorted(d.job_id for d in ds),
                    "example_reasons": sorted({r for d in ds for r in d.reasons})[:5], "lever": lever})
    return out


def _flips(baseline: list[Decision], current: list[Decision], dataset: Dataset) -> dict[str, Any]:
    base = {d.job_id: d.verdict for d in baseline}
    fixed, broken, unlabelled = [], [], []
    for d in current:
        if d.job_id not in base or base[d.job_id] == d.verdict:
            continue
        lab = dataset.labels.get(d.job_id)
        if lab is None or lab.expected == "borderline":
            unlabelled.append(d.job_id)
        elif (d.verdict == "accept") == (lab.expected == "accept"):
            fixed.append(d.job_id)
        else:
            broken.append(d.job_id)
    return {"fixed": sorted(fixed), "broken": sorted(broken), "unjudged": sorted(unlabelled)}


def check_dataset(dataset: Dataset, strategy: StrategyRef, golden: GoldenDecisions | None) -> dict[str, Any]:
    """Self-check one dataset; ``golden`` is its recorded baseline (``None``: no baseline recorded)."""
    result: RunResult = run_evaluation(dataset, strategy, assess=True)
    current = result.decisions
    provenance = Counter(lab.label_source for lab in dataset.labels.values())
    accepts = sum(1 for lab in dataset.labels.values() if lab.expected == "accept")
    unsafe = sorted(d.job_id for d in current
                    if d.verdict == "reject" and (lab := dataset.labels.get(d.job_id)) and lab.expected == "accept")
    silent = sorted(d.job_id for d in current if d.verdict == "reject" and not d.reasons)
    n_reject = sum(1 for d in current if d.verdict == "reject")
    review = review_metrics(current, dataset.labels)
    limitations = []
    if dataset.manifest.kind == "synthetic":
        limitations.append("synthetic, author-written postings: says nothing about accuracy on real offers")
    if set(provenance) - {"human"}:
        limitations.append(f"labels by provenance {dict(sorted(provenance.items()))}: only `human` labels are "
                           "independent evidence")
    if accepts < 30:
        limitations.append(f"only {accepts} accept label(s): recall and unsafe-rejection rate rest on few jobs")
    limitations.extend(result.warnings)

    entry: dict[str, Any] = {
        "dataset": {"id": dataset.manifest.dataset_id, "version": dataset.manifest.version,
                    "kind": dataset.manifest.kind, "n_jobs": len(current), "n_labelled": len(dataset.labels),
                    "accept_labels": accepts, "label_provenance": dict(sorted(provenance.items())),
                    "jobs_sha256": dataset.manifest.jobs_sha256, "labels_sha256": dataset.manifest.labels_sha256},
        "strategy": {"name": result.strategy.get("name"), "fingerprint": result.strategy.get("fingerprint")},
        "code": result.code,
        "safety": {"unsafe_rejections": [{"job_id": j, "reasons": next(d.reasons for d in current if d.job_id == j)}
                                         for j in unsafe],
                   "rejections_without_reason": silent},
        "eligibility": _quality(current, dataset),
        "uncertainty": {"review_rate": review.get("review_rate"), "n_review": review.get("n_review"),
                        "unsafe_rejections_not_in_review": review.get("unsafe_rejections_not_in_review"),
                        "review_usefulness": review.get("status")},
        "explanations": {"rejections": n_reject, "with_reason": n_reject - len(silent),
                         "coverage": (n_reject - len(silent)) / n_reject if n_reject else None},
        "ranking": {"status": "not_measured",
                    "why": "the dataset carries eligibility labels (accept/reject), not graded relevance labels",
                    "score_separation": _score_separation(current, dataset)},
        "patterns": _patterns(current, dataset),
        "limitations": limitations,
    }
    statuses = ["safety_violation"] if unsafe or silent else []
    if golden is None:
        entry["baseline"] = {"status": "missing"}
        statuses.append("changed")
    else:
        ident = {k: result.dataset[k] for k in golden.dataset}
        if ident != golden.dataset:
            entry["baseline"] = {"status": "mismatch", "recorded": golden.dataset, "current": ident}
            statuses.append("baseline_mismatch")
        else:
            diffs = diff_golden(golden, to_golden(result.model_copy(update={"decisions": _without_assessment(current)})))
            flips = _flips(golden.decisions, current, dataset)
            b_q, c_q = _quality(golden.decisions, dataset), entry["eligibility"]
            entry["baseline"] = {
                "status": "identical" if not diffs else "differs",
                "strategy_fingerprint": golden.strategy_fingerprint,
                "same_strategy": golden.strategy_fingerprint == result.strategy.get("fingerprint"),
                "eligibility": b_q,
                "delta": {k: (c_q[k] - b_q[k]) if c_q[k] is not None and b_q[k] is not None else None
                          for k in _QUALITY},
                "flips": flips, "n_differences": len(diffs), "differences": diffs[:20],
            }
            if flips["broken"]:
                statuses.append("regression")
            elif diffs:
                statuses.append("changed")
    entry["status"] = _worst(statuses)
    return entry


def _recommendations(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    recs: list[dict[str, str]] = []
    for e in entries:
        ds = e["dataset"]["id"]
        for u in e["safety"]["unsafe_rejections"]:
            recs.append({"kind": "action", "dataset": ds,
                         "text": f"Release blocker: {u['job_id']} is labelled suitable but rejected "
                                 f"({'; '.join(u['reasons']) or 'no reason'}). Fix the rule with a regression "
                                 "test; do not relabel the job to make this pass."})
        if e["safety"]["rejections_without_reason"]:
            recs.append({"kind": "action", "dataset": ds,
                         "text": "Some rejections record no reason and cannot be audited: "
                                 + ", ".join(e["safety"]["rejections_without_reason"])})
        base = e["baseline"]
        if base["status"] == "mismatch":
            recs.append({"kind": "action", "dataset": ds,
                         "text": "The recorded baseline was made on other data: rebuild it from the dataset "
                                 "version in use before trusting any comparison."})
        elif base["status"] == "missing":
            recs.append({"kind": "action", "dataset": ds,
                         "text": "No baseline is recorded for this dataset: record one (golden --update) once "
                                 "the current decisions have been reviewed."})
        elif base["status"] == "differs":
            f = base["flips"]
            recs.append({"kind": "action", "dataset": ds,
                         "text": f"Decisions changed since the baseline ({len(f['fixed'])} fixed, {len(f['broken'])} "
                                 f"broken, {len(f['unjudged'])} unlabelled or borderline). Review the differences; "
                                 "refresh the baseline only if every change is intended."})
        for p in e["patterns"]:
            if p["lever"]["kind"] == "lab":
                recs.append({"kind": "suggestion", "dataset": ds,
                             "text": f"{p['count']} {p['kind'].replace('_', ' ')} ({p['key']}): {p['lever']['detail']} "
                                     f"- python -m src.lab agent <dataset> (isolated; approval stays human)."})
            else:
                recs.append({"kind": "suggestion", "dataset": ds,
                             "text": f"{p['count']} {p['kind'].replace('_', ' ')} ({p['key']}): {p['lever']['detail']}."})
    return recs


def feedback_coverage(db_path: str | Path) -> dict[str, Any]:
    """Read-only: what explicit feedback says about the latest decisions — signals, never labels.

    ``latest`` answers count once per job (the most recent explicit answer wins, as in the feedback
    dataset); ``unsure`` is reported separately and is neither a positive nor a negative. A contradiction
    (rejected but answered "interested", accepted but "not interested") is a lead to review, not proof.
    """
    p = Path(db_path)
    if not p.is_file():
        return {"status": "unavailable", "why": f"database not found: {p.name}"}
    conn = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        latest_fb = {r["job_id"]: r["type"] for r in conn.execute(
            """SELECT job_id, type FROM (SELECT job_id, type, ROW_NUMBER() OVER (
                   PARTITION BY job_id ORDER BY occurred_at DESC, rowid DESC) AS rn
               FROM feedback_events WHERE signal_class = 'explicit') WHERE rn = 1""")}
        latest_dec = {r["job_id"]: dict(r) for r in conn.execute(
            """SELECT job_id, outcome, evidence_json, strategy_version FROM (SELECT *, ROW_NUMBER() OVER (
                   PARTITION BY job_id ORDER BY decided_at DESC, rowid DESC) AS rn FROM job_decisions) WHERE rn = 1""")}
    except sqlite3.OperationalError as exc:
        return {"status": "unavailable", "why": f"journal tables missing: {exc}"}
    finally:
        conn.close()
    with_evidence = sum(1 for d in latest_dec.values() if d["evidence_json"] not in (None, "", "[]"))
    contradictions: dict[str, list[str]] = {"rejected_but_interested": [], "accepted_but_not_interested": []}
    for job_id, fb in sorted(latest_fb.items()):
        outcome = (latest_dec.get(job_id) or {}).get("outcome")
        if outcome == "rejected" and fb == "interested":
            contradictions["rejected_but_interested"].append(job_id)
        elif outcome == "accepted" and fb == "not_interested":
            contradictions["accepted_but_not_interested"].append(job_id)
    return {
        "status": "measured",
        "decided_jobs": len(latest_dec),
        "jobs_with_explicit_feedback": len(latest_fb),
        "coverage": len(latest_fb) / len(latest_dec) if latest_dec else None,
        "latest_answers": dict(sorted(Counter(latest_fb.values()).items())),
        "decisions_with_evidence": with_evidence,
        "explanation_coverage": with_evidence / len(latest_dec) if latest_dec else None,
        "contradictions": {k: {"count": len(v), "job_ids": v[:20]} for k, v in contradictions.items()},
        "note": "explicit answers are user signals: a contradiction is a lead to review, not a verified label",
    }


def run_self_check(
    targets: list[tuple[str | Path, str | Path | None]], strategy: StrategyRef, *, db: str | Path | None = None,
) -> dict[str, Any]:
    """``targets`` = (dataset dir, golden file or None) pairs. Returns the report (not written)."""
    entries = []
    for ds_path, golden_path in targets:
        ds = load_dataset(ds_path)
        golden = read_golden(golden_path) if golden_path and Path(golden_path).is_file() else None
        entries.append(check_dataset(ds, strategy, golden))
    report: dict[str, Any] = {
        "schema_version": SELF_CHECK_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "status": _worst([e["status"] for e in entries]),
        "generated_by": "deterministic",
        "datasets": entries,
        "recommendations": _recommendations(entries),
        "next_steps": ("Patterns with a data lever: run the lab agent on a labelled dataset (python -m src.lab agent); "
                       "it tests a bounded candidate against the same baseline and dataset and never approves. "
                       "Review its experiments in the Lab page; only `python -m src.lab approve` (a human) promotes."),
    }
    if db is not None:
        report["feedback"] = feedback_coverage(db)
    return report


def write_report(report: dict[str, Any], out_dir: str | Path) -> Path:
    """Write the report with exclusive creation: a past self-check is never overwritten."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = report["created_at"].replace(":", "").replace("-", "")[:15]
    fps = "-".join(sorted({(e["strategy"]["fingerprint"] or "")[:8] for e in report["datasets"]})) or "none"
    path = d / f"{stamp}-{fps}.json"
    n = 1
    while path.exists():
        path, n = d / f"{stamp}-{fps}-{n}.json", n + 1
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return path


def render_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Self-check - {report['status'].upper()}", "", f"_{report['created_at']}, {report['generated_by']}_", ""]
    for e in report["datasets"]:
        ds, q, b = e["dataset"], e["eligibility"], e["baseline"]
        lines += [f"## {ds['id']}@{ds['version']} ({ds['kind']}, {ds['n_jobs']} jobs, {ds['accept_labels']} accept "
                  f"labels) - {e['status']}", "",
                  f"- Unsafe rejections: **{len(e['safety']['unsafe_rejections'])}**; rejections without a reason: "
                  f"{len(e['safety']['rejections_without_reason'])}",
                  f"- Eligibility: tp {q['tp']} · fp {q['fp']} · fn {q['fn']} · tn {q['tn']} · borderline "
                  f"{q['borderline']}; precision {q['precision']}, recall {q['recall']}",
                  f"- Baseline: {b['status']}"
                  + (f" (fixed {b['flips']['fixed']}, broken {b['flips']['broken']})" if b["status"] == "differs" else ""),
                  f"- Ranking: {e['ranking']['status']} - {e['ranking']['why']}"]
        for p in e["patterns"]:
            lines.append(f"- Pattern: {p['count']} x {p['kind']} `{p['key']}` ({', '.join(p['job_ids'])})")
        lines += [f"- Limitation: {x}" for x in e["limitations"]] + [""]
    if "feedback" in report:
        lines += ["## Feedback (signals, not labels)", "", "```json", json.dumps(report["feedback"], indent=2), "```", ""]
    lines += ["## Recommendations", ""] + [f"- [{r['kind']}] {r['dataset']}: {r['text']}" for r in report["recommendations"]]
    return "\n".join(lines) + "\n"
