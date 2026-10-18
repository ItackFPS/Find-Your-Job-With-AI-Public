"""Orchestrator wiring tests for the LLM semantic eligibility gate.

Verifies the config toggle (matching.llm_eligibility_enabled) actually reaches
src/ai/eligibility.py::EligibilityGate during a real run_pipeline() call, the
same "production path, not just manual instantiation" discipline used by
tests/test_hard_filter_integration.py::TestOrchestratorWiresEducationHardConstraint.

Uses a minimal config dir with no sources.yaml, so no collector/network call
runs — the pipeline completes quickly with zero collected jobs, which is
enough to prove whether the eligibility gate stage was reached at all.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch



def _write_profile(config_dir: Path, content: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "profile.yaml").write_text(textwrap.dedent(content), encoding="utf-8")
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")


class TestOrchestratorWiresEligibilityGate:
    """These tests patch EligibilityGate.run (the instance method) rather
    than the whole class, since the enable/disable decision and provider
    construction now live inside EligibilityGate.run_stage() itself (see
    src/ai/eligibility.py) — the orchestrator just calls run_stage()
    unconditionally every run. Patching only .run() still proves the same
    thing the original class-level patch did: whether the gate was actually
    invoked, and with what arguments — while exercising the real run_stage()
    gating logic (construction included) for real."""

    def test_disabled_by_default_never_constructs_gate(self, tmp_path):
        """No matching.llm_eligibility_enabled key at all → gate never runs,
        even with --ai — matches every other LLM feature's opt-in default."""
        from src.ai.eligibility import EligibilityGate
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_profile(
            config_dir,
            """
            target_domains: [software_development]
            target_skills: [python]
            max_education: bac+2
            """,
        )

        with patch.object(EligibilityGate, "run") as spy_run:
            result = run_pipeline(
                db_path=str(tmp_path / "test.db"), config_dir=str(config_dir), use_ai=True,
            )

        assert result.status == "completed", f"Pipeline failed: {result.error}"
        spy_run.assert_not_called()
        assert result.stats.llm_eligibility == {}
        assert result.stats.llm_eligibility_rejected == 0

    def test_explicitly_disabled_never_constructs_gate(self, tmp_path):
        from src.ai.eligibility import EligibilityGate
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_profile(
            config_dir,
            """
            target_domains: [software_development]
            target_skills: [python]
            max_education: bac+2
            matching:
              llm_eligibility_enabled: false
            """,
        )

        with patch.object(EligibilityGate, "run") as spy_run:
            result = run_pipeline(
                db_path=str(tmp_path / "test.db"), config_dir=str(config_dir), use_ai=True,
            )

        assert result.status == "completed"
        spy_run.assert_not_called()

    def test_enabled_without_use_ai_never_constructs_gate(self, tmp_path):
        """The gate needs a running LLM server regardless of the config flag —
        --ai (use_ai) must also be passed, matching Phase 2's own gating."""
        from src.ai.eligibility import EligibilityGate
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_profile(
            config_dir,
            """
            target_domains: [software_development]
            target_skills: [python]
            max_education: bac+2
            matching:
              llm_eligibility_enabled: true
            """,
        )

        with patch.object(EligibilityGate, "run") as spy_run:
            result = run_pipeline(
                db_path=str(tmp_path / "test.db"), config_dir=str(config_dir), use_ai=False,
            )

        assert result.status == "completed"
        spy_run.assert_not_called()

    def test_enabled_with_use_ai_reaches_gate_construction_and_run(self, tmp_path):
        """The real assertion: with the config flag AND --ai, run_stage()
        constructs a real EligibilityGate and calls .run() on it — even with
        zero collected jobs (no sources.yaml here), proving the wiring
        itself, not needing a live LLM server or real jobs to pass."""
        from src.ai.eligibility import EligibilityGate
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_profile(
            config_dir,
            """
            target_domains: [software_development]
            target_skills: [python]
            max_education: bac+2
            matching:
              llm_eligibility_enabled: true
              llm_eligibility_reject_confidence: 0.8
            """,
        )

        with patch.object(EligibilityGate, "run") as spy_run:
            spy_run.return_value = ([], {}, _empty_batch_result())

            result = run_pipeline(
                db_path=str(tmp_path / "test.db"), config_dir=str(config_dir), use_ai=True,
            )

        assert result.status == "completed", f"Pipeline failed: {result.error}"
        spy_run.assert_called_once()
        # reject_confidence from profile.yaml must reach the call.
        _, kwargs = spy_run.call_args
        assert kwargs.get("reject_confidence") == 0.8

    def test_reject_confidence_defaults_to_0_75_when_unset(self, tmp_path):
        from src.ai.eligibility import EligibilityGate
        from src.pipeline.orchestrator import run_pipeline

        config_dir = tmp_path / "config"
        _write_profile(
            config_dir,
            """
            target_domains: [software_development]
            target_skills: [python]
            max_education: bac+2
            matching:
              llm_eligibility_enabled: true
            """,
        )

        with patch.object(EligibilityGate, "run") as spy_run:
            spy_run.return_value = ([], {}, _empty_batch_result())

            result = run_pipeline(
                db_path=str(tmp_path / "test.db"), config_dir=str(config_dir), use_ai=True,
            )

        assert result.status == "completed"
        _, kwargs = spy_run.call_args
        assert kwargs.get("reject_confidence") == 0.75


def _empty_batch_result():
    from src.ai.models import BatchEligibilityResult

    return BatchEligibilityResult(total_jobs=0, successful=0, failed=0, results=[])
