"""Tests for full-pipeline item-level progress reporting.

Covers the optional ``progress_callback`` parameter added to every stage
function (validate_jobs, deduplicate_jobs, filter_jobs, gate_jobs,
score_jobs_with_breakdown, apply_semantic_matching, apply_personalization)
and the orchestrator's ``_stage_reporter``/``stage_progress`` plumbing.

The central guarantee under test throughout this file: passing a
progress_callback must never change a single filtering/scoring/ranking
decision or output value — every test that exercises a real stage function
asserts the callback-attached run produces byte-identical results to the
no-callback run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.models.job import Job
from tests.fixtures.profiles import TEST_PROFILE_PATH

_NOW = datetime.now(UTC)


def _job(job_id: str, **overrides) -> Job:
    defaults = dict(
        source="test_source",
        source_job_id=f"src-{job_id}",
        title="Junior Python Developer",
        company="Acme",
        description="Python development role for a junior engineer.",
        location="Paris, France",
        countries=["France"],
        remote=True,
        employment_type="full-time",
        published_at=_NOW - timedelta(days=1),
    )
    defaults.update(overrides)
    return Job(id=job_id, **defaults)


def _make_jobs(n: int) -> list[Job]:
    # Distinct company per job so cross_source_deduplicate's content-identity
    # tier (company_norm, title_norm, location_norm, employment_type) never
    # collapses these into fewer jobs than requested — these are meant to be
    # n distinct postings, not n copies of the same one.
    return [_job(f"j{i}", company=f"Acme{i}") for i in range(n)]


# ---------------------------------------------------------------------------
# orchestrator._stage_reporter — cadence/throttling unit tests
# ---------------------------------------------------------------------------

class TestStageReporterHelper:
    def test_none_callback_returns_noop(self):
        from src.pipeline.orchestrator import PipelineStats, _stage_reporter

        stats = PipelineStats()
        reporter = _stage_reporter(None, stats, "validate", 100)
        reporter(50)  # must not raise
        assert stats.stage_progress == {}

    def test_zero_total_returns_noop(self):
        from src.pipeline.orchestrator import PipelineStats, _stage_reporter

        calls = []
        stats = PipelineStats()
        reporter = _stage_reporter(lambda stage, s: calls.append(stage), stats, "validate", 0)
        reporter(1)
        assert calls == []

    def test_reports_final_item_even_if_throttled(self):
        from src.pipeline.orchestrator import PipelineStats, _stage_reporter

        calls: list[dict] = []
        stats = PipelineStats()

        def cb(stage, stats_dict):
            calls.append(stats_dict["stage_progress"])

        reporter = _stage_reporter(cb, stats, "score", 1000)
        for i in range(1, 1001):
            reporter(i)

        assert len(calls) > 0
        assert calls[-1] == {"stage": "score", "processed": 1000, "total": 1000}
        # Cadence must not flood: far fewer than 1000 calls for 1000 items.
        assert len(calls) < 100

    def test_small_total_still_gets_reported(self):
        """A tiny stage (e.g. 3 jobs) must not be silently skipped."""
        from src.pipeline.orchestrator import PipelineStats, _stage_reporter

        calls: list[dict] = []
        stats = PipelineStats()
        reporter = _stage_reporter(lambda s, d: calls.append(d["stage_progress"]), stats, "gate", 3)
        for i in (1, 2, 3):
            reporter(i)
        assert calls[-1]["processed"] == 3
        assert calls[-1]["total"] == 3

    def test_pipeline_stats_to_dict_includes_stage_progress(self):
        from src.pipeline.orchestrator import PipelineStats

        stats = PipelineStats()
        assert "stage_progress" in stats.to_dict()
        stats.stage_progress = {"stage": "filter", "processed": 5, "total": 10}
        assert stats.to_dict()["stage_progress"] == {"stage": "filter", "processed": 5, "total": 10}


# ---------------------------------------------------------------------------
# Per-stage: callback observed correctly AND output unaffected
# ---------------------------------------------------------------------------

class TestValidateJobsProgress:
    def test_callback_reaches_total_and_output_unchanged(self):
        from src.pipeline.validate import validate_jobs

        jobs = _make_jobs(7)
        without_cb = validate_jobs(jobs)

        events: list[tuple[int, int]] = []
        with_cb = validate_jobs(jobs, progress_callback=lambda p, t: events.append((p, t)))

        assert with_cb == without_cb
        assert events[-1] == (7, 7)
        assert [p for p, _ in events] == sorted(p for p, _ in events)  # monotonic


class TestDeduplicateProgress:
    def test_intra_source_deduplicate_progress(self):
        from src.pipeline.deduplicate import intra_source_deduplicate

        jobs = _make_jobs(6)
        without_cb = intra_source_deduplicate(jobs)
        events: list[tuple[int, int]] = []
        with_cb = intra_source_deduplicate(jobs, progress_callback=lambda p, t: events.append((p, t)))

        assert [j.id for j in with_cb] == [j.id for j in without_cb]
        assert events[-1] == (6, 6)

    def test_cross_source_deduplicate_progress(self):
        from src.pipeline.deduplicate import cross_source_deduplicate

        jobs = _make_jobs(5)
        without_cb = cross_source_deduplicate(jobs)
        events: list[tuple[int, int]] = []
        with_cb = cross_source_deduplicate(jobs, progress_callback=lambda p, t: events.append((p, t)))

        assert [j.id for j in with_cb] == [j.id for j in without_cb]
        assert events[-1] == (5, 5)

    def test_deduplicate_jobs_progress_is_monotonic_across_both_phases(self):
        """deduplicate_jobs runs two internal passes (intra then cross) — the
        combined progress must never reset/decrease between them."""
        from src.pipeline.deduplicate import deduplicate_jobs

        # A cross-source duplicate pair so both phases do real work.
        j1 = _job("dup1", source="board_a", title="Backend Engineer", company="Acme",
                   location="Paris, France", employment_type="full-time")
        j2 = _job("dup2", source="board_b", title="Backend Engineer", company="Acme",
                   location="Paris, France", employment_type="full-time")
        jobs = [j1, j2] + _make_jobs(4)

        without_cb = deduplicate_jobs(list(jobs))
        events: list[tuple[int, int]] = []
        with_cb = deduplicate_jobs(list(jobs), progress_callback=lambda p, t: events.append((p, t)))

        assert with_cb[1] == without_cb[1]  # same number of duplicates removed
        assert [j.id for j in with_cb[0]] == [j.id for j in without_cb[0]]
        processed_values = [p for p, _ in events]
        assert processed_values == sorted(processed_values), "processed must never decrease"
        assert events[-1][0] == events[-1][1]  # final call reaches its own total


class TestFilterJobsProgress:
    def test_callback_reaches_total_and_output_unchanged(self):
        from src.pipeline.filter import filter_jobs, load_filter_config

        cfg = load_filter_config(TEST_PROFILE_PATH)
        jobs = _make_jobs(5)
        without_cb = filter_jobs(jobs, cfg)
        events: list[tuple[int, int]] = []
        with_cb = filter_jobs(jobs, cfg, progress_callback=lambda p, t: events.append((p, t)))

        assert with_cb == without_cb
        assert events[-1] == (5, 5)


class TestGateJobsProgress:
    def test_callback_reaches_total_and_output_unchanged(self):
        from src.requirements.gate import HardConstraintGate, gate_jobs
        from src.requirements.models import JobRequirements

        jobs = _make_jobs(4)
        requirements_map = {j.id: JobRequirements() for j in jobs}
        gate = HardConstraintGate(user_max_education_level=5, profile_version_hash="h1")

        without_cb = gate_jobs(jobs, requirements_map, gate)
        events: list[tuple[int, int]] = []
        with_cb = gate_jobs(
            jobs, requirements_map, gate, progress_callback=lambda p, t: events.append((p, t))
        )

        assert [j.id for j in with_cb[0]] == [j.id for j in without_cb[0]]
        assert events[-1] == (4, 4)


def _normalized_breakdown(result):
    """Sort by job id and round floats, so two independent calls a few ms
    apart compare equal despite scoring's pre-existing (unrelated to this
    feature) dependence on wall-clock time via the recency factor, and
    despite the final .sort() being a stable sort over near-tied scores
    (identical inputs here can differ in the 10th decimal between calls,
    which is enough to flip tie-break order but not the actual outcome)."""
    return sorted(
        (
            (job.id, round(score, 6), {k: round(v, 6) for k, v in factors.items()})
            for job, score, factors in result
        ),
        key=lambda t: t[0],
    )


class TestScoreJobsWithBreakdownProgress:
    def test_callback_reaches_total_and_output_unchanged(self):
        from src.pipeline.scoring import load_scoring_config, score_jobs_with_breakdown

        cfg = load_scoring_config("config/scoring.yaml")
        jobs = _make_jobs(6)
        without_cb = score_jobs_with_breakdown(jobs, cfg)
        events: list[tuple[int, int]] = []
        with_cb = score_jobs_with_breakdown(
            jobs, cfg, progress_callback=lambda p, t: events.append((p, t))
        )

        assert _normalized_breakdown(without_cb) == _normalized_breakdown(with_cb)
        assert events[-1] == (6, 6)


class TestPersonalizationProgress:
    def test_callback_reaches_total_and_output_unchanged(self, tmp_path):
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import apply_personalization
        from src.personalization.storage import PersonalizationStorage

        jobs = _make_jobs(5)
        ranked = [(job, 0.5) for job in jobs]
        cfg = load_personalization_config("config/personalization.yaml")

        db1 = str(tmp_path / "p1.db")
        s1 = PersonalizationStorage(db_path=db1)
        s1.init_db()
        without_cb = apply_personalization(ranked, s1, cfg)
        s1.close()

        db2 = str(tmp_path / "p2.db")
        s2 = PersonalizationStorage(db_path=db2)
        s2.init_db()
        events: list[tuple[int, int]] = []
        with_cb = apply_personalization(
            ranked, s2, cfg, progress_callback=lambda p, t: events.append((p, t))
        )
        s2.close()

        assert [j.id for j, _ in with_cb[0]] == [j.id for j, _ in without_cb[0]]
        assert [round(s, 6) for _, s in with_cb[0]] == [round(s, 6) for _, s in without_cb[0]]
        assert events[-1] == (5, 5)


class TestSemanticMatchingProgress:
    """Uses the real local embedding model (no network) — same setup already
    used by tests/test_e2e_controlled_pipeline.py's semantic matching tests."""

    def test_callback_reaches_total_and_output_unchanged(self):
        from src.embeddings.embedding import EmbeddingService
        from src.pipeline.semantic_matching import (
            apply_semantic_matching,
            build_profile_embedding,
            load_semantic_config,
        )

        jobs = _make_jobs(4)
        sem_cfg = load_semantic_config("config/semantic_config.yaml")
        svc = EmbeddingService(
            model_name=sem_cfg["embedding"]["model_name"],
            cache_max_entries=sem_cfg["embedding"]["cache_max_entries"],
            device=sem_cfg["embedding"].get("device", "auto"),
        )
        profile_info = {
            "seniority": "junior",
            "employment_type": "full-time",
            "location": "France",
            "languages": ["French", "English"],
            "skills": ["Python"],
            "responsibilities": "development",
        }
        profile_emb = build_profile_embedding(profile_info, svc)

        without_cb, _ = apply_semantic_matching(jobs, profile_emb, sem_cfg, svc)
        events: list[tuple[int, int]] = []
        with_cb, _ = apply_semantic_matching(
            jobs, profile_emb, sem_cfg, svc, progress_callback=lambda p, t: events.append((p, t))
        )

        assert [j.id for j, _ in with_cb] == [j.id for j, _ in without_cb]
        assert [round(s, 6) for _, s in with_cb] == [round(s, 6) for _, s in without_cb]
        assert events[-1] == (len(jobs), len(jobs))


