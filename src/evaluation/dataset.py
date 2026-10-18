"""Dataset loading / writing with strict validation (never silently drops data).

Layout of a dataset directory::

    manifest.json   DatasetManifest (hashes of the files below)
    jobs.jsonl      one object per line, kwargs of ``src.models.job.Job``
    labels.jsonl    one LabelRecord per line (joined AFTER inference only)
    profile.yaml    optional pinned profile (so results never depend on the
                    developer's personal config/profile.yaml)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.evaluation.fingerprint import sha256_bytes
from src.evaluation.models import DatasetManifest, LabelRecord
from src.models.job import Job

MANIFEST = "manifest.json"
JOBS = "jobs.jsonl"
LABELS = "labels.jsonl"
PROFILE = "profile.yaml"


class DatasetError(ValueError):
    """Raised for any invalid dataset; the message names the offending item."""


@dataclass(frozen=True)
class Dataset:
    path: Path
    manifest: DatasetManifest
    job_dicts: list[dict[str, Any]]
    labels: dict[str, LabelRecord]
    unlabeled_ids: tuple[str, ...]

    @property
    def profile_path(self) -> Path | None:
        p = self.path / PROFILE
        return p if p.is_file() else None

    def build_jobs(self) -> list[Job]:
        """Build ``Job`` objects with a deterministic ``discovered_at`` (= manifest ``as_of``
        unless the dataset states one) so no wall-clock value leaks into a replay."""
        return [Job(**{"discovered_at": self.manifest.as_of, **d}) for d in self.job_dicts]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _parse_jsonl(raw: bytes, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # split on "\n" only: str.splitlines() also breaks on U+2028/U+2029/NEL,
    # which legitimately appear unescaped inside scraped descriptions.
    for lineno, line in enumerate(raw.decode("utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{name}:{lineno}: invalid JSON ({exc})") from exc
        if not isinstance(obj, dict):
            raise DatasetError(f"{name}:{lineno}: expected an object")
        rows.append(obj)
    return rows


def _validate_jobs(rows: list[dict[str, Any]]) -> None:
    known = set(Job.model_fields)
    seen: set[str] = set()
    for i, row in enumerate(rows, start=1):
        jid = row.get("id")
        if not isinstance(jid, str) or not jid:
            raise DatasetError(f"{JOBS}:{i}: missing explicit string 'id' (a random id is not reproducible)")
        if jid in seen:
            raise DatasetError(f"{JOBS}: duplicate job id {jid!r}")
        seen.add(jid)
        unknown = sorted(set(row) - known)
        if unknown:
            raise DatasetError(f"{JOBS}: job {jid!r} has unknown field(s) {unknown}")
        try:
            Job(**row)
        except ValidationError as exc:
            raise DatasetError(f"{JOBS}: job {jid!r} invalid: {exc}") from exc


def load_dataset(path: str | Path, *, require_complete_labels: bool = True) -> Dataset:
    root = Path(path)
    mpath = root / MANIFEST
    if not mpath.is_file():
        raise DatasetError(f"{mpath}: manifest not found")
    try:
        manifest = DatasetManifest(**json.loads(mpath.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise DatasetError(f"{mpath}: invalid manifest ({exc})") from exc

    jobs_raw = (root / JOBS).read_bytes() if (root / JOBS).is_file() else None
    labels_raw = (root / LABELS).read_bytes() if (root / LABELS).is_file() else None
    if jobs_raw is None or labels_raw is None:
        raise DatasetError(f"{root}: {JOBS} and {LABELS} are both required")
    if sha256_bytes(jobs_raw) != manifest.jobs_sha256:
        raise DatasetError(f"{JOBS}: sha256 mismatch with manifest (file modified?)")
    if sha256_bytes(labels_raw) != manifest.labels_sha256:
        raise DatasetError(f"{LABELS}: sha256 mismatch with manifest (file modified?)")
    prof = root / PROFILE
    if manifest.profile_sha256 is None and prof.is_file():
        raise DatasetError(f"{PROFILE}: present but not declared in the manifest (unverified profile)")
    if manifest.profile_sha256 is not None:
        if not prof.is_file() or sha256_bytes(prof.read_bytes()) != manifest.profile_sha256:
            raise DatasetError(f"{PROFILE}: missing or sha256 mismatch with manifest")

    job_rows = _parse_jsonl(jobs_raw, JOBS)
    _validate_jobs(job_rows)
    if len(job_rows) != manifest.job_count:
        raise DatasetError(f"{JOBS}: {len(job_rows)} jobs but manifest says {manifest.job_count}")

    labels: dict[str, LabelRecord] = {}
    job_ids = {r["id"] for r in job_rows}
    for i, row in enumerate(_parse_jsonl(labels_raw, LABELS), start=1):
        try:
            rec = LabelRecord(**row)
        except ValidationError as exc:
            raise DatasetError(f"{LABELS}:{i}: invalid label ({exc})") from exc
        if rec.job_id in labels:
            raise DatasetError(f"{LABELS}: duplicate label for job {rec.job_id!r}")
        if rec.job_id not in job_ids:
            raise DatasetError(f"{LABELS}: orphan label, no job {rec.job_id!r}")
        labels[rec.job_id] = rec
    unlabeled = tuple(sorted(job_ids - set(labels)))
    if unlabeled and require_complete_labels:
        raise DatasetError(f"{LABELS}: {len(unlabeled)} job(s) without label, e.g. {unlabeled[:3]}")
    return Dataset(root, manifest, sorted(job_rows, key=lambda r: r["id"]), labels, unlabeled)


def write_dataset(
    dest: str | Path,
    *,
    dataset_id: str,
    version: str,
    kind: str,
    created_at: str,
    as_of: str,
    jobs: list[dict[str, Any]],
    labels: list[LabelRecord],
    profile_yaml: bytes | None = None,
    notes: str = "",
) -> DatasetManifest:
    """Write a dataset directory deterministically (sorted, stable bytes)."""
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    jobs_bytes = ("\n".join(_dumps(r) for r in sorted(jobs, key=lambda r: str(r.get("id", "")))) + "\n").encode()
    labels_bytes = (
        "\n".join(_dumps(lab.model_dump()) for lab in sorted(labels, key=lambda lab: lab.job_id)) + "\n"
    ).encode()
    manifest = DatasetManifest(
        dataset_id=dataset_id, version=version, kind=kind,  # type: ignore[arg-type]
        created_at=created_at, as_of=as_of, job_count=len(jobs),
        jobs_sha256=sha256_bytes(jobs_bytes), labels_sha256=sha256_bytes(labels_bytes),
        profile_sha256=sha256_bytes(profile_yaml) if profile_yaml is not None else None,
        notes=notes,
    )
    (root / JOBS).write_bytes(jobs_bytes)
    (root / LABELS).write_bytes(labels_bytes)
    if profile_yaml is not None:
        (root / PROFILE).write_bytes(profile_yaml)
    elif (root / PROFILE).exists():
        (root / PROFILE).unlink()   # never leave a stale, undeclared profile behind
    (root / MANIFEST).write_text(
        json.dumps(manifest.model_dump(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",   # byte-stable: no platform newline translation (hash-pinned fixtures)
    )
    return manifest
