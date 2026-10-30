"""Human-approved promotion: the only place the lab writes ``jobs.db``.

``active_strategy`` is an append-only history of pointers (the last row is current); rollback appends
a new row pointing at an earlier bundle; ``deactivate`` appends a row returning production to
``config/``. Approval requires an unmodified bundle that still reproduces from the current ``config/``
(otherwise the approved combination was never measured), a named approver and a reason, and evidence
**recomputed here**: ``lab.db`` is writable by the lab and its agent, so its stored verdict is only a
pointer to what to re-check, never the evidence. :func:`approve` reloads the dataset (content-hashed),
requires it to be the one the experiment recorded, replays baseline (what production runs now) and
candidate itself, and refuses unless the fresh replay is an eligible ``improvement`` that matches the
record (same fingerprints, same verdict). Evidence that is only ``exploratory_only`` (not independently
validated) needs ``accept_exploratory=True``. What was recomputed, who approved (name, OS account, host)
and why is stored with the pointer in ``approval_evidence``.

None of this stops a process that can write ``jobs.db`` from writing it directly: who may promote is
decided by which operating-system accounts can write ``jobs.db`` (``src/lab/isolation.py``, ADR 0011).

The production pipeline honours the latest row through ``src/decisions/active_strategy.py``. Rows written
here carry ``pipeline_effective = 1``; rows recorded before the pipeline honoured pointers have 0 and stay
inert, so an approval made when it had no effect never starts having one silently.
"""

from __future__ import annotations

import difflib
import getpass
import json
import platform
import sqlite3
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.decisions.active_strategy import EFFECTIVE_COLUMN, approval_basis, check_bundle, resolve_active_strategy
from src.decisions.bundle_format import BUNDLE_FILES, render_bundle
from src.evaluation.dataset import Dataset
from src.evaluation.models import StrategyRef
from src.lab.bundle import verify_bundle
from src.lab.experiment import run_experiment
from src.lab.store import LabStore
from src.storage.migrations import ensure_columns

EVIDENCE_COLUMN = "approval_evidence"

_DDL = """
CREATE TABLE IF NOT EXISTS active_strategy (
    pointer_id TEXT PRIMARY KEY, set_at TEXT NOT NULL, bundle_hash TEXT NOT NULL, bundle_path TEXT NOT NULL,
    approved_by TEXT NOT NULL, candidate_id TEXT, experiment_id TEXT, reason TEXT NOT NULL,
    action TEXT NOT NULL,                        -- approve | rollback | deactivate
    pipeline_effective INTEGER NOT NULL DEFAULT 0);  -- 1: written since the pipeline honours pointers
CREATE TRIGGER IF NOT EXISTS active_strategy_no_update BEFORE UPDATE ON active_strategy
    BEGIN SELECT RAISE(ABORT, 'active_strategy is append-only'); END;
CREATE TRIGGER IF NOT EXISTS active_strategy_no_delete BEFORE DELETE ON active_strategy
    BEGIN SELECT RAISE(ABORT, 'active_strategy is append-only'); END;
"""


_COLUMNS = ("pointer_id", "set_at", "bundle_hash", "bundle_path", "approved_by", "candidate_id", "experiment_id",
            "reason", "action")


class PromotionError(RuntimeError):
    pass


def approver_identity(name: str) -> dict[str, str]:
    """The named approver plus the account and host the command ran under (as the OS reports them)."""
    try:
        account = getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name available: recorded as unknown, never invented
        account = "unknown"
    return {"name": name, "os_account": account, "host": platform.node()}


def _append(jobs_db: str | Path, row: tuple[Any, ...], evidence: dict[str, Any] | None = None) -> None:
    if not Path(jobs_db).is_file():
        raise PromotionError(f"production database not found: {jobs_db} (refusing to create one)")
    conn = sqlite3.connect(str(jobs_db))
    try:
        conn.executescript(_DDL)
        with conn:
            ensure_columns(conn, "active_strategy", {
                EFFECTIVE_COLUMN: "INTEGER NOT NULL DEFAULT 0",  # table from before the pipeline honoured pointers: old rows stay 0
                EVIDENCE_COLUMN: "TEXT",                         # older rows have none: they were approved on the stored verdict
            })
            conn.execute(f"INSERT INTO active_strategy ({', '.join(_COLUMNS)}, {EFFECTIVE_COLUMN}, {EVIDENCE_COLUMN}) "
                         f"VALUES ({', '.join('?' * len(_COLUMNS))}, 1, ?)",
                         (*row, json.dumps(evidence, sort_keys=True, default=str) if evidence else None))
    except sqlite3.Error as exc:
        raise PromotionError(f"could not record the pointer in {jobs_db}: {exc}") from exc
    finally:
        conn.close()


