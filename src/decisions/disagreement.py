"""Disagreement / confidence / abstention engine (P2).

Deterministic, explainable and uncalibrated. It looks at HETEROGENEOUS signals about a job — the
deterministic fit, the semantic similarity (on the same fixed-anchor 0-1 scale the ranking uses),
the Phase 2 profile-match, the LLM eligibility verdict (as a categorical signal only), the density
of UNKNOWN evidence, evidence completeness and contradictions — and decides ``accept | review |
reject`` plus a ``decision_confidence``.

Hard rules:
  * uncertainty is NEVER turned into a rejection: an accepted job becomes ``review``, never
    ``reject``; a rejected job becomes ``review``, never ``accept``;
  * the confidence an LLM states about itself is never used as the decision confidence (it is kept
    as a signal elsewhere); ``decision_confidence`` is agreement-based and ``calibrated = False``
    until it has been calibrated against labelled data;
  * ``review`` never removes a job from ranking or notifications — it is a flag for a human. In
    ``active`` mode only an ACCEPTED job's journaled outcome becomes ``review`` (it stays ranked and
    exposed); a rejected job keeps outcome ``rejected`` and is surfaced through
    ``assessment.state == "review"`` (the review queue);
  * a signal that is not a finite number in [0, 1] is ignored AND reported (broken input is
    uncertainty, never silently trusted);
  * the user's own feedback is a signal too: a rejection of a job the user said they want (explicit
    ``interested``, or an application status showing they pursued it) is ``review``, never a silent
    ``reject``; an accepted job the user marked ``not_interested`` is a recorded conflict (``review``).
    Weak behavioural signals (views, clicks) are never used;
  * the default mode is ``shadow``: the assessment is journaled next to the production decision
    without changing it.

Thresholds/mode live in ``config/disagreement.yaml``; each assessment records the hash of the config
it was made under (the file is deliberately NOT part of the strategy version: it never changes what
the pipeline does).
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

ENGINE_VERSION = "disagreement.v2"   # v2: user feedback stance
State = Literal["accept", "review", "reject"]
# Evidence sources a hard rejection can rest on without an LLM (text/metadata/rule backed).
SOLID_SOURCES = frozenset({"heuristic", "llm_corroborated", "metadata", "rule"})


class DisagreementConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "shadow", "active"] = "shadow"

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_off(cls, v: Any) -> Any:
        # YAML 1.1 reads an unquoted `off` as the boolean False; accept it as "off".
        return "off" if v is False else v

    # max-min over the numeric fit signals (rules / semantic / phase2, all on 0-1) above which the
    # signals are considered to disagree.
    spread_review: float = Field(default=0.30, ge=0.0, le=1.0)
    unknown_density_review: float = Field(default=0.50, ge=0.0, le=1.0)
    completeness_min: float = Field(default=0.40, ge=0.0, le=1.0)
    review_on_conflict: bool = True
    # An LLM REJECT that did not remove the job (low confidence / unsupported) vs a deterministic accept.
    review_on_llm_reject_override: bool = True
    # A rejection resting ONLY on LLM evidence (or on no recorded evidence) is flagged for review.
    review_llm_only_rejection: bool = True
    # The user's own explicit feedback / application outcome contradicts the pipeline's decision.
    review_on_user_feedback: bool = True

    def config_hash(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:12]


def load_disagreement_config(config_dir: str | Path) -> DisagreementConfig:
    path = Path(config_dir) / "disagreement.yaml"
    if not path.is_file():
        return DisagreementConfig()
    try:
        return DisagreementConfig(**(yaml.safe_load(path.read_text(encoding="utf-8")) or {}))
    except (OSError, yaml.YAMLError, TypeError, ValidationError) as exc:
        logger.warning("Invalid %s (%s); using defaults", path, exc)
        return DisagreementConfig()


@dataclass
class Signals:
    outcome: Literal["accepted", "rejected"]
    rules_score: float | None = None          # deterministic fit, 0-1 (accepted jobs)
    semantic: float | None = None             # semantic similarity on the ranking's fixed 0-1 scale
    phase2: float | None = None               # Phase 2 profile_match_score, 0-1
    eligibility: str | None = None            # "PASS" | "UNCERTAIN" | "REJECT" (categorical only)
    eligibility_reject_overridden: bool = False   # LLM said REJECT but the job survived
    unknown_density: float = 0.0
    completeness: float | None = None         # None = no evidence recorded (nothing to judge)
    conflicts: list[str] = field(default_factory=list)   # contradictions IN THE EVIDENCE (not journal artefacts)
    reject_sources: tuple[str, ...] = ()      # evidence_source of the HARD violated items of a rejection
    unknown_dimensions: tuple[str, ...] = ()
    user_stance: Literal["wants", "declines"] | None = None   # from the feedback journal (explicit/outcome only)
    user_stance_basis: str | None = None                       # e.g. "explicit:interested", "status:applied"


@dataclass
class Assessment:
    state: State
    confidence: float
    spread: float | None
    reasons: list[str]
    unknowns: list[str]
    conflicts: list[str]
    invalid_signals: list[str] = field(default_factory=list)
    calibrated: bool = False
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state, "confidence": round(self.confidence, 4),
            "spread": None if self.spread is None else round(self.spread, 4),
            "reasons": self.reasons, "unknowns": self.unknowns, "conflicts": self.conflicts,
            "invalid_signals": self.invalid_signals, "calibrated": self.calibrated,
            "engine_version": self.engine_version,
        }


def evidence_stats(evidence: list[Any]) -> tuple[float, float | None, list[str]]:
    """``(unknown_density, completeness, unknown_dimensions)`` over a list of ``EvidenceItem``.

    completeness = share of the dimensions that appear in the evidence for which at least one item
    is NOT unknown (a dimension we could actually establish); ``None`` when there is no evidence at
    all (nothing recorded is not the same as nothing established)."""
    if not evidence:
        return 0.0, None, []
    unknown = [e for e in evidence if e.state == "unknown"]
    dims = {e.dimension for e in evidence}
    known_dims = {e.dimension for e in evidence if e.state != "unknown"}
    unknown_dims = sorted({e.dimension for e in unknown} - known_dims)
    return len(unknown) / len(evidence), len(known_dims) / len(dims), unknown_dims


def _valid(x: float | None) -> bool:
    return x is not None and math.isfinite(x) and 0.0 <= x <= 1.0


def assess(signals: Signals, cfg: DisagreementConfig) -> Assessment:
    reasons: list[str] = []
    conflicts = list(signals.conflicts)
    unknowns = list(signals.unknown_dimensions)
    density = signals.unknown_density if math.isfinite(signals.unknown_density) else 1.0
    penalty_conflicts = len(signals.conflicts)          # contradictions in the evidence

    if signals.outcome == "rejected":
        solid = any(s in SOLID_SOURCES for s in signals.reject_sources)
        penalty = min(0.5, 0.25 * penalty_conflicts)
        if cfg.review_on_user_feedback and signals.user_stance == "wants":
            basis = signals.user_stance_basis or "user feedback"
            reasons.append(f"the user's own feedback ({basis}) says they want this job; a rejection that "
                           "contradicts it is never final")
            conflicts.append(f"user feedback ({basis}) vs pipeline rejection")
            return Assessment("review", 0.5 * (1 - penalty), None, reasons, unknowns, conflicts)
        if not solid and cfg.review_llm_only_rejection:
            reasons.append("rejection rests only on LLM evidence, with no text/rule-backed violation"
                           if signals.reject_sources else "rejection has no recorded evidence")
            return Assessment("review", 0.6 * (1 - penalty), None, reasons, unknowns, conflicts)
        if conflicts:
            reasons.append("evidence contradicts itself")
        base = 0.95 if solid else 0.6
        return Assessment("reject", base * (1 - 0.25 * density) * (1 - penalty), None, reasons, unknowns, conflicts)

    raw = {"rules": signals.rules_score, "semantic": signals.semantic, "phase2": signals.phase2}
    invalid = [k for k, v in raw.items() if v is not None and not _valid(v)]
    numeric = {k: v for k, v in raw.items() if v is not None and _valid(v)}
    if invalid:
        reasons.append(f"unusable signal(s) ignored: {', '.join(invalid)} (not a number in [0, 1])")
    spread = (max(numeric.values()) - min(numeric.values())) if len(numeric) >= 2 else None
    if spread is not None and spread > cfg.spread_review:
        hi, lo = max(numeric, key=lambda k: numeric[k]), min(numeric, key=lambda k: numeric[k])
        reasons.append(f"signals disagree: {hi} {numeric[hi]:.2f} vs {lo} {numeric[lo]:.2f} (spread {spread:.2f})")
        conflicts.append(f"{hi} and {lo} fit signals differ by {spread:.2f}")   # reported; already priced via `agree`
    if density > cfg.unknown_density_review:
        reasons.append(f"{density:.0%} of the evidence is UNKNOWN")
    completeness = signals.completeness
    if completeness is not None and completeness < cfg.completeness_min:
        reasons.append(f"evidence covers only {completeness:.0%} of the relevant dimensions")
    if cfg.review_on_conflict and signals.conflicts:
        reasons.append(f"{len(signals.conflicts)} contradiction(s) in the evidence")
    if cfg.review_on_user_feedback and signals.user_stance == "declines":
        basis = signals.user_stance_basis or "user feedback"
        reasons.append(f"the user marked this job not interesting ({basis}) but the pipeline accepted it")
        conflicts.append(f"user feedback ({basis}) vs pipeline accept")
        penalty_conflicts += 1
    if cfg.review_on_llm_reject_override and signals.eligibility_reject_overridden:
        reasons.append("the LLM eligibility check said REJECT but the deterministic pipeline kept the job")
        conflicts.append("LLM eligibility REJECT vs deterministic accept")
        penalty_conflicts += 1

    agree = 1.0 - spread if spread is not None else 0.7        # a single signal cannot show agreement
    comp = 0.5 if completeness is None else completeness       # nothing recorded: neutral, never "incomplete"
    confidence = (agree * (0.5 + 0.5 * comp) * (1 - 0.5 * min(1.0, density))
                  * (1 - min(0.5, 0.25 * penalty_conflicts)))
    return Assessment("review" if reasons else "accept", max(0.0, min(1.0, confidence)), spread,
                      reasons, unknowns, conflicts, invalid_signals=invalid)
