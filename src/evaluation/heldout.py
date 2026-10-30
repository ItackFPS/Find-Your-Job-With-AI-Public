"""Held-out split of a labelled dataset: the labels a tuning loop never sees.

Choosing a change on the same labels that then judge it overstates the change. Each job of a dataset
therefore falls, once and for all, into one of two parts, decided by a hash of the dataset id and the
job id (nothing about the job's content, label or score):

* ``heldout`` — :data:`HELDOUT_FRACTION` of the jobs. Used only to test a change chosen beforehand
  (a pre-registered candidate, ``docs/lab/preregistered/``), never to explore or tune;
* ``tuning`` — the rest: investigation, the lab agent, trying variants.

The split is fixed before any label exists (it depends on ids only), so labelling more jobs later never
moves a job from one part to the other. ``all`` keeps every label (the default outside the lab), and
every report states which split it was computed on.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Literal

from pathlib import Path

from src.evaluation.dataset import Dataset, load_dataset

Split = Literal["all", "heldout", "tuning"]
SPLITS: tuple[Split, ...] = ("all", "heldout", "tuning")
HELDOUT_FRACTION = 0.30
_SALT = "fyj-heldout-v1"    # changing it re-draws every split: never change it once labels exist
RULE = (f"sha256('{_SALT}:<dataset_id>:<job_id>'), first 8 hex digits / 16^8 < {HELDOUT_FRACTION}: held out")


def is_heldout(dataset_id: str, job_id: str) -> bool:
    digest = hashlib.sha256(f"{_SALT}:{dataset_id}:{job_id}".encode()).hexdigest()
    return int(digest[:8], 16) / 16**8 < HELDOUT_FRACTION


def restrict(dataset: Dataset, split: Split) -> Dataset:
    """``dataset`` with only the labels of ``split`` (the jobs all stay; the others count as unlabelled).

    A restricted dataset carries ``<dataset_id>@<split>`` as its id, so lab experiments and their approval
    are recorded per split: evidence from the tuning part can never be presented as held-out evidence."""
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r} (expected one of {', '.join(SPLITS)})")
    if split == "all":
        return dataset
    if split_of(dataset) != "all":
        raise ValueError(f"dataset {dataset.manifest.dataset_id} is already restricted to one split")
    dataset_id = dataset.manifest.dataset_id
    keep = {jid: lab for jid, lab in dataset.labels.items() if is_heldout(dataset_id, jid) == (split == "heldout")}
    unlabelled = tuple(sorted({j["id"] for j in dataset.job_dicts} - set(keep)))
    manifest = dataset.manifest.model_copy(update={"dataset_id": f"{dataset_id}@{split}"})
    return dataclasses.replace(dataset, manifest=manifest, labels=keep, unlabeled_ids=unlabelled)


def split_of(dataset: Dataset) -> Split:
    """The split a (possibly restricted) dataset holds, from its id."""
    _, _, suffix = dataset.manifest.dataset_id.rpartition("@")
    return suffix if "@" in dataset.manifest.dataset_id and suffix in SPLITS else "all"  # type: ignore[return-value]


def load_split(path: str | Path, split: Split = "all") -> Dataset:
    """Load a dataset for a run, restricted to ``split``.

    A real dataset is being labelled over time, so its unlabelled jobs are allowed (they are reported and
    left out of the metrics); a synthetic one must stay completely labelled."""
    ds = load_dataset(path, require_complete_labels=False)
    if ds.manifest.kind != "real" and ds.unlabeled_ids:
        ds = load_dataset(path)            # raises with the usual message
    return restrict(ds, split)


def describe(dataset: Dataset) -> dict[str, object]:
    """What a report states about the split its labels come from."""
    return {"name": split_of(dataset), "heldout_fraction": HELDOUT_FRACTION, "rule": RULE,
            "labels": len(dataset.labels)}
