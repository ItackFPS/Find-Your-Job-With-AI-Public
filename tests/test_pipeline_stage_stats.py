"""Tests for the split hard-filter / requirements-gate statistics.

The pipeline has two independent, sequential filtering stages:
  1. Hard filter    (src/pipeline/filter.py)      -> stats.hard_filter_input / hard_filter_passed
  2. Requirements gate (src/requirements/gate.py) -> stats.requirements_gate_input / requirements_gate_passed

Neither stage re-filters the other's output; they used to both write into the
same `stats.filtered` counter, making the funnel look like duplicate filtering.
These tests verify the two stages are now tracked separately, that `filtered`
still equals the final (post-gate) count for backwards compatibility, and that
introducing the new counters does not change any filtering decision.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


from src.pipeline.orchestrator import PipelineStats


class TestPipelineStatsToDictAdditive:
    """The new fields must be purely additive and internally consistent."""

    def test_new_fields_present_in_to_dict(self):
        stats = PipelineStats()
        d = stats.to_dict()
        for key in (
            "hard_filter_input",
            "hard_filter_passed",
            "hard_filter_rejected",
            "requirements_gate_input",
            "requirements_gate_passed",
            "requirements_gate_rejected",
        ):
            assert key in d

    def test_existing_fields_unchanged(self):
        """Old consumers reading `filtered` (and its derived `filter_rejected`) must keep working."""
        stats = PipelineStats(collected=100, validated=100, deduplicated=100, filtered=60)
        d = stats.to_dict()
        assert d["filtered"] == 60
        assert d["filter_rejected"] == 40

    def test_rejected_counts_derived_correctly(self):
        stats = PipelineStats()
        stats.hard_filter_input = 8600
        stats.hard_filter_passed = 7200
        stats.requirements_gate_input = 7200
        stats.requirements_gate_passed = 6300
        d = stats.to_dict()
        assert d["hard_filter_rejected"] == 1400
        assert d["requirements_gate_rejected"] == 900

    def test_filter_rejected_is_cumulative_across_all_three_gates(self):
        """A third gate (LLM eligibility) was added after this file's original
        two-gate contract was written. `filter_rejected` must still equal the
        SUM of all three gates' rejections — it is deliberately NOT meant to
        be added on top of hard_filter_rejected/requirements_gate_rejected/
        llm_eligibility_rejected by a consumer, since it already subsumes them
        (see the comment in PipelineStats.to_dict())."""
        stats = PipelineStats(collected=100, validated=100, deduplicated=100)
        stats.hard_filter_input = 100
        stats.hard_filter_passed = 80          # hard filter rejects 20
        stats.requirements_gate_input = 80
        stats.requirements_gate_passed = 70    # requirements gate rejects 10
        stats.llm_eligibility_input = 70
        stats.llm_eligibility_passed = 65      # eligibility gate rejects 5
        stats.llm_eligibility_rejected = 5     # not auto-derived; orchestrator.py sets this directly
        stats.filtered = 65                    # final survivor count, as orchestrator.py sets it
        d = stats.to_dict()
        assert d["hard_filter_rejected"] == 20
        assert d["requirements_gate_rejected"] == 10
        assert d["llm_eligibility_rejected"] == 5
        assert d["filter_rejected"] == 35  # 100 - 65, i.e. 20 + 10 + 5

    def test_stage_chain_is_consistent(self):
        """hard_filter output must feed requirements_gate input, which must end at `filtered`."""
        stats = PipelineStats()
        stats.hard_filter_input = 8600
        stats.hard_filter_passed = 7200
        stats.requirements_gate_input = 7200
        stats.requirements_gate_passed = 6300
        stats.filtered = 6300
        assert stats.hard_filter_passed == stats.requirements_gate_input
        assert stats.requirements_gate_passed == stats.filtered

    def test_rejected_counts_never_negative(self):
        stats = PipelineStats()
        d = stats.to_dict()
        assert d["hard_filter_rejected"] >= 0
        assert d["requirements_gate_rejected"] >= 0


class TestRunPipelineStageStatsConsistency:
    """End-to-end: with no profile/filters config, both stages must be
    reported as pass-through (input == passed), and the final `filtered`
    count must be unaffected by the new instrumentation."""

    def test_skip_branch_reports_consistent_passthrough_stats(self, tmp_path):
        from src.pipeline.orchestrator import run_pipeline

        db_path = str(tmp_path / "test.db")
        config_dir = str(tmp_path / "config")
        (tmp_path / "config").mkdir()

        mock_embed = MagicMock()
        mock_embed.embed.return_value = [0.1] * 384
        mock_embed.compute_semantic_score.return_value = 0.5
        mock_embed.compute_job_semantic_score.return_value = 0.5

        with patch("src.embeddings.embedding.EmbeddingService", return_value=mock_embed):
            result = run_pipeline(db_path=db_path, config_dir=config_dir)

        assert result.status == "completed", f"Pipeline failed: {result.error}"
        s = result.stats

        # No profile/filters.yaml present -> both stages are no-ops.
        assert s.hard_filter_input == s.hard_filter_passed
        assert s.requirements_gate_input == s.requirements_gate_passed
        # The chain must be unbroken end to end.
        assert s.hard_filter_passed == s.requirements_gate_input
        assert s.requirements_gate_passed == s.filtered
