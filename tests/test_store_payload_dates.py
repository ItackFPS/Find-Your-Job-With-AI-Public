"""An offer whose raw payload still holds parsed dates is stored, not dropped with its whole batch
(split from #192, Phase B B2-D)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from src.models.job import Job
from src.storage.sqlite import Storage


@pytest.mark.parametrize("method", ["upsert_job", "upsert_jobs_batch"])
def test_parsed_values_in_the_raw_payload_are_stored_as_text(tmp_path: Path, method: str) -> None:
    raw = {"published_at": datetime(2026, 11, 1, 10, 0, tzinfo=UTC), "valid": date(2026, 12, 1),
           "salary": Decimal("2500.50"), "title": "Technicien"}
    job = Job(id="j", source="s", title="Technicien", description="d", raw_payload=raw)
    st = Storage(db_path=str(tmp_path / "t.db"))
    st.init_db()
    try:
        if method == "upsert_job":
            st.upsert_job(job)
        else:
            assert st.upsert_jobs_batch([job])["inserted"] == 1
    finally:
        st.close()
    with sqlite3.connect(tmp_path / "t.db") as conn:
        stored = conn.execute("SELECT raw_payload FROM jobs WHERE id = 'j'").fetchone()[0]
    assert '"published_at": "2026-11-01T10:00:00+00:00"' in stored
    assert '"valid": "2026-12-01"' in stored
    assert '"salary": "2500.50"' in stored


def test_plain_payloads_are_stored_exactly_as_before(tmp_path: Path) -> None:
    import json

    raw = {"title": "Technicien", "n": 3, "tags": ["a", "é"], "nested": {"x": None}}
    st = Storage(db_path=str(tmp_path / "t.db"))
    st.init_db()
    try:
        st.upsert_jobs_batch([Job(id="j", source="s", title="T", description="d", raw_payload=raw)])
    finally:
        st.close()
    with sqlite3.connect(tmp_path / "t.db") as conn:
        stored = conn.execute("SELECT raw_payload FROM jobs WHERE id = 'j'").fetchone()[0]
    assert stored == json.dumps(raw)
