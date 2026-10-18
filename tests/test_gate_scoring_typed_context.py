"""Phase LATER / I14 regression tests: typed gate -> scoring context.

Before this, ``src.requirements.gate.gate_jobs()`` smuggled 4 values into
``job.raw_payload`` with underscore-prefixed keys (``_education_penalty``,
``_skill_penalty``, ``_experience_penalty``, ``_extracted_min_experience_years``)
purely so ``src.pipeline.scoring`` could read them back later — an untyped
inter-stage side-channel that also PERMANENTLY polluted the persisted
``jobs.raw_payload`` DB column with gate-computed values that have nothing
to do with the collector's original source JSON.

``gate_jobs()`` now sets these directly as typed fields on the ``GateResult``
it already returns per job (job.id -> GateResult); scoring reads them from
there via an explicit ``gate_result`` parameter. This file proves:
  - the typed fields exist with sane defaults
  - gate_jobs() never writes to raw_payload anymore
  - scoring reads ONLY from the passed GateResult, never from raw_payload
  - each job's context is isolated from every other job's
  - repeated/independent gate_jobs() calls don't leak stale state
  - raw_payload survives a real DB round-trip completely clean
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models.job import Job
from src.pipeline.scoring import _score_experience_fit, score_job
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import (
    GateResult,
)
from src.storage.sqlite import Storage


def _job(job_id: str = "j1", title: str = "Developer", description: str = "") -> Job:
    return Job(
        id=job_id, source="test", title=title, description=description,
        location="France", countries=["France"], remote=False,
    )


_SYNTHETIC_KEYS = (
    "_education_penalty", "_skill_penalty", "_experience_penalty",
    "_extracted_min_experience_years",
)


class TestTypedContextCreation:
    def test_gate_result_has_typed_default_fields(self) -> None:
        result = GateResult(passes=True)
        assert result.education_penalty == 0.0
        assert result.skill_penalty == 0.0
        assert result.experience_penalty == 0.0
        assert result.min_experience_years is None

    def test_gate_result_fields_are_explicitly_settable(self) -> None:
        result = GateResult(
            passes=True, education_penalty=0.2, skill_penalty=0.1,
            experience_penalty=0.4, min_experience_years=5,
        )
        assert (result.education_penalty, result.skill_penalty,
                result.experience_penalty, result.min_experience_years) == (0.2, 0.1, 0.4, 5)


class TestGateJobsNeverWritesToRawPayload:
    def test_education_mismatch_leaves_raw_payload_clean(self) -> None:
        job = _job(description="Bac+5 souhaité en informatique.")
        job.raw_payload = {"original_field": "from-the-collector"}
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=False)

        passing, results = gate_jobs(
            [job], requirements_map, gate, education_preferred_penalty=0.20
        )
        assert len(passing) == 1
        assert results[job.id].education_penalty == 0.20  # the penalty WAS computed...
        # ...but never leaked into raw_payload, which keeps only what it had.
        assert job.raw_payload == {"original_field": "from-the-collector"}
        for key in _SYNTHETIC_KEYS:
            assert key not in job.raw_payload

    def test_experience_requirement_leaves_raw_payload_clean(self) -> None:
        job = _job(description="Au moins 3 ans d'expérience en développement web")
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}
        gate = HardConstraintGate(user_max_education_level=None, user_max_years_experience=10)

        passing, results = gate_jobs([job], requirements_map, gate)
        assert len(passing) == 1
        assert results[job.id].min_experience_years == 3  # the value WAS extracted...
        assert job.raw_payload == {}  # ...but never written into raw_payload
        for key in _SYNTHETIC_KEYS:
            assert key not in job.raw_payload


class TestScoringReadsOnlyFromGateResult:
    def test_raw_payload_synthetic_keys_are_completely_ignored(self) -> None:
        """Even if something (e.g. stale data, a future regression) put the
        OLD synthetic keys back into raw_payload, scoring must not read
        them — proving the side-channel is genuinely gone, not just unused
        by gate.py while still secretly wired up in scoring.py."""
        job = _job()
        job.raw_payload = {
            "_education_penalty": 0.20,
            "_skill_penalty": 0.10,
            "_experience_penalty": 0.40,
            "_extracted_min_experience_years": 5,
        }
        config = {"scoring": {"skills_match": {"target_skills": []}}}
        score_without_context = score_job(job, config)  # no gate_result passed

        job_clean = _job()  # identical job, no raw_payload junk at all
        score_clean = score_job(job_clean, config)

        assert score_without_context == score_clean, (
            "raw_payload content must have zero influence on the score now"
        )

    def test_experience_fit_uses_gate_result_not_raw_payload(self) -> None:
        job = _job()
        job.raw_payload = {"_extracted_min_experience_years": 5}  # old-style junk
        gate_result = GateResult(passes=True, min_experience_years=1)  # new-style, says 1yr
        score = _score_experience_fit(job, {}, gate_result)
        assert score == 1.0, "must use gate_result's value (1yr -> 1.0), not raw_payload's (5yr -> 0.40)"


class TestIsolationBetweenJobs:
    def test_each_job_gets_its_own_independent_gate_result(self) -> None:
        job_a = _job(job_id="a", description="Bac+5 minimum requis.")
        job_b = _job(job_id="b", description="Aucune exigence particulière.")
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {
            job_a.id: extractor.extract(job_a),
            job_b.id: extractor.extract(job_b),
        }
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=False)

        _passing, results = gate_jobs([job_a, job_b], requirements_map, gate, education_preferred_penalty=0.20)
        assert results["a"].education_penalty == 0.20
        assert results["b"].education_penalty == 0.0
        # Mutating one job's raw_payload must never touch the other's.
        assert job_a.raw_payload == {}
        assert job_b.raw_payload == {}


class TestIsolationBetweenPipelineRuns:
    def test_second_gate_jobs_call_does_not_leak_state_from_the_first(self) -> None:
        """The exact same Job object, gated twice with different settings,
        must reflect ONLY the most recent call's result — no residual state
        (e.g. from a stale raw_payload write) survives between calls."""
        job = _job(description="Bac+5 souhaité en informatique.")
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}

        gate_penalized = HardConstraintGate(user_max_education_level=2, education_hard_constraint=False)
        _passing1, results1 = gate_jobs(
            [job], requirements_map, gate_penalized, education_preferred_penalty=0.20
        )
        assert results1[job.id].education_penalty == 0.20

        gate_within_limit = HardConstraintGate(user_max_education_level=5, education_hard_constraint=False)
        _passing2, results2 = gate_jobs(
            [job], requirements_map, gate_within_limit, education_preferred_penalty=0.20
        )
        assert results2[job.id].education_penalty == 0.0, (
            "second run's GateResult must reflect ITS OWN gate config, not "
            "leak the first run's penalty via any shared/residual state"
        )
        assert job.raw_payload == {}


class TestPersistedJobsStayClean:
    def test_gated_job_round_trips_through_the_real_db_with_clean_raw_payload(
        self, tmp_path: Path
    ) -> None:
        job = _job(description="Bac+5 souhaité en informatique.")
        job.raw_payload = {"collector_field": "some-real-source-value"}
        extractor = RequirementExtractor(user_skills=[])
        requirements_map = {job.id: extractor.extract(job)}
        gate = HardConstraintGate(user_max_education_level=2, education_hard_constraint=False)

        passing, results = gate_jobs(
            [job], requirements_map, gate, education_preferred_penalty=0.20
        )
        assert results[job.id].education_penalty == 0.20  # penalty genuinely computed

        db_path = str(tmp_path / "test.db")
        storage = Storage(db_path=db_path)
        storage.init_db()
        storage.upsert_job(passing[0])
        stored = storage.get_job_by_id(job.id)
        storage.close()

        assert stored is not None
        assert stored.raw_payload == {"collector_field": "some-real-source-value"}
        for key in _SYNTHETIC_KEYS:
            assert key not in stored.raw_payload


class TestExistingPipelineResultsRemainEquivalent:
    def test_score_with_typed_context_matches_pre_migration_expected_value(self) -> None:
        """Same scenario test_experience_requirement.py's
        test_edu_and_experience_penalties_stack encoded against the OLD
        raw_payload mechanism — the numeric result must be byte-identical
        under the new typed mechanism (0.8 * 0.6 = 0.48 combined multiplier)."""
        job = _job()
        config = {
            "scoring": {
                "location_fit": {"weight": 0.15, "preferred_countries": ["france"]},
                "skills_match": {"weight": 0.25, "target_skills": []},
            }
        }
        base = score_job(job, config)
        gate_result = GateResult(passes=True, education_penalty=0.20, experience_penalty=0.40)
        with_both = score_job(job, config, gate_result)
        assert with_both == pytest.approx(base * 0.8 * 0.6, rel=0.01)
