"""Metrics over labelled decisions. Stdlib only (no numpy/scipy).

Positive class = ``accept``. A false negative is a good job that was lost
(``unsafe_rejection``); a false positive is a bad job that was let through.
``borderline`` labels are excluded from every rate and reported separately.
"""

from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from src.evaluation.models import Decision, LabelRecord


@dataclass
class Confusion:
    tp: int = 0   # should accept, accepted
    tn: int = 0   # should reject, rejected
    fp: int = 0   # should reject, accepted
    fn: int = 0   # should accept, rejected
    borderline: int = 0

    @property
    def scored(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    def _ratio(self, num: int, den: int) -> float | None:
        return num / den if den else None

    @property
    def accuracy(self) -> float | None:
        return self._ratio(self.tp + self.tn, self.scored)

    @property
    def precision(self) -> float | None:
        return self._ratio(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> float | None:
        return self._ratio(self.tp, self.tp + self.fn)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def reject_recall(self) -> float | None:
        return self._ratio(self.tn, self.tn + self.fp)

    @property
    def reject_precision(self) -> float | None:
        return self._ratio(self.tn, self.tn + self.fn)

    @property
    def unsafe_rejection_rate(self) -> float | None:
        """Share of should-accept jobs that were rejected. Safety gate: must be 0."""
        return self._ratio(self.fn, self.tp + self.fn)

    def to_dict(self) -> dict[str, float | int | None]:
        d: dict[str, float | int | None] = dict(asdict(self))
        d.update(
            scored=self.scored, accuracy=self.accuracy, precision=self.precision,
            recall=self.recall, f1=self.f1, reject_recall=self.reject_recall,
            reject_precision=self.reject_precision,
            unsafe_rejection_rate=self.unsafe_rejection_rate,
        )
        return d


def _outcome(expected: str, verdict: str) -> str:
    if expected == "borderline":
        return "borderline"
    should_accept = expected == "accept"
    did_accept = verdict == "accept"
    if should_accept and did_accept:
        return "tp"
    if not should_accept and not did_accept:
        return "tn"
    return "fp" if did_accept else "fn"


def confusion(pairs: Iterable[tuple[str, str]]) -> Confusion:
    """``pairs`` = (expected, verdict)."""
    c = Confusion()
    for expected, verdict in pairs:
        outcome = _outcome(expected, verdict)
        setattr(c, outcome, getattr(c, outcome) + 1)
    return c


FRESHNESS_REASON = "filter:published_age"

DIAGNOSTIC_WARNING = (
    "DIAGNOSTIC ONLY - a different evaluation population: jobs whose recorded rejection reason is "
    "published_age are removed, so these figures are NOT comparable with the overall metrics. The "
    "removal is by recorded reason, not by proof that freshness was the only problem: the hard filter "
    "stops at its first failing check, so the checks after it and the requirements gate were never "
    "evaluated for the removed jobs. No confidence interval is given."
)


def rejection_reason(d: Decision) -> str | None:
    """Why a decision is a rejection: the first failing hard filter (``filter:<code>``), else the stage."""
    if d.verdict != "reject":
        return None
    if d.stage == "filter" and d.reasons:
        return d.reasons[0]
    return d.stage


def _label_key(job_id: str, labels: dict[str, LabelRecord]) -> str:
    return labels[job_id].expected if job_id in labels else "unlabelled"


def rejections_by_reason(decisions: list[Decision], labels: dict[str, LabelRecord]) -> dict[str, dict]:
    """Pipeline rejections grouped by reason, each split by the human label (``unlabelled`` if none)."""
    out: dict[str, dict] = {}
    for d in decisions:
        reason = rejection_reason(d)
        if reason is None:
            continue
        bucket = out.setdefault(reason, {"total": 0, "by_label": {}})
        key = _label_key(d.job_id, labels)
        bucket["total"] += 1
        bucket["by_label"][key] = bucket["by_label"].get(key, 0) + 1
    return {r: {"total": b["total"], "by_label": dict(sorted(b["by_label"].items()))}
            for r, b in sorted(out.items())}


def diagnostic_excluding_freshness(decisions: list[Decision], labels: dict[str, LabelRecord]) -> dict:
    """Confusion metrics over the jobs NOT rejected for ``published_age`` (supplementary, see warning)."""
    kept = [d for d in decisions if rejection_reason(d) != FRESHNESS_REASON]
    removed = [d for d in decisions if rejection_reason(d) == FRESHNESS_REASON]
    overall = confusion((labels[d.job_id].expected, d.verdict) for d in kept if d.job_id in labels)
    excluded_by_label: dict[str, int] = {}
    for d in removed:
        key = _label_key(d.job_id, labels)
        excluded_by_label[key] = excluded_by_label.get(key, 0) + 1
    return {
        "kind": "diagnostic_not_comparable",
        "warning": DIAGNOSTIC_WARNING,
        "excluded_reason": FRESHNESS_REASON,
        "n_jobs": len(kept),
        "n_excluded": len(removed),
        "n_labelled": sum(1 for d in kept if d.job_id in labels),
        "excluded_by_label": dict(sorted(excluded_by_label.items())),
        "overall": overall.to_dict(),
    }


INDEPENDENT_WARNING = (
    "DIAGNOSTIC ONLY - restricted to labels given without an assistant proposal on record "
    "(label_source=human). A different, smaller and not randomly selected population: NOT comparable "
    "with the overall metrics. No confidence interval is given."
)


def effective_sources(labels: dict[str, LabelRecord], assisted: frozenset[str] = frozenset()) -> dict[str, str]:
    """``job_id → label_source`` as evidence: a label recorded ``human`` although an assistant proposal is on
    record for that job (``assisted``; imported before provenance tracking) counts as ``human_assisted``."""
    return {jid: ("human_assisted" if lab.label_source == "human" and jid in assisted else lab.label_source)
            for jid, lab in labels.items()}


def label_provenance(
    decisions: list[Decision], labels: dict[str, LabelRecord], assisted: frozenset[str] = frozenset(),
) -> dict:
    """Labels by provenance, plus the confusion over independent (``human``) labels only when other
    provenances are mixed in — assistant-assisted labels are not independent evidence.

    ``basis`` says what the overall metrics rest on: ``independent`` (every label ``human``), ``mixed``,
    ``not_independent`` (no ``human`` label at all) or ``none`` (no label). ``by_class`` gives, per expected
    class, how many labels there are and how many are independent — recall and the unsafe-rejection rate
    rest on the ``accept`` class alone."""
    sources = effective_sources(labels, assisted)
    by_source: dict[str, dict[str, int]] = {}
    by_class: dict[str, dict[str, int]] = {}
    for jid, lab in labels.items():
        bucket = by_source.setdefault(sources[jid], {})
        bucket[lab.expected] = bucket.get(lab.expected, 0) + 1
        cls = by_class.setdefault(lab.expected, {"total": 0, "independent": 0})
        cls["total"] += 1
        cls["independent"] += sources[jid] == "human"
    n_independent = sum(1 for s in sources.values() if s == "human")
    out: dict[str, Any] = {
        "by_source": {k: dict(sorted(v.items())) for k, v in sorted(by_source.items())},
        "n_independent": n_independent,
        "n_assisted": sum(1 for s in sources.values() if s == "human_assisted"),
        "n_derived": sum(1 for s in sources.values() if s == "derived"),
        "n_recorded_human_but_assisted": sum(
            1 for jid, lab in labels.items() if lab.label_source == "human" and sources[jid] != "human"),
        "basis": ("none" if not labels else "independent" if n_independent == len(labels)
                  else "mixed" if n_independent else "not_independent"),
        "by_class": dict(sorted(by_class.items())),
    }
    if n_independent and len(by_source) > 1:
        ind = [(labels[d.job_id].expected, d.verdict) for d in decisions
               if d.job_id in labels and sources[d.job_id] == "human"]
        out["independent_only"] = {"kind": "diagnostic_not_comparable", "warning": INDEPENDENT_WARNING,
                                   "n_labelled": len(ind), "overall": confusion(ind).to_dict()}
    return out


def evaluate(
    decisions: list[Decision], labels: dict[str, LabelRecord], assisted: frozenset[str] = frozenset(),
) -> dict:
    """Join labels to decisions (after inference) and compute all metrics. ``assisted``: job ids with an
    assistant proposal on record (see :func:`effective_sources`)."""
    labelled = [(d, labels[d.job_id]) for d in decisions if d.job_id in labels]
    overall = confusion((lab.expected, d.verdict) for d, lab in labelled)
    by_category: dict[str, Confusion] = {}
    for _d, lab in labelled:
        by_category.setdefault(lab.category or "uncategorised", Confusion())
    for cat in by_category:
        by_category[cat] = confusion(
            (lab.expected, d.verdict) for d, lab in labelled if (lab.category or "uncategorised") == cat
        )
    stage_counts: dict[str, int] = {}
    for d in decisions:
        if d.verdict == "reject":
            stage_counts[d.stage] = stage_counts.get(d.stage, 0) + 1
    fp_ids = sorted(d.job_id for d, lab in labelled if _outcome(lab.expected, d.verdict) == "fp")
    fn_ids = sorted(d.job_id for d, lab in labelled if _outcome(lab.expected, d.verdict) == "fn")
    return {
        "n_jobs": len(decisions),
        "n_labelled": len(labelled),
        "n_unlabelled": len(decisions) - len(labelled),
        "overall": overall.to_dict(),
        "by_category": {k: v.to_dict() for k, v in sorted(by_category.items())},
        "rejections_by_stage": dict(sorted(stage_counts.items())),
        "rejections_by_reason": rejections_by_reason(decisions, labels),
        "false_positive_ids": fp_ids,
        "false_negative_ids": fn_ids,
        "uncertainty": _uncertainty(decisions),
        "diagnostic_excluding_freshness": diagnostic_excluding_freshness(decisions, labels),
        "label_provenance": label_provenance(decisions, labels, assisted),
        "ranking": ranking_metrics(decisions, labels),
    }


def _uncertainty(decisions: list[Decision]) -> dict[str, int]:
    return {
        "accepted_with_flags": sum(1 for d in decisions if d.verdict == "accept" and d.flags),
        "accepted": sum(1 for d in decisions if d.verdict == "accept"),
    }


_STATS = ("accuracy", "precision", "recall", "f1", "unsafe_rejection_rate")


def bootstrap_ci(
    pairs: Sequence[tuple[str, str]], *, n_boot: int = 1000, seed: int = 20260917, alpha: float = 0.05
) -> dict[str, dict[str, float | None]]:
    """Percentile bootstrap CI over labelled jobs (borderline kept as-is, ignored by rates).

    Deterministic for a given ``seed``. With fewer than 2 scored items the
    interval is reported as ``None`` rather than fabricated.
    """
    scored = [p for p in pairs if p[0] != "borderline"]
    out: dict[str, dict[str, float | None]] = {s: {"lo": None, "hi": None} for s in _STATS}
    if len(scored) < 2:
        return out
    rng = random.Random(seed)
    samples: dict[str, list[float]] = {s: [] for s in _STATS}
    for _ in range(n_boot):
        c = confusion(rng.choice(scored) for _ in scored)
        for s in _STATS:
            v = getattr(c, s)
            if v is not None:
                samples[s].append(v)
    for s, vals in samples.items():
        if len(vals) >= max(2, n_boot // 2):
            vals.sort()
            out[s] = {
                "lo": vals[int((alpha / 2) * (len(vals) - 1))],
                "hi": vals[int((1 - alpha / 2) * (len(vals) - 1))],
            }
    return out


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact binomial p-value on discordant pairs (b, c)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    vs = sorted(values)
    return float(vs[int(round(q * (len(vs) - 1)))])


def review_metrics(decisions: list[Decision], labels: dict[str, LabelRecord]) -> dict:
    """How useful is the ``review`` flag? Computed on labelled, non-borderline decisions.

    A decision is *wrong* when a good job is rejected or a bad job accepted. ``lift`` = share of wrong
    decisions captured in review / share of all decisions flagged (>1 means review concentrates the
    errors). ``status`` is ``insufficient_evidence`` below 30 labelled decisions or 5 wrong ones — no
    conclusion is drawn from fewer.
    """
    rows = []
    for d in decisions:
        lab = labels.get(d.job_id)
        if lab is None or lab.expected == "borderline" or not d.assessment:
            continue
        wrong = (d.verdict == "accept") != (lab.expected == "accept")
        rows.append((wrong, d.assessment["state"] == "review", d.verdict, lab.expected))
    n = len(rows)
    n_review = sum(1 for _, r, _, _ in rows if r)
    n_wrong = sum(1 for w, _, _, _ in rows if w)
    captured = sum(1 for w, r, _, _ in rows if w and r)
    auto = [(w, r) for w, r, _, _ in rows if not r]
    review_rate = n_review / n if n else None
    capture_rate = captured / n_wrong if n_wrong else None
    lift = capture_rate / review_rate if capture_rate is not None and review_rate else None
    insufficient = n < 30 or n_wrong < 5
    if insufficient:
        status = "insufficient_evidence"
    elif lift is None or lift <= 1.0:
        status = "not_useful"
    else:
        status = "promising"
    return {
        "n_labelled": n, "n_review": n_review, "review_rate": review_rate,
        "n_wrong": n_wrong, "n_wrong_in_review": captured, "capture_rate": capture_rate, "lift": lift,
        "overall_accuracy": (n - n_wrong) / n if n else None,
        "auto_accuracy": (sum(1 for w, _ in auto if not w) / len(auto)) if auto else None,
        "unsafe_rejections_not_in_review": sum(
            1 for _, r, v, e in rows if v == "reject" and e == "accept" and not r),
        "status": status,
        "note": ("fewer than 30 labelled decisions or 5 wrong ones: no conclusion about usefulness can be drawn"
                 if insufficient else "")
                + " Offline runs use rules only: a rejected job cannot be flagged for review without LLM/semantic signals.",
    }


# --- ranking: how the score orders what the gates let through ------------------------------------------
#
# The gates only reject; a job outside the profile's preferred locations, say, passes them and is ranked
# low instead (ADR 0005). Confusion metrics alone therefore call such a job a false positive, and cannot
# see a scoring change at all. These metrics judge the order. Offline, ``score`` is the deterministic
# score (no semantic blend), so they are a proxy for what production ranks.

RANKING_KS = (5, 10, 20)
VISIBILITY_TIER = "relevant"     # the floor the weekly top-N and notifications apply by default
MOVED_TOP = 10
RANKING_DEFINITION = {
    "population": "labelled jobs accepted by the gates (verdict accept) that have a score",
    "positive": "accept",
    "negative": "reject",
    "excluded": "borderline labels (see auc_borderline_as_accept), unlabelled jobs, jobs the gates rejected "
                "(counted in accept_rejected_by_gates)",
    "direction": "higher score ranks first; AUC = P(positive outscores negative), ties count half",
    "score": "deterministic score of the replay (no semantic blend): a proxy for production's final_score",
}


def _auc(pos: Sequence[float], neg: Sequence[float]) -> float | None:
    """P(a random accept-labelled job outscores a random reject-labelled one); ties count half."""
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def _ranked(decisions: Sequence[Decision], labels: dict[str, LabelRecord]) -> list[tuple[float, str, str]]:
    """``(score, job_id, expected)`` for labelled jobs the gates accepted, best first (job id breaks ties)."""
    rows = [(d.score, d.job_id, labels[d.job_id].expected) for d in decisions
            if d.verdict == "accept" and d.score is not None and d.job_id in labels]
    return sorted(rows, key=lambda r: (-r[0], r[1]))


def _auc_of(ranked: Sequence[tuple[float, str, str]], *, borderline_as: str | None = None) -> float | None:
    def kind(e: str) -> str:
        return borderline_as if (e == "borderline" and borderline_as) else e
    return _auc([s for s, _, e in ranked if kind(e) == "accept"], [s for s, _, e in ranked if kind(e) == "reject"])


def ranking_metrics(decisions: Sequence[Decision], labels: dict[str, LabelRecord]) -> dict[str, Any]:
    """AUC, precision@k and the visibility floor over the labelled jobs the gates accepted."""
    from src.pipeline.scoring import TIER_THRESHOLDS, score_to_tier, tier_meets_minimum

    ranked = _ranked(decisions, labels)
    precision_at = {str(k): (sum(e == "accept" for _, _, e in ranked[:k]) / k if len(ranked) >= k else None)
                    for k in RANKING_KS}
    shown = [e for s, _, e in ranked if tier_meets_minimum(score_to_tier(round(s * 100)), VISIBILITY_TIER)]
    return {
        "definition": RANKING_DEFINITION,
        "auc": _auc_of(ranked),
        "auc_borderline_as_accept": _auc_of(ranked, borderline_as="accept"),
        "n_accept": sum(e == "accept" for _, _, e in ranked),
        "n_reject": sum(e == "reject" for _, _, e in ranked),
        "n_borderline": sum(e == "borderline" for _, _, e in ranked),
        "precision_at": precision_at,
        "visible_at_floor": {
            "tier": VISIBILITY_TIER,
            "min_score": next(t for t, name in TIER_THRESHOLDS if name == VISIBILITY_TIER) / 100,
            "accept": shown.count("accept"), "borderline": shown.count("borderline"), "reject": shown.count("reject"),
            "accept_hidden": sum(e == "accept" for _, _, e in ranked) - shown.count("accept"),
        },
        "accept_rejected_by_gates": sum(1 for d in decisions if d.verdict == "reject" and d.job_id in labels
                                        and labels[d.job_id].expected == "accept"),
    }


def ranking_delta(
    baseline: Sequence[Decision], candidate: Sequence[Decision], labels: dict[str, LabelRecord],
    *, n_boot: int = 2000, seed: int = 20260917,
) -> dict[str, Any]:
    """Candidate minus baseline AUC on ONE cohort: the labelled jobs both runs accept.

    Each side's own AUC (``ranking_metrics``) is over the jobs its gates accept; when a candidate changes
    gate decisions those are two populations, and their difference mixes re-ranking with who was let
    through. ``cohort`` says how many jobs only one side accepts. The CI is a paired percentile bootstrap
    over the cohort (same resample for both sides). ``moved`` lists the cohort jobs whose rank changed
    most. ``auc`` is ``None`` when the cohort has no accept/reject pair."""
    rb, rc = _ranked(baseline, labels), _ranked(candidate, labels)
    ib, ic = {r[1] for r in rb}, {r[1] for r in rc}
    common = ib & ic
    rb, rc = [r for r in rb if r[1] in common], [r for r in rc if r[1] in common]
    cohort = {"common": len(common), "only_baseline": len(ib - ic), "only_candidate": len(ic - ib)}
    pos_b, pos_c = {r[1]: (i + 1, r) for i, r in enumerate(rb)}, {r[1]: (i + 1, r) for i, r in enumerate(rc)}
    shifted = sorted((j for j in common if pos_b[j][0] != pos_c[j][0]),
                     key=lambda j: (-abs(pos_b[j][0] - pos_c[j][0]), j))[:MOVED_TOP]
    moved = [{"job_id": j, "expected": pos_b[j][1][2], "rank": [pos_b[j][0], pos_c[j][0]],
              "score": [pos_b[j][1][0], pos_c[j][1][0]]} for j in shifted]
    ab, ac = _auc_of(rb), _auc_of(rc)
    if ab is None or ac is None:
        return {"auc": None, "auc_diff_ci95": None, "cohort": cohort, "moved": moved}
    ids = sorted(j for _, j, e in rb if e != "borderline")
    rng = random.Random(seed)
    diffs = []
    mb, mc = {r[1]: r for r in rb}, {r[1]: r for r in rc}
    for _ in range(n_boot):
        sample = [rng.choice(ids) for _ in ids]
        b, c = _auc_of([mb[j] for j in sample]), _auc_of([mc[j] for j in sample])
        if b is not None and c is not None:
            diffs.append(c - b)
    ci = (percentile(diffs, 0.025), percentile(diffs, 0.975)) if diffs else None
    return {"auc": ac - ab, "auc_diff_ci95": ci, "cohort": cohort, "moved": moved}
