"""CLI: ``python -m src.evaluation <validate|run|compare|golden> ...``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.evaluation.compare import compare_runs
from src.evaluation.dataset import DatasetError, load_dataset
from src.evaluation.models import LLMSpec, StrategyRef
from src.evaluation.report import render_markdown
from src.evaluation.runner import EvaluationError, run_evaluation
from src.evaluation.store import (
    diff_golden, golden_to_json, read_golden, read_run_result, to_golden, write_run_result,
)

DEFAULT_RUNS_DIR = "data/benchmarks/runs"


def _strategy(ns: argparse.Namespace) -> StrategyRef:
    llm = None
    if getattr(ns, "llm_endpoint", None):
        meta = dict(kv.split("=", 1) for kv in (ns.model_meta or []))
        llm = LLMSpec(endpoint=ns.llm_endpoint, model_name=ns.llm_model, model_meta=meta)
    return StrategyRef(name=ns.strategy, config_dir=ns.config_dir, profile_path=ns.profile, llm=llm,
                       esco_db=ns.esco_db)


def _add_strategy_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strategy", default="current")
    p.add_argument("--config-dir", default="config")
    p.add_argument("--profile", default=None, help="override the dataset's pinned profile")
    p.add_argument("--esco-db", default=None, help="enable ESCO synonyms in scoring (default: off, deterministic)")
    p.add_argument("--llm-endpoint", default=None, help="opt-in local LLM (e.g. http://localhost:1234)")
    p.add_argument("--llm-model", default="qwen/qwen3-8b")
    p.add_argument("--model-meta", action="append", metavar="K=V", help="e.g. quant=Q4_K_M (repeatable)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.evaluation")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate a dataset directory")
    v.add_argument("dataset")
    v.add_argument("--allow-unlabeled", action="store_true")

    r = sub.add_parser("run", help="run a dataset under a strategy")
    r.add_argument("dataset")
    _add_strategy_args(r)
    r.add_argument("--out", default=None, help=f"results dir (default {DEFAULT_RUNS_DIR}/<dataset_id>)")
    r.add_argument("--no-write", action="store_true")
    r.add_argument("--markdown", action="store_true")
    r.add_argument("--assess", action="store_true",
                   help="also run the disagreement engine on deterministic evidence (shadow review metrics)")

    c = sub.add_parser("compare", help="compare two run result files")
    c.add_argument("baseline")
    c.add_argument("candidate")
    c.add_argument("--dataset", default=None, help="dataset dir to classify flips as fixed/broken")

    g = sub.add_parser("golden", help="check or update golden decisions")
    g.add_argument("dataset")
    g.add_argument("golden_file")
    mode = g.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--update", action="store_true")
    _add_strategy_args(g)

    e = sub.add_parser("export", help="read-only stratified export of real jobs for annotation")
    e.add_argument("--db", default="data/jobs.db")
    e.add_argument("--out", required=True, help="must be git-ignored, e.g. data/benchmarks/real-v1")
    e.add_argument("--n", type=int, default=200)
    e.add_argument("--seed", type=int, default=20260917)
    e.add_argument("--dataset-id", default="real-v1")
    e.add_argument("--as-of", default=None)
    e.add_argument("--no-anonymize", action="store_true")
    e.add_argument("--salt", default=None, help="anonymisation salt (default: random, stored in the private sidecar)")
    e.add_argument("--rejected", type=int, default=0,
                   help="also add up to N jobs the pipeline rejected, from the decision journal (survival bias)")

    f = sub.add_parser("feedback-dataset", help="derive a frozen dataset from the feedback journal (read-only)")
    f.add_argument("--db", default="data/jobs.db")
    f.add_argument("--out", required=True, help="must be git-ignored, e.g. data/benchmarks/feedback-v1")
    f.add_argument("--config-dir", default="config")
    f.add_argument("--dataset-id", default="feedback-v1")
    f.add_argument("--salt", default=None, help="fix it (with --as-of) to rebuild an identical dataset")
    f.add_argument("--as-of", default=None)
    f.add_argument("--profile", default=None,
                   help="pinned into the dataset for reproducible replays "
                        "(default: the profile in effect, config/profile.local.yaml if present)")
    f.add_argument("--no-anonymize", action="store_true")
    f.add_argument("--no-first-seen", action="store_true",
                   help="omit first_seen_at, to rebuild a dataset made before jobs carried it")

    q = sub.add_parser("label-queue", help="review-flagged decisions still lacking explicit feedback")
    q.add_argument("--db", default="data/jobs.db")
    q.add_argument("--limit", type=int, default=50)

    au = sub.add_parser("audit", help="read-only audit of an exported dataset (counts only, no job content)")
    au.add_argument("dataset")
    au.add_argument("--db", default=None, help="also check real company names and that a re-export reproduces the same jobs hash")
    au.add_argument("--explain", action="store_true",
                    help="list each residual identifier as job id / field / kind / content-free shape")

    a = sub.add_parser("annotate-validate", help="strictly validate an annotation file (no write)")
    a.add_argument("dataset")
    a.add_argument("annotations")
    a.add_argument("--labeler", default="validate-only")

    i = sub.add_parser("annotate-import", help="write labels.jsonl + a new dataset version")
    i.add_argument("dataset")
    i.add_argument("annotations")
    i.add_argument("--labeler", required=True)
    i.add_argument("--version", default=None)

    ns = ap.parse_args(argv)
    try:
        if ns.cmd == "validate":
            ds = load_dataset(ns.dataset, require_complete_labels=not ns.allow_unlabeled)
            print(f"OK {ds.manifest.dataset_id}@{ds.manifest.version}: {ds.manifest.job_count} jobs, "
                  f"{len(ds.labels)} labels, {len(ds.unlabeled_ids)} unlabeled")
            return 0
        if ns.cmd == "export":
            from src.evaluation.export import export_real_dataset

            info = export_real_dataset(ns.db, ns.out, n=ns.n, seed=ns.seed, dataset_id=ns.dataset_id,
                                       as_of=ns.as_of, anonymize=not ns.no_anonymize, salt=ns.salt,
                                       rejected=ns.rejected)
            print(json.dumps({"n_jobs": info["n_jobs"], "strata": info["strata_counts"],
                              "rejected_pool": info["rejected_pool"]}, indent=2))
            print(f"annotate: {ns.out}/annotations.csv (fill 'expected' = accept|reject|borderline)", file=sys.stderr)
            return 0
        if ns.cmd == "feedback-dataset":
            from src.evaluation.feedback_dataset import build_feedback_dataset
            from src.evaluation.runner import default_profile_path

            info = build_feedback_dataset(ns.db, ns.out, config_dir=ns.config_dir, dataset_id=ns.dataset_id,
                                          salt=ns.salt, as_of=ns.as_of, anonymize=not ns.no_anonymize,
                                          profile_path=ns.profile or default_profile_path(ns.config_dir),
                                          first_seen=not ns.no_first_seen)
            print(json.dumps({k: info[k] for k in ("status", "n_jobs", "labels", "skipped", "thresholds")}, indent=2))
            return 0
        if ns.cmd == "audit":
            from src.evaluation.audit import audit_dataset

            report = audit_dataset(ns.dataset, db=ns.db)
            for name, c in report["checks"].items():
                tag = "PASS" if c["ok"] else ("WARN" if c["advisory"] else "FAIL")
                print(f"{tag}  {name}: {c['detail']}")
            if ns.explain:
                for x in report["leaks"]:
                    print(f"LEAK  {x['job_id']}  field={x['field']}  kind={x['kind']}  shape={x['shape']}")
            return 0 if report["ok"] else 1
        if ns.cmd == "label-queue":
            from src.evaluation.feedback_dataset import labelling_queue

            for item in labelling_queue(ns.db, ns.limit):
                print(json.dumps(item, ensure_ascii=False))
            return 0
        if ns.cmd in ("annotate-validate", "annotate-import"):
            from src.evaluation.annotation_assist import assisted_job_ids
            from src.evaluation.export import import_annotations, parse_annotations

            assisted = assisted_job_ids(Path(ns.dataset))   # jobs with an assistant proposal on record
            if ns.cmd == "annotate-validate":
                parsed, unlabeled = parse_annotations(ns.dataset, ns.annotations, labeler=ns.labeler,
                                                      assisted=assisted)
                n_assisted = sum(1 for lab in parsed if lab.label_source == "human_assisted")
                print(f"OK {len(parsed)} labelled ({n_assisted} assistant-assisted), "
                      f"{len(unlabeled)} not annotated yet")
            else:
                print(json.dumps(import_annotations(ns.dataset, ns.annotations, labeler=ns.labeler,
                                                    new_version=ns.version, assisted=assisted), indent=2))
            return 0
        if ns.cmd == "run":
            ds = load_dataset(ns.dataset)
            result = run_evaluation(ds, _strategy(ns), assess=ns.assess)
            print(render_markdown(result) if ns.markdown else json.dumps(result.metrics["overall"], indent=2))
            if not ns.no_write:
                out = ns.out or f"{DEFAULT_RUNS_DIR}/{ds.manifest.dataset_id}"
                print(f"written: {write_run_result(result, out)}", file=sys.stderr)
            return 0
        if ns.cmd == "compare":
            base, cand = read_run_result(ns.baseline), read_run_result(ns.candidate)
            labels: dict[str, str] | None = None
            provenance: dict[str, str] | None = None
            if ns.dataset:
                ds = load_dataset(ns.dataset, require_complete_labels=False)
                for key, mine in (("jobs_sha256", ds.manifest.jobs_sha256), ("labels_sha256", ds.manifest.labels_sha256)):
                    if mine != base.dataset[key]:     # flips must be judged against the labels the runs were scored on
                        raise ValueError(f"--dataset {key} {mine[:12]} differs from the runs' {base.dataset[key][:12]}: "
                                         "pass the dataset version the runs were made on (see its versions/ archive)")
                labels = {k: v.expected for k, v in ds.labels.items()}
                from src.evaluation.annotation_assist import assisted_job_ids
                from src.evaluation.metrics import effective_sources

                # a label recorded `human` over an assistant proposal on record is not independent either
                provenance = effective_sources(ds.labels, frozenset(assisted_job_ids(ds.path)))
            print(json.dumps(compare_runs(base, cand, labels, provenance), indent=2))
            return 0
        if ns.cmd == "golden":
            ds = load_dataset(ns.dataset)
            actual = to_golden(run_evaluation(ds, _strategy(ns)))
            path = Path(ns.golden_file)
            if ns.update:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(golden_to_json(actual), encoding="utf-8", newline="\n")   # committed `-text`: byte-stable
                print(f"golden updated: {path}")
                return 0
            diffs = diff_golden(read_golden(path), actual)
            if diffs:
                print("GOLDEN DIFFERS (a change detector, not a quality verdict):")
                print("\n".join(f"  {d}" for d in diffs))
                return 1
            print("golden identical")
            return 0
    except (DatasetError, EvaluationError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
