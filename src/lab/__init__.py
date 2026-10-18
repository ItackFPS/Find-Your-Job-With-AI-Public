"""Experiment Lab (P5): human-driven hypothesis → experiment → verdict → candidate → approval.

Never imported by the production pipeline. Strategies are *config bundles* (data, never code);
the lab database ``lab.db`` is separate from ``jobs.db``. The ONLY code path that writes
``jobs.db`` is ``promotion.approve`` / ``promotion.rollback`` (explicit human action).
"""

LAB_SCHEMA_VERSION = 1
