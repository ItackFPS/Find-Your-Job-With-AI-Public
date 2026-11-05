"""Where personal data lives: every SQLite table the application creates, classified (ADR 0015).

The four in-app privacy actions (pause, reset, erase my activity, delete all) decide what to keep or
delete from this registry, and ``tests/test_data_classification.py`` fails on any table created in
``src/`` that is not listed here. Adding a table therefore means classifying it.

Classes (ADR 0014 planes, ADR 0015):

* ``personal``    — what the user did or said (plane P). Deleted by "Erase my activity".
* ``derived``     — computed from personal data (plane P). Cleared by "Reset learning", deleted by
  "Erase my activity".
* ``control``     — erasure bookkeeping and application state; counts and opaque ids only. Never in
  an erasure's scope.
* ``public``      — collected offers and what the engine computed from them (planes E, M). Kept by
  "Erase my activity".
* ``operational`` — runs, schedules, sources, jobs of the application itself (plane O). Kept by
  "Erase my activity".

A table of class ``public`` or ``operational`` may still hold personal **columns** (the application
tracker in ``jobs.application_status``, a user stance copied into a decision). They are listed in
``PERSONAL_COLUMNS`` and are set to NULL, never deleted with their row.
"""

from __future__ import annotations

from typing import Literal

DataClass = Literal["personal", "derived", "control", "public", "operational"]

# Tables of jobs.db (the application database).
JOBS_DB_TABLES: dict[str, DataClass] = {
    # -- personal: the user's own actions and answers
    "feedback_events": "personal",          # explicit, behaviour, outcome journal (ADR 0009)
    "outcome_events": "personal",           # legacy outcome tracking (viewed, apply link, status)
    "job_feedback": "personal",             # legacy personalization feedback (and its bridge rows)
    "exposure_log": "personal",             # visible exposures (ADR 0016)
    # -- derived from personal data
    "personalization_score_cache": "derived",
    "personal_model_state": "derived",      # personal model summary (ADR 0018), rebuilt from the journal
    "ranking_shadow": "derived",            # production vs challenger positions per run (ADR 0019)
    "shadow_positive": "derived",           # one anchored positive per offer, written once (ADR 0019)
    "personal_health": "derived",           # activation state, decision and monitor (ADR 0019)
    # -- control
    "db_migrations": "control",
    "app_state": "control",                # data epoch, erasure generation, learning switches
    "erasure_jobs": "control",             # erasure state machine: counts, paths, checks (ADR 0015)
    # -- public: offers and what the engine computed from them
    "jobs": "public",
    "job_fts": "public",                    # FTS5 index over jobs (and its shadow tables below)
    "job_fts_config": "public",
    "job_fts_content": "public",
    "job_fts_data": "public",
    "job_fts_docsize": "public",
    "job_fts_idx": "public",
    "job_embeddings": "public",
    "job_scores": "public",
    "job_rejections": "public",
    "job_snapshots": "public",
    "job_decisions": "public",
    "job_provenance": "public",
    "strategy_versions": "public",
    "ai_analysis": "public",
    "linkedin_staging": "public",
    "hellowork_staging": "public",
    # -- operational
    "pipeline_runs": "operational",
    "ops_metrics_daily": "operational",    # per-source daily metrics (ADR 0025)
    "ops_alerts": "operational",           # detector alerts (ADR 0025)
    "source_checkpoints": "operational",
    "source_breakers": "operational",
    "sources": "operational",
    "schedule_definitions": "operational",
    "schedule_executions": "operational",
    "linkedin_scan_runs": "operational",
    "linkedin_scan_checkpoint": "operational",
    "hellowork_scan_runs": "operational",
    "hellowork_scan_checkpoint": "operational",
    "quality_jobs": "operational",
}

# Personal columns inside non-personal tables: set to NULL by "Erase my activity".
# ``jobs.application_status`` is the application tracker; the user may choose to keep it.
PERSONAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "jobs": ("application_status",),
    # what the personal model (in shadow) would have applied, derived from the journal (ADR 0018)
    "job_scores": ("personal_adjustment", "personal_model_version", "visible_position"),
}

# Cleared by "Reset learning" (v5 §4.12): derived learned state plus the legacy learning tables.
# The journal, exposure log, tracker and stored scores are kept; learning ignores pre-marker signals.
RESET_CLEARED_TABLES: tuple[str, ...] = (
    "personalization_score_cache",
    "personal_model_state",
    "ranking_shadow",
    "shadow_positive",
    "personal_health",
    "job_feedback",
    "outcome_events",
)

# Separate read-only or developer databases. They never hold application-user activity, except the
# lab, whose results may hold per-offer content derived from labels (ADR 0015, lab purge rules).
OTHER_DB_TABLES: dict[str, dict[str, DataClass]] = {
    "esco": {
        "esco_concepts": "public",
        "esco_fts": "public",
    },
    "lab": {
        "hypotheses": "operational",
        "candidate_strategies": "operational",
        "experiments": "operational",
        "approvals": "operational",
        "active_strategy": "operational",
        "agent_runs": "operational",
        "memory_items": "operational",
        "memory_status": "operational",
    },
}

# Files (relative to the data folder) that hold personal activity outside jobs.db.
PERSONAL_FILE_GLOBS: tuple[str, ...] = (
    "calibration/**/*",                     # Quality › Calibration answers and their backups
)


def all_classified_tables() -> dict[str, DataClass]:
    """Every classified table name, across databases."""
    out: dict[str, DataClass] = dict(JOBS_DB_TABLES)
    for tables in OTHER_DB_TABLES.values():
        out.update(tables)
    return out


def tables_of_class(*classes: DataClass) -> list[str]:
    """jobs.db tables of the given classes, sorted."""
    return sorted(t for t, c in JOBS_DB_TABLES.items() if c in classes)
