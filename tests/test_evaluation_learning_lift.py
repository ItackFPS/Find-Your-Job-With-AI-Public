"""The before/after measure scores each label with a model that never saw it."""

from __future__ import annotations

import json
from pathlib import Path

from src.evaluation.learning_lift import Item, auc, load_items, measure


def _embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] if "reseau" in t else [0.0, 1.0] for t in texts]


def _items(n: int = 20) -> list[Item]:
    # Same base score: only learning can separate the kinds.
    out = []
    for i in range(n):
        wanted = i % 2 == 0
        title = "technicien reseau" if wanted else "comptable clients"
        out.append(Item(f"j{i}", wanted, 0.5, title, ""))
    return out


def test_auc_counts_ties_as_half() -> None:
    assert auc([(1.0, True), (1.0, False)]) == 0.5
    assert auc([(2.0, True), (1.0, False)]) == 1.0
    assert auc([(1.0, True)]) is None


def test_learning_lifts_unseen_offers_of_a_learnable_kind() -> None:
    result = measure(_items(), _embed)
    assert result.auc_before == 0.5 and result.auc_after == 1.0
    assert result.labelled == 20 and result.accepted == 10


def test_a_label_never_trains_the_model_that_scores_it() -> None:
    # Unique titles and uninformative embeddings: nothing transfers, so nothing can leak in.
    items = [Item(f"j{i}", i % 2 == 0, 0.5, f"mot{i}", "") for i in range(20)]
    result = measure(items, lambda texts: [[1.0, 0.0] for _ in texts])
    assert result.auc_after is not None and result.auc_after <= 0.6


def test_load_items_keeps_only_labelled_scored_offers(tmp_path: Path) -> None:
    (tmp_path / "labels.jsonl").write_text(
        "\n".join(json.dumps(r) for r in [{"job_id": "a", "expected": "accept"}, {"job_id": "b", "expected": "reject"},
                                          {"job_id": "c", "expected": "borderline"}]), encoding="utf-8")
    (tmp_path / "jobs.jsonl").write_text(
        "\n".join(json.dumps({"job_id": j, "title": j, "description": "d"}) for j in "abc"), encoding="utf-8")
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"decisions": [{"job_id": "a", "score": 0.7}, {"job_id": "b", "score": None},
                                             {"job_id": "c", "score": 0.4}]}), encoding="utf-8")
    assert [i.job_id for i in load_items(run, tmp_path)] == ["a"]
    assert [i.job_id for i in load_items(run, tmp_path, scored_only=False)] == ["a", "b"]
