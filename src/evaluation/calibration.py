"""In-app blind calibration of an exported real dataset (completion plan P2.3, review R-Q5 / R-A5).

The owner labels offers of an export (``real-v2``) in short sessions from the Quality page instead of
filling ``annotations.csv`` by hand. What this module guarantees:

* **originals untouched** — ``import_dataset`` copies the export folder into ``<data>/calibration/<id>/``
  and only that copy is ever written;
* **blind** — an item carries the offer's own text only: no score, tier, decision, stratum or
  assistant proposal (anchoring would make the labels agreement with the machine, not evidence);
* **representative order** — the first labels are spread across every export stratum in proportion
  to its size (R-A5: never the top-scored ones first); the order depends on ids only, so it is the
  same on every machine and labelling more never reshuffles what came before;
* **progress saved** — each answer is appended to ``calibration_answers.jsonl`` at once; ``save_session``
  then writes the pending answers as a NEW dataset version through ``import_annotations`` (the previous
  version is archived under ``versions/``), with ``label_source: human`` (or ``human_assisted`` for a
  job an assistant proposal exists for).

Answers: ``relevant`` -> ``accept``, ``not_relevant`` -> ``reject``, ``not_sure`` -> ``borderline``, with an
optional reason category.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evaluation.dataset import DatasetError, load_dataset
from src.evaluation.export import EXPORT_META, import_annotations

CALIBRATION_DIR = "calibration"
ANSWERS = "calibration_answers.jsonl"
STATE = "calibration_state.json"
LABELER = "in-app calibration"
ANSWER_TO_EXPECTED = {"relevant": "accept", "not_relevant": "reject", "not_sure": "borderline"}
REASONS = ("location", "domain", "seniority", "experience", "skills", "contract", "language", "other")
FIRST_TARGET = 100
TARGET_STEP = 100
MAX_SESSION = 30
DESCRIPTION_CHARS = 8000

_LOCK = threading.Lock()


class CalibrationError(Exception):
    """A refused calibration action, with a stable ``code`` for the UI."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def calibration_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / CALIBRATION_DIR


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dataset_dir(root: Path, dataset_id: str) -> Path:
    path = root / dataset_id
    if "/" in dataset_id or "\\" in dataset_id or dataset_id in ("", ".", "..") or not (path / "manifest.json").is_file():
        raise CalibrationError("not_found")
    return path


def import_dataset(source: str | Path, root: str | Path) -> dict[str, Any]:
    """Copy an exported real dataset into the calibration folder (the source is only read)."""
    src, root = Path(source).expanduser(), Path(root)
    try:
        ds = load_dataset(src, require_complete_labels=False)
    except (DatasetError, OSError) as exc:
        raise CalibrationError("invalid_dataset", str(exc)) from exc
    if ds.manifest.kind != "real":
        raise CalibrationError("not_real")
    dest = root / ds.manifest.dataset_id
    if dest.exists():
        raise CalibrationError("already_imported")
    if dest.resolve().is_relative_to(src.resolve()) or src.resolve().is_relative_to(root.resolve()):
        raise CalibrationError("invalid_dataset", "the source and the calibration folder overlap")
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)
    return summary(dest)


def _strata(path: Path) -> dict[str, str]:
    """Each job's export stratum, from the private sidecar; read for ordering only, never shown."""
    meta = path / EXPORT_META
    if not meta.is_file():
        return {}
    return dict(json.loads(meta.read_text(encoding="utf-8")).get("strata") or {})


def labelling_order(dataset_id: str, job_ids: Iterable[str], strata: dict[str, str]) -> list[str]:
    """Every job once, ordered so that any prefix holds each stratum in proportion to its size.

    Within a stratum the jobs are shuffled by a hash of their id; the i-th of n jobs gets the key
    (i + 0.5) / n, and sorting all jobs by key interleaves the strata evenly."""
    def h(jid: str) -> str:
        return hashlib.sha256(f"calibration:{dataset_id}:{jid}".encode()).hexdigest()

    groups: dict[str, list[str]] = {}
    for jid in job_ids:
        groups.setdefault(strata.get(jid, "unknown"), []).append(jid)
    keyed = []
    for members in groups.values():
        members.sort(key=h)
        keyed += [((i + 0.5) / len(members), h(jid), jid) for i, jid in enumerate(members)]
    return [jid for _, _, jid in sorted(keyed)]


