"""The personal preference model (ADR 0018, v5 AD-3), pure computation: no I/O.

Two parts, both fitted on the per-offer signals of :mod:`src.learning.signals`:

* **embedding part**: weighted ridge regression of ``y`` on the (unit-normalised) job embedding, with
  the profile embedding as prior mean ``w0``. Only the deviation ``δ = w − w0`` is used to score, so
  with no signal ``δ = 0`` and the part is exactly 0. Solved in its dual form (one m × m system,
  m = offers with a content signal), cheap at any realistic volume;
* **structured part**: per feature and value, a shrunk weighted mean of ``y``
  (``Σ w·y / (Σ w + shrinkage)``), summed over the features an offer has a value for.

The raw sum is squashed and bounded by the evidence-scaled cap: ``cap(n) · tanh(raw / clamp)``, with
``n`` the number of offers carrying a signal. ``n = 0`` gives exactly ``0.0`` for every offer (INV-7).
The gates are re-applied by the caller (``cap_personalization_at_gates``).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from src.learning.config import PersonalModelConfig
from src.learning.signals import EMBEDDING, STRUCTURED, TERMS, OfferSignal

ALGORITHM_VERSION = "pm2"

Values = Mapping[str, Any]
EMBEDDING_LABEL = "similar_content"   # how the embedding part is named in an explanation
_TERM_TOP = 3                         # an offer's strongest known terms, averaged
_REASONS_KEPT = 3

_WORD = re.compile(r"[a-z0-9+#.]{2,}")
_NOT_TERMS = frozenset({
    "and", "alternance", "apprentissage", "au", "aux", "avec", "cdd", "cdi", "dans", "de", "des", "du", "emploi",
    "en", "et", "fh", "for", "h.f", "hf", "interim", "job", "la", "le", "les", "mission", "nf", "offre", "of",
    "ou", "par", "pour", "poste", "recrute", "recherche", "stage", "sur", "temps", "the", "to", "un", "une",
})


def offer_terms(title: str | None, skills: Sequence[str] | None) -> tuple[str, ...]:
    """The words naming what an offer is about: its title and listed skills, one form per word."""
    text = " ".join([title or "", *(skills or [])])
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    seen: dict[str, None] = {}
    for word in _WORD.findall(plain):
        word = word.strip(".")
        if len(word) > 4 and word[-1] in "sx":
            word = word[:-1]
        if len(word) >= 2 and word not in _NOT_TERMS and not word.isdigit():
            seen.setdefault(word)
    return tuple(seen)


def _unit(vec: Sequence[float] | None) -> np.ndarray | None:
    if vec is None or len(vec) == 0:
        return None
    a = np.asarray(vec, dtype=np.float64)
    norm = float(np.linalg.norm(a))
    return a / norm if norm > 0 else None


def input_hash(signals: Sequence[OfferSignal], profile: Sequence[float] | None, embedding_model: str) -> str:
    blob = json.dumps({
        "signals": [(s.job_id, s.y, sorted(s.features), s.effective_at, round(s.weight, 9)) for s in signals],
        "profile": hashlib.sha256(json.dumps([round(float(x), 7) for x in profile or []]).encode()).hexdigest(),
        "embedding_model": embedding_model,
    }, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class PersonalModel:
    n: int
    cap: float
    clamp: float
    delta: np.ndarray | None
    tables: dict[str, dict[str, float]] = field(default_factory=dict)
    input_hash: str = ""
    config_digest: str = ""
    content_signals: int = 0
    content_with_vector: int = 0
    strong: int = 0

    @property
    def version(self) -> str:
        return f"{ALGORITHM_VERSION}+{self.config_digest}+{self.input_hash[:12]}"

    @property
    def coverage(self) -> float | None:
        return None if not self.content_signals else self.content_with_vector / self.content_signals

    def raw(self, vector: Sequence[float] | None, values: Values) -> float:
        total = 0.0
        x = _unit(vector)
        if self.delta is not None and x is not None and x.shape == self.delta.shape:
            total += float(x @ self.delta)
        for feature, table in self.tables.items():
            value = values.get(feature)
            if feature != TERMS and value is not None:
                total += table.get(value, 0.0)
        return total + self._terms_part(values)

    def _terms_part(self, values: Values) -> float:
        table = self.tables.get(TERMS)
        if not table:
            return 0.0
        known = [table[t] for t in values.get(TERMS) or () if t in table]
        known = sorted(known, key=abs, reverse=True)[:_TERM_TOP]
        return sum(known) / len(known) if known else 0.0

    def reasons(self, vector: Sequence[float] | None, values: Values) -> list[dict[str, Any]]:
        """What moved this offer, strongest first: the learned preferences it matches."""
        parts: list[tuple[str, str, float]] = []
        x = _unit(vector)
        if self.delta is not None and x is not None and x.shape == self.delta.shape:
            parts.append((EMBEDDING, EMBEDDING_LABEL, float(x @ self.delta)))
        for feature, table in self.tables.items():
            if feature == TERMS:
                parts.extend((TERMS, t, table[t]) for t in values.get(TERMS) or () if t in table)
            elif (value := values.get(feature)) is not None and value in table:
                parts.append((feature, value, table[value]))
        parts = [p for p in parts if abs(p[2]) > 1e-6]
        parts.sort(key=lambda p: abs(p[2]), reverse=True)
        return [{"feature": f, "value": v, "weight": round(w, 4)} for f, v, w in parts[:_REASONS_KEPT]]

    def adjustment(self, vector: Sequence[float] | None, values: Values) -> float:
        if self.n == 0 or self.cap == 0.0:
            return 0.0
        return self.cap * math.tanh(self.raw(vector, values) / self.clamp)

    def summary(self) -> dict[str, object]:
        """Inspectable state, stored in ``personal_model_state`` (the embedding δ is not stored:
        it is rebuilt from the journal, INV-4)."""
        return {"n": self.n, "strong": self.strong, "cap": self.cap, "tables": self.tables,
                "content_signals": self.content_signals,
                "content_with_vector": self.content_with_vector,
                "delta_norm": None if self.delta is None else float(np.linalg.norm(self.delta))}


def fit(signals: Sequence[OfferSignal], vectors: Mapping[str, Sequence[float]],
        values: Mapping[str, Values], profile: Sequence[float] | None, cfg: PersonalModelConfig,
        embedding_model: str = "", forgotten: Collection[tuple[str, str]] = ()) -> PersonalModel:
    """Fit the model on ``signals``. ``vectors`` and ``values`` are keyed by job id; missing entries
    simply contribute nothing to the part that needs them. A ``(feature, value)`` in ``forgotten``
    (the user corrected it, ADR 0030) is left out of the result."""
    signals = sorted(signals, key=lambda s: s.job_id)
    digest, ihash = cfg.digest(), input_hash(signals, profile, embedding_model)
    n = len(signals)

    content = [s for s in signals if EMBEDDING in s.features]
    rows = [(s, x) for s in content if (x := _unit(vectors.get(s.job_id))) is not None]
    delta: np.ndarray | None = None
    if rows and (EMBEDDING, EMBEDDING_LABEL) not in forgotten:
        X = np.vstack([x for _, x in rows])
        w0 = _unit(profile)
        if w0 is None or w0.shape[0] != X.shape[1]:
            w0 = np.zeros(X.shape[1])
        y = np.array([s.y for s, _ in rows])
        sw = np.array([s.weight for s, _ in rows])
        r = y - X @ w0
        K = X @ X.T + cfg.prior_strength * np.diag(1.0 / sw)
        delta = X.T @ np.linalg.solve(K, r)

    tables: dict[str, dict[str, float]] = {}
    for feature in STRUCTURED:
        sums: dict[str, list[float]] = {}
        for s in signals:
            value = values.get(s.job_id, {}).get(feature)
            if feature in s.features and value is not None:
                acc = sums.setdefault(value, [0.0, 0.0])
                acc[0] += s.weight * s.y
                acc[1] += s.weight
        if sums:
            tables[feature] = {v: wy / (w + cfg.shrinkage) for v, (wy, w) in sorted(sums.items())}

    term_sums: dict[str, list[float]] = {}
    for s in signals:
        if TERMS in s.features:
            for term in values.get(s.job_id, {}).get(TERMS) or ():
                acc = term_sums.setdefault(term, [0.0, 0.0])
                acc[0] += s.weight * s.y
                acc[1] += s.weight
    if term_sums:
        tables[TERMS] = {t: wy / (w + cfg.shrinkage) for t, (wy, w) in sorted(term_sums.items())}
    tables = {f: kept for f, table in tables.items()
              if (kept := {v: a for v, a in table.items() if (f, v) not in forgotten})}

    return PersonalModel(n=n, cap=cfg.cap(n), clamp=cfg.clamp, delta=delta, tables=tables, input_hash=ihash,
                         config_digest=digest, content_signals=len(content), content_with_vector=len(rows),
                         strong=sum(1 for s in signals if abs(s.y) >= 1.0))


# -- structured values -----------------------------------------------------------------------------

def _band(score: float | None, cuts: tuple[float, float] = (0.34, 0.67)) -> str | None:
    if score is None:
        return None
    return "low" if score < cuts[0] else ("mid" if score < cuts[1] else "high")


def _salary_bucket(salary_min: float | None) -> str | None:
    if salary_min is None or salary_min <= 0:
        return None
    annual = salary_min * 12 if salary_min < 10_000 else salary_min     # monthly figures stored as is
    for limit, label in ((30_000, "<30k"), (45_000, "30-45k"), (60_000, "45-60k")):
        if annual < limit:
            return label
    return "60k+"


def structured_values(*, source: str | None, work_mode: str | None, remote: bool | None,
                      employment_type: str | None, salary_min: float | None,
                      factors: Mapping[str, float] | None) -> dict[str, str | None]:
    """The structured feature values of one offer, from its stored fields and scoring factors."""
    factors = factors or {}
    mode = (work_mode or "").strip().lower() or ("remote" if remote else None)
    return {
        "source": source or None,
        "remote": mode,
        "contract": (employment_type or "").strip().lower() or None,
        "salary": _salary_bucket(salary_min),
        "distance": _band(factors.get("location_fit")),
        "seniority": _band(factors.get("seniority_match")),
    }
