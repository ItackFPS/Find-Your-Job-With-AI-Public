"""The rejected-job sample never counts a duplicate of a surfaced listing as a missed opportunity."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.decisions.storage import DecisionStorage
from src.evaluation.dataset import load_dataset
from src.evaluation.export import export_real_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation
from src.models.job import Job
from src.storage.sqlite import Storage
from tests.test_evaluation_export_rejected import _reject

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


def test_duplicates_of_a_surfaced_listing_are_not_missed_opportunities(tmp_path):
    db, ids = _db_with_undated_copies(tmp_path / "jobs.db")
    ds = DecisionStorage(str(db))
    ds.init_db()
    ds.close()
    with sqlite3.connect(db) as conn:
        stored_hash = "h-stored"            # the pipeline stores normalisation's dedup key with every job
        conn.execute("UPDATE jobs SET content_hash = ? WHERE id = ?", (stored_hash, ids["old"]))
        for jid, h in (("copy_of_stored", stored_hash), ("twin_a", "h-twin"), ("twin_b", "h-twin"),
                       ("unique", "h-unique")):
            _reject(conn, jid, "filter", at="2026-10-01T00:00:00+00:00")
            conn.execute("UPDATE job_decisions SET content_hash = ? WHERE job_id = ?", (h, jid))
    info = export_real_dataset(db, tmp_path / "o", n=10, seed=1, rejected=10, salt="s", as_of=AS_OF,
                               anonymize=False)
    meta = json.loads((tmp_path / "o" / "PRIVATE_export_meta.json").read_text(encoding="utf-8"))
    rejected = {old for new, old in meta["id_map"].items() if meta["strata"][new] == "pipeline_rejected"}
    assert rejected == {"twin_a", "unique"} and info["rejected_pool"] == 2
