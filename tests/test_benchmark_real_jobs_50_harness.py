"""Regression tests for scripts/benchmark_real_jobs_50.py's eligibility wiring.

Background: the benchmark originally called ``EligibilityGate.evaluate_batch()``
directly and re-implemented its own confidence-threshold survivor rule inline
— which meant it never exercised ``EligibilityGate.run()``, the actual
production decision path used by ``src/pipeline/orchestrator.py`` (the one
that applies the zero-evidence corroboration check and the
deterministic-modality precedence check in
``src/ai/eligibility.py::_deterministic_downgrade_reason``). A 2026-09-05
benchmark re-run based on that duplicated logic under-reported the AI path's
real recall.

These tests verify, statically (via the AST — so text inside docstrings/
comments explaining what NOT to do can never produce a false pass or false
fail), that the harness actually calls ``EligibilityGate.run()`` and never
calls ``EligibilityGate.evaluate_batch()`` anywhere in the module. This is
the cheapest robust guard against silently regressing back to a duplicated,
drifted re-implementation of the gate's consumption rule — a full live
integration run (real DB, real LLM) is exercised manually via the script
itself, not from the test suite.
"""

from __future__ import annotations

import ast
from pathlib import Path

import scripts.benchmark_real_jobs_50 as benchmark_module

_SOURCE_PATH = Path(benchmark_module.__file__)


def _parse_source() -> ast.Module:
    return ast.parse(_SOURCE_PATH.read_text(encoding="utf-8"), filename=str(_SOURCE_PATH))


def test_calls_eligibility_gate_run():
    """The harness must call `eligibility_gate.run(...)` — the production
    decision path — not just construct the gate."""
    tree = _parse_source()
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "eligibility_gate"
        ):
            found = True
            break
    assert found, (
        "scripts/benchmark_real_jobs_50.py must call eligibility_gate.run(...) "
        "so the benchmark exercises EligibilityGate's real production decision "
        "path, not a hand-rolled survivor rule."
    )


def test_never_calls_evaluate_batch_directly():
    """The harness must NOT call `.evaluate_batch(...)` anywhere — that bypasses
    the corroboration/precedence checks that only `.run()` applies. Checked via
    the AST (not a substring search) so mentioning 'evaluate_batch' in a
    docstring or comment — e.g. explaining why NOT to call it — can never
    accidentally fail this test."""
    tree = _parse_source()
    call_sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "evaluate_batch"
    ]
    assert call_sites == [], (
        "scripts/benchmark_real_jobs_50.py must not call .evaluate_batch() "
        "directly — it bypasses EligibilityGate.run()'s corroboration and "
        "deterministic-modality precedence checks."
    )


def test_uses_a_throwaway_storage_not_the_real_jobs_db():
    """`.run()` requires an AIAnalysisStorage; the harness's docstring commits
    to never touching data/jobs.db, so the storage passed to it must come from
    a temp path, not `args.db`."""
    source = _SOURCE_PATH.read_text(encoding="utf-8")
    assert "AIAnalysisStorage(db_path=str(Path(tmp_dir)" in source or "tempfile" in source, (
        "eligibility_gate.run() must be given a temp-file-backed "
        "AIAnalysisStorage, not one pointed at the real jobs database."
    )


def test_output_reports_which_eligibility_path_was_used():
    """The raw output JSON must self-report that EligibilityGate.run() was
    used, so a benchmark result file is unambiguous about its own methodology
    without needing to cross-reference the script version that produced it."""
    source = _SOURCE_PATH.read_text(encoding="utf-8")
    assert '"eligibility_consumption"' in source
    assert "EligibilityGate.run()" in source
