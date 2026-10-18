"""Append-only user feedback, outcomes and behaviour, traceable to the decision that produced them.

Every event keeps its provenance, timestamp, the ``decision_id`` and ``strategy_version`` the
user was reacting to, and its signal class. No weight is ever computed at insertion: how much
each class is worth is decided at analysis time (ADR 0009).
"""

FEEDBACK_SCHEMA_VERSION = 1
REASON_VOCAB_VERSION = 1
