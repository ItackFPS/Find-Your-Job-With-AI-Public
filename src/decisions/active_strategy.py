"""Which strategy bundle production runs: the human-approved ``active_strategy`` pointer, resolved safely.

The lab records approvals as an append-only pointer history in ``jobs.db`` (``src/lab/promotion.py``);
this module is the only production reader of it (production never imports ``src.lab``, ADR 0011).

The latest pointer row decides, and anything doubtful falls back to ``config/`` — the behaviour the
pipeline had before pointers were honoured:

* no table / no row → ``default`` (``config/``);
* ``deactivate`` → ``deactivated`` (``config/``, explicitly chosen by a human);
* a row recorded before the pipeline honoured pointers (no ``pipeline_effective`` flag) → ``inert``
  (``config/``): such an approval was made when it had no effect, so it never starts having one silently;
* a bundle that is missing, modified, unreadable or not a mapping → ``invalid`` (``config/``, logged as an error);
* a bundle whose ``overrides.json`` applied to the CURRENT ``config/`` no longer reproduces it → ``stale``
  (``config/``): the configuration changed since the experiment, so the approved combination was never
  measured — re-run the experiment and approve again;
* otherwise ``active``: ``filters.yaml`` and ``scoring.yaml`` are read from the bundle.

Reading is read-only (``mode=ro``) and never raises.

``approval_basis`` (in :meth:`ActiveStrategy.to_dict`) says what evidence the latest approval rested on,
from the pointer's ``approval_evidence``: ``independently_supported``, ``exploratory_only`` (approved with
an explicit acknowledgement that it is not independently validated), ``unrecorded`` (a pointer written
without evidence, e.g. before it was recorded — never presented as validated), or ``None`` (no pointer,
or a deactivation). It describes the approval; it never changes what runs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from src.decisions.bundle_format import BUNDLE_FILES, bundle_hash, render_bundle, rendered_hash

logger = logging.getLogger(__name__)

Status = Literal["default", "deactivated", "inert", "invalid", "stale", "active"]
EFFECTIVE_COLUMN = "pipeline_effective"


@dataclass(frozen=True)
class ActiveStrategy:
    status: Status
    config_dir: Path
    reason: str
    pointer: dict[str, Any] | None = None
    bundle_dir: Path | None = None
    files: dict[str, Path] = field(default_factory=dict)

    def path(self, name: str) -> Path:
        """Where ``name`` (``filters.yaml``, ``scoring.yaml``, …) is read from for this run."""
        return self.files.get(name, self.config_dir / name)

    @property
    def uses_bundle(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict[str, Any]:
        p = self.pointer or {}
        return {"status": self.status, "reason": self.reason, "pointer_id": p.get("pointer_id"),
                "bundle_hash": p.get("bundle_hash") or None, "action": p.get("action"),
                "approved_by": p.get("approved_by"), "experiment_id": p.get("experiment_id"),
                "approval_basis": approval_basis(self.pointer),
                "files": {k: str(v) for k, v in self.files.items()}}


def approval_basis(pointer: dict[str, Any] | None) -> str | None:
    """What evidence a pointer's approval rested on (module docstring); never raises."""
    if not pointer or pointer.get("action") == "deactivate":
        return None
    try:
        evidence = json.loads(pointer.get("approval_evidence") or "null")
    except (TypeError, ValueError):
        evidence = None
    basis = evidence.get("promotion_evidence") if isinstance(evidence, dict) else None
    return basis if basis in ("independently_supported", "exploratory_only") else "unrecorded"


def read_pointer_history(db_path: str | Path) -> tuple[list[dict[str, Any]], bool]:
    """``(rows newest first, table_has_effective_flag)``; ``([], False)`` when there is no pointer table."""
    db = Path(db_path)
    if not db.is_file():
        return [], False
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(active_strategy)").fetchall()}
        if not cols:
            return [], False
        rows = [dict(r) for r in conn.execute("SELECT * FROM active_strategy ORDER BY set_at DESC, rowid DESC")]
        return rows, EFFECTIVE_COLUMN in cols
    finally:
        conn.close()


