"""EXPERIMENTAL phase code (I17-I20): evaluation/observation only.

Nothing under this package is imported by src.pipeline.orchestrator,
src.api.app, or any production wiring — it can never affect eligibility,
scoring, ranking, personalization, or notifications for a real pipeline run.
Modules here are invoked explicitly (by tests or a standalone evaluation
script), the same quarantine pattern already used for
src/collectors/experimental/.
"""
