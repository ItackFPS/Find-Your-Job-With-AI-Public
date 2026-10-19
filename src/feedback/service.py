"""Recording feedback: validation, decision/strategy linkage, legacy-status mapping.

No weight or score is computed here — events are stored as facts with provenance.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from src.decisions.storage import DecisionStorage
from src.feedback.config import FeedbackConfig, load_feedback_config
from src.feedback.models import (
    ALLOWED_REASONS, BEHAVIOR_TYPES, DEFAULT_LEGACY_MAPPING, EXPLICIT_TYPES, FeedbackEvent, Source,
)
from src.feedback.storage import FeedbackStorage

logger = logging.getLogger(__name__)
MAX_NOTE = 500
# Rows the bridge writes into the legacy ``job_feedback`` table: ``bridge:<feedback event id>``.
BRIDGE_PREFIX = "bridge:"
# Explicit answers that set (or, for ``unsure``, clear) the mirrored preference of a job.
_BRIDGED_TYPES = ("interested", "not_interested", "unsure")


class FeedbackError(ValueError):
    """Invalid feedback input (maps to HTTP 422)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def mapping_hash(config: FeedbackConfig) -> str:
    """Short hash of the effective legacy→canonical mapping (stored on every outcome event)."""
    mapping = {**DEFAULT_LEGACY_MAPPING, **config.legacy_status_mapping}
    return hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:12]


def canonical_status(legacy: str | None, config: FeedbackConfig) -> tuple[str | None, dict[str, Any]]:
    """Map a legacy ``application_status`` to ``(canonical, extra_metadata)``; ``None`` = unmapped.

    The legacy value is never altered. ``accepted``/``rejected``/``denied`` stay unmapped until the
    owner resolves them in ``config/feedback.yaml``. ``metadata.canonical_*`` stored on an event is a
    snapshot made under ``metadata.mapping_hash``; analysis must derive the canonical value from
    ``metadata.legacy_to`` with the CURRENT mapping (see ``FeedbackService.canonical_for``), because
    events are immutable and the owner may resolve ambiguous statuses later.
    """
    if legacy is None:
        return None, {}
    mapping = {**DEFAULT_LEGACY_MAPPING, **config.legacy_status_mapping}
    extra: dict[str, Any] = {"awaiting_response": True} if legacy == "waiting" else {}
    return mapping.get(legacy), extra


