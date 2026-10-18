"""Tests for Phase 4: Personalized Ranking and Feedback.

Covers:
- Feedback storage: add/retrieve, fingerprinting, score cache
- Preference profile construction (deterministic weighted averages)
- Positive feedback increases future ranking for similar jobs
- Negative feedback decreases future ranking for similar jobs
- Hard-filter authority: personalization never adds/reintroduces jobs
- Cold start / min feedback threshold behavior
- Excluded (already-actioned) jobs are dropped, not re-scored upward
- Explainability: every adjustment has a human-readable summary
- Determinism: same feedback + same jobs -> same output every time
- Caching: unchanged jobs are not recomputed when feedback is unchanged
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.models.job import Job
from src.personalization.config import load_personalization_config
from src.personalization.engine import (
    apply_personalization,
    build_preference_profile,
    score_job_personalization,
)
from src.personalization.models import FeedbackType
from src.personalization.storage import PersonalizationStorage

# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def temp_db() -> str:
    """Create a unique temporary database file for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield str(db_path)
    try:
        db_path.unlink(missing_ok=True)
    except PermissionError:
        pass


@pytest.fixture
def storage(temp_db: str) -> PersonalizationStorage:
    s = PersonalizationStorage(db_path=temp_db)
    s.init_db()
    yield s
    s.close()


@pytest.fixture
def default_config() -> dict:
    return load_personalization_config("config/does-not-exist.yaml")


def make_job(
    job_id: str,
    title: str = "Backend Engineer",
    company: str = "Acme Corp",
    skills: list[str] | None = None,
    remote: bool = False,
    content_hash: str = "",
) -> Job:
    return Job(
        id=job_id,
        source="greenhouse",
        title=title,
        company=company,
        skills=skills or ["Python", "Django"],
        remote=remote,
        content_hash=content_hash or f"hash-{job_id}",
    )


# -- Storage: feedback CRUD ----------------------------------------------------


class TestPersonalizationStorage:
    def test_add_and_get_feedback_for_job(self, storage: PersonalizationStorage) -> None:
        job = make_job("j1")
        storage.add_feedback(job, FeedbackType.INTERESTED, note="looks great")

        records = storage.get_feedback_for_job("j1")
        assert len(records) == 1
        assert records[0].feedback_type == FeedbackType.INTERESTED
        assert records[0].note == "looks great"
        assert records[0].company == "Acme Corp"
        assert records[0].skills == ["Python", "Django"]

    def test_get_all_feedback(self, storage: PersonalizationStorage) -> None:
        storage.add_feedback(make_job("j1"), FeedbackType.INTERESTED)
        storage.add_feedback(make_job("j2"), FeedbackType.NOT_INTERESTED)

        all_feedback = storage.get_all_feedback()
        assert len(all_feedback) == 2

    def test_count_feedback(self, storage: PersonalizationStorage) -> None:
        assert storage.count_feedback() == 0
        storage.add_feedback(make_job("j1"), FeedbackType.APPLIED)
        assert storage.count_feedback() == 1

    def test_feedback_version_changes_when_feedback_added(
        self, storage: PersonalizationStorage
    ) -> None:
        v0 = storage.get_feedback_version()
        storage.add_feedback(make_job("j1"), FeedbackType.INTERESTED)
        v1 = storage.get_feedback_version()
        assert v0 != v1

    def test_feedback_version_stable_when_unchanged(
        self, storage: PersonalizationStorage
    ) -> None:
        storage.add_feedback(make_job("j1"), FeedbackType.INTERESTED)
        v1 = storage.get_feedback_version()
        v2 = storage.get_feedback_version()
        assert v1 == v2

    def test_score_cache_round_trip(self, storage: PersonalizationStorage) -> None:
        storage.cache_score("j1", "v1", "hash-j1", 0.15, {"job_id": "j1"})
        cached = storage.get_cached_score("j1", "v1", "hash-j1")
        assert cached is not None
        adjustment, explanation = cached
        assert adjustment == 0.15
        assert explanation["job_id"] == "j1"

    def test_score_cache_invalidated_by_content_hash_change(
        self, storage: PersonalizationStorage
    ) -> None:
        storage.cache_score("j1", "v1", "hash-old", 0.15, {"job_id": "j1"})
        assert storage.get_cached_score("j1", "v1", "hash-new") is None

    def test_score_cache_invalidated_by_profile_version_change(
        self, storage: PersonalizationStorage
    ) -> None:
        storage.cache_score("j1", "v1", "hash-j1", 0.15, {"job_id": "j1"})
        assert storage.get_cached_score("j1", "v2", "hash-j1") is None


# -- Preference profile --------------------------------------------------------


