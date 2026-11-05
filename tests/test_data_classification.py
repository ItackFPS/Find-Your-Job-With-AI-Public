"""Every table the application creates is classified for the privacy actions (ADR 0015).

Two checks: a static one over the DDL in ``src/`` (catches a table no test initialises), and a
dynamic one over a temporary jobs.db initialised by every storage class (catches FTS shadow tables
and anything created without a literal ``CREATE TABLE``).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from src.storage import data_classes as dc

SRC = Path(__file__).resolve().parents[1] / "src"
_DDL = re.compile(r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(|USING)", re.I)


def _ddl_tables() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in SRC.rglob("*.py"):
        for name in _DDL.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name, []).append(str(path.relative_to(SRC)))
    return found


def test_every_table_created_in_src_is_classified():
    classified = dc.all_classified_tables()
    missing = {t: files for t, files in _ddl_tables().items() if t not in classified}
    assert not missing, (
        "Unclassified tables (add them to src/storage/data_classes.py with their privacy class): "
        f"{missing}")


def test_every_table_of_an_initialised_jobs_db_is_classified(tmp_path):
    from src.ai.storage import AIAnalysisStorage
    from src.decisions.storage import DecisionStorage
    from src.feedback.exposure import ExposureStorage
    from src.feedback.storage import FeedbackStorage
    from src.hellowork.staging import HelloWorkStagingStorage
    from src.linkedin.staging import LinkedInStagingStorage
    from src.outcomes.storage import OutcomeEventStorage
    from src.personalization.storage import PersonalizationStorage
    from src.scheduler.storage import SchedulerStorage
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    db = str(tmp_path / "jobs.db")
    for cls in (Storage, RunStorage, FeedbackStorage, ExposureStorage, DecisionStorage, OutcomeEventStorage,
                PersonalizationStorage, AIAnalysisStorage, SchedulerStorage,
                LinkedInStagingStorage, HelloWorkStagingStorage):
        try:
            store = cls(db_path=db)
        except TypeError:
            store = cls(db)
        store.init_db()
        close = getattr(store, "close", None)
        if close:
            close()
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
                  if not r[0].startswith("sqlite_")}
    finally:
        conn.close()
    unclassified = sorted(tables - set(dc.JOBS_DB_TABLES))
    assert not unclassified, f"jobs.db tables missing from JOBS_DB_TABLES: {unclassified}"


def test_personal_columns_belong_to_classified_non_personal_tables():
    for table in dc.PERSONAL_COLUMNS:
        assert dc.JOBS_DB_TABLES.get(table) in ("public", "operational"), table


def test_classes_are_known_values():
    allowed = {"personal", "derived", "control", "public", "operational"}
    assert set(dc.all_classified_tables().values()) <= allowed
    assert "feedback_events" in dc.tables_of_class("personal")
