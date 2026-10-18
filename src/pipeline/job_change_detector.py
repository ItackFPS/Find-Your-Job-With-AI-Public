"""I19 — EXPERIMENTAL job-change detector.

Classifies a freshly-collected job observation as NEW / UNCHANGED / CHANGED
against that job's previously-stored state, and — for CHANGED — which
fields changed. Built entirely on the identity/content_hash infrastructure
`src.storage.sqlite.Storage` and `src.pipeline.deduplicate` already use
(the job's stable `id`, matched against its previously-stored row); this
module does not invent a new identity or fingerprint scheme.

Pure and side-effect-free: `detect_change()` takes two Job-shaped inputs and
returns an observation, nothing else. It never deletes a job, never mutates
either input, and is never called from anywhere that would let its output
change eligibility or ranking. Its one caller is
`Storage.upsert_jobs_batch(..., detect_field_changes=True)` — an opt-in
parameter (default False) that only records which fields changed for its
own already-existing `field_changes` stats key; the fields actually written
to the `jobs` table are identical whether or not detection runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from src.models.job import Job

_WHITESPACE_RE = re.compile(r"\s+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Text fields normalized (HTML/whitespace-collapsed, case-folded) before
# comparison, so re-formatting or re-casing of the same content is never
# reported as a change.
_NORMALIZED_TEXT_FIELDS = ("title", "company", "location", "description")

# Fields compared as-is (no text normalization needed/appropriate).
_DIRECT_COMPARE_FIELDS = (
    "employment_type",
    "salary_min",
    "salary_max",
    "salary_currency",
    "canonical_url",
    "apply_url",
)


def _normalize_text(value: str | None) -> str:
    if not value:
        return ""
    stripped = _HTML_TAG_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", stripped).strip().lower()


def _normalize_skills(skills: list[str] | None) -> tuple[str, ...]:
    return tuple(sorted({s.strip().lower() for s in (skills or []) if s and s.strip()}))


@dataclass(frozen=True)
class FieldChange:
    field: str
    old_value: object
    new_value: object


@dataclass(frozen=True)
class JobChangeResult:
    status: str  # "new" | "unchanged" | "changed"
    changed_fields: tuple[FieldChange, ...] = ()


def detect_change(existing_job: Job | None, new_job: Job) -> JobChangeResult:
    """Compare a job's previous stored state to a freshly-collected observation.

    ``existing_job=None`` always means NEW — the caller (upsert_jobs_batch)
    only invokes this for ids it already found in storage, so in practice
    this only happens when a caller uses the function directly without that
    lookup; it is handled here anyway so the function is safe standalone.

    Limitation (documented, not a bug): experience/education requirements
    are extracted from `description` at gate time and are not stored as Job
    fields, so a change to those specifically is only detectable indirectly,
    via the `description` text change they'd normally accompany. Re-running
    extraction just to diff it would add real per-job cost for a benefit not
    required by this phase's scope.
    """
    if existing_job is None:
        return JobChangeResult(status="new")

    changes: list[FieldChange] = []

    for field_name in _NORMALIZED_TEXT_FIELDS:
        old_value = getattr(existing_job, field_name)
        new_value = getattr(new_job, field_name)
        if _normalize_text(old_value) != _normalize_text(new_value):
            changes.append(FieldChange(field_name, old_value, new_value))

    for field_name in _DIRECT_COMPARE_FIELDS:
        old_value = getattr(existing_job, field_name)
        new_value = getattr(new_job, field_name)
        if old_value != new_value:
            changes.append(FieldChange(field_name, old_value, new_value))

    old_skills = _normalize_skills(existing_job.skills)
    new_skills = _normalize_skills(new_job.skills)
    if old_skills != new_skills:
        changes.append(FieldChange("skills", list(old_skills), list(new_skills)))

    if not changes:
        return JobChangeResult(status="unchanged")
    return JobChangeResult(status="changed", changed_fields=tuple(changes))
