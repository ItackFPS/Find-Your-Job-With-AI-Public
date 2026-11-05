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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from src.learning.config import PersonalModelConfig
from src.learning.signals import EMBEDDING, STRUCTURED, OfferSignal

ALGORITHM_VERSION = "pm1"

Values = Mapping[str, str | None]


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
            if value is not None:
                total += table.get(value, 0.0)
        return total

    def adjustment(self, vector: Sequence[float] | None, values: Values) -> float:
        if self.n == 0 or self.cap == 0.0:
            return 0.0
        return self.cap * math.tanh(self.raw(vector, values) / self.clamp)

    def summary(self) -> dict[str, object]:
        """Inspectable state, stored in ``personal_model_state`` (the embedding δ is not stored:
        it is rebuilt from the journal, INV-4)."""
        return {"n": self.n, "cap": self.cap, "tables": self.tables, "content_signals": self.content_signals,
                "content_with_vector": self.content_with_vector,
                "delta_norm": None if self.delta is None else float(np.linalg.norm(self.delta))}


def fit(signals: Sequence[OfferSignal], vectors: Mapping[str, Sequence[float]],
        values: Mapping[str, Values], profile: Sequence[float] | None, cfg: PersonalModelConfig,
        embedding_model: str = "") -> PersonalModel:
    """Fit the model on ``signals``. ``vectors`` and ``values`` are keyed by job id; missing entries
    simply contribute nothing to the part that needs them."""
    signals = sorted(signals, key=lambda s: s.job_id)
    digest, ihash = cfg.digest(), input_hash(signals, profile, embedding_model)
    n = len(signals)

    content = [s for s in signals if EMBEDDING in s.features]
    rows = [(s, x) for s in content if (x := _unit(vectors.get(s.job_id))) is not None]
    delta: np.ndarray | None = None
    if rows:
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

    return PersonalModel(n=n, cap=cfg.cap(n), clamp=cfg.clamp, delta=delta, tables=tables, input_hash=ihash,
                         config_digest=digest, content_signals=len(content), content_with_vector=len(rows))


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
