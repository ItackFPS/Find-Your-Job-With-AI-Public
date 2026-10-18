"""I19 — EXPERIMENTAL job-change detector tests.

Covers the pure detect_change() classification (new/unchanged/changed,
field-level diffing, formatting-noise immunity) and its opt-in wiring into
Storage.upsert_jobs_batch — proving the default (detect_field_changes=False)
path is byte-identical to pre-I19 behavior, and that turning it on only adds
observational data without altering what gets written, without deleting
anything, and without touching scoring/ranking code at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.models.job import Job
from src.pipeline.job_change_detector import FieldChange, detect_change
from src.storage.sqlite import Storage

REPO_ROOT = Path(__file__).resolve().parents[1]


def _job(**overrides) -> Job:
    defaults = {
        "id": "job-1",
        "source": "test_source",
        "source_job_id": "ext-1",
        "title": "Python Developer",
        "company": "Acme Corp",
        "location": "Paris",
        "description": "Build backend services in Python.",
        "employment_type": "full_time",
        "salary_min": 40000.0,
        "salary_max": 55000.0,
        "salary_currency": "EUR",
        "canonical_url": "https://example.com/job/1",
        "apply_url": "https://example.com/apply/1",
        "skills": ["python", "django"],
        "content_hash": "hash-1",
    }
    defaults.update(overrides)
    return Job(**defaults)


class TestDetectChangeStatusClassification:
    def test_no_existing_job_is_new(self) -> None:
        result = detect_change(None, _job())
        assert result.status == "new"
        assert result.changed_fields == ()

    def test_identical_job_is_unchanged(self) -> None:
        result = detect_change(_job(), _job())
        assert result.status == "unchanged"
        assert result.changed_fields == ()

    def test_title_change_is_changed(self) -> None:
        old = _job(title="Python Developer")
        new = _job(title="Senior Python Developer")
        result = detect_change(old, new)
        assert result.status == "changed"
        assert any(c.field == "title" for c in result.changed_fields)

    def test_description_change_is_changed(self) -> None:
        old = _job(description="Build backend services.")
        new = _job(description="Build backend and frontend services.")
        result = detect_change(old, new)
        assert result.status == "changed"
        assert any(c.field == "description" for c in result.changed_fields)

    def test_salary_change_is_changed(self) -> None:
        old = _job(salary_min=40000.0)
        new = _job(salary_min=45000.0)
        result = detect_change(old, new)
        assert result.status == "changed"
        assert any(c.field == "salary_min" for c in result.changed_fields)

    def test_location_change_is_changed(self) -> None:
        old = _job(location="Paris")
        new = _job(location="Lyon")
        result = detect_change(old, new)
        assert result.status == "changed"
        assert any(c.field == "location" for c in result.changed_fields)

    def test_skills_change_is_changed(self) -> None:
        old = _job(skills=["python", "django"])
        new = _job(skills=["python", "fastapi"])
        result = detect_change(old, new)
        assert result.status == "changed"
        assert any(c.field == "skills" for c in result.changed_fields)


class TestFormattingNoiseIsNotAChange:
    def test_whitespace_only_description_change_is_unchanged(self) -> None:
        old = _job(description="Build   backend services.\n\n\nIn Python.")
        new = _job(description="Build backend services. In Python.")
        result = detect_change(old, new)
        assert result.status == "unchanged"

    def test_html_only_description_change_is_unchanged(self) -> None:
        old = _job(description="<p>Build backend services.</p>")
        new = _job(description="Build backend services.")
        result = detect_change(old, new)
        assert result.status == "unchanged"

    def test_casing_only_title_change_is_unchanged(self) -> None:
        old = _job(title="Python Developer")
        new = _job(title="PYTHON DEVELOPER")
        result = detect_change(old, new)
        assert result.status == "unchanged"

    def test_skills_reordering_is_unchanged(self) -> None:
        old = _job(skills=["python", "django", "postgresql"])
        new = _job(skills=["postgresql", "python", "django"])
        result = detect_change(old, new)
        assert result.status == "unchanged"

    def test_skills_case_variation_is_unchanged(self) -> None:
        old = _job(skills=["Python", "Django"])
        new = _job(skills=["python", "django"])
        result = detect_change(old, new)
        assert result.status == "unchanged"


class TestMissingOptionalFields:
    def test_both_none_is_unchanged(self) -> None:
        old = _job(location=None)
        new = _job(location=None)
        result = detect_change(old, new)
        assert result.status == "unchanged"

    def test_none_to_value_is_changed(self) -> None:
        old = _job(location=None)
        new = _job(location="Paris")
        result = detect_change(old, new)
        assert result.status == "changed"
        assert any(c.field == "location" for c in result.changed_fields)

    def test_value_to_none_is_changed(self) -> None:
        old = _job(salary_min=40000.0)
        new = _job(salary_min=None)
        result = detect_change(old, new)
        assert result.status == "changed"


class TestFieldChangeCarriesOldAndNewValues:
    def test_field_change_records_both_values(self) -> None:
        old = _job(title="Python Developer")
        new = _job(title="Senior Python Developer")
        result = detect_change(old, new)
        change = next(c for c in result.changed_fields if c.field == "title")
        assert change == FieldChange("title", "Python Developer", "Senior Python Developer")


@pytest.fixture
def temp_db(tmp_path) -> str:
    return str(tmp_path / "job_change_test.db")


@pytest.fixture
def storage(temp_db: str) -> Storage:
    s = Storage(db_path=temp_db)
    s.init_db()
    return s


class TestUpsertJobsBatchWiring:
    def test_default_behavior_unchanged_no_field_changes_key(self, storage: Storage) -> None:
        job = _job(id="job-a", content_hash="h1")
        result = storage.upsert_jobs_batch([job])
        assert "field_changes" not in result
        assert result == {
            "inserted": 1,
            "updated": 0,
            "unchanged": 0,
            "by_source": {"test_source": {"inserted": 1, "updated": 0, "unchanged": 0}},
        }

    def test_detect_field_changes_true_reports_changed_fields(self, storage: Storage) -> None:
        storage.upsert_jobs_batch(
            [_job(id="job-a", title="Python Developer", content_hash="h1")]
        )
        result = storage.upsert_jobs_batch(
            [_job(id="job-a", title="Senior Python Developer", content_hash="h2")],
            detect_field_changes=True,
        )
        assert result["field_changes"] == {"job-a": ["title"]}

    def test_detect_field_changes_true_no_changes_yields_empty_dict(self, storage: Storage) -> None:
        storage.upsert_jobs_batch([_job(id="job-a", content_hash="h1")])
        result = storage.upsert_jobs_batch(
            [_job(id="job-a", content_hash="h1")], detect_field_changes=True
        )
        assert result["field_changes"] == {}

    def test_new_job_never_appears_in_field_changes(self, storage: Storage) -> None:
        result = storage.upsert_jobs_batch(
            [_job(id="job-new", content_hash="h1")], detect_field_changes=True
        )
        assert result["field_changes"] == {}
        assert result["inserted"] == 1

    def test_formatting_only_change_not_reported(self, storage: Storage) -> None:
        storage.upsert_jobs_batch(
            [_job(id="job-a", description="<p>Build backend services.</p>", content_hash="h1")]
        )
        result = storage.upsert_jobs_batch(
            [_job(id="job-a", description="Build backend services.", content_hash="h1")],
            detect_field_changes=True,
        )
        assert result["field_changes"] == {}

    def test_does_not_delete_any_job(self, storage: Storage) -> None:
        storage.upsert_jobs_batch([_job(id="job-a", content_hash="h1")])
        storage.upsert_jobs_batch(
            [_job(id="job-a", title="Changed Title", content_hash="h2")],
            detect_field_changes=True,
        )
        assert storage.get_job_by_id("job-a") is not None

    def test_empty_batch_with_detect_field_changes_true(self, storage: Storage) -> None:
        result = storage.upsert_jobs_batch([], detect_field_changes=True)
        assert result["field_changes"] == {}


class TestNoScoringOrRankingCoupling:
    """Structural proof that the change detector is only ever reachable from
    storage bookkeeping, never from scoring/ranking/eligibility code."""

    @pytest.mark.parametrize(
        "production_file",
        [
            "src/pipeline/scoring.py",
            "src/requirements/gate.py",
            "src/personalization/engine.py",
            "src/pipeline/orchestrator.py",
        ],
    )
    def test_module_never_imported_by_scoring_or_ranking_code(self, production_file: str) -> None:
        source = (REPO_ROOT / production_file).read_text(encoding="utf-8")
        assert "job_change_detector" not in source
