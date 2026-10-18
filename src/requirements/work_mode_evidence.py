"""Work mode evidence extraction from job description text.

Extracts work mode signals (full remote, hybrid, on-site) from free-form
job description text, and detects contradictions between structured metadata
and text-derived evidence.

Design principles
-----------------
- Absence of a work mode mention → UNKNOWN (never a rejection reason alone).
- A generic "remote" mention without quantification → UNKNOWN granularity,
  not proof of full remote.
- "100% télétravail" / "fully remote" → strong FULL_REMOTE signal.
- "N days on site" / "N jours sur site" → HYBRID or ON_SITE signal.
- "remote-friendly" / "télétravail possible" → UNKNOWN (available but not
  guaranteed as exclusive).
- Explicit contradictions (e.g. metadata remote=True but "4 days on site"
  in description) are tracked but never cause standalone rejection.

The caller uses returned evidence to:
  - Detect contradictions for the audit trail.
  - Populate ``GateResult.contradictions`` and ``uncertainty=True``.
  - NOT directly reject jobs (work mode constraint is evaluated by filter.py
    using structured metadata; text evidence supplements for audit only).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from src.requirements.models import EvidenceSpan


# ---------------------------------------------------------------------------
# Work mode vocabulary
# ---------------------------------------------------------------------------

class WorkModeSignal(str):
    """String constants for work mode signals."""
    FULL_REMOTE = "full_remote"
    HYBRID = "hybrid"
    ON_SITE = "on_site"
    UNKNOWN = "unknown"


@dataclass
class WorkModeEvidence:
    """One extracted work mode claim from job text.

    Attributes:
        signal:     The detected work mode (FULL_REMOTE/HYBRID/ON_SITE/UNKNOWN).
        confidence: HIGH — unambiguous explicit claim; MEDIUM — probable;
                    LOW — indirect mention.
        mandatory_on_site_days: Extracted on-site day count (1–7) when present.
        span:       Source span for the matching text.
    """
    signal: str
    confidence: str  # "HIGH" | "MEDIUM" | "LOW"
    mandatory_on_site_days: int | None = None
    span: EvidenceSpan | None = None


@dataclass
class WorkModeExtractionResult:
    """All work mode evidence extracted from a single job's text."""
    evidence: list[WorkModeEvidence] = field(default_factory=list)
    # Resolved signal after reconciling all evidence.
    resolved_signal: str = WorkModeSignal.UNKNOWN
    # True when evidence items conflict (e.g. full_remote AND on_site signals)
    has_contradiction: bool = False
    # Human-readable contradiction description for audit trail.
    contradiction_description: str = ""


# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


# Strong full-remote signals (HIGH confidence).
_FULL_REMOTE_PATTERNS: list[re.Pattern] = [
    re.compile(r"100\s*%\s*t[eé]l[eé]travail", re.I),
    re.compile(r"t[eé]l[eé]travail\s+100\s*%", re.I),
    re.compile(r"100\s*%\s*remote", re.I),
    re.compile(r"remote\s+100\s*%", re.I),
    re.compile(r"fully\s+remote", re.I),
    re.compile(r"full(?:y|-)\s*remote", re.I),
    re.compile(r"full\s+t[eé]l[eé]travail", re.I),
    re.compile(r"t[eé]l[eé]travail\s+complet", re.I),
    re.compile(r"enti[eè]rement\s+(?:en\s+)?t[eé]l[eé]travail", re.I),
    re.compile(r"work(?:ing)?\s+from\s+anywhere", re.I),
    re.compile(r"poste\s+(?:enti[eè]rement\s+)?(?:en\s+)?t[eé]l[eé]travail", re.I),
]

# On-site day quantification patterns (HIGH confidence → HYBRID/ON_SITE).
# Group 1 captures the day count.
_ON_SITE_DAYS_PATTERNS: list[re.Pattern] = [
    # "3 days on site" / "3 jours sur site"
    re.compile(r"([1-7])\s*(?:days?|jours?)\s+(?:per\s+week\s+)?(?:on[- ]?site|au\s+bureau|en\s+pr[eé]sentiel)", re.I),
    # "on-site 3 days"
    re.compile(r"on[- ]?site\s+([1-7])\s*(?:days?|jours?)", re.I),
    # "présence N jours"
    re.compile(r"pr[eé]sence\s+(?:de\s+)?([1-7])\s*(?:jours?|days?)", re.I),
    # "N jours/semaine en présentiel"
    re.compile(r"([1-7])\s*(?:jours?|days?)\s*(?:/|par)\s*semaine\s+(?:en\s+)?(?:pr[eé]sentiel|bureau)", re.I),
    # "présence obligatoire N jours"
    re.compile(r"pr[eé]sence\s+(?:obligatoire|requise|exig[eé]e)\s+(?:de\s+)?([1-7])\s*(?:jours?|days?)", re.I),
    # "minimum N jours par semaine au bureau"
    re.compile(r"(?:minimum|au\s+moins)\s+([1-7])\s*(?:jours?|days?)\s+(?:par\s+semaine\s+)?(?:au\s+bureau|on[- ]?site|en\s+pr[eé]sentiel)", re.I),
]

