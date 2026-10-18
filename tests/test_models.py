"""Tests for the Job and Source models."""

from datetime import UTC, datetime

from src.models.job import Job
from src.models.source import Source


class TestJob:
    def test_default_fields(self):
        job = Job()
        assert isinstance(job.id, str)
        assert len(job.id) == 36  # UUID format
        assert job.source == ""
        assert job.remote is False
        assert job.salary_currency == "EUR"

    def test_from_dict_roundtrip(self):
        original = Job(
            id="abc123",
            source="greenhouse",
            title="Software Engineer",
            company="Acme Corp",
            published_at=datetime.now(UTC),
        )
        data = original.to_dict()
        restored = Job.from_dict(data)
        assert restored.id == original.id
        assert restored.title == original.title

    def test_to_dict_serializes_dts(self):
        job = Job(
            id="abc123",
            title="Test",
            published_at=datetime.now(UTC),
        )
        d = job.to_dict()
        assert isinstance(d["published_at"], str)


class TestSource:
    def test_default_fields(self):
        src = Source(name="Greenhouse")
        assert src.name == "Greenhouse"
        assert src.active is True

    def test_from_dict_roundtrip(self):
        original = Source(
            id="src1",
            name="Test Source",
            type="greenhouse",
            active=True,
        )
        data = original.to_dict()
        restored = Source.from_dict(data)
        assert restored.name == original.name
