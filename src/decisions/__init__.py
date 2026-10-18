"""Decision journal: one auditable record per job per run (accepted AND rejected).

Observability only — recording never changes a decision, a score or a ranking, and
never raises into the pipeline (fail-open). See ``docs/adr/0008-decision-journal.md``.
"""

DECISION_SCHEMA_VERSION = 1
