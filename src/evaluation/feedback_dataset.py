"""Turn the feedback journal + decision journal into a frozen, anonymised evaluation dataset (P4).

Read-only on ``jobs.db`` (``mode=ro``): nothing here can write production data. Labels are
*derived* with explicit provenance and reliability; behaviour signals (views, clicks) never create a
label on their own. A dataset below the minimum evidence thresholds is still written as a draft but
reported ``insufficient_evidence`` — no quality conclusion may be drawn from it.

Known bias (written into the manifest): only jobs the user was shown can receive feedback.

Freshness context (ADR 0012): a job carries ``first_seen_at`` from ``jobs`` when it was stored before the
dataset's ``as_of``, exactly as ``export`` does, so a replay ages an undated listing from its first sighting
like production. Jobs never stored (rejected on every run) carry none, as in production. Datasets built before
this field existed are rebuilt byte for byte with ``first_seen=False`` (``audit`` reads the private sidecar).

Reproducibility: only feedback events and decisions not after ``as_of`` are used, so a fixed ``salt`` and
``as_of`` rebuild the same dataset later, as long as the journal rows it used still exist (retention may purge
snapshot text). Listings with the same ``content_hash`` count once (the first job id), like the rejected sample.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import zlib
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.evaluation.dataset import write_dataset
from src.evaluation.export import (
    EXPORT_META, ExportError, _first_seen_before, assert_not_committable, find_leaks, open_readonly, scrub_text,
    summarise_leaks,
)
from src.evaluation.models import LabelRecord
from src.feedback.config import FeedbackConfig, load_feedback_config
from src.feedback.service import canonical_status

BUILDER = "feedback-dataset"   # recorded in the private sidecar so `audit` knows how to rebuild it
MIN_LABELLED = 100
MIN_PER_CLASS = 20
# canonical statuses meaning "the user judged the job worth acting on"
_ACTED = frozenset({"applied", "interview", "offer", "accepted_offer", "employer_rejected"})


def derive_label(events: list[dict[str, Any]], config: FeedbackConfig) -> tuple[str, str, str] | None:
    """``(expected, reliability, rationale)`` from a job's feedback events (oldest first), or ``None``.

    Explicit feedback outranks outcomes; the latest explicit event wins. An explicit verdict that
    contradicts an acted-on outcome is ``borderline`` (contested) rather than guessed.
    """
    explicit: tuple[str, str] | None = None
    outcome_accept: str | None = None
    for e in events:
        if e["signal_class"] == "explicit":
            reasons = json.loads(e["reasons_json"] or "[]")
            if e["type"] == "interested":
                explicit = ("accept", "explicit:interested")
            elif e["type"] == "not_interested":
                explicit = ("reject", "explicit:not_interested[" + ",".join(reasons) + "]")
            elif e["type"] == "ranking_feedback" and "interesting_despite_low_score" in reasons:
                explicit = ("accept", "explicit:interesting_despite_low_score")
            elif e["type"] == "unsure":
                explicit = ("borderline", "explicit:unsure")
        elif e["signal_class"] == "outcome":
            meta = json.loads(e["metadata_json"] or "{}")
            canon, _ = canonical_status(meta.get("legacy_to"), config)
            if canon in _ACTED:
                outcome_accept = f"outcome:{canon}"
    if explicit and outcome_accept and explicit[0] == "reject":
        return "borderline", "low", f"contested: {explicit[1]} vs {outcome_accept}"
    if explicit:
        return explicit[0], "low" if explicit[0] == "borderline" else "high", explicit[1]
    if outcome_accept:
        return "accept", "medium", outcome_accept
    return None


def _snapshot(conn: sqlite3.Connection, h: str | None) -> dict[str, Any] | None:
    if not h:
        return None
    row = conn.execute("SELECT text_blob, compression FROM job_snapshots WHERE snapshot_hash = ?", (h,)).fetchone()
    if row is None or row["text_blob"] is None:
        return None
    raw = zlib.decompress(row["text_blob"]) if row["compression"] == "zlib" else bytes(row["text_blob"])
    return dict(json.loads(raw.decode("utf-8")))


def _not_after(raw: str | None, ref: datetime) -> bool:
    """``raw`` (ISO text) is a valid timestamp not after ``ref``; naive values are read as UTC."""
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)) <= ref


def build_feedback_dataset(
    db_path: str | Path, out_dir: str | Path, *, config_dir: str | Path = "config",
    dataset_id: str = "feedback-v1", salt: str | None = None, as_of: str | None = None,
    anonymize: bool = True, profile_path: str | Path | None = None, first_seen: bool = True,
) -> dict[str, Any]:
    """``profile_path`` is pinned into the dataset so replays never depend on a personal config.
    Pass ``salt`` and ``as_of`` to rebuild a byte-identical dataset; ``first_seen=False`` reproduces
    datasets built before jobs carried ``first_seen_at`` (see the module docstring)."""
    out = Path(out_dir)
    assert_not_committable(out)
    if (out / "manifest.json").exists():
        raise ExportError(f"{out} already contains a dataset; refusing to overwrite")
    cfg = load_feedback_config(config_dir)
    as_of = as_of or datetime.now(UTC).isoformat(timespec="seconds")
    try:
        as_of_dt = datetime.fromisoformat(as_of)
    except ValueError as exc:
        raise ExportError(f"--as-of is not an ISO timestamp: {as_of!r}") from exc
    as_of_dt = as_of_dt if as_of_dt.tzinfo is not None else as_of_dt.replace(tzinfo=UTC)
    salt = salt if salt is not None else secrets.token_hex(16)
    conn = open_readonly(db_path)
    skipped: Counter[str] = Counter()
    jobs: list[dict[str, Any]] = []
    labels: list[LabelRecord] = []
    id_map: dict[str, str] = {}
    real_companies: dict[str, str | None] = {}
    decision_of: dict[str, dict[str, Any]] = {}
    seen_content: set[str] = set()
    audited = 0
    try:
        try:
            conn.execute("SELECT 1 FROM job_decisions LIMIT 1")
            conn.execute("SELECT 1 FROM job_snapshots LIMIT 1")
            events = conn.execute(
                "SELECT * FROM feedback_events ORDER BY job_id, occurred_at, rowid").fetchall()
        except sqlite3.OperationalError as exc:
            raise ExportError(f"feedback/decision journal tables missing in {db_path}: {exc}") from exc
        first_seen_at: dict[str, str] = {}
        if first_seen:
            try:
                first_seen_at = {r[0]: r[1] for r in conn.execute(
                    "SELECT id, first_seen_at FROM jobs WHERE first_seen_at IS NOT NULL").fetchall()}
            except sqlite3.OperationalError:
                first_seen_at = {}      # no jobs table: nothing was ever stored, so no job has a first sighting
        by_job: dict[str, list[dict[str, Any]]] = {}
        for r in events:
            if not _not_after(r["occurred_at"], as_of_dt):
                skipped["event_after_as_of"] += 1
                continue
            by_job.setdefault(r["job_id"], []).append(dict(r))
        for job_id, evs in sorted(by_job.items()):
            derived = derive_label(evs, cfg)
            if derived is None:
                skipped["no_label_signal"] += 1
                continue
            expected, reliability, why = derived
            # the decision the user reacted to (pinned), else the latest one
            # the decision the label-determining event reacted to (explicit first, else outcome)
            klass = "explicit" if why.startswith("explicit") else "outcome"
            pinned = next((e["decision_id"] for e in reversed(evs)
                           if e["signal_class"] == klass and e["decision_id"]), None)
            row = (conn.execute("SELECT * FROM job_decisions WHERE decision_id = ?", (pinned,)).fetchone()
                   if pinned else None) or next(
                (d for d in conn.execute("SELECT * FROM job_decisions WHERE job_id = ? "
                                         "ORDER BY decided_at DESC, rowid DESC", (job_id,))
                 if _not_after(d["decided_at"], as_of_dt)), None)
            if row is None:
                skipped["no_decision_record"] += 1
                continue
            if row["content_hash"]:
                if row["content_hash"] in seen_content:
                    skipped["duplicate_content"] += 1
                    continue
                seen_content.add(row["content_hash"])
            payload = _snapshot(conn, row["snapshot_hash"])
            if payload is None:
                skipped["job_text_unavailable"] += 1
                continue
            digest = hashlib.sha256(f"{salt}:id:{job_id}".encode()).hexdigest()[:12]
            new_id = f"j-{digest}" if anonymize else job_id
            company = payload.get("company")
            alias = (f"Company-{hashlib.sha256(f'{salt}:co:{company}'.encode()).hexdigest()[:8]}"
                     if anonymize and company else None)
            rec = {k: v for k, v in payload.items() if v is not None and k != "id"}
            if anonymize:
                rec["company"] = alias
                for k in ("description", "title", "location", "department"):
                    if k in rec:
                        rec[k] = scrub_text(rec[k], company, alias)
                if "skills" in rec:
                    rec["skills"] = [scrub_text(x, company, alias) for x in rec["skills"]]
                if alias is None:
                    rec.pop("company", None)
            rec["id"] = new_id
            rec.setdefault("source", row["source"] or "unknown")
            seen = _first_seen_before(first_seen_at.get(job_id), as_of_dt)
            if seen is not None:
                rec["first_seen_at"] = seen
            jobs.append(rec)
            # the decision the label answers, kept private: the dataset itself stays blind to the outcome
            decision_of[new_id] = {k: row[k] for k in (
                "decision_id", "run_id", "decided_at", "outcome", "deciding_stage", "strategy_version")}
            # a rejection the user saw through the audit sample: its stratum, for stratified analysis
            audit = (json.loads(row["signals_json"] or "{}") or {}).get("audit")
            if audit:
                decision_of[new_id]["audit"] = audit
                audited += 1
            id_map[new_id] = job_id
            real_companies[new_id] = company
            labels.append(LabelRecord(
                job_id=new_id, expected=expected, category="feedback", label_source="derived",  # type: ignore[arg-type]
                reason=f"reliability={reliability}; {why}", labeler="feedback-derive", labeled_at=as_of))
    finally:
        conn.close()

    if anonymize:
        leaks = find_leaks(jobs, real_companies)
        if leaks:           # same contract as `export`: never write a leaking "anonymised" dataset
            raise ExportError(f"anonymisation left {len(leaks)} residual identifier(s) ({summarise_leaks(leaks)}); "
                              "nothing was written")
    counts = Counter(lab.expected for lab in labels)
    decisive = {k: counts.get(k, 0) for k in ("accept", "reject")}
    labelled = sum(decisive.values())
    insufficient = labelled < MIN_LABELLED or min(decisive.values()) < MIN_PER_CLASS
    status = "insufficient_evidence" if insufficient else "ok"
    profile_yaml = Path(profile_path).read_bytes() if profile_path and Path(profile_path).is_file() else None
    manifest = write_dataset(
        out, dataset_id=dataset_id, version="1", kind="real", created_at=as_of, as_of=as_of,
        jobs=jobs, labels=labels, profile_yaml=profile_yaml,
        notes=(f"Derived from feedback_events + job_decisions. status={status}. "
               "SELECTION BIAS: only jobs shown to the user can receive feedback; behaviour signals never "
               "create labels; labels are derived (reliability in each reason), not human-annotated. "
               f"{audited} rejected job(s) were shown through the rejection audit sample (stratified, "
               "bounded, NOT a uniform sample: stratum per job in the private sidecar)."),
    )
    (out / EXPORT_META).write_text(json.dumps(
        {"WARNING": "PRIVATE: maps anonymised ids back to real jobs; never share or commit",
         "builder": BUILDER, "salt": salt, "anonymized": anonymize, "first_seen_at": first_seen,
         "id_map": id_map, "decisions": decision_of}, indent=1, sort_keys=True), encoding="utf-8")
    return {
        "status": status, "n_jobs": len(jobs), "labels": dict(counts), "skipped": dict(skipped),
        "thresholds": {"min_labelled": MIN_LABELLED, "min_per_class": MIN_PER_CLASS},
        "manifest": manifest.model_dump(),
    }


def labelling_queue(db_path: str | Path, limit: int = 50) -> list[dict[str, Any]]:
    """Active labelling queue: each job's latest decision flagged ``review`` by the disagreement
    engine (or with outcome ``review``, or picked by the rejection audit sample) that has no explicit
    feedback yet, least confident first."""
    conn = open_readonly(db_path)
    try:
        rows = conn.execute(
            """WITH latest AS (
                 SELECT *, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY decided_at DESC, rowid DESC) AS rn
                 FROM job_decisions)
               SELECT d.job_id, d.decision_id, d.title, d.company, d.outcome, d.decision_confidence,
                      json_extract(d.signals_json, '$.assessment.reasons') AS reasons
               FROM latest d
               WHERE d.rn = 1
                 AND (d.outcome = 'review' OR json_extract(d.signals_json, '$.assessment.state') = 'review'
                      OR json_extract(d.signals_json, '$.audit.sampled') = 1)
                 AND NOT EXISTS (SELECT 1 FROM feedback_events f
                                 WHERE f.job_id = d.job_id AND f.signal_class = 'explicit')
               ORDER BY COALESCE(d.decision_confidence, 0), d.job_id LIMIT ?""", (limit,)).fetchall()
    except sqlite3.OperationalError as exc:
        raise ExportError(f"feedback/decision journal tables missing in {db_path}: {exc}") from exc
    finally:
        conn.close()
    return [{**dict(r), "reasons": json.loads(r["reasons"]) if r["reasons"] else []} for r in rows]
