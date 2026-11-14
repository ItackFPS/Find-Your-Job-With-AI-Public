"""Per-offer learning signal from the feedback journal (ADR 0018 "Signals", v5 AD-3.1).

``offer_signals`` reads ``feedback_events`` and returns, for every offer the user acted on, one
:class:`OfferSignal`: the aggregated value ``y``, the features it is routed to, its effective time and
its weight (recency × profile-change discount). Deterministic: the same journal, reset marker, profile
change time and ``now`` give the same result.

Rules (initial weights in :mod:`src.learning.config`):

* only non-superseded events at or after the ``learning_reset_at`` marker count (ADR 0015);
* the current state of an offer in each family (save, hide, stance, status) is its latest event in
  insert order (ADR 0016);
* rendered and visible are not evidence and are not read here; *ignored* is inert at launch;
* opened (``viewed``) is a weak positive, once per offer, routed to the embedding and occupation;
* saved (current state), apply link, interested, ``to_apply`` / applied / interview / offer /
  accepted are positives routed to every feature; the strongest one is the offer's positive;
* an employer rejection is not the user's preference: an earlier outcome positive is kept;
  ``withdrawn`` cancels the outcome positive;
* not interested is a negative routed to the embedding and occupation, or, **with a reason, only to
  that reason's feature**; hidden without a reason is 0 (ADR 0017); hidden with a reason is a negative
  routed to that reason's feature; unsure clears the stance;
* a negative and a positive on the same offer: the negative holds unless a positive of at least +1.0
  happened after it (opening an offer does not undo "not interested").

Until occupation groups exist (stage L6), the job embedding stands in for the occupation feature.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.learning.config import PersonalModelConfig

EMBEDDING = "embedding"
OCCUPATION = "occupation"
TERMS = "terms"       # the words of an offer's title and skills: what the user is drawn to, by name
# The source is not a preference: where offers come from only reflects where the user looks.
STRUCTURED = ("distance", "remote", "contract", "seniority", "salary")
ALL_FEATURES: frozenset[str] = frozenset((EMBEDDING, OCCUPATION, TERMS, *STRUCTURED))
CONTENT: frozenset[str] = frozenset((EMBEDDING, OCCUPATION, TERMS))

# Reason → the one feature it moves. Reasons with no feature yet are recorded and move nothing.
REASON_FEATURE: dict[str, str | None] = {
    "too_far": "distance",
    "too_senior": "seniority",
    "salary": "salary",
    "contract_type": "contract",
    "off_domain": OCCUPATION,
    "insufficient_skills": None,      # no skills-gap feature yet
    "mis_ranked": None,
    "other": None,
}

_OUTCOME_KEYS = ("to_apply", "applied", "interview", "offer", "accepted_offer")


@dataclass(frozen=True)
class OfferSignal:
    job_id: str
    y: float
    features: frozenset[str]
    effective_at: str
    weight: float
    basis: str
    reasons: tuple[str, ...] = ()

    @property
    def is_positive(self) -> bool:
        return self.y >= 1.0


@dataclass
class _Offer:
    opened_at: str | None = None
    apply_at: str | None = None
    saved_at: str | None = None                       # set while the current Save state is saved
    outcome: tuple[float, str, str] | None = None     # (value, at, status)
    stance: tuple[str, str, tuple[str, ...]] | None = None   # (type, at, reasons)
    hidden: tuple[str, tuple[str, ...]] | None = None         # (at, reasons) while hidden
    positives: list[tuple[float, str, str]] = field(default_factory=list)


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _effective(row: sqlite3.Row, cols: set[str]) -> str:
    if "client_created_at" in cols and row["client_created_at"] and row["client_time_trusted"]:
        return str(row["client_created_at"])
    return str(row["occurred_at"])


def _route(reasons: tuple[str, ...]) -> frozenset[str]:
    routed = frozenset(f for r in reasons if (f := REASON_FEATURE.get(r)) is not None)
    # Until occupation groups exist, the occupation is read through the offer's content.
    return routed | CONTENT if OCCUPATION in routed else routed


def default_canonical(metadata: dict[str, Any]) -> str | None:
    """Canonical application status of an outcome event: its stored snapshot."""
    value = metadata.get("canonical_to")
    return value if isinstance(value, str) else None


def _fold(rows: list[sqlite3.Row], cols: set[str], cfg: PersonalModelConfig,
          canonical: Callable[[dict[str, Any]], str | None]) -> dict[str, _Offer]:
    w = cfg.weights
    offers: dict[str, _Offer] = {}
    for r in rows:
        o = offers.setdefault(r["job_id"], _Offer())
        at, typ, cls = _effective(r, cols), r["type"], r["signal_class"]
        reasons = tuple(json.loads(r["reasons_json"] or "[]"))
        if cls == "behavior" and typ == "viewed":
            o.opened_at = o.opened_at or at
        elif cls == "behavior" and typ == "apply_link_opened":
            o.apply_at = o.apply_at or at
        elif cls == "list" and typ in ("saved", "unsaved"):
            o.saved_at = at if typ == "saved" else None
        elif cls == "list" and typ in ("hidden", "unhidden"):
            o.hidden = (at, reasons) if typ == "hidden" else None
        elif cls == "explicit" and typ in ("interested", "not_interested"):
            o.stance = (typ, at, reasons)
        elif cls == "explicit" and typ == "unsure":
            o.stance = None
        elif cls == "explicit" and typ == "ranking_feedback" and "interesting_despite_low_score" in reasons:
            o.stance = ("interested", at, reasons)
        elif cls == "outcome":
            status = canonical(json.loads(r["metadata_json"] or "{}"))
            if status == "withdrawn":
                o.outcome = None
            elif status in _OUTCOME_KEYS:
                value = w.get(status, 0.0)
                if o.outcome is None or value > o.outcome[0]:
                    o.outcome = (value, at, status)
            # employer_rejected and unmapped statuses: the earlier outcome positive is kept
    for o in offers.values():
        if o.opened_at:
            o.positives.append((w["opened"], o.opened_at, "opened"))
        if o.apply_at:
            o.positives.append((w["apply_link_opened"], o.apply_at, "apply_link_opened"))
        if o.saved_at:
            o.positives.append((w["saved"], o.saved_at, "saved"))
        if o.outcome:
            o.positives.append((o.outcome[0], o.outcome[1], f"status:{o.outcome[2]}"))
        if o.stance and o.stance[0] == "interested":
            o.positives.append((w["interested"], o.stance[1], "interested"))
    return offers


def _resolve(job_id: str, o: _Offer, cfg: PersonalModelConfig) -> tuple[float, frozenset[str], str, str,
                                                                         tuple[str, ...]] | None:
    negatives: list[tuple[float, str, str, frozenset[str], tuple[str, ...]]] = []
    if o.stance and o.stance[0] == "not_interested":
        reasons = o.stance[2]
        negatives.append((cfg.weights["not_interested"], o.stance[1], "not_interested",
                          _route(reasons) if reasons else CONTENT, reasons))
    if o.hidden and o.hidden[1]:
        negatives.append((cfg.weights["hidden_with_reason"], o.hidden[0], "hidden", _route(o.hidden[1]),
                          o.hidden[1]))
    best = max(o.positives, key=lambda p: (p[0], [-ord(c) for c in p[1]])) if o.positives else None
    if negatives:
        neg = max(negatives, key=lambda n: _parse(n[1]))
        later_strong = [p for p in o.positives if p[0] >= 1.0 and _parse(p[1]) > _parse(neg[1])]
        if not later_strong:
            return neg[0], neg[3], neg[1], neg[2], neg[4]
        best = max(later_strong, key=lambda p: p[0])
    if best is None or best[0] == 0:
        return None
    features = CONTENT if best[2] == "opened" else ALL_FEATURES
    return best[0], features, best[1], best[2], ()


def offer_signals(conn: sqlite3.Connection, cfg: PersonalModelConfig, *, reset_at: str | None,
                  now: datetime, profile_changed_at: str | None = None, before: str | None = None,
                  canonical: Callable[[dict[str, Any]], str | None] = default_canonical) -> list[OfferSignal]:
    """Aggregated signal per offer, sorted by job id. ``before`` (ISO time) keeps only events received
    strictly earlier (used by the shadow path and the offline learning curve: no leakage)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback_events)")}
    except sqlite3.Error:
        return []
    if not cols:
        return []
    where = ["1 = 1"]
    params: list[Any] = []
    if "superseded" in cols:
        where.append("superseded = 0")
    conn.row_factory = sqlite3.Row
    rows = [r for r in conn.execute(
        f"SELECT * FROM feedback_events WHERE {' AND '.join(where)} ORDER BY rowid", params)
        if (reset_at is None or _parse(r["occurred_at"]) >= _parse(reset_at))
        and (before is None or _parse(r["occurred_at"]) < _parse(before))]
    out: list[OfferSignal] = []
    changed = _parse(profile_changed_at) if profile_changed_at else None
    for job_id, o in sorted(_fold(rows, cols, cfg, canonical).items()):
        resolved = _resolve(job_id, o, cfg)
        if resolved is None:
            continue
        y, features, at, basis, reasons = resolved
        if not features:
            continue                     # a reason with no feature yet: recorded, moves nothing
        y = max(-cfg.clamp, min(cfg.clamp, y))
        age_days = max(0.0, (now - _parse(at)).total_seconds() / 86400)
        weight = max(cfg.min_recency, 0.5 ** (age_days / cfg.half_life_days))
        if changed is not None and _parse(at) < changed:
            weight *= cfg.profile_change_discount
        out.append(OfferSignal(job_id, y, features, at, weight, basis, reasons))
    return out