# Mandatory on-site (no day count) — HIGH confidence ON_SITE or HYBRID.
_MANDATORY_PRESENCE_PATTERNS: list[re.Pattern] = [
    re.compile(r"pr[eé]sence\s+obligatoire\b(?!\s+\d)", re.I),
    re.compile(r"poste\s+(?:en\s+)?pr[eé]sentiel", re.I),
    re.compile(r"poste\s+sur\s+site", re.I),
    re.compile(r"on[- ]?site\s+(?:position|role|work)", re.I),
    re.compile(r"no\s+remote\b", re.I),
    re.compile(r"pas\s+de\s+t[eé]l[eé]travail\b", re.I),
]

# Hybrid signals (MEDIUM confidence).
_HYBRID_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bhybrid(?:e)?\b", re.I),
    re.compile(r"\bt[eé]l[eé]travail\s+partiel\b", re.I),
    re.compile(r"\bpartial\s+remote\b", re.I),
    re.compile(r"\bmi[- ]t[eé]l[eé]travail\b", re.I),
    re.compile(r"\bflex(?:ible)?\s+(?:work(?:ing)?|t[eé]l[eé]travail)\b", re.I),
]

# Weak remote signals (LOW confidence — "remote available" not "exclusively remote").
_WEAK_REMOTE_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bremote[- ]friendly\b", re.I),
    re.compile(r"\bt[eé]l[eé]travail\s+possible\b", re.I),
    re.compile(r"\bt[eé]l[eé]travail\s+(?:autoris[eé]|accept[eé]|envisageable)\b", re.I),
    re.compile(r"\bwork\s+from\s+home\b", re.I),
    re.compile(r"\bWFH\b"),
]


def extract_work_mode_evidence(text: str, field: str = "description") -> WorkModeExtractionResult:
    """Extract work mode evidence from free-form job description text.

    Scans the text for work mode signals and returns a reconciled result.
    The caller should treat UNKNOWN as non-actionable for hard rejection.

    Args:
        text:  The job description or relevant text section.
        field: Source field label for provenance ("description", "title", etc.)

    Returns:
        WorkModeExtractionResult with all evidence and reconciled signal.
    """
    evidence: list[WorkModeEvidence] = []

    # ── Strong full-remote signals ────────────────────────────────────────
    for pattern in _FULL_REMOTE_PATTERNS:
        m = pattern.search(text)
        if m:
            span = EvidenceSpan(
                text=text[max(0, m.start() - 20): m.end() + 20].strip(),
                start=m.start(),
                end=m.end(),
                field=field,
                extractor_id="work_mode.full_remote.v1",
            )
            evidence.append(WorkModeEvidence(
                signal=WorkModeSignal.FULL_REMOTE,
                confidence="HIGH",
                span=span,
            ))

    # ── On-site day count (quantified hybrid/on-site) ─────────────────────
    on_site_days_found: list[int] = []
    for pattern in _ON_SITE_DAYS_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                days = int(m.group(1))
                on_site_days_found.append(days)
                span = EvidenceSpan(
                    text=text[max(0, m.start() - 20): m.end() + 20].strip(),
                    start=m.start(),
                    end=m.end(),
                    field=field,
                    extractor_id="work_mode.on_site_days.v1",
                )
                signal = WorkModeSignal.ON_SITE if days >= 5 else WorkModeSignal.HYBRID
                evidence.append(WorkModeEvidence(
                    signal=signal,
                    confidence="HIGH",
                    mandatory_on_site_days=days,
                    span=span,
                ))
            except (IndexError, ValueError):
                pass

    # ── Mandatory presence (no day count) ─────────────────────────────────
    for pattern in _MANDATORY_PRESENCE_PATTERNS:
        m = pattern.search(text)
        if m:
            span = EvidenceSpan(
                text=text[max(0, m.start() - 20): m.end() + 20].strip(),
                start=m.start(),
                end=m.end(),
                field=field,
                extractor_id="work_mode.mandatory_presence.v1",
            )
            evidence.append(WorkModeEvidence(
                signal=WorkModeSignal.ON_SITE,
                confidence="HIGH",
                span=span,
            ))

    # ── Hybrid signals ────────────────────────────────────────────────────
    for pattern in _HYBRID_PATTERNS:
        m = pattern.search(text)
        if m:
            span = EvidenceSpan(
                text=text[max(0, m.start() - 20): m.end() + 20].strip(),
                start=m.start(),
                end=m.end(),
                field=field,
                extractor_id="work_mode.hybrid.v1",
            )
            evidence.append(WorkModeEvidence(
                signal=WorkModeSignal.HYBRID,
                confidence="MEDIUM",
                span=span,
            ))

    # ── Weak remote signals ───────────────────────────────────────────────
    for pattern in _WEAK_REMOTE_PATTERNS:
        m = pattern.search(text)
        if m:
            span = EvidenceSpan(
                text=text[max(0, m.start() - 20): m.end() + 20].strip(),
                start=m.start(),
                end=m.end(),
                field=field,
                extractor_id="work_mode.remote_available.v1",
            )
            # Weak signals → UNKNOWN (available but not exclusively remote)
            evidence.append(WorkModeEvidence(
                signal=WorkModeSignal.UNKNOWN,
                confidence="LOW",
                span=span,
            ))

    if not evidence:
        return WorkModeExtractionResult(resolved_signal=WorkModeSignal.UNKNOWN)

    # ── Reconcile evidence ────────────────────────────────────────────────
    return _reconcile_evidence(evidence)


