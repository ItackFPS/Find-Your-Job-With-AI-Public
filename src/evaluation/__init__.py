"""Offline evaluation harness (benchmarks, golden decisions, comparisons).

ISOLATION RULE: nothing outside this package may import ``src.evaluation``
(enforced by ``tests/test_evaluation_isolation.py``). The harness drives the
REAL production functions (``filter_jobs``, ``RequirementExtractor``,
``gate_jobs``, …) and never re-implements business logic; production never
depends on it. It performs no network access unless an LLM endpoint is
explicitly requested, and never writes to ``jobs.db``.

See ``docs/adr/0007-evaluation-harness-outside-production-path.md``.
"""

SCHEMA_VERSION = 1
