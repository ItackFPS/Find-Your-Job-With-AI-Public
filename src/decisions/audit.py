"""Rejection audit sample: which rule-backed rejections the user is asked about (ADR 0010).

A solid rule-backed rejection is never flagged ``review`` by the disagreement engine and a rejected
job is never shown, so without this a wrong rule would hide good jobs with no way to notice. The
recorder marks a few of them (``signals.audit``); the review queue lists them until the user answers;
the answers become labelled false-negative evidence (``python -m src.evaluation feedback-dataset``).

Observability only: nothing here changes an outcome, a score, a ranking or a notification.

Selection is bounded (``slots``: how many audit items may be open at once, minus those already open),
stratified (one rejection per stage+rule stratum in turn, so one prolific rule cannot fill the queue)
and deterministic (hash order, reproducible from the journal). It is NOT a probability sample: each
mark records its stratum and the stratum's size in that run, so analyses can stratify, but rates
must never be computed by pooling audit answers as if they were a uniform sample.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.decisions.models import DecisionRecord

AUDIT_SAMPLER = "rejection-audit.v1"
AUDITABLE_STAGES = frozenset({"filter", "requirements_gate"})
# Freshness says nothing about whether the job suits the user: asking would only add noise.
EXCLUDED_STRATA = frozenset({"filter:published_age"})


def stratum(record: DecisionRecord) -> str | None:
    """``"<stage>:<rule>"`` of an auditable rejection, else ``None``."""
    if record.outcome != "rejected" or record.deciding_stage not in AUDITABLE_STAGES:
        return None
    if record.deciding_stage == "filter":
        code = next((t.detail for t in reversed(record.stage_trace)
                     if t.stage == "filter" and t.result == "reject" and t.detail), None)
        return f"filter:{code or 'unknown'}"
    dim = next((e.dimension for e in record.evidence
                if e.state == "violated" and e.source.stage == "requirements_gate"), "other")
    return f"requirements_gate:{dim}"


def _key(*parts: str) -> str:
    return hashlib.sha256(":".join((AUDIT_SAMPLER, *parts)).encode()).hexdigest()


def select_audit_sample(
    records: list[DecisionRecord], *, run_id: str, slots: int, excluded_job_ids: set[str],
) -> list[tuple[DecisionRecord, dict[str, Any]]]:
    """Up to ``slots`` auditable rejections with their ``signals.audit`` payload.

    Eligible: a rule-backed rejection (``AUDITABLE_STAGES``, not ``EXCLUDED_STRATA``) whose posting
    text was kept (the user must be able to read it), that the engine has not already flagged
    ``review`` (it is queued anyway) and whose job is not in ``excluded_job_ids`` (already audited
    in this episode, or already answered by the user)."""
    if slots <= 0:
        return []
    strata: dict[str, list[DecisionRecord]] = {}
    for r in records:
        s = stratum(r)
        if (s is None or s in EXCLUDED_STRATA or r.snapshot_hash is None
                or r.job_id in excluded_job_ids
                or (r.signals.get("assessment") or {}).get("state") == "review"):
            continue
        strata.setdefault(s, []).append(r)
    for members in strata.values():
        members.sort(key=lambda r: _key(r.job_id))
    # Strata take turns, in an order that rotates with the run so no rule is always served first.
    order = sorted(strata, key=lambda s: _key(run_id, s))
    picked: list[tuple[DecisionRecord, dict[str, Any]]] = []
    depth = 0
    while len(picked) < slots and any(depth < len(strata[s]) for s in order):
        for s in order:
            if depth < len(strata[s]) and len(picked) < slots:
                picked.append((strata[s][depth], {
                    "sampled": True, "sampler": AUDIT_SAMPLER, "stratum": s,
                    "stratum_size": len(strata[s]), "rank_in_stratum": depth + 1,
                }))
        depth += 1
    return picked
