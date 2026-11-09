"""Does learning from a person's judgments improve the ranking of offers they have not judged?

Offline before/after measure of the personal model (ADR 0030) on human labels. The labels of a
dataset are split into folds. For each fold, the other folds play the part of the user's actions
(accept = interested, reject = not interested) and the model reorders the held-back fold; both
orders are scored against the held-back human labels. No label is ever scored by a model that saw
it. Labels are independent of the model, so this measures transfer to unseen offers; it does not
measure how the same model behaves with the far fewer, noisier signals of everyday use.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.learning.config import PersonalModelConfig
from src.learning.model import fit, offer_terms
from src.learning.signals import ALL_FEATURES, TERMS, OfferSignal

Embed = Callable[[list[str]], Sequence[Sequence[float]]]
_AT = "2026-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class Item:
    job_id: str
    label: bool
    score: float
    title: str
    text: str
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lift:
    folds: int
    labelled: int
    accepted: int
    auc_before: float | None
    auc_after: float | None
    precision_at_5_before: float
    precision_at_5_after: float
    moved_share: float


def auc(items: Sequence[tuple[float, bool]]) -> float | None:
    """Probability that an accepted offer outranks a rejected one (ties count half)."""
    pos = [s for s, y in items if y]
    neg = [s for s, y in items if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def _precision(items: Sequence[tuple[float, bool]], k: int) -> float:
    top = sorted(items, key=lambda p: p[0], reverse=True)[:k]
    return sum(1 for _, y in top if y) / len(top) if top else 0.0


def _signal(item: Item, cfg: PersonalModelConfig) -> OfferSignal:
    y = cfg.weights["interested"] if item.label else cfg.weights["not_interested"]
    return OfferSignal(item.job_id, y, ALL_FEATURES, _AT, 1.0, "label")


def measure(items: Sequence[Item], embed: Embed, cfg: PersonalModelConfig | None = None, folds: int = 5,
            seed: int = 20261109) -> Lift:
    """Cross-validated before/after on ``items``. Deterministic for a given seed."""
    cfg = cfg or PersonalModelConfig()
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    vectors = dict(zip((i.job_id for i in shuffled),
                       embed([f"{i.title} {i.text}"[:2000] for i in shuffled]), strict=True))
    values = {i.job_id: {TERMS: offer_terms(i.title, i.skills)} for i in shuffled}
    before: list[tuple[float, bool]] = []
    after: list[tuple[float, bool]] = []
    moved = 0
    for fold in range(folds):
        test = shuffled[fold::folds]
        train = [i for j, i in enumerate(shuffled) if j % folds != fold]
        model = fit([_signal(i, cfg) for i in train], vectors, values, None, cfg)
        scored = [(i, model.adjustment(vectors[i.job_id], values[i.job_id])) for i in test]
        before.extend((i.score, i.label) for i, _ in scored)
        after.extend((i.score + a, i.label) for i, a in scored)
        order_before = [i.job_id for i, _ in sorted(scored, key=lambda p: p[0].score, reverse=True)]
        order_after = [i.job_id for i, _ in sorted(scored, key=lambda p: p[0].score + p[1], reverse=True)]
        moved += sum(1 for a, b in zip(order_before, order_after, strict=True) if a != b)
    return Lift(folds=folds, labelled=len(shuffled), accepted=sum(1 for i in shuffled if i.label),
                auc_before=auc(before), auc_after=auc(after),
                precision_at_5_before=_precision(before, 5), precision_at_5_after=_precision(after, 5),
                moved_share=moved / len(shuffled) if shuffled else 0.0)


def _jsonl_lines(path: Path) -> list[str]:
    # Not splitlines(): offer text holds U+2028 and friends, which it would treat as line ends.
    return path.read_text(encoding="utf-8").split("\n")


def load_items(run_file: str | Path, dataset_dir: str | Path, *, scored_only: bool = True) -> list[Item]:
    """Offers of a ``src.evaluation`` run result that carry a human accept or reject label."""
    run = json.loads(Path(run_file).read_text(encoding="utf-8"))
    dataset = Path(dataset_dir)
    labels: dict[str, str] = {}
    for line in _jsonl_lines(dataset / "labels.jsonl"):
        if line.strip():
            record = json.loads(line)
            labels[record["job_id"]] = record["expected"]
    jobs: dict[str, dict[str, Any]] = {}
    for line in _jsonl_lines(dataset / "jobs.jsonl"):
        if line.strip():
            record = json.loads(line)
            jobs[record.get("job_id") or record.get("id")] = record
    items: list[Item] = []
    for decision in run["decisions"]:
        job_id, expected = decision["job_id"], labels.get(decision["job_id"])
        if expected not in ("accept", "reject") or job_id not in jobs:
            continue
        if scored_only and decision.get("score") is None:
            continue
        job = jobs[job_id].get("job") or jobs[job_id]
        items.append(Item(job_id=job_id, label=expected == "accept", score=float(decision.get("score") or 0.0),
                          title=str(job.get("title") or ""), text=str(job.get("description") or ""),
                          skills=tuple(str(s) for s in job.get("skills") or ())))
    return items


def report(lift: Lift) -> dict[str, Any]:
    return asdict(lift)


def main() -> None:
    """``python -m src.evaluation.learning_lift RUN_FILE DATASET_DIR [--all]``: tuning labels only."""
    import argparse

    from src.embeddings.embedding import EmbeddingService
    from src.evaluation.heldout import is_heldout

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_file")
    parser.add_argument("dataset_dir")
    parser.add_argument("--dataset-id", default=None, help="default: the dataset folder name")
    parser.add_argument("--all", action="store_true", help="include offers the pipeline rejected (score 0)")
    args = parser.parse_args()
    dataset_id = args.dataset_id or Path(args.dataset_dir).name
    items = [i for i in load_items(args.run_file, args.dataset_dir, scored_only=not args.all)
             if not is_heldout(dataset_id, i.job_id)]
    service = EmbeddingService()
    print(json.dumps(report(measure(items, lambda texts: service.embed_batch(texts))), indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