def check_bundle(bundle_dir: str | Path, expected_hash: str, config_dir: str | Path) -> tuple[str, str]:
    """``("active" | "invalid" | "stale", reason)`` for running ``bundle_dir`` on top of ``config_dir``."""
    bdir = Path(bundle_dir)
    try:
        if not bdir.is_dir() or any(not (bdir / n).is_file() for n in BUNDLE_FILES):
            return "invalid", f"bundle {bdir} is missing or incomplete"
        if bundle_hash(bdir) != expected_hash:
            return "invalid", f"bundle {bdir} no longer matches its approved hash (modified after approval)"
        for name in BUNDLE_FILES:
            if not isinstance(yaml.safe_load((bdir / name).read_text(encoding="utf-8")), dict):
                return "invalid", f"bundle {bdir / name} is not a YAML mapping"
        overrides = json.loads((bdir / "overrides.json").read_text(encoding="utf-8"))
        if not isinstance(overrides, dict) or set(overrides) - set(BUNDLE_FILES):
            return "invalid", f"bundle {bdir} has unexpected overrides"
        if rendered_hash(render_bundle(config_dir, overrides)) != expected_hash:
            return "stale", (f"{config_dir} changed since bundle {expected_hash[:12]} was built, so the approved "
                             "combination was never measured: re-run the experiment and approve again")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return "invalid", f"bundle {bdir} cannot be read: {exc}"
    return "active", f"approved bundle {expected_hash[:12]}"


def resolve_active_strategy(db_path: str | Path, config_dir: str | Path) -> ActiveStrategy:
    """The strategy a production run uses (see module docstring). Never raises; logs anything non-default."""
    cdir = Path(config_dir)
    try:
        rows, has_flag = read_pointer_history(db_path)
    except sqlite3.Error as exc:
        logger.error("active_strategy unreadable, running on %s: %s", cdir, exc)
        return ActiveStrategy("invalid", cdir, f"pointer table unreadable: {exc}")
    if not rows:
        return ActiveStrategy("default", cdir, "no approved strategy: running on the configuration directory")
    row = rows[0]
    if row.get("action") == "deactivate":
        return ActiveStrategy("deactivated", cdir, f"deactivated by {row.get('approved_by')}: running on {cdir}",
                              pointer=row)
    if not has_flag or not row.get(EFFECTIVE_COLUMN):
        result = ActiveStrategy("inert", cdir, "the latest approval was recorded before the pipeline honoured "
                                "approvals; approve the experiment again to make it effective", pointer=row)
        logger.warning("active_strategy %s is inert: %s", row.get("pointer_id"), result.reason)
        return result
    if not (isinstance(row.get("bundle_path"), str) and isinstance(row.get("bundle_hash"), str)):
        reason = f"pointer {row.get('pointer_id')} names no bundle (malformed row)"
        logger.error("active_strategy not used (invalid), running on %s: %s", cdir, reason)
        return ActiveStrategy("invalid", cdir, reason, pointer=row)
    status, reason = check_bundle(row["bundle_path"], row["bundle_hash"], cdir)
    if status != "active":
        logger.error("active_strategy %s not used (%s), running on %s: %s", row.get("pointer_id"), status, cdir, reason)
        return ActiveStrategy(status, cdir, reason, pointer=row)  # type: ignore[arg-type]
    bdir = Path(row["bundle_path"])
    logger.info("active_strategy %s: running approved bundle %s", row.get("pointer_id"), row["bundle_hash"][:12])
    return ActiveStrategy("active", cdir, reason, pointer=row, bundle_dir=bdir,
                          files={n: bdir / n for n in BUNDLE_FILES})


def strategy_of_run(recorded: dict[str, Any] | None, config_dir: str | Path) -> ActiveStrategy:
    """The strategy a past run used, from its ``stats.active_strategy`` record (``ActiveStrategy.to_dict``).

    For describing that run (e.g. explaining its decisions), not for running a new one: a bundle the run used
    is read again only while it still matches its recorded hash — whether ``config/`` changed since does not
    matter here. A run that used ``config/`` (or predates the record) maps to ``config/``. When the bundle a
    run used is gone or modified, ``config/`` is returned with status ``invalid`` and a warning is logged, so
    a caller never mistakes the substitute for what the run actually used. Never raises.
    """
    cdir = Path(config_dir)
    rec = recorded if isinstance(recorded, dict) else {}
    if rec.get("status") != "active":
        return ActiveStrategy(rec.get("status") or "default", cdir,  # type: ignore[arg-type]
                              rec.get("reason") or "the run used the configuration directory")
    files = {n: Path(p) for n, p in (rec.get("files") or {}).items() if n in BUNDLE_FILES}
    digest = rec.get("bundle_hash") or ""
    bdir = files["filters.yaml"].parent if "filters.yaml" in files else None
    try:
        intact = (bdir is not None and all(files.get(n) == bdir / n for n in BUNDLE_FILES)
                  and bundle_hash(bdir) == digest)
    except (OSError, ValueError):
        intact = False
    if not intact:
        reason = f"the bundle {digest[:12] or '?'} this run used is missing or modified; described with {cdir}"
        logger.warning("strategy_of_run: %s", reason)
        return ActiveStrategy("invalid", cdir, reason)
    return ActiveStrategy("active", cdir, f"approved bundle {digest[:12]} (as recorded by the run)",
                          bundle_dir=bdir, files=files)