def _pending(path: Path) -> dict[str, dict[str, Any]]:
    """Answers recorded since the last save, latest per job."""
    f = path / ANSWERS
    out: dict[str, dict[str, Any]] = {}
    if f.is_file():
        # every answer ends with a newline: a last line without one was cut by a crash mid-write and
        # holds no complete answer (that offer simply comes back in the next session)
        for line in f.read_text(encoding="utf-8").split("\n")[:-1]:
            if line.strip():
                row = json.loads(line)
                out[row["job_id"]] = row
    return out


def _target(path: Path, total: int) -> int:
    f = path / STATE
    stored = json.loads(f.read_text(encoding="utf-8")).get("target", FIRST_TARGET) if f.is_file() else FIRST_TARGET
    return min(total, max(FIRST_TARGET, int(stored)))


def summary(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    ds = load_dataset(path, require_complete_labels=False)
    pending = _pending(path)
    total = len(ds.job_dicts)
    provenance: dict[str, int] = {}
    for lab in ds.labels.values():
        provenance[lab.label_source] = provenance.get(lab.label_source, 0) + 1
    by_answer = {a: sum(1 for lab in ds.labels.values() if lab.expected == e) for a, e in ANSWER_TO_EXPECTED.items()}
    return {
        "dataset_id": ds.manifest.dataset_id, "version": ds.manifest.version, "total": total,
        "labelled": len(ds.labels), "pending": len(pending), "target": _target(path, total),
        "by_answer": by_answer, "provenance": dict(sorted(provenance.items())),
    }


def list_datasets(root: str | Path) -> list[dict[str, Any]]:
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for child in sorted(p for p in root.iterdir() if (p / "manifest.json").is_file()):
        try:
            out.append(summary(child))
        except DatasetError as exc:
            out.append({"dataset_id": child.name, "error": "invalid_dataset", "detail": str(exc)})
    return out


def exports_found(folder: str | Path, imported: set[str]) -> list[dict[str, Any]]:
    """Real exports directly under ``folder`` not imported yet, so the page can offer them by name."""
    found = []
    folder = Path(folder)
    for child in sorted(p for p in folder.iterdir() if (p / "manifest.json").is_file()) if folder.is_dir() else []:
        try:
            m = load_dataset(child, require_complete_labels=False).manifest
        except DatasetError:
            continue
        if m.kind == "real" and m.dataset_id not in imported:
            found.append({"source": str(child), "dataset_id": m.dataset_id, "total": m.job_count})
    return found


def next_items(root: str | Path, dataset_id: str, n: int) -> list[dict[str, Any]]:
    """The next ``n`` offers to label: blind (the offer's own text only), in the representative order."""
    path = _dataset_dir(Path(root), dataset_id)
    ds = load_dataset(path, require_complete_labels=False)
    done = set(ds.labels) | set(_pending(path))
    by_id = {j["id"]: j for j in ds.job_dicts}
    items = []
    for jid in labelling_order(ds.manifest.dataset_id, by_id, _strata(path)):
        if jid in done:
            continue
        j = by_id[jid]
        items.append({
            "job_id": jid, "title": j.get("title", ""), "company": j.get("company", ""),
            "location": j.get("location", ""), "remote": j.get("remote"), "source": j.get("source", ""),
            "description": (j.get("description") or "")[:DESCRIPTION_CHARS],
            "description_truncated": len(j.get("description") or "") > DESCRIPTION_CHARS,
        })
        if len(items) >= max(1, min(n, MAX_SESSION)):
            break
    return items


def record_answer(root: str | Path, dataset_id: str, job_id: str, answer: str, reason: str | None = None) -> dict[str, Any]:
    """Save one answer at once (saved as a label by ``save_session``)."""
    if answer not in ANSWER_TO_EXPECTED:
        raise CalibrationError("invalid_answer")
    if reason is not None and reason not in REASONS:
        raise CalibrationError("invalid_reason")
    path = _dataset_dir(Path(root), dataset_id)
    with _LOCK:
        ds = load_dataset(path, require_complete_labels=False)
        if job_id not in {j["id"] for j in ds.job_dicts}:
            raise CalibrationError("unknown_job")
        if job_id in ds.labels:
            raise CalibrationError("already_labelled")
        row = {"job_id": job_id, "expected": ANSWER_TO_EXPECTED[answer], "category": reason or "",
               "reason": "", "answered_at": _now()}
        answers = path / ANSWERS
        if answers.is_file():
            data = answers.read_bytes()
            if data and not data.endswith(b"\n"):          # drop what a crash cut mid-write (see _pending)
                with open(answers, "r+b") as f:
                    f.truncate(data.rfind(b"\n") + 1)
        with open(answers, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())          # on disk before the page moves on: survives a crash or power cut
    return summary(path)


def save_session(root: str | Path, dataset_id: str) -> dict[str, Any]:
    """Write the pending answers as a new dataset version, then clear them."""
    from src.evaluation.annotation_assist import assisted_job_ids

    path = _dataset_dir(Path(root), dataset_id)
    with _LOCK:
        pending = _pending(path)
        if not pending:
            return {**summary(path), "changed": 0}
        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "answers.jsonl"
            sheet.write_text("".join(json.dumps({k: r[k] for k in ("job_id", "expected", "category", "reason")},
                                                ensure_ascii=False) + "\n" for r in pending.values()),
                             encoding="utf-8")
            result = import_annotations(path, sheet, labeler=LABELER, assisted=assisted_job_ids(path))
        (path / ANSWERS).unlink()
    return {**summary(path), "changed": result["changed"]}


def extend_target(root: str | Path, dataset_id: str) -> dict[str, Any]:
    """"Label 100 more": raise the goal by ``TARGET_STEP`` (never beyond the dataset)."""
    path = _dataset_dir(Path(root), dataset_id)
    s = summary(path)
    (path / STATE).write_text(json.dumps({"target": min(s["total"], s["target"] + TARGET_STEP)}), encoding="utf-8")
    return summary(path)


def main(argv: list[str] | None = None) -> int:
    """``python -m src.evaluation.calibration <root> <action> [...]`` → one JSON object on stdout.

    The app calls this in a child process: production code never imports the evaluation package
    (ADR 0007). Refusals print ``{"error": <code>}`` and exit 2."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m src.evaluation.calibration")
    ap.add_argument("root")
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("list").add_argument("--exports", default=None, help="folder to look for exports not imported yet")
    sub.add_parser("import").add_argument("source")
    nx = sub.add_parser("next")
    nx.add_argument("dataset_id")
    nx.add_argument("n", type=int)
    an = sub.add_parser("answer")
    for a in ("dataset_id", "job_id", "answer"):
        an.add_argument(a)
    an.add_argument("--reason", default=None)
    for name in ("save", "extend"):
        sub.add_parser(name).add_argument("dataset_id")
    ns = ap.parse_args(argv)
    try:
        if ns.action == "list":
            datasets = list_datasets(ns.root)
            out: Any = {"datasets": datasets, "exports_found": exports_found(ns.exports, {d["dataset_id"] for d in datasets})
                        if ns.exports else [], "reasons": list(REASONS), "max_session": MAX_SESSION}
        elif ns.action == "import":
            out = import_dataset(ns.source, ns.root)
        elif ns.action == "next":
            out = {"items": next_items(ns.root, ns.dataset_id, ns.n)}
        elif ns.action == "answer":
            out = record_answer(ns.root, ns.dataset_id, ns.job_id, ns.answer, ns.reason)
        elif ns.action == "save":
            out = save_session(ns.root, ns.dataset_id)
        else:
            out = extend_target(ns.root, ns.dataset_id)
    except CalibrationError as exc:
        print(json.dumps({"error": exc.code}))
        return 2
    except DatasetError:
        print(json.dumps({"error": "invalid_dataset"}))
        return 2
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
