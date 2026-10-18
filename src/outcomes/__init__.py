"""I18 — outcome-tracking instrumentation.

A small, local-first, explicit event log: it records genuine user actions
the product already supports (viewing a job's detail page, opening its
original listing, changing its application status) so a future experiment
can consume real outcomes. It never infers intent from absence of
interaction, never trains anything, and never changes scoring/ranking.
"""
