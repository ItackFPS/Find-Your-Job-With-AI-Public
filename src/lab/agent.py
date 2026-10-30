"""The lab investigator: evidence → hypotheses → experiments → recommendations, inside the lab only.

It runs exclusively under the lab write guard (``src/lab/guard.py``): it reads ``jobs.db`` at most
read-only, writes only ``lab.db`` and the bundle store, cannot start processes or reach the network,
and therefore cannot change production decisions, the profile, the configuration or the code — nor
approve anything (approval writes ``jobs.db``, which the guard refuses). Its output is a report and
memory items; a human decides.

Deterministic and explainable by construction:

1. **Investigate** a frozen, labelled dataset under the baseline strategy. Each accept-labelled job
   the baseline rejects is attributed exactly: for the failing hard-filter check, every keyword of the
   ``filters.yaml`` lists that feed it is removed in turn and the REAL check re-run; a keyword whose
   removal alone lets the job through is responsible. Rejections with no data lever (freshness,
   location, salary come from the profile; the requirements gate is code) are recorded as findings
   for a human. Attribution concerns the FIRST failing check only (the filter stops there), so a
   recovered job may still fail a later check: the experiment, not the attribution, measures the
   effect. False positives are mined for title tokens shared by ≥3 reject-labelled accepted
   jobs that occur (as a substring, the way title exclusions match) in no accept/borderline title
   and in no include keyword.
2. **Hypothesise** one bounded, data-only override per actionable finding.
3. **Experiment** with :func:`src.lab.experiment.run_experiment` (same dataset, guardrails, verdict).
4. **Remember** everything in :class:`src.lab.memory.LabMemory`; a hypothesis already tested on the
   same inputs (dataset hashes + strategy fingerprint) is not re-tested — replays are deterministic.
5. **Recommend**: an ``improvement`` whose guardrails are green becomes a conclusion asking for HUMAN
   review (with the exact approve command); a ``regression`` refutes the hypothesis.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.evaluation.dataset import Dataset
from src.evaluation.metrics import rejection_reason
from src.evaluation.models import RunResult, StrategyRef
from src.evaluation.runner import run_evaluation
from src.lab.bundle import create_bundle
from src.lab.experiment import run_experiment
from src.lab.guard import guard_installed
from src.lab.memory import LabMemory
from src.lab.store import LabStore

# failing hard-filter check -> filters.yaml sections whose keyword lists feed it
LEVERS: dict[str, tuple[str, ...]] = {
    "seniority": ("seniority_exclusions",),
    "title_keywords": ("excluded_title_keywords", "non_it_title_functions"),
    "contract_type": ("contract_keywords",),
}
NO_LEVER = {
    "published_age": "the freshness window comes from the profile (posted_within_days)",
    "location": "locations come from the profile",
    "distance": "home, maximum distance and strictness come from the profile",
    "remote_policy": "the remote policy comes from the profile",
    "remote": "remote preferences come from the profile",
    "work_mode": "work modes come from the profile",
    "salary": "salary bounds come from the profile",
    "requirements_gate": "requirements-gate rules are code (versioned), not configuration",
}
MINED_GROUP = "lab_mined"
MIN_MINED_COUNT = 3
MIN_TOKEN_LEN = 5
MAX_MINED = 3
MINED_CAUTION = ("an exclusion keyword rejects EVERY future title containing it; that no labelled wanted job "
                 "contains it is weak evidence that no wanted job ever will — check before approving")


class AgentError(ValueError):
    pass


@dataclass
class Finding:
    key: str
    kind: str                         # recoverable_rejection | no_lever_rejection | false_positive_token
    statement: str
    job_ids: list[str]
    details: dict[str, Any] = field(default_factory=dict)
    overrides: dict[str, Any] | None = None     # None: nothing the lab may change


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _list_leaves(cfg: Any, sections: tuple[str, ...]) -> list[tuple[tuple[str, ...], list[str]]]:
    out: list[tuple[tuple[str, ...], list[str]]] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, (*path, str(k)))
        elif isinstance(node, list) and all(isinstance(x, str) for x in node):
            out.append((path, node))

    for s in sections:
        if s in cfg:
            walk(cfg[s], (s,))
    return out


def _set_path(cfg: dict[str, Any], path: tuple[str, ...], value: Any) -> dict[str, Any]:
    out = copy.deepcopy(cfg)
    node = out
    for p in path[:-1]:
        node = node.setdefault(p, {})
    node[path[-1]] = value
    return out


def _nested(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    node: dict[str, Any] = {path[-1]: value}
    for p in reversed(path[:-1]):
        node = {p: node}
    return node


def _inputs(dataset: Dataset, run: RunResult) -> dict[str, str]:
    return {"dataset_id": dataset.manifest.dataset_id, "dataset_jobs_sha256": dataset.manifest.jobs_sha256,
            "dataset_labels_sha256": dataset.manifest.labels_sha256,
            "strategy_fingerprint": run.strategy["fingerprint"]}


def _profile_path(dataset: Dataset, strategy: StrategyRef) -> str:
    if strategy.profile_path:
        return strategy.profile_path
    if dataset.profile_path is not None:
        return str(dataset.profile_path)
    raise AgentError("the dataset pins no profile: pass one explicitly (replays never read a personal config implicitly)")


def investigate(dataset: Dataset, run: RunResult, strategy: StrategyRef) -> list[Finding]:
    """Findings about the baseline run, each traceable to job ids and (when actionable) an override."""
    from src.pipeline.filter import _FILTER_CHECKS, _normalize, build_filter_config, load_filters, load_profile

    profile = load_profile(_profile_path(dataset, strategy))
    engine = load_filters(str(Path(strategy.config_dir) / "filters.yaml"))
    checks = dict(_FILTER_CHECKS)
    jobs = {j.id: j for j in dataset.build_jobs()}
    labels = dataset.labels
    findings: list[Finding] = []

    recoverable: dict[tuple[tuple[str, ...], str, str], list[str]] = {}
    no_lever: dict[str, list[str]] = {}
    for d in run.decisions:
        lab = labels.get(d.job_id)
        if lab is None or lab.expected != "accept" or d.verdict != "reject":
            continue
        reason = rejection_reason(d) or d.stage
        code = reason.split(":", 1)[1] if reason.startswith("filter:") else reason
        hits = []
        if code in LEVERS and code in checks:
            for path, words in _list_leaves(engine, LEVERS[code]):
                for kw in words:
                    trial = build_filter_config(profile, _set_path(engine, path, [w for w in words if w != kw]))
                    if checks[code](jobs[d.job_id], trial):
                        hits.append((path, kw))
        for path, kw in hits:
            recoverable.setdefault((path, kw, reason), []).append(d.job_id)
        if not hits:
            why = NO_LEVER.get(code, "no single filters.yaml keyword is responsible")
            no_lever.setdefault(f"{reason}|{why}", []).append(d.job_id)

    for (path, kw, reason), ids in sorted(recoverable.items()):
        current = _list_leaves(engine, (path[0],))
        words = next(w for p, w in current if p == path)
        findings.append(Finding(
            key=_key("remove", path, kw), kind="recoverable_rejection",
            statement=(f"{len(ids)} accept-labelled job(s) rejected by {reason} because of the keyword {kw!r} "
                       f"in filters.yaml {'.'.join(path)}"),
            job_ids=sorted(ids), details={"path": list(path), "keyword": kw, "reason": reason},
            overrides={"filters.yaml": _nested(path, [w for w in words if w != kw])}))
    for k, ids in sorted(no_lever.items()):
        reason, why = k.split("|", 1)
        findings.append(Finding(
            key=_key("no_lever", reason), kind="no_lever_rejection",
            statement=f"{len(ids)} accept-labelled job(s) rejected by {reason}: outside the lab's levers ({why})",
            job_ids=sorted(ids), details={"reason": reason, "why": why}))

    # False positives: title tokens that only ever appear on jobs the user does not want. A token sharing a
    # stem with an include keyword or with the profile's own vocabulary (skills, domains, accepted contract
    # types) is never proposed: "ingenieure" is a form of the IT title "ingenieur", not a non-IT signal.
    include = [_normalize(k) for k in build_filter_config(profile, engine)["title_keywords"].get("include", [])]
    accepted_contracts = {c.lower() for c in profile.contract_types}
    vocab_texts = [*include, *profile.target_skills, *profile.target_domains, *profile.contract_types]
    for ct, kws in (engine.get("contract_keywords") or {}).items():
        if ct.lower() in accepted_contracts and isinstance(kws, dict):
            vocab_texts += [k for lst in kws.values() if isinstance(lst, list) for k in lst]
    stems = {w for t in vocab_texts for w in re.findall(r"[a-z0-9+#]+", _normalize(str(t))) if len(w) >= 4}
    protected = [_normalize(jobs[j].title or "") for j, lab in labels.items()
                 if lab.expected in ("accept", "borderline") and j in jobs]
    fp_ids = [d.job_id for d in run.decisions
              if d.verdict == "accept" and labels.get(d.job_id) and labels[d.job_id].expected == "reject"]
    counts: Counter[str] = Counter()
    where: dict[str, list[str]] = {}
    for j in fp_ids:
        for tok in set(re.findall(r"[a-z0-9+#]+", _normalize(jobs[j].title or ""))):
            if len(tok) >= MIN_TOKEN_LEN:
                counts[tok] += 1
                where.setdefault(tok, []).append(j)
    existing = list((engine.get("excluded_title_keywords") or {}).get(MINED_GROUP, []))
    mined = 0
    for tok, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if mined >= MAX_MINED or n < MIN_MINED_COUNT:
            break
        if (any(tok in t for t in protected) or any(tok in k or k in tok for k in include if k) or tok in existing
                or any(tok.startswith(w) or w.startswith(tok) for w in stems)):
            continue
        mined += 1
        findings.append(Finding(
            key=_key("exclude_title", tok), kind="false_positive_token",
            statement=(f"the title token {tok!r} appears in {n} accepted job(s) labelled reject and in no "
                       "accept/borderline title"),
            job_ids=sorted(where[tok]), details={"token": tok, "caution": MINED_CAUTION},
            overrides={"filters.yaml": {"excluded_title_keywords": {MINED_GROUP: [*existing, tok]}}}))
    return findings


def run_agent(
    store: LabStore, dataset: Dataset, baseline: StrategyRef, *, bundle_store: str | Path,
    max_hypotheses: int = 5, experiment: bool = True,
) -> dict[str, Any]:
    """One investigation cycle. Refuses to run outside the lab write guard."""
    if not guard_installed():
        raise AgentError("the lab agent only runs under the lab write guard (python -m src.lab agent ...)")
    memory = LabMemory(store)
    base = run_evaluation(dataset, baseline, assess=True)
    inputs = _inputs(dataset, base)
    report: dict[str, Any] = {"inputs": inputs, "baseline": base.metrics["overall"], "findings": [],
                              "hypotheses": [], "skipped": [], "warnings": list(base.warnings)}
    findings = investigate(dataset, base, baseline)
    for f in findings:
        known = [m for m in memory.recall(kind="evidence", key=f.key, current=inputs) if not m["stale"]]
        evidence_id = known[0]["item_id"] if known else memory.add(
            "evidence", f.statement, inputs=inputs, key=f.key,
            details={"kind": f.kind, "job_ids": f.job_ids, **f.details})
        report["findings"].append({"memory_id": evidence_id, "kind": f.kind, "statement": f.statement,
                                   "job_ids": f.job_ids, "actionable": f.overrides is not None})
    tested = 0
    for f in findings:
        if f.overrides is None:
            continue
        overrides: dict[str, Any] = f.overrides
        if tested >= max_hypotheses:
            report["skipped"].append({"statement": f.statement, "why": f"max_hypotheses={max_hypotheses} reached"})
            continue
        # replays are deterministic: a hypothesis already tested on identical inputs would give the same verdict
        prior = [m for m in memory.recall(kind="observation", key=f.key, current=inputs) if not m["stale"]]
        if prior:
            report["skipped"].append({"statement": f.statement, "memory_id": prior[0]["item_id"],
                                      "why": f"already tested on these inputs ({prior[0]['details'].get('verdict')})"})
            continue
        statement = (f"Changing filters.yaml as {json.dumps(overrides['filters.yaml'], ensure_ascii=False)[:200]} "
                     f"fixes: {f.statement}")
        hyp_id = store.add_hypothesis(statement, rationale=f"agent evidence on {inputs['dataset_id']}: {f.job_ids}")
        digest, path = create_bundle(baseline.config_dir, overrides, bundle_store)
        cand_id = store.add_candidate(hyp_id, digest, str(path), overrides)
        mem_h = memory.add("hypothesis", statement, inputs=inputs, key=f.key, ref=cand_id,
                           details={"overrides": overrides, "finding": f.kind, "job_ids": f.job_ids})
        entry: dict[str, Any] = {"memory_id": mem_h, "hypothesis_id": hyp_id, "candidate_id": cand_id,
                                 "statement": statement}
        tested += 1
        if not experiment:
            report["hypotheses"].append(entry)
            continue
        res = run_experiment(dataset, baseline, path, name=cand_id)
        exp_id = store.add_experiment(cand_id, dataset.manifest.model_dump(), res)
        cmp_ = res["comparison"]
        obs = memory.add("observation", f"experiment {exp_id}: {res['verdict']} — {res['explanation']}",
                         inputs=inputs, key=f.key, ref=exp_id,
                         details={"verdict": res["verdict"], "guardrails": res["guardrails"],
                                  "delta": cmp_["delta"], "limitations": cmp_["limitations"]})
        limitations = list(cmp_["limitations"])
        if f.kind == "false_positive_token":
            limitations.append(MINED_CAUTION)
        entry.update(experiment_id=exp_id, verdict=res["verdict"], eligible=res["eligible_for_approval"],
                     promotion_evidence=res["promotion_evidence"],
                     delta={k: cmp_["delta"][k] for k in ("precision", "recall", "unsafe_rejection_rate", "fp", "fn")},
                     limitations=limitations, observation_id=obs)
        if res["eligible_for_approval"]:
            memory.set_status(mem_h, "confirmed", f"experiment {exp_id}: improvement, guardrails green", exp_id)
            basis = ("independently supported" if res["independently_validated"] else
                     "EXPLORATORY ONLY, not independently validated: the independent human labels alone do not "
                     "support it")
            # the replay's profile too: approve recomputes the evidence and needs it when the dataset pins none
            profile = f' --profile "{baseline.profile_path}"' if baseline.profile_path else ""
            entry["recommendation"] = memory_rec = (
                f"Eligible for HUMAN review ({basis}): inspect experiment {exp_id}, then if you agree run "
                f"`python -m src.lab approve {exp_id} --dataset \"{dataset.path}\"{profile} --by <you> --reason \"...\""
                f"{'' if res['independently_validated'] else ' --accept-exploratory-evidence'}` in your own terminal "
                "(it re-runs the experiment and asks you to type a confirmation code)")
            memory.add("conclusion", memory_rec, inputs=inputs, key=f.key, ref=exp_id)
        elif res["verdict"] == "regression":
            memory.set_status(mem_h, "rejected", f"experiment {exp_id}: {res['explanation']}", exp_id)
            memory.add("conclusion", f"Refuted: {statement} ({res['explanation']})", inputs=inputs, key=f.key,
                       ref=exp_id, status="rejected", reason="experiment verdict: regression")
        report["hypotheses"].append(entry)
    report["run_id"] = memory.record_run(inputs, report)
    return report


def render_report(report: dict[str, Any]) -> str:
    b = report["baseline"]
    lines = [f"# Lab agent run {report.get('run_id', '')}", "",
             f"- dataset `{report['inputs']['dataset_id']}` (jobs {report['inputs']['dataset_jobs_sha256'][:12]}, "
             f"labels {report['inputs']['dataset_labels_sha256'][:12]}), strategy {report['inputs']['strategy_fingerprint'][:12]}",
             f"- baseline: tp={b['tp']} fp={b['fp']} fn={b['fn']} tn={b['tn']} borderline={b['borderline']}, "
             f"unsafe rejection rate {b['unsafe_rejection_rate']}", "", "## Findings (evidence)", ""]
    lines += [f"- [{f['kind']}] {f['statement']}{'' if f['actionable'] else ' — needs a human decision'}"
              for f in report["findings"]] or ["- none"]
    lines += ["", "## Hypotheses tested", ""]
    for h in report["hypotheses"]:
        lines.append(f"- {h['statement']}")
        if "verdict" in h:
            lines.append(f"  - experiment {h['experiment_id']}: **{h['verdict']}** "
                         f"({h['promotion_evidence'].replace('_', ' ')}), deltas {h['delta']}")
            lines += [f"  - limitation: {x}" for x in h["limitations"]]
        if h.get("recommendation"):
            lines.append(f"  - {h['recommendation']}")
    if not report["hypotheses"]:
        lines.append("- none")
    if report["skipped"]:
        lines += ["", "## Not re-tested", ""] + [f"- {s['statement']} ({s['why']})" for s in report["skipped"]]
    if report["warnings"]:
        lines += ["", "## Dataset warnings", ""] + [f"- {w}" for w in report["warnings"]]
    lines += ["", "Nothing here changes production: approval is a separate, human-only command."]
    return "\n".join(lines) + "\n"
