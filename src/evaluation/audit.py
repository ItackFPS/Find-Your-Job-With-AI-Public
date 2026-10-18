"""Read-only audit of an exported/annotated dataset (run it on the machine that holds the real data).

Prints counts and PASS/FAIL/WARN only — never job content. Checks: manifest/hash integrity, residual
identifiers (e-mail / URL / phone / company) in EVERY string field of the anonymised jobs and in the
annotation sheet, annotation sheet format and ids, label provenance, git-ignore status of every file
(including archived ``versions/``), and (with ``--db``) that re-exporting with the recorded
seed/salt/as-of reproduces the same ``jobs.jsonl`` hash.

``ok`` covers the integrity/safety checks. ``evidence_threshold`` is advisory (WARN): a dataset below
the minimum is valid but any metric computed from it is ``insufficient_evidence``.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from src.evaluation.dataset import DatasetError, load_dataset
from src.evaluation.export import (
    _EMAIL, _EMAIL_OBFUSCATED, _PHONE, _TEMPLATE_COLUMNS, _URL, _fold_digits, ANNOTATIONS, EXPORT_META, ExportError, assert_not_committable,
    REJECTED_STRATUM, export_real_dataset, find_leaks, open_readonly, summarise_leaks,
)

_NEEDED_LABEL_MIN = 150
_ID = re.compile(r"^j-[0-9a-f]{12}$")
_ALIAS = re.compile(r"^Company-[0-9a-f]{8}$")
ADVISORY = frozenset({"evidence_threshold"})


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def _identifier_hits(texts: list[str]) -> dict[str, int]:
    """Residual identifier counts; each text is scanned on its own."""
    out = {"email": 0, "url": 0, "phone": 0}
    for t in texts:
        t = _fold_digits(t)
        out["email"] += len(_EMAIL.findall(t)) + len(_EMAIL_OBFUSCATED.findall(t))
        out["url"] += len(_URL.findall(t))
        out["phone"] += len(_PHONE.findall(t))
    return out


def _is_feedback_dataset(meta: dict[str, Any]) -> bool:
    """The sidecar was written by ``feedback-dataset`` (older ones carry no ``builder``, and no ``seed``)."""
    return meta.get("builder") == "feedback-dataset" or ("seed" not in meta and "strata" not in meta)


def audit_dataset(path: str | Path, *, db: str | Path | None = None) -> dict[str, Any]:
    root = Path(path)
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, ok: bool, detail: str = "", *, advisory: bool = False) -> None:
        checks[name] = {"ok": ok, "detail": detail, "advisory": advisory or name in ADVISORY}

    report_leaks: list[dict[str, str]] = []

    def finish() -> dict[str, Any]:
        return {"ok": all(c["ok"] for c in checks.values() if not c["advisory"]), "checks": checks,
                "leaks": report_leaks}

    try:
        ds = load_dataset(root, require_complete_labels=False)
        record("integrity", True, f"{ds.manifest.job_count} jobs, hashes match manifest")
    except DatasetError as exc:
        record("integrity", False, str(exc)[:200])
        return finish()
    except Exception as exc:  # noqa: BLE001 - report, never traceback; class name only (no echoed values)
        record("integrity", False, f"dataset unreadable ({type(exc).__name__})")
        return finish()

    meta: dict[str, Any] = {}
    meta_path = root / EXPORT_META
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        meta = {}
    record("sidecar", bool(meta), "private sidecar present and readable" if meta else f"{EXPORT_META} missing or unreadable")
    anonymised = meta.get("anonymized") is True

    companies: dict[str, str | None] = {}
    missing_rows = 0
    checked = False
    if db is not None and isinstance(meta.get("id_map"), dict):
        try:
            conn = open_readonly(db)
            try:
                for new_id, real_id in meta["id_map"].items():
                    row = conn.execute("SELECT company FROM jobs WHERE id = ?", (real_id,)).fetchone()
                    if row is None and meta.get("strata", {}).get(new_id) == REJECTED_STRATUM:
                        row = conn.execute(          # a pipeline-rejected job: never stored in `jobs`
                            "SELECT company FROM job_decisions WHERE job_id = ? ORDER BY decided_at DESC LIMIT 1",
                            (real_id,)).fetchone()
                    if row is None:
                        missing_rows += 1
                    companies[new_id] = row["company"] if row else None
            finally:
                conn.close()
            checked = missing_rows == 0
        except (ExportError, sqlite3.Error, TypeError, AttributeError):
            checked = False
    leaks = find_leaks(ds.job_dicts, companies)
    company_leaks = [x for x in leaks if x["kind"] == "company"]
    other_leaks = [x for x in leaks if x["kind"] != "company"]
    bad_ids = sum(1 for j in ds.job_dicts if not _ID.match(str(j.get("id", ""))))
    by_kind: dict[str, int] = {}
    for x in other_leaks:
        by_kind[x["kind"]] = by_kind.get(x["kind"], 0) + 1
    record(
        "anonymisation",
        anonymised and not other_leaks and not bad_ids,
        f"anonymised flag={anonymised}; residual identifiers by kind {by_kind or 'none'}"
        + (f" [{summarise_leaks(other_leaks)}]" if other_leaks else "") + f"; non-anonymised ids={bad_ids}",
    )
    if checked:
        record("company_names", not company_leaks,
               f"{len(company_leaks)} whole-word mention(s) of the real company name"
               + (f" [{summarise_leaks(company_leaks)}]" if company_leaks else "") + " (checked against the database)")
    else:
        why = (f"{missing_rows} exported job(s) not found in that database (wrong database?)" if missing_rows
               else "not checked: pass --db with the database the dataset was exported from")
        record("company_names", False, why, advisory=True)
    report_leaks.extend(leaks)

    ann = root / ANNOTATIONS
    if ann.is_file():
        try:
            with open(ann, newline="", encoding="utf-8-sig") as f:
                rd = csv.DictReader(f)
                header = tuple(rd.fieldnames or ())
                rows = list(rd)
            ids = [r.get("job_id", "") for r in rows]
            problems = []
            if header != _TEMPLATE_COLUMNS:
                problems.append("columns differ from the template")
            if sorted(ids) != sorted(j["id"] for j in ds.job_dicts):
                problems.append("job_id set differs from the dataset (missing/duplicate/unknown ids)")
            if any((r.get("expected") or "").strip() not in {"", "accept", "reject", "borderline"} for r in rows):
                problems.append("invalid `expected` value(s)")
            sheet_hits = {"email": 0, "url": 0, "phone": 0}
            for r in rows:
                for c in r.values():                       # each cell on its own: no number across cells
                    for k, n in _identifier_hits([c or ""]).items():
                        sheet_hits[k] += n
            if any(sheet_hits.values()):
                problems.append(f"identifiers in sheet {sheet_hits}")
            filled = sum(1 for r in rows if (r.get("expected") or "").strip())
            record("annotation_sheet", not problems, f"{len(rows)} rows, {filled} filled" + (f"; {'; '.join(problems)}" if problems else ""))
        except (OSError, ValueError, csv.Error) as exc:
            record("annotation_sheet", False, f"unreadable ({type(exc).__name__})")
    else:
        record("annotation_sheet", False, "annotations.csv missing")

    labels = list(ds.labels.values())
    bad = [lab.job_id for lab in labels
           if lab.label_source not in ("human", "human_assisted") or not lab.labeler or not lab.labeled_at]
    n_assisted = sum(1 for lab in labels if lab.label_source == "human_assisted")
    record("provenance", not bad, f"{len(labels)} labels (recorded: {len(labels) - n_assisted - len(bad)} human, "
                                  f"{n_assisted} human_assisted), {len(bad)} without human/labeler/timestamp provenance")
    from src.evaluation.annotation_assist import assisted_job_ids

    assisted = assisted_job_ids(root)
    unmarked = sorted(lab.job_id for lab in labels if lab.label_source == "human" and lab.job_id in assisted)
    record("assisted_provenance", not unmarked,
           f"{len(unmarked)} label(s) recorded as independent `human` although an assistant proposal is on record; "
           "re-run annotate-import to stamp them `human_assisted`" if unmarked
           else "no label claims independence over an assistant proposal", advisory=True)
    decisive = sum(1 for lab in labels if lab.expected != "borderline")
    record("evidence_threshold", decisive >= _NEEDED_LABEL_MIN,
           f"{decisive} decisive labels (minimum {_NEEDED_LABEL_MIN}); below it every metric is insufficient_evidence")

    try:
        for d in [root, *sorted(p for p in (root / "versions").glob("*") if p.is_dir())]:
            assert_not_committable(d)
        record("git_ignored", True, "all dataset files (including versions/) are git-ignored or outside a repository")
    except ExportError as exc:
        record("git_ignored", False, str(exc)[:200])

    if db is not None:
        if not meta:
            record("reproducible", False, "sidecar missing: cannot re-export with the recorded seed/salt")
        else:
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    if _is_feedback_dataset(meta):      # rebuilt by its own builder, not by `export`
                        from src.evaluation.feedback_dataset import build_feedback_dataset

                        build_feedback_dataset(db, Path(tmp) / "r", dataset_id=ds.manifest.dataset_id,
                                               salt=meta["salt"], as_of=ds.manifest.as_of, anonymize=anonymised,
                                               first_seen=bool(meta.get("first_seen_at", False)))
                    else:
                        n_rejected = sum(1 for s in meta.get("strata", {}).values() if s == REJECTED_STRATUM)
                        export_real_dataset(db, Path(tmp) / "r", n=ds.manifest.job_count - n_rejected,
                                            seed=int(meta["seed"]), dataset_id=ds.manifest.dataset_id,
                                            as_of=ds.manifest.as_of, anonymize=anonymised, salt=meta["salt"],
                                            rejected=int(meta.get("rejected_requested", 0)),
                                            first_seen=bool(meta.get("first_seen_at", False)))
                    again = load_dataset(Path(tmp) / "r", require_complete_labels=False)
                    same = again.manifest.jobs_sha256 == ds.manifest.jobs_sha256
                    record("reproducible", same, "re-export with the recorded seed/salt/as-of "
                           + ("gives the same jobs hash" if same else "differs (the database changed since the export?)"))
                except (ExportError, DatasetError, KeyError, ValueError, TypeError) as exc:
                    record("reproducible", False, f"re-export failed ({type(exc).__name__})")
    return finish()
