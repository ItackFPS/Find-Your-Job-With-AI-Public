"""``export --top-scored``: enrich a blind export with the highest-scored jobs, where accepts concentrate."""

from __future__ import annotations

import json

import pytest

from src.evaluation.dataset import load_dataset
from src.evaluation.export import ExportError, export_real_dataset
from tests.test_evaluation_export import _build_db

AS_OF = "2026-10-01T00:00:00+00:00"


@pytest.fixture()
def db(tmp_path):
    return _build_db(tmp_path / "jobs.db")


def _scores(n: int = 30) -> dict[str, float]:
    return {f"id{i:03d}": (0.60 + (i % 5) * 0.01) if i % 2 else 0.30 for i in range(n)}


def test_top_scored_jobs_are_added_beyond_n_from_the_highest_scores(db, tmp_path):
    info = export_real_dataset(db, tmp_path / "o", n=6, seed=1, top_scored=5, salt="s", as_of=AS_OF)
    meta = json.loads((tmp_path / "o" / "PRIVATE_export_meta.json").read_text())
    top = {old for new, old in meta["id_map"].items() if meta["strata"][new] == "top_scored"}
    assert len(top) == 5 and info["strata_counts"]["top_scored"] == 5 and info["n_jobs"] <= 11
    scores = _scores()
    left_out = [s for jid, s in scores.items() if jid not in meta["id_map"].values()]
    assert min(scores[j] for j in top) >= max(left_out)            # nothing better was left behind
    assert meta["top_scored_requested"] == 5
    ds = load_dataset(tmp_path / "o", require_complete_labels=False)
    assert "highest production scores" in ds.manifest.notes


def test_the_sheet_stays_blind_and_the_draw_is_deterministic(db, tmp_path):
    kw = dict(n=6, seed=1, top_scored=5, salt="s", as_of=AS_OF)
    export_real_dataset(db, tmp_path / "a", **kw)
    export_real_dataset(db, tmp_path / "b", **kw)
    assert (tmp_path / "a" / "jobs.jsonl").read_bytes() == (tmp_path / "b" / "jobs.jsonl").read_bytes()
    sheet = (tmp_path / "a" / "annotations.csv").read_text(encoding="utf-8")
    assert "top_scored" not in sheet and "0.64" not in sheet


def test_default_export_is_unchanged_and_negative_is_refused(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=6, seed=1, salt="s", as_of=AS_OF)
    meta = json.loads((tmp_path / "o" / "PRIVATE_export_meta.json").read_text())
    assert "top_scored" not in meta["strata"].values() and "top_scored_requested" not in meta
    with pytest.raises(ExportError, match="top-scored"):
        export_real_dataset(db, tmp_path / "x", n=6, top_scored=-1)


def test_audit_reproduces_an_export_with_a_top_scored_stratum(db, tmp_path):
    """The audit's re-export must replay --top-scored, not count those jobs as part of the --n sample."""
    from src.evaluation.audit import audit_dataset

    export_real_dataset(db, tmp_path / "o", n=6, seed=1, top_scored=5, salt="s", as_of=AS_OF)
    checks = audit_dataset(tmp_path / "o", db=db)["checks"]
    assert checks["reproducible"]["ok"], checks["reproducible"]
