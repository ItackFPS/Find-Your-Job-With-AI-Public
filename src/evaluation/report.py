"""Markdown rendering of a RunResult."""

from __future__ import annotations

from src.evaluation.models import RunResult


def _f(v: object) -> str:
    return "n/a" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))


_BASIS_TEXT = {
    "not_independent": "no label in this dataset was given independently (all are assistant-assisted or derived)",
    "mixed": "only some labels were given independently",
}


def _evidence(r: RunResult) -> list[str]:
    """What the metrics below rest on — stated BEFORE them, so assisted labels are never read as validation."""
    m = r.metrics
    prov = m.get("label_provenance")
    if not prov:            # results written before provenance was reported
        return []
    cls = prov.get("by_class", {})
    acc = cls.get("accept", {"total": 0, "independent": 0})
    rej = cls.get("reject", {"total": 0, "independent": 0})
    n_jobs = m.get("n_jobs") or 0
    coverage = f" ({m['n_labelled'] / n_jobs:.0%})" if n_jobs else ""
    lines = ["", "## Evidence behind these numbers", "",
             f"- labelled {m['n_labelled']} of {n_jobs} jobs{coverage}; accept labels {acc['total']} "
             f"({acc['independent']} independent), reject labels {rej['total']} ({rej['independent']} independent), "
             f"borderline {cls.get('borderline', {}).get('total', 0)}",
             f"- label basis: **{prov.get('basis', 'unknown')}** — {prov['n_independent']} independent `human`, "
             f"{prov['n_assisted']} assistant-assisted, {prov.get('n_derived', 0)} derived"]
    sampling = next((w for w in r.warnings if w.startswith("real dataset")), None)
    if sampling:
        lines.append(f"- sampling: {sampling}")
    basis = prov.get("basis")
    if basis in _BASIS_TEXT:
        lines += ["", f"> **NOT independent validation**: {_BASIS_TEXT[basis]}. Recall and the unsafe-rejection "
                      f"rate rest on {acc['total']} accept label(s), {acc['independent']} of them independent. "
                      "Read the metrics as agreement with these labels, not as measured quality."]
    if 0 < acc["total"] < 30:
        lines.append(f"> Only {acc['total']} accept label(s): recall and the unsafe-rejection rate are very "
                     "uncertain (see the CI width).")
    return lines


