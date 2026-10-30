"""Held-out split (completion plan P2.4): fixed by ids before any label, stated in every report."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from src.evaluation.dataset import DatasetError, load_dataset, write_dataset
from src.evaluation.heldout import HELDOUT_FRACTION, is_heldout, load_split, restrict, split_of
from src.evaluation.models import LabelRecord, StrategyRef
from src.evaluation.report import render_markdown
from src.evaluation.runner import run_evaluation

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


def _label(jid: str) -> LabelRecord:
    return LabelRecord(job_id=jid, expected="reject", label_source="human", labeler="t", labeled_at="2026-10-30T00:00:00+00:00")


def _real(tmp_path: Path, n: int, labelled: int) -> Path:
    jobs = [{"id": f"j{i:04d}", "title": "Data analyst", "company": "Acme"} for i in range(n)]
    write_dataset(tmp_path / "real", dataset_id="real-t", version="1", kind="real", created_at="2026-10-17T00:00:00+00:00",
                  as_of="2026-10-17T00:00:00+00:00", jobs=jobs, labels=[_label(j["id"]) for j in jobs[:labelled]])
    return tmp_path / "real"


def test_the_split_depends_on_ids_only_and_holds_out_about_30_percent():
    ids = [f"job{i}" for i in range(3000)]
    share = sum(is_heldout("real-v2", j) for j in ids) / len(ids)
    assert abs(share - HELDOUT_FRACTION) < 0.03
    assert [is_heldout("real-v2", j) for j in ids[:50]] == [is_heldout("real-v2", j) for j in ids[:50]]
    assert [is_heldout("real-v2", j) for j in ids[:200]] != [is_heldout("real-v3", j) for j in ids[:200]]


def test_heldout_and_tuning_partition_the_labels_and_keep_every_job():
    ds = load_dataset(DS)
    held, tune = restrict(ds, "heldout"), restrict(ds, "tuning")
    assert set(held.labels) | set(tune.labels) == set(ds.labels) and not set(held.labels) & set(tune.labels)
    assert held.job_dicts == tune.job_dicts == ds.job_dicts
    assert (held.manifest.dataset_id, split_of(held), split_of(ds)) == (f"{ds.manifest.dataset_id}@heldout", "heldout", "all")
    assert restrict(ds, "all") is ds
    with pytest.raises(ValueError):
        restrict(held, "tuning")
    with pytest.raises(ValueError):
        restrict(ds, "test")         # type: ignore[arg-type]


def test_labelling_more_never_moves_a_job_between_parts(tmp_path):
    few = restrict(load_split(_real(tmp_path / "a", 200, 40)), "heldout")
    more = restrict(load_split(_real(tmp_path / "b", 200, 200)), "heldout")
    assert set(few.labels) <= set(more.labels)


def test_a_real_dataset_may_be_partly_labelled_a_synthetic_one_may_not(tmp_path):
    assert len(load_split(_real(tmp_path, 20, 5), "all").labels) == 5
    ds = load_dataset(DS)
    synth = tmp_path / "synth"
    write_dataset(synth, dataset_id="s", version="1", kind="synthetic", created_at=ds.manifest.created_at,
                  as_of=ds.manifest.as_of, jobs=ds.job_dicts, labels=list(ds.labels.values())[:3])
    with pytest.raises(DatasetError):
        load_split(synth)


def test_every_run_states_its_split():
    ds = restrict(load_dataset(DS), "heldout")
    run = run_evaluation(ds, StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(ds.profile_path)))
    assert run.metrics["split"]["name"] == "heldout" and run.metrics["split"]["labels"] == len(ds.labels)
    assert run.metrics["n_labelled"] == len(ds.labels)
    assert "`heldout` split" in render_markdown(run)
    whole = run_evaluation(load_dataset(DS), StrategyRef(config_dir=str(ROOT / "config")))
    assert whole.metrics["split"]["name"] == "all"


def test_the_lab_defaults_to_the_tuning_part_of_a_real_dataset(tmp_path):
    from src.lab.__main__ import _split

    real = load_split(_real(tmp_path, 50, 50))
    assert split_of(_split(real, None)) == "tuning"
    assert split_of(_split(real, "heldout")) == "heldout"
    synth = load_dataset(DS)
    assert _split(synth, None) is synth
    assert split_of(_split(dataclasses.replace(synth), "tuning")) == "tuning"