# ---------------------------------------------------------------------------
# Full chained end-to-end run — mirrors
# tests/test_e2e_controlled_pipeline.py::TestFullE2EPipeline but wires a
# progress_callback through every stage and asserts events were actually
# emitted for each one, with the documented pipeline outcome unchanged.
# ---------------------------------------------------------------------------

class TestFullPipelineProgressE2E:
    def test_progress_emitted_for_every_stage(self, tmp_path):
        from src.embeddings.embedding import EmbeddingService
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import apply_personalization
        from src.personalization.storage import PersonalizationStorage
        from src.pipeline.deduplicate import deduplicate_jobs
        from src.pipeline.filter import filter_jobs, load_filter_config
        from src.pipeline.scoring import load_scoring_config, score_jobs_with_breakdown
        from src.pipeline.semantic_matching import (
            apply_semantic_matching,
            build_profile_embedding,
            integrate_semantic_into_ranking,
            load_semantic_config,
        )
        from src.pipeline.validate import validate_jobs
        from src.requirements.gate import HardConstraintGate, gate_jobs
        from src.requirements.models import JobRequirements

        events: list[tuple[str, int, int]] = []

        def track(stage: str):
            return lambda p, t: events.append((stage, p, t))

        jobs = _make_jobs(6)

        valid, _ = validate_jobs(jobs, progress_callback=track("validate"))
        assert len(valid) == 6

        deduped, _ = deduplicate_jobs(valid, progress_callback=track("deduplicate"))
        assert len(deduped) == 6

        flt_cfg = load_filter_config(TEST_PROFILE_PATH)
        filtered, _ = filter_jobs(deduped, flt_cfg, progress_callback=track("filter"))
        assert len(filtered) > 0

        requirements_map = {j.id: JobRequirements() for j in filtered}
        gate = HardConstraintGate(user_max_education_level=5, profile_version_hash="h1")
        gated, _ = gate_jobs(filtered, requirements_map, gate, progress_callback=track("requirements_gate"))
        assert len(gated) == len(filtered)

        sc_cfg = load_scoring_config("config/scoring.yaml")
        scored = score_jobs_with_breakdown(gated, sc_cfg, progress_callback=track("score"))
        assert len(scored) == len(gated)
        scored_pairs = [(job, score) for job, score, _ in scored]

        sem_cfg = load_semantic_config("config/semantic_config.yaml")
        svc = EmbeddingService(
            model_name=sem_cfg["embedding"]["model_name"],
            cache_max_entries=sem_cfg["embedding"]["cache_max_entries"],
            device=sem_cfg["embedding"].get("device", "auto"),
        )
        profile_info = {
            "seniority": "junior", "employment_type": "full-time", "location": "France",
            "languages": ["French", "English"], "skills": ["Python"], "responsibilities": "development",
        }
        profile_emb = build_profile_embedding(profile_info, svc)
        jobs_only = [job for job, _ in scored_pairs]
        semantic_results, _ = apply_semantic_matching(
            jobs_only, profile_emb, sem_cfg, svc, progress_callback=track("semantic")
        )
        final_ranked = integrate_semantic_into_ranking(scored_pairs, semantic_results, sem_cfg)

        p_storage = PersonalizationStorage(db_path=str(tmp_path / "e2e.db"))
        p_storage.init_db()
        p_cfg = load_personalization_config("config/personalization.yaml")
        personalized, _ = apply_personalization(
            final_ranked, p_storage, p_cfg, progress_callback=track("personalize")
        )
        p_storage.close()
        assert len(personalized) == len(final_ranked)

        # Every stage must have reported progress, and each must finish at
        # its own total (processed == total on the last event for that stage).
        seen_stages = {s for s, _, _ in events}
        expected_stages = {
            "validate", "deduplicate", "filter", "requirements_gate", "score",
            "semantic", "personalize",
        }
        assert expected_stages <= seen_stages, f"Missing stage progress: {expected_stages - seen_stages}"

        for stage in expected_stages:
            stage_events = [(p, t) for s, p, t in events if s == stage]
            assert stage_events, f"No events for stage {stage}"
            last_p, last_t = stage_events[-1]
            assert last_p == last_t, f"Stage {stage} did not finish at its total: {last_p}/{last_t}"
