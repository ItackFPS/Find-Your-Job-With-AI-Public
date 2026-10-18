"""Immutable on-disk artefacts: run results and golden decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.evaluation.models import Decision, GoldenDecisions, RunResult


def write_run_result(result: RunResult, out_dir: str | Path) -> Path:
    """Write ``result`` with exclusive creation — an existing run is never overwritten."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = result.created_at.replace(":", "").replace("-", "")[:15]
    name = f"{stamp}-{result.strategy['fingerprint'][:8]}-{result.run_id[:8]}.json"
    path = d / name
    payload = json.dumps(result.model_dump(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(payload)
    return path


def read_run_result(path: str | Path) -> RunResult:
    return RunResult(**json.loads(Path(path).read_text(encoding="utf-8")))


def to_golden(result: RunResult) -> GoldenDecisions:
    ds = result.dataset
    return GoldenDecisions(
        dataset={k: ds[k] for k in ("id", "version", "jobs_sha256", "labels_sha256", "as_of")},
        strategy_fingerprint=result.strategy["fingerprint"],
        decisions=[Decision(**d.model_dump()) for d in result.decisions],
    )


def golden_to_json(g: GoldenDecisions) -> str:
    return json.dumps(g.model_dump(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def read_golden(path: str | Path) -> GoldenDecisions:
    return GoldenDecisions(**json.loads(Path(path).read_text(encoding="utf-8")))


def diff_golden(expected: GoldenDecisions, actual: GoldenDecisions) -> list[str]:
    """Human-readable differences; empty list means identical."""
    out: list[str] = []
    if expected.dataset != actual.dataset:
        out.append(f"dataset identity differs: {expected.dataset} != {actual.dataset}")
    if expected.strategy_fingerprint != actual.strategy_fingerprint:
        out.append("strategy fingerprint differs (a config file changed)")
    exp = {d.job_id: d for d in expected.decisions}
    act = {d.job_id: d for d in actual.decisions}
    for jid in sorted(set(exp) | set(act)):
        if jid not in exp or jid not in act:
            out.append(f"{jid}: present in only one side")
        elif exp[jid] != act[jid]:
            e, a = exp[jid].model_dump(), act[jid].model_dump()
            changed = ", ".join(f"{k}: {e[k]!r} -> {a[k]!r}" for k in e if e[k] != a[k])
            out.append(f"{jid}: {changed}")
    return out