class TestBuildPreferenceProfile:
    def test_empty_feedback_yields_empty_profile(self, default_config: dict) -> None:
        profile = build_preference_profile([], default_config, version="v0")
        assert profile.feedback_count == 0
        assert profile.company == {}

    def test_positive_feedback_produces_positive_company_average(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(make_job("j1", company="Acme Corp"), FeedbackType.INTERESTED)
        records = storage.get_all_feedback()
        profile = build_preference_profile(records, default_config, version="v0")

        assert profile.company["acme corp"].average > 0

    def test_negative_feedback_produces_negative_company_average(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(make_job("j1", company="BadFit Inc"), FeedbackType.NOT_INTERESTED)
        records = storage.get_all_feedback()
        profile = build_preference_profile(records, default_config, version="v0")

        assert profile.company["badfit inc"].average < 0


# -- Job-level scoring ---------------------------------------------------------


class TestScoreJobPersonalization:
    def test_cold_start_no_feedback_returns_zero(self, default_config: dict) -> None:
        profile = build_preference_profile([], default_config, version="v0")
        job = make_job("new-job", company="Unseen Co")
        adjustment, explanation = score_job_personalization(job, profile, default_config)

        assert adjustment == 0.0
        assert explanation.cold_start is True

    def test_positive_feedback_on_company_raises_similar_job_score(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("liked-1", company="Acme Corp", skills=["Python"]),
            FeedbackType.INTERESTED,
        )
        profile = build_preference_profile(storage.get_all_feedback(), default_config, version="v0")

        new_job = make_job("j2", company="Acme Corp", skills=["Python"])
        adjustment, explanation = score_job_personalization(new_job, profile, default_config)

        assert adjustment > 0.0
        assert any(m.signal_type == "company" for m in explanation.matches)

    def test_negative_feedback_lowers_similar_job_score(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("disliked-1", company="BadFit Inc", skills=["COBOL"]),
            FeedbackType.NOT_INTERESTED,
        )
        profile = build_preference_profile(storage.get_all_feedback(), default_config, version="v0")

        new_job = make_job("j2", company="BadFit Inc", skills=["COBOL"])
        adjustment, _explanation = score_job_personalization(new_job, profile, default_config)

        assert adjustment < 0.0

    def test_adjustment_is_bounded_by_max_abs_adjustment(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        # Pile up many strong positive signals on the exact same features.
        for i in range(20):
            storage.add_feedback(
                make_job(f"liked-{i}", company="Acme Corp", skills=["Python", "Django"]),
                FeedbackType.APPLIED,
            )
        profile = build_preference_profile(storage.get_all_feedback(), default_config, version="v0")

        new_job = make_job("target", company="Acme Corp", skills=["Python", "Django"])
        adjustment, _ = score_job_personalization(new_job, profile, default_config)

        max_abs = default_config["personalization"]["max_abs_adjustment"]
        assert abs(adjustment) <= max_abs + 1e-9

    def test_unrelated_job_gets_no_matches(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("liked-1", company="Acme Corp", skills=["Python"]),
            FeedbackType.INTERESTED,
        )
        profile = build_preference_profile(storage.get_all_feedback(), default_config, version="v0")

        unrelated_job = make_job("j2", title="Marketing Manager", company="Totally Different LLC", skills=["SEO"])
        adjustment, explanation = score_job_personalization(unrelated_job, profile, default_config)

        assert adjustment == 0.0
        assert explanation.matches == []

    def test_explanation_summary_is_human_readable(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("liked-1", company="Acme Corp", skills=["Python"]),
            FeedbackType.INTERESTED,
        )
        profile = build_preference_profile(storage.get_all_feedback(), default_config, version="v0")
        job = make_job("j2", company="Acme Corp", skills=["Python"])
        _, explanation = score_job_personalization(job, profile, default_config)

        assert isinstance(explanation.summary(), str)
        assert len(explanation.summary()) > 0


# -- apply_personalization: full integration -----------------------------------


class TestApplyPersonalization:
    def test_never_adds_jobs_not_in_the_input_list(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        """Hard filters remain authoritative: personalization only re-scores
        jobs it was given, it never introduces new ones."""
        storage.add_feedback(make_job("hidden-job", company="Acme Corp"), FeedbackType.INTERESTED)

        ranked = [(make_job("visible-1", company="Other Co"), 0.5)]
        result, _ = apply_personalization(ranked, storage, default_config)

        result_ids = {job.id for job, _ in result}
        assert result_ids == {"visible-1"}
        assert "hidden-job" not in result_ids

    def test_positive_feedback_improves_relative_ranking(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("liked-1", company="Acme Corp", skills=["Python"]),
            FeedbackType.INTERESTED,
        )

        job_a = make_job("a", company="Acme Corp", skills=["Python"], content_hash="ha")
        job_b = make_job("b", company="Neutral Co", skills=["Ruby"], content_hash="hb")
        ranked = [(job_a, 0.5), (job_b, 0.5)]

        result, explanations = apply_personalization(ranked, storage, default_config)
        scores = {job.id: score for job, score in result}

        assert scores["a"] > scores["b"]
        assert explanations["a"].clamped_adjustment > 0

    def test_negative_feedback_worsens_relative_ranking(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("disliked-1", company="BadFit Inc", skills=["COBOL"]),
            FeedbackType.NOT_INTERESTED,
        )

        job_a = make_job("a", company="BadFit Inc", skills=["COBOL"], content_hash="ha")
        job_b = make_job("b", company="Neutral Co", skills=["Ruby"], content_hash="hb")
        ranked = [(job_a, 0.5), (job_b, 0.5)]

        result, explanations = apply_personalization(ranked, storage, default_config)
        scores = {job.id: score for job, score in result}

        assert scores["a"] < scores["b"]
        assert explanations["a"].clamped_adjustment < 0

    def test_scores_stay_within_valid_range(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(make_job("liked-1", company="Acme Corp"), FeedbackType.APPLIED)
        ranked = [(make_job("a", company="Acme Corp", content_hash="ha"), 0.95)]

        result, _ = apply_personalization(ranked, storage, default_config)
        for _, score in result:
            assert 0.0 <= score <= 1.0

    def test_exclude_actioned_jobs_removes_same_job(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        job = make_job("j1", company="Acme Corp")
        storage.add_feedback(job, FeedbackType.APPLIED)

        ranked = [(job, 0.7), (make_job("j2", company="Other Co"), 0.5)]
        result, explanations = apply_personalization(ranked, storage, default_config)

        result_ids = {j.id for j, _ in result}
        assert "j1" not in result_ids
        assert explanations["j1"].excluded_reason is not None

    def test_exclude_actioned_jobs_can_be_disabled(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        cfg = {**default_config, "personalization": {**default_config["personalization"], "exclude_actioned_jobs": False}}
        job = make_job("j1", company="Acme Corp")
        storage.add_feedback(job, FeedbackType.APPLIED)

        ranked = [(job, 0.7)]
        result, _ = apply_personalization(ranked, storage, cfg)

        assert {j.id for j, _ in result} == {"j1"}

    def test_deterministic_output_for_same_inputs(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("liked-1", company="Acme Corp", skills=["Python"]),
            FeedbackType.INTERESTED,
        )
        ranked = [
            (make_job("a", company="Acme Corp", skills=["Python"], content_hash="ha"), 0.5),
            (make_job("b", company="Other Co", skills=["Go"], content_hash="hb"), 0.6),
        ]

        result1, _ = apply_personalization(ranked, storage, default_config)
        result2, _ = apply_personalization(ranked, storage, default_config)

        assert [(j.id, round(s, 9)) for j, s in result1] == [(j.id, round(s, 9)) for j, s in result2]

    def test_min_feedback_count_gates_personalization(
        self, storage: PersonalizationStorage
    ) -> None:
        cfg = load_personalization_config("config/does-not-exist.yaml")
        cfg = {**cfg, "personalization": {**cfg["personalization"], "min_feedback_count": 5}}

        storage.add_feedback(make_job("liked-1", company="Acme Corp"), FeedbackType.INTERESTED)
        job = make_job("target", company="Acme Corp", content_hash="ht")
        result, explanations = apply_personalization([(job, 0.5)], storage, cfg)

        assert result[0][1] == 0.5
        assert explanations["target"].cold_start is True

    def test_caches_score_and_skips_recompute_for_unchanged_job(
        self, storage: PersonalizationStorage, default_config: dict
    ) -> None:
        storage.add_feedback(
            make_job("liked-1", company="Acme Corp", skills=["Python"]),
            FeedbackType.INTERESTED,
        )
        job = make_job("target", company="Acme Corp", skills=["Python"], content_hash="stable-hash")

        apply_personalization([(job, 0.5)], storage, default_config)
        version = storage.get_feedback_version()
        cached = storage.get_cached_score("target", version, "stable-hash")

        assert cached is not None
        cached_adjustment, _ = cached

        # A second run with unchanged feedback + unchanged job content hash
        # must reuse the cached adjustment rather than recompute it.
        _result2, explanations2 = apply_personalization([(job, 0.5)], storage, default_config)
        assert explanations2["target"].clamped_adjustment == cached_adjustment


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
