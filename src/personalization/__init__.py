"""Phase 4: Personalized Ranking and Feedback.

This package is additive on top of the Phase 1-3 pipeline
(COLLECT -> NORMALIZE -> VALIDATE -> DEDUPLICATE -> HARD FILTERS ->
AI UNDERSTANDING -> SEMANTIC MATCHING). It never modifies job data,
never reintroduces jobs rejected by hard filters, and only nudges the
score of jobs that already made it through every earlier stage.
"""

from __future__ import annotations
