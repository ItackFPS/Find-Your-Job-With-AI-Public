"""Held-out protection (owner decision D4, Phase B B3-H): tuning by default; held-out labels only for a
named pre-registered candidate; every use logged; no held-out job decision in a written result."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.evaluation import heldout_guard as hg
from src.evaluation.dataset import load_dataset, write_dataset
from src.evaluation.heldout import is_heldout

ROOT = Path(__file__).resolve().parents[1]
SYNTH = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
PREREGISTERED = "2026-10-30-h1-distance-first-in-order"


@pytest.fixture()
def real(tmp_path) -> Path:
    ds = load_dataset(SYNTH)
    dest = tmp_path / "real-t"
    write_dataset(dest, dataset_id="real-t", version="1", kind="real", created_at=ds.manifest.created_at,
                  as_of=ds.manifest.as_of, jobs=ds.job_dicts, labels=list(ds.labels.values()),
                  profile_yaml=ds.profile_path.read_bytes())
    return dest


def test_rules() -> None:
    hg.authorise("synthetic", "all", None)                 # nothing to protect
    hg.authorise("synthetic", "heldout", None)
    hg.authorise("real", "tuning", None)
    for split in ("heldout", "all"):
        with pytest.raises(hg.HeldoutRefused, match="pre-registered"):
            hg.authorise("real", split, None)
        with pytest.raises(hg.HeldoutRefused, match="not a pre-registered"):
            hg.authorise("real", split, "made-up")
        hg.authorise("real", split, PREREGISTERED)
    assert PREREGISTERED in hg.preregistered_ids()
    assert hg.preregistered_ids(Path("/nonexistent/dir")) == set()


def test_the_usage_log_only_grows(real) -> None:
    ds = load_dataset(real, require_complete_labels=False)
    hg.log_use(real, ds, "heldout", PREREGISTERED, "cli", "evaluation run")
    hg.log_use(real, ds, "all", PREREGISTERED, "lab", "lab run x")
    lines = hg.read_log(real)
    assert [x["caller"] for x in lines] == ["cli", "lab"]
    assert lines[0]["dataset_id"] == "real-t" and lines[0]["labels_sha256"] == ds.manifest.labels_sha256
    with pytest.raises(ValueError):
        hg.log_use(real, ds, "heldout", PREREGISTERED, "someone", "x")


def _cli(*args: str) -> int:
    from src.evaluation.__main__ import main

    return main(list(args))


def test_cli_run_measures_tuning_by_default_and_logs_nothing(real, tmp_path, capsys) -> None:
    assert _cli("run", str(real), "--out", str(tmp_path / "runs")) == 0
    written = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert written["dataset"]["id"] == "real-t@tuning"
    assert hg.read_log(real) == []


def test_cli_run_refuses_held_out_without_a_candidate(real, tmp_path, capsys) -> None:
    for split in ("heldout", "all"):
        assert _cli("run", str(real), "--split", split, "--out", str(tmp_path / "runs")) != 0
    assert "pre-registered" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists() and hg.read_log(real) == []


def test_cli_held_out_run_is_logged_and_writes_aggregates_only(real, tmp_path) -> None:
    assert _cli("run", str(real), "--split", "heldout", "--candidate", PREREGISTERED,
                "--out", str(tmp_path / "runs")) == 0
    log = hg.read_log(real)
    assert len(log) == 1 and log[0]["split"] == "heldout" and log[0]["candidate"] == PREREGISTERED
    written = json.loads(next((tmp_path / "runs").glob("*.json")).read_text(encoding="utf-8"))
    assert written["decisions"], "tuning jobs' decisions stay"
    assert not any(is_heldout("real-t", d["job_id"]) for d in written["decisions"])
    assert any("aggregates only" in w for w in written["warnings"])
    assert written["metrics"]["overall"]                    # metrics kept


def test_compare_classifies_flips_on_tuning_labels_by_default(real, tmp_path, capsys) -> None:
    out = tmp_path / "runs"
    assert _cli("run", str(real), "--out", str(out)) == 0
    assert _cli("run", str(real), "--out", str(out)) == 0
    a, b = sorted(out.glob("*.json"))
    assert _cli("compare", str(a), str(b), "--dataset", str(real)) == 0
    assert _cli("compare", str(a), str(b), "--dataset", str(real), "--split", "all") != 0
    assert hg.read_log(real) == []


def test_lab_split_guard(real) -> None:
    from src.lab.__main__ import _split

    ds = load_dataset(real, require_complete_labels=False)
    assert _split(ds, None).manifest.dataset_id == "real-t@tuning"
    with pytest.raises(hg.HeldoutRefused):
        _split(ds, "heldout")
    assert _split(ds, "heldout", candidate=PREREGISTERED, what="lab run c1").manifest.dataset_id == "real-t@heldout"
    assert [x["caller"] for x in hg.read_log(real)] == ["lab"]