def current_pointer(jobs_db: str | Path) -> dict[str, Any] | None:
    if not Path(jobs_db).is_file():
        return None
    conn = sqlite3.connect(f"file:{Path(jobs_db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM active_strategy ORDER BY set_at DESC, rowid DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return dict(row) if row else None


def _require_current(bundle_path: str, bundle_hash: str, config_dir: str | Path) -> None:
    status, why = check_bundle(bundle_path, bundle_hash, config_dir)
    if status != "active":
        raise PromotionError(f"the pipeline would not run this bundle ({status}): {why}")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _production_dir(jobs_db: str | Path, config_dir: str | Path) -> Path:
    """The configuration production runs now: the active bundle, else ``config_dir``."""
    active = resolve_active_strategy(jobs_db, config_dir)
    return active.bundle_dir if active.uses_bundle and active.bundle_dir is not None else Path(config_dir)


def strategy_change(production_dir: str | Path, bundle_dir: str | Path) -> list[str]:
    """Unified diff of what production runs now -> the bundle, both rendered the way bundles are written.

    Built from the hash-verified bundle files, not from anything the lab (or a proposer) wrote about the
    change: a hypothesis statement or a candidate's recorded overrides can say one thing and change another.
    Characters that a terminal could interpret (controls, bidi overrides) are shown escaped."""
    before = render_bundle(production_dir, {})
    lines: list[str] = []
    for name in BUNDLE_FILES:
        after = (Path(bundle_dir) / name).read_text(encoding="utf-8")
        lines += [line.rstrip("\n") for line in difflib.unified_diff(
            before[name].splitlines(True), after.splitlines(True), f"production/{name}", f"bundle/{name}", n=1)]
    return ["".join(f"\\u{ord(c):04x}" if unicodedata.category(c).startswith("C") else c for c in line)
            for line in lines]


def recompute_evidence(lab: LabStore, jobs_db: str | Path, experiment_id: str, dataset: Dataset, *,
                       config_dir: str | Path = "config", profile_path: str | None = None) -> dict[str, Any]:
    """Re-run ``experiment_id`` from its inputs, in this process, against what production runs now.

    Raises :class:`PromotionError` unless the dataset is the recorded one, lives outside the lab's writable
    bundle store, and the fresh replay matches the record. Returns the fresh result (not the stored one)."""
    exp = lab.get_experiment(experiment_id)
    cand = lab.get_candidate(exp["candidate_id"])
    verify_bundle(cand["bundle_path"], cand["bundle_hash"])
    if exp["result"].get("bundle_hash") not in (None, cand["bundle_hash"]):
        raise PromotionError("experiment was run on a different bundle than the candidate's")
    if _inside(dataset.path, Path(cand["bundle_path"]).parent):
        raise PromotionError(f"dataset {dataset.path} lives in the lab's writable bundle store: its labels are not "
                             "evidence a human controls")
    m = dataset.manifest
    if (m.dataset_id, m.jobs_sha256, m.labels_sha256) != (
            exp["dataset_id"], exp["dataset_jobs_sha256"], exp["dataset_labels_sha256"]):
        raise PromotionError(f"dataset {dataset.path} ({m.dataset_id}, labels {m.labels_sha256[:12]}) is not the one "
                             f"experiment {experiment_id} recorded ({exp['dataset_id']}, labels "
                             f"{exp['dataset_labels_sha256'][:12]})")
    try:
        fresh = run_experiment(dataset, StrategyRef(config_dir=str(_production_dir(jobs_db, config_dir)),
                                                    profile_path=profile_path),
                               cand["bundle_path"], name=cand["candidate_id"])
    except ValueError as exc:
        raise PromotionError(f"the experiment cannot be replayed: {exc}") from exc
    if (fresh["baseline_fingerprint"], fresh["candidate_fingerprint"]) != (
            exp["baseline_fingerprint"], exp["candidate_fingerprint"]):
        raise PromotionError(f"experiment {experiment_id} was run against a different baseline or candidate than "
                             "production and the bundle give now (strategy or profile changed since): re-run it")
    if fresh["verdict"] != exp["verdict"] or bool(fresh["eligible_for_approval"]) != bool(exp["eligible_for_approval"]):
        raise PromotionError(f"the lab's record of {experiment_id} ({exp['verdict']}) does not match a fresh replay "
                             f"({fresh['verdict']}): the record may have been altered; re-run the experiment")
    return fresh


def approve(lab: LabStore, jobs_db: str | Path, experiment_id: str, *, dataset: Dataset, approved_by: str,
            reason: str, config_dir: str | Path = "config", profile_path: str | None = None,
            accept_exploratory: bool = False, confirm: Callable[[dict[str, Any]], bool] | None = None) -> str:
    """Recompute, then (if confirm accepts the evidence it is shown) append the pointer."""
    if not approved_by.strip() or not reason.strip():
        raise PromotionError("approval needs a named approver and a reason")
    cand = lab.get_candidate(lab.get_experiment(experiment_id)["candidate_id"])
    if cand["status"] != "candidate":
        raise PromotionError(f"candidate {cand['candidate_id']} is already {cand['status']}; "
                             "run a new experiment instead of re-approving")
    _require_current(cand["bundle_path"], cand["bundle_hash"], config_dir)
    fresh = recompute_evidence(lab, jobs_db, experiment_id, dataset, config_dir=config_dir, profile_path=profile_path)
    if fresh["verdict"] != "improvement" or not fresh["eligible_for_approval"]:
        raise PromotionError(f"experiment {experiment_id} is not eligible (fresh replay: {fresh['verdict']}; "
                             f"{fresh['explanation']})")
    tier = fresh.get("promotion_evidence")
    if tier != "independently_supported" and not accept_exploratory:
        raise PromotionError(f"experiment {experiment_id} is {tier}: not independently validated "
                             f"({fresh['independent_evidence']['why']}); approving it anyway needs an explicit "
                             "acknowledgement (--accept-exploratory-evidence)")
    m = dataset.manifest
    evidence = {
        "recomputed_at": datetime.now(UTC).isoformat(timespec="seconds"), "experiment_id": experiment_id,
        "candidate_id": cand["candidate_id"], "bundle_hash": cand["bundle_hash"],
        "dataset": {"path": str(dataset.path), "dataset_id": m.dataset_id, "version": m.version,
                    "jobs_sha256": m.jobs_sha256, "labels_sha256": m.labels_sha256},
        "baseline_fingerprint": fresh["baseline_fingerprint"], "candidate_fingerprint": fresh["candidate_fingerprint"],
        "verdict": fresh["verdict"], "explanation": fresh["explanation"], "fixed": fresh["fixed"],
        "broken": fresh["broken"], "paired_accuracy_diff_ci95": fresh["paired_accuracy_diff_ci95"],
        "promotion_evidence": tier, "evidence_basis": fresh.get("evidence_basis"),
        "independent_evidence": fresh.get("independent_evidence"),
        "limitations": (fresh.get("comparison") or {}).get("limitations", []),
        "acknowledged_exploratory": tier != "independently_supported",
        "change": strategy_change(_production_dir(jobs_db, config_dir), cand["bundle_path"]),
        "approver": approver_identity(approved_by), "reason": reason,
    }
    if confirm is not None and not confirm(evidence):
        raise PromotionError("not confirmed: nothing was recorded")
    pid = f"ptr-{uuid.uuid4().hex[:10]}"
    # pointer first: if production cannot be written, nothing is recorded as approved
    _append(jobs_db, (pid, datetime.now(UTC).isoformat(timespec="seconds"), cand["bundle_hash"],
                      cand["bundle_path"], approved_by, cand["candidate_id"], experiment_id, reason, "approve"),
            evidence)
    lab.record_approval(cand["candidate_id"], experiment_id, approved_by, reason)
    return pid


def rollback(jobs_db: str | Path, to_pointer_id: str, *, approved_by: str, reason: str,
             config_dir: str | Path = "config") -> str:
    if not approved_by.strip() or not reason.strip():
        raise PromotionError("rollback needs a named approver and a reason")
    if not Path(jobs_db).is_file():
        raise PromotionError(f"production database not found: {jobs_db}")
    conn = sqlite3.connect(f"file:{Path(jobs_db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM active_strategy WHERE pointer_id = ?", (to_pointer_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        raise PromotionError(f"no approval has ever been recorded in {jobs_db}: {exc}") from exc
    finally:
        conn.close()
    if row is None:
        raise PromotionError(f"unknown pointer {to_pointer_id}")
    if row["action"] == "deactivate":
        return deactivate(jobs_db, approved_by=approved_by, reason=reason)
    verify_bundle(row["bundle_path"], row["bundle_hash"])
    _require_current(row["bundle_path"], row["bundle_hash"], config_dir)
    pid = f"ptr-{uuid.uuid4().hex[:10]}"
    _append(jobs_db, (pid, datetime.now(UTC).isoformat(timespec="seconds"), row["bundle_hash"], row["bundle_path"],
                      approved_by, row["candidate_id"], row["experiment_id"], reason, "rollback"),
            # the restored approval's basis travels with it: a rollback never upgrades the evidence
            {"to_pointer_id": to_pointer_id, "promotion_evidence": approval_basis(dict(row)),
             "approver": approver_identity(approved_by), "reason": reason})
    return pid


def deactivate(jobs_db: str | Path, *, approved_by: str, reason: str) -> str:
    """Return production to ``config/``: appends a ``deactivate`` pointer (history is kept)."""
    if not approved_by.strip() or not reason.strip():
        raise PromotionError("deactivation needs a named approver and a reason")
    pid = f"ptr-{uuid.uuid4().hex[:10]}"
    _append(jobs_db, (pid, datetime.now(UTC).isoformat(timespec="seconds"), "", "", approved_by, None, None,
                      reason, "deactivate"), {"approver": approver_identity(approved_by), "reason": reason})
    return pid