def render_markdown(r: RunResult) -> str:
    m = r.metrics
    o = m["overall"]
    ci = m.get("bootstrap_ci_95", {})
    lines = [
        f"# Evaluation run {r.run_id[:8]}",
        "",
        f"- dataset: `{r.dataset['id']}@{r.dataset['version']}` ({r.dataset['kind']}), {m['n_jobs']} jobs, {m['n_labelled']} labelled",
        f"- strategy: `{r.strategy['name']}` fingerprint `{r.strategy['fingerprint'][:12]}`",
        f"- code: `{(r.code.get('git_sha') or 'unknown')[:10]}` dirty={r.code.get('dirty')}",
        f"- LLM: {'enabled, %s calls' % r.llm['calls'] if r.llm.get('enabled') else 'disabled (deterministic)'}",
        *_evidence(r),
        "",
        "## Accept-class metrics (95% bootstrap CI)",
        "",
        "| metric | value | CI lo | CI hi |",
        "|---|---|---|---|",
    ]
    for k in ("accuracy", "precision", "recall", "f1", "unsafe_rejection_rate"):
        c = ci.get(k, {})
        lines.append(f"| {k} | {_f(o.get(k))} | {_f(c.get('lo'))} | {_f(c.get('hi'))} |")
    lines += [
        "",
        f"Confusion: tp={o['tp']} tn={o['tn']} fp={o['fp']} fn={o['fn']} borderline={o['borderline']} "
        f"(reject recall {_f(o['reject_recall'])}, reject precision {_f(o['reject_precision'])})",
        "",
        f"False positives: {m['false_positive_ids'] or 'none'}",
        f"False negatives (unsafe rejections): {m['false_negative_ids'] or 'none'}",
        f"Rejections by stage: {m['rejections_by_stage']}",
        "",
        "## By category",
        "",
        "| category | tp | tn | fp | fn | borderline |",
        "|---|---|---|---|---|---|",
    ]
    for cat, c in m["by_category"].items():
        lines.append(f"| {cat} | {c['tp']} | {c['tn']} | {c['fp']} | {c['fn']} | {c['borderline']} |")
    by_reason = m.get("rejections_by_reason")
    if by_reason:
        lines += [
            "",
            "## Rejections by reason",
            "",
            "Reason = the first failing hard filter (later checks are not evaluated for that job) or the requirements gate.",
            "",
            "| reason | total | accept | reject | borderline | unlabelled |",
            "|---|---|---|---|---|---|",
        ]
        for reason, b in by_reason.items():
            bl = b["by_label"]
            lines.append(f"| {reason} | {b['total']} | {bl.get('accept', 0)} | {bl.get('reject', 0)} | "
                         f"{bl.get('borderline', 0)} | {bl.get('unlabelled', 0)} |")
    diag = m.get("diagnostic_excluding_freshness")
    if diag:
        d = diag["overall"]
        lines += [
            "",
            f"## Diagnostic view excluding `{diag['excluded_reason']}` rejections (NOT comparable with the overall metrics)",
            "",
            f"> **{diag['warning']}**",
            "",
            f"- population: {diag['n_jobs']} jobs ({diag['n_labelled']} labelled); excluded {diag['n_excluded']} "
            f"(by label: {diag['excluded_by_label'] or 'none'})",
            f"- confusion: tp={d['tp']} tn={d['tn']} fp={d['fp']} fn={d['fn']} borderline={d['borderline']}",
            f"- precision {_f(d['precision'])}, recall {_f(d['recall'])}, reject recall {_f(d['reject_recall'])}, "
            f"reject precision {_f(d['reject_precision'])}, unsafe rejection rate {_f(d['unsafe_rejection_rate'])}",
        ]
    prov = m.get("label_provenance")
    if prov:
        lines += ["", "## Label provenance", "",
                  f"- by source: {prov['by_source']} — recorded as {prov['n_independent']} `human` (independent), "
                  f"{prov['n_assisted']} `human_assisted` (see Warnings for labels imported before tracking)"]
        if prov["n_assisted"]:
            lines.append("- assistant-assisted labels were given with a machine proposal on record: they are not "
                         "independent evidence of the assistant's or a strategy's quality")
        ind = prov.get("independent_only")
        if ind:
            d = ind["overall"]
            lines += ["", f"> **{ind['warning']}**", "",
                      f"- independent labels only: {ind['n_labelled']} labelled; tp={d['tp']} tn={d['tn']} "
                      f"fp={d['fp']} fn={d['fn']} borderline={d['borderline']}; precision {_f(d['precision'])}, "
                      f"recall {_f(d['recall'])}, unsafe rejection rate {_f(d['unsafe_rejection_rate'])}"]
    rv = m.get("review")
    if rv:
        lines += ["", "## Review (disagreement engine, shadow, rules-only signals)", "",
                  f"status: **{rv['status']}**" + (f" — {rv['note']}" if rv.get("note") else ""), "",
                  f"- review rate {_f(rv['review_rate'])} ({rv['n_review']}/{rv['n_labelled']} labelled)",
                  f"- wrong decisions {rv['n_wrong']}, captured in review {rv['n_wrong_in_review']}"
                  f" (capture rate {_f(rv['capture_rate'])}, lift {_f(rv['lift'])})",
                  f"- accuracy of auto decisions {_f(rv['auto_accuracy'])} vs overall {_f(rv['overall_accuracy'])}",
                  f"- good jobs rejected and NOT in review (unsafe): {rv['unsafe_rejections_not_in_review']}"]
    t = r.timings
    lines += ["", f"Timings (s): {t['stage_seconds']}; extraction mean {_f(t.get('extraction_ms_mean'))} ms, p95 {_f(t.get('extraction_ms_p95'))} ms"]
    if r.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in r.warnings]
    return "\n".join(lines) + "\n"