def _reconcile_evidence(evidence: list[WorkModeEvidence]) -> WorkModeExtractionResult:
    """Reconcile multiple evidence items into a single resolved signal.

    Resolution rules (in priority order):
    1. Quantified on-site days ≥ 1 + full_remote signal → CONTRADICTION
    2. HIGH confidence on-site/hybrid > HIGH confidence full_remote → HYBRID/ON_SITE
       (specific day counts dominate vague "remote" language)
    3. HIGH confidence full_remote (no contradicting on-site) → FULL_REMOTE
    4. MEDIUM hybrid signals → HYBRID
    5. LOW / UNKNOWN signals only → UNKNOWN
    """
    high_full_remote = [e for e in evidence if e.signal == WorkModeSignal.FULL_REMOTE and e.confidence == "HIGH"]
    high_on_site = [e for e in evidence if e.signal in (WorkModeSignal.ON_SITE, WorkModeSignal.HYBRID) and e.confidence == "HIGH"]
    medium_hybrid = [e for e in evidence if e.signal == WorkModeSignal.HYBRID and e.confidence == "MEDIUM"]

    # Contradiction: full_remote AND on-site both with HIGH confidence
    if high_full_remote and high_on_site:
        fr_span = high_full_remote[0].span
        os_span = high_on_site[0].span
        fr_text = fr_span.text if fr_span else "full remote claim"
        os_text = os_span.text if os_span else "on-site claim"
        return WorkModeExtractionResult(
            evidence=evidence,
            resolved_signal=WorkModeSignal.UNKNOWN,
            has_contradiction=True,
            contradiction_description=(
                f"Conflicting work mode evidence: full-remote claim ('{fr_text}') "
                f"vs on-site/hybrid claim ('{os_text}')"
            ),
        )

    if high_on_site:
        # Most specific: use the highest day-count evidence.
        max_days_ev = max(
            (e for e in high_on_site if e.mandatory_on_site_days is not None),
            key=lambda e: e.mandatory_on_site_days or 0,
            default=high_on_site[0],
        )
        days = max_days_ev.mandatory_on_site_days
        signal = WorkModeSignal.ON_SITE if (days is not None and days >= 5) else WorkModeSignal.HYBRID
        return WorkModeExtractionResult(evidence=evidence, resolved_signal=signal)

    if high_full_remote:
        return WorkModeExtractionResult(evidence=evidence, resolved_signal=WorkModeSignal.FULL_REMOTE)

    if medium_hybrid:
        return WorkModeExtractionResult(evidence=evidence, resolved_signal=WorkModeSignal.HYBRID)

    # Only LOW/UNKNOWN evidence → unresolved
    return WorkModeExtractionResult(evidence=evidence, resolved_signal=WorkModeSignal.UNKNOWN)


def detect_work_mode_contradiction(
    metadata_remote: bool | None,
    description_text: str,
) -> str:
    """Detect contradiction between job metadata remote flag and description text.

    Returns a non-empty description string when a contradiction is detected,
    empty string when no contradiction found.

    This is used to populate ``GateResult.contradictions`` for audit.
    It does NOT cause rejection — it signals uncertainty.

    Args:
        metadata_remote:   ``job.remote`` field (True/False/None).
        description_text:  The job description to scan.
    """
    if not description_text:
        return ""

    result = extract_work_mode_evidence(description_text[:3000])

    if result.has_contradiction:
        return result.contradiction_description

    if metadata_remote is True:
        # Metadata says remote, but description has HIGH on-site signal.
        on_site_ev = [
            e for e in result.evidence
            if e.signal in (WorkModeSignal.ON_SITE, WorkModeSignal.HYBRID)
            and e.confidence == "HIGH"
        ]
        if on_site_ev:
            ev_text = on_site_ev[0].span.text if on_site_ev[0].span else "on-site requirement"
            return (
                f"Metadata marks job as remote, but description contains "
                f"on-site/hybrid requirement: '{ev_text}'"
            )

    if metadata_remote is False:
        # Metadata says not remote, but description has HIGH full-remote signal.
        remote_ev = [
            e for e in result.evidence
            if e.signal == WorkModeSignal.FULL_REMOTE
            and e.confidence == "HIGH"
        ]
        if remote_ev:
            ev_text = remote_ev[0].span.text if remote_ev[0].span else "full remote claim"
            return (
                f"Metadata marks job as on-site, but description claims full remote: "
                f"'{ev_text}'"
            )

    return ""
