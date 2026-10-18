"""Exports carry ``first_seen_at`` so a replay ages undated listings as production does (ADR 0012)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.evaluation.audit import audit_dataset
from src.evaluation.dataset import load_dataset
from src.evaluation.export import export_real_dataset
from src.evaluation.metrics import rejection_reason
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation
from src.models.job import Job
from src.storage.sqlite import Storage

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
AS_OF = "2026-10-05T00:00:00+00:00"


def _db_with_undated_copies(path: Path) -> tuple[Path, dict[str, str]]:
    """Accepted synthetic jobs stored WITHOUT a publication date, first seen long ago / recently / never before."""
    ds = load_dataset(DS)
    run = run_evaluation(ds, StrategyRef(config_dir=str(ROOT / "config")))
    accepted = [d.job_id for d in run.decisions if d.verdict == "accept"][:3]
    by_id = {j["id"]: j for j in ds.job_dicts}
    st = Storage(str(path))
    st.init_db()
    st.upsert_jobs_batch([Job(**{**by_id[i], "published_at": None}) for i in accepted])
    st.close()
    as_of = datetime.fromisoformat(AS_OF)
    seen = {accepted[0]: as_of - timedelta(days=60), accepted[1]: as_of - timedelta(days=5),
            accepted[2]: as_of + timedelta(days=1)}                 # stored only after the export's as_of
    with sqlite3.connect(path) as conn:
        for jid, at in seen.items():
            conn.execute("UPDATE jobs SET first_seen_at = ? WHERE id = ?", (at.isoformat(), jid))
    return path, {"old": accepted[0], "recent": accepted[1], "future": accepted[2]}


def test_a_replay_ages_an_undated_listing_from_its_first_sighting(tmp_path):
    db, ids = _db_with_undated_copies(tmp_path / "jobs.db")
    out = tmp_path / "export"
    export_real_dataset(db, out, n=10, seed=1, as_of=AS_OF, anonymize=False, salt="s")
    jobs = {j["id"]: j for j in load_dataset(out, require_complete_labels=False).job_dicts}
    assert "published_at" not in jobs[ids["old"]]
    assert jobs[ids["old"]]["first_seen_at"].startswith("2026-08-06")
    assert "first_seen_at" not in jobs[ids["future"]]           # not yet seen at as_of: new to us, as in production
    meta = json.loads((out / "PRIVATE_export_meta.json").read_text(encoding="utf-8"))
    assert meta["first_seen_at"] is True

    ds = load_dataset(out, require_complete_labels=False)
    run = run_evaluation(ds, StrategyRef(config_dir=str(ROOT / "config"), profile_path=str(DS / "profile.yaml")))
    verdicts = {d.job_id: d for d in run.decisions}
    assert rejection_reason(verdicts[ids["old"]]) == "filter:published_age"
    assert verdicts[ids["recent"]].verdict == "accept"
    assert verdicts[ids["future"]].verdict == "accept"

    report = audit_dataset(out, db=db)
    assert report["checks"]["reproducible"]["ok"], report["checks"]["reproducible"]


def test_first_seen_false_reproduces_the_previous_export_format(tmp_path):
    db, _ = _db_with_undated_copies(tmp_path / "jobs.db")
    out = tmp_path / "export"
    export_real_dataset(db, out, n=10, seed=1, as_of=AS_OF, anonymize=False, salt="s", first_seen=False)
    assert all("first_seen_at" not in j for j in load_dataset(out, require_complete_labels=False).job_dicts)
    report = audit_dataset(out, db=db)
    assert report["checks"]["reproducible"]["ok"], report["checks"]["reproducible"]

