"""Held-out protection (owner decision D4, option B, 2026-11-14; Phase B B3-H).

* Every tool measures on the ``tuning`` part by default (UI, CLI, lab).
* A run that reads a **real** dataset's held-out labels (split ``heldout``, or ``all`` which includes
  them) needs a named pre-registered candidate — a file ``docs/lab/preregistered/<id>.json`` committed
  before the run — and is refused otherwise.
* Each such run appends one line to ``<dataset dir>/heldout-usage.jsonl``: when, dataset id and
  version, split, candidate, caller, what was run. The log is append-only and lives with the dataset,
  never in a result export.
* The result file such a run writes holds no held-out job's decision: aggregates only.

Synthetic datasets have no held-out labels worth protecting and are not guarded.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evaluation.dataset import Dataset
from src.evaluation.heldout import is_heldout, split_of

PREREGISTERED_DIR = Path(__file__).resolve().parents[2] / "docs" / "lab" / "preregistered"
USAGE_LOG = "heldout-usage.jsonl"
CALLERS = ("cli", "ui", "lab")


class HeldoutRefused(ValueError):
    """A run would read held-out labels without a named pre-registered candidate."""


def preregistered_ids(directory: Path | None = None) -> set[str]:
    """Ids of the pre-registered candidates (file stems); empty when the folder is absent (an installed
    app ships no pre-registrations: held-out runs are a development function)."""
    d = directory or PREREGISTERED_DIR
    return {p.stem for p in d.glob("*.json")} if d.is_dir() else set()


def reads_heldout(kind: str, split: str) -> bool:
    return kind == "real" and split in ("heldout", "all")


def authorise(kind: str, split: str, candidate: str | None, directory: Path | None = None) -> None:
    """Raise :class:`HeldoutRefused` unless this run may read held-out labels."""
    if not reads_heldout(kind, split):
        return
    if not candidate:
        raise HeldoutRefused(
            f"split '{split}' reads held-out labels of a real dataset: name the pre-registered candidate "
            "this run tests (--candidate <id>, a file in docs/lab/preregistered/), or use --split tuning")
    if candidate not in preregistered_ids(directory):
        raise HeldoutRefused(f"'{candidate}' is not a pre-registered candidate (docs/lab/preregistered/{candidate}.json)")


def base_dataset_id(dataset: Dataset) -> str:
    dataset_id = dataset.manifest.dataset_id
    return dataset_id.rpartition("@")[0] if split_of(dataset) != "all" else dataset_id


def log_use(dataset_dir: str | Path, dataset: Dataset, split: str, candidate: str, caller: str, what: str,
            now: datetime | None = None) -> Path:
    """Append one usage line (never rewrites earlier lines)."""
    if caller not in CALLERS:
        raise ValueError(f"caller must be one of {', '.join(CALLERS)}")
    path = Path(dataset_dir) / USAGE_LOG
    line = json.dumps({
        "at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
        "dataset_id": base_dataset_id(dataset), "dataset_version": dataset.manifest.version,
        "labels_sha256": dataset.manifest.labels_sha256, "split": split, "candidate": candidate,
        "caller": caller, "what": what,
    }, sort_keys=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


def read_log(dataset_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(dataset_dir) / USAGE_LOG
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def aggregates_only(result: Any, dataset: Dataset) -> Any:
    """``result`` without the decisions of held-out jobs (their per-job rows would expose the part that
    judges pre-registered changes). Metrics are kept."""
    base = base_dataset_id(dataset)
    kept = [d for d in result.decisions if not is_heldout(base, d.job_id)]
    note = ("held-out labels were read: per-job decisions of held-out jobs are not written "
            f"({len(result.decisions) - len(kept)} left out); aggregates only")
    return result.model_copy(update={"decisions": kept, "warnings": [*result.warnings, note]})
