"""I17 — EXPERIMENTAL local domain classifier (SHADOW MODE ONLY).

Production domain classification (`src.pipeline.scoring._score_domain_relevance`)
is a hand-maintained keyword/vocabulary match. This module explores a
different technique — local sentence-embedding similarity against the SAME
domain/off-domain vocabulary already selected by the user's profile — so
that disagreement between the two is evidence about a real generalization
gap (a paraphrase or synonym the keyword list misses), not just noise from
reimplementing the same logic twice.

This module is never imported by src.pipeline.orchestrator or any other
production wiring (see src/experimental/__init__.py). It is invoked only by
tests and by the standalone evaluation harness in domain_shadow_eval.py.
Nothing here can reject, accept, score, rank, or otherwise influence a real
job — it only ever returns an observation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

_DEFAULT_MARGIN_THRESHOLD = 0.05
_DESCRIPTION_WINDOW = 2000  # matches production's _score_domain_relevance window


class _EmbeddingLike(Protocol):
    def embed(self, text: str) -> list[float]: ...
    def cosine_similarity(self, a: list[float], b: list[float]) -> float: ...


@dataclass(frozen=True)
class ShadowDomainResult:
    """One observation. Never persisted anywhere production reads from."""

    label: str  # "on_domain" | "off_domain" | "uncertain"
    confidence: float
    domain_similarity: float | None = None
    off_domain_similarity: float | None = None
    error: str | None = None


class ExperimentalDomainClassifier:
    """Embedding-similarity domain/off-domain classifier.

    Anchor vocabulary (the same profile-selected domain_title_keywords /
    domain_description_keywords / off_domain_keywords production already
    injects into scoring config) is embedded once, lazily, on first use —
    not per job — so per-job cost is a single embed() call plus O(anchors)
    cosine similarities, not O(anchors) embed() calls per job.
    """

    def __init__(
        self,
        embedding_service: _EmbeddingLike,
        domain_keywords: list[str],
        off_domain_keywords: list[str],
        margin_threshold: float = _DEFAULT_MARGIN_THRESHOLD,
    ) -> None:
        self._embedding_service = embedding_service
        self._margin_threshold = margin_threshold
        self._domain_keywords = [k for k in dict.fromkeys(domain_keywords) if k and k.strip()]
        self._off_domain_keywords = [
            k for k in dict.fromkeys(off_domain_keywords) if k and k.strip()
        ]
        self._domain_vectors: list[list[float]] | None = None
        self._off_domain_vectors: list[list[float]] | None = None

    def _ensure_anchor_vectors(self) -> None:
        if self._domain_vectors is None:
            self._domain_vectors = [self._embedding_service.embed(k) for k in self._domain_keywords]
        if self._off_domain_vectors is None:
            self._off_domain_vectors = [
                self._embedding_service.embed(k) for k in self._off_domain_keywords
            ]

    def classify(self, title: str | None, description: str | None) -> ShadowDomainResult:
        """Classify a job in isolation. Never raises — a broken embedding
        model, empty text, or empty vocabulary all fail safely into
        "uncertain" with an `error` field, per I17's requirement that
        malformed/missing experimental output must never crash the caller.
        """
        text = f"{title or ''} {(description or '')[:_DESCRIPTION_WINDOW]}".strip()
        if not text:
            return ShadowDomainResult(label="uncertain", confidence=0.0, error="empty_text")
        if not self._domain_keywords and not self._off_domain_keywords:
            return ShadowDomainResult(
                label="uncertain", confidence=0.0, error="no_anchor_vocabulary"
            )

        try:
            self._ensure_anchor_vectors()
            job_vector = self._embedding_service.embed(text)
            domain_sim = max(
                (self._embedding_service.cosine_similarity(job_vector, v) for v in self._domain_vectors or []),
                default=0.0,
            )
            off_domain_sim = max(
                (
                    self._embedding_service.cosine_similarity(job_vector, v)
                    for v in self._off_domain_vectors or []
                ),
                default=0.0,
            )
        except Exception as exc:  # noqa: BLE001 - shadow mode must never propagate a failure
            return ShadowDomainResult(label="uncertain", confidence=0.0, error=str(exc))

        margin = domain_sim - off_domain_sim
        if margin > self._margin_threshold:
            label = "on_domain"
        elif margin < -self._margin_threshold:
            label = "off_domain"
        else:
            label = "uncertain"

        return ShadowDomainResult(
            label=label,
            confidence=abs(margin),
            domain_similarity=domain_sim,
            off_domain_similarity=off_domain_sim,
        )


def production_label_from_score(
    domain_relevance: float,
    *,
    on_domain_threshold: float = 0.7,
    off_domain_threshold: float = 0.05,
) -> str:
    """Map production's continuous domain_relevance score onto the same
    3-way label space the shadow classifier uses, so the two are comparable.

    Thresholds mirror _score_domain_relevance's own tiers: <=0.05 is its
    "confirmed wrong" tier, >=0.7 is its "domain keyword found" tier; the
    0.35/0.45 "broad indicator only" / "undetermined" tiers in between carry
    no confirmed domain evidence either way, so they map to "uncertain".
    """
    if domain_relevance <= off_domain_threshold:
        return "off_domain"
    if domain_relevance >= on_domain_threshold:
        return "on_domain"
    return "uncertain"


def build_domain_classifier_from_config(
    embedding_service: _EmbeddingLike, scoring_config: dict[str, Any]
) -> ExperimentalDomainClassifier:
    """Convenience constructor pulling the same injected keyword lists
    production's own _score_domain_relevance reads, from an already-built
    scoring config (see orchestrator.py's domain_rel_cfg injection)."""
    domain_cfg = scoring_config.get("scoring", {}).get("domain_relevance", {})
    domain_keywords = list(domain_cfg.get("domain_title_keywords", [])) + list(
        domain_cfg.get("domain_description_keywords", [])
    )
    off_domain_keywords = list(domain_cfg.get("off_domain_keywords", []))
    return ExperimentalDomainClassifier(embedding_service, domain_keywords, off_domain_keywords)