class FeedbackService:
    def __init__(self, db_path: str, config_dir: str = "config") -> None:
        self._db_path = db_path
        self.config = load_feedback_config(config_dir)

    # -- linkage ----------------------------------------------------------------------------------

    def canonical_for(self, event: FeedbackEvent) -> str | None:
        """Canonical application status of an outcome event under the CURRENT mapping."""
        return canonical_status(event.metadata.get("legacy_to"), self.config)[0]

    def _link(
        self, job_id: str, at: str | None = None, decision_id: str | None = None,
        store: DecisionStorage | None = None,
    ) -> tuple[str | None, str | None, dict[str, Any]]:
        """``(decision_id, strategy_version, metadata)`` of the decision the user reacted to.

        An explicit ``decision_id`` (the one the client displayed) wins and is validated; otherwise
        the latest decision (or the one in force at ``at``) is used. A journal problem never loses
        the feedback itself: it is recorded unlinked with ``link_error``."""
        own = store is None
        store = store or DecisionStorage(self._db_path)
        try:
            store.init_db()
            if decision_id is not None:
                rec = store.get_decision(decision_id)
                if rec is None or rec.job_id != job_id:
                    raise FeedbackError(f"decision {decision_id!r} does not exist for job {job_id!r}")
            else:
                rec = store.get_decision_at(job_id, at) if at else store.get_latest_decision(job_id)
        except FeedbackError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Decision linkage failed for job %s (feedback kept unlinked): %s", job_id, exc)
            return None, None, {"no_decision": True, "link_error": str(exc)[:200]}
        finally:
            if own:
                store.close()
        if rec is None:
            return None, None, {"no_decision": True}
        return rec.decision_id, rec.strategy_version, {"decision_run_id": rec.run_id, "decision_outcome": rec.outcome}

    def _append(self, **fields: Any) -> FeedbackEvent:
        event = FeedbackEvent(event_id=str(uuid.uuid4()), **fields)
        store = FeedbackStorage(self._db_path)
        try:
            store.init_db()
            store.append(event)
        finally:
            store.close()
        return event

    # -- recording --------------------------------------------------------------------------------

    def record_explicit(
        self, job_id: str, type_: str, reasons: list[str] | None = None, note: str | None = None,
        source: Source = "ui", decision_id: str | None = None,
    ) -> FeedbackEvent:
        if type_ not in EXPLICIT_TYPES:
            raise FeedbackError(f"unknown explicit feedback type {type_!r}")
        reasons = list(dict.fromkeys(reasons or []))
        bad = [r for r in reasons if r not in ALLOWED_REASONS[type_]]
        if bad:
            raise FeedbackError(f"reason(s) {bad} not allowed for {type_!r}; allowed: {sorted(ALLOWED_REASONS[type_])}")
        note = (note or "").strip() or None
        if note and len(note) > MAX_NOTE:
            raise FeedbackError(f"note longer than {MAX_NOTE} characters")
        decision_id, strategy, meta = self._link(job_id, decision_id=decision_id)
        event = self._append(
            job_id=job_id, decision_id=decision_id, strategy_version=strategy, signal_class="explicit",
            type=type_, reasons=reasons, note=note, source=source, occurred_at=_now(), metadata=meta)
        self._bridge(event)
        return event

    def record_behavior(
        self, job_id: str, type_: str, source: Source = "ui", metadata: dict[str, Any] | None = None
    ) -> FeedbackEvent:
        if type_ not in BEHAVIOR_TYPES:
            raise FeedbackError(f"unknown behavior type {type_!r}")
        decision_id, strategy, meta = self._link(job_id)
        return self._append(
            job_id=job_id, decision_id=decision_id, strategy_version=strategy, signal_class="behavior",
            type=type_, source=source, occurred_at=_now(), metadata={**meta, **(metadata or {})})

    def record_status_change(
        self, job_id: str, from_status: str | None, to_status: str | None, source: Source = "ui",
        occurred_at: str | None = None, extra: dict[str, Any] | None = None,
        outcome_event_id: str | None = None, store: DecisionStorage | None = None,
    ) -> FeedbackEvent:
        """``outcome_event_id`` ties the event to its ``outcome_events`` row (unique-indexed), so the
        live mirror and the backfill can never both journal the same transition."""
        canon_to, canon_extra = canonical_status(to_status, self.config)
        canon_from, _ = canonical_status(from_status, self.config)
        at = occurred_at or _now()
        decision_id, strategy, meta = self._link(job_id, at if occurred_at else None, store=store)
        ref = {"outcome_event_id": outcome_event_id} if outcome_event_id else {}
        return self._append(
            job_id=job_id, decision_id=decision_id, strategy_version=strategy, signal_class="outcome",
            type="application_status_changed", source=source, occurred_at=at,
            metadata={**meta, "legacy_from": from_status, "legacy_to": to_status,
                      "canonical_from": canon_from, "canonical_to": canon_to,
                      "mapping_hash": mapping_hash(self.config), **canon_extra, **ref, **(extra or {})})

    # -- optional bridge to the legacy personalization table -----------------------------------------
    #
    # The mirrored rows are a derived view of the journal, never a second source of truth: per job, at
    # most one row (``bridge:<event id>``) carrying the LATEST explicit interested / not_interested
    # answer with its original timestamp. Repeated clicks therefore count once, a changed mind replaces
    # the earlier answer instead of cancelling it, and the whole effect can be removed
    # (``clear_bridge``) or rebuilt from the journal (``sync_bridge``) at any time.

    def _bridge(self, event: FeedbackEvent) -> None:
        if not self.config.personalization_bridge or event.type not in _BRIDGED_TYPES:
            return
        try:
            self.sync_bridge([event.job_id])
        except Exception as exc:  # noqa: BLE001 — the bridge never blocks recording feedback
            logger.warning("Personalization bridge failed (non-fatal): %s", exc)

    def sync_bridge(self, job_ids: list[str] | None = None) -> tuple[int, int]:
        """Rebuild the mirrored rows of ``job_ids`` (all journaled jobs when ``None``) from the
        journal → ``(written, removed)``. Rows written by the personalization CLI are never touched;
        a job no longer in ``jobs`` keeps no mirrored row (its attributes cannot be snapshotted)."""
        from src.personalization.models import FeedbackType
        from src.personalization.storage import PersonalizationStorage
        from src.storage.sqlite import Storage

        fs = FeedbackStorage(self._db_path)
        fs.init_db()
        try:
            latest = fs.latest_explicit(_BRIDGED_TYPES, job_ids)
        finally:
            fs.close()
        st = Storage(db_path=self._db_path)
        ps = PersonalizationStorage(db_path=self._db_path)
        st.init_db()
        ps.init_db()
        written = removed = 0
        try:
            for job_id in (job_ids if job_ids is not None else sorted(latest)):
                removed += ps.delete_feedback_with_id_prefix(BRIDGE_PREFIX, job_id)
                event = latest.get(job_id)
                if event is None or event.type == "unsure":
                    continue
                job = st.get_job_by_id(job_id)
                if job is None:
                    continue
                ps.add_feedback(job, FeedbackType(event.type), note=event.note,
                                feedback_id=f"{BRIDGE_PREFIX}{event.event_id}",
                                created_at=datetime.fromisoformat(event.occurred_at))
                written += 1
        finally:
            st.close()
            ps.close()
        return written, removed

    def clear_bridge(self) -> int:
        """Remove every mirrored row (the journal keeps every event): undoes the bridge's effect."""
        from src.personalization.storage import PersonalizationStorage

        ps = PersonalizationStorage(db_path=self._db_path)
        ps.init_db()
        try:
            return ps.delete_feedback_with_id_prefix(BRIDGE_PREFIX)
        finally:
            ps.close()

    # -- one-time import of historical status transitions ----------------------------------------------

    def backfill_from_outcome_events(self) -> tuple[int, int]:
        """Import historical ``application_status_changed`` outcome events → ``(imported, skipped)``.

        Idempotent and safe next to the live mirror: a transition already journaled (by either path)
        is detected through the unique ``outcome_event_id`` reference. Malformed rows are skipped and
        counted, never aborting the import. A missing ``outcome_events`` table imports nothing;
        any other database error propagates."""
        conn = sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, job_id, occurred_at, metadata FROM outcome_events "
                "WHERE event_type = 'application_status_changed' ORDER BY occurred_at, rowid"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return 0, 0
            raise
        finally:
            conn.close()
        store = FeedbackStorage(self._db_path)
        decisions = DecisionStorage(self._db_path)
        store.init_db()
        decisions.init_db()
        imported = skipped = 0
        try:
            for r in rows:
                if store.has_outcome_ref(r["id"]):
                    continue
                try:
                    meta = json.loads(r["metadata"] or "{}")
                    at = datetime.fromisoformat(r["occurred_at"])
                    at_iso = (at if at.tzinfo else at.replace(tzinfo=UTC)).astimezone(UTC).isoformat(timespec="seconds")
                    self.record_status_change(
                        r["job_id"], meta.get("from"), meta.get("to"), source="import", occurred_at=at_iso,
                        outcome_event_id=r["id"], extra={"backfilled": True}, store=decisions)
                    imported += 1
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    skipped += 1
                    logger.warning("Backfill skipped outcome event %s: %s", r["id"], exc)
        finally:
            store.close()
            decisions.close()
        return imported, skipped
