"""I18 — outcome event storage tests.

Covers: valid event creation, persistence/retrieval, job association,
timestamp handling, the intentional duplicate-allowed semantics for VIEWED
events, and a structural proof that no scoring/ranking/eligibility code
knows this module exists.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.outcomes.models import OutcomeEvent, OutcomeEventType
from src.outcomes.storage import OutcomeEventStorage

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def temp_db(tmp_path) -> str:
    return str(tmp_path / "outcomes_test.db")


@pytest.fixture
def storage(temp_db: str) -> OutcomeEventStorage:
    s = OutcomeEventStorage(db_path=temp_db)
    s.init_db()
    return s


class TestRecordEvent:
    def test_valid_event_creation_returns_populated_event(self, storage: OutcomeEventStorage) -> None:
        event = storage.record_event("job-1", OutcomeEventType.VIEWED)
        assert event.id
        assert event.job_id == "job-1"
        assert event.event_type == OutcomeEventType.VIEWED
        assert isinstance(event.occurred_at, datetime)

    def test_timestamp_defaults_to_now_utc(self, storage: OutcomeEventStorage) -> None:
        before = datetime.now(UTC)
        event = storage.record_event("job-1", OutcomeEventType.VIEWED)
        after = datetime.now(UTC)
        assert before <= event.occurred_at <= after

    def test_metadata_defaults_to_empty_dict(self, storage: OutcomeEventStorage) -> None:
        event = storage.record_event("job-1", OutcomeEventType.VIEWED)
        assert event.metadata == {}

    def test_metadata_is_persisted(self, storage: OutcomeEventStorage) -> None:
        storage.record_event(
            "job-1",
            OutcomeEventType.APPLICATION_STATUS_CHANGED,
            metadata={"from": None, "to": "applied"},
        )
        events = storage.get_events_for_job("job-1")
        assert events[0].metadata == {"from": None, "to": "applied"}

    def test_run_id_association(self, storage: OutcomeEventStorage) -> None:
        event = storage.record_event("job-1", OutcomeEventType.VIEWED, run_id="run-abc")
        assert event.run_id == "run-abc"
        stored = storage.get_events_for_job("job-1")[0]
        assert stored.run_id == "run-abc"

    def test_run_id_optional(self, storage: OutcomeEventStorage) -> None:
        event = storage.record_event("job-1", OutcomeEventType.VIEWED)
        assert event.run_id is None


class TestPersistenceAndRetrieval:
    def test_get_events_for_job_returns_newest_first(self, storage: OutcomeEventStorage) -> None:
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        storage.record_event("job-1", OutcomeEventType.APPLY_LINK_OPENED)
        events = storage.get_events_for_job("job-1")
        assert len(events) == 2
        assert events[0].event_type == OutcomeEventType.APPLY_LINK_OPENED

    def test_get_events_filtered_by_type(self, storage: OutcomeEventStorage) -> None:
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        storage.record_event("job-1", OutcomeEventType.APPLY_LINK_OPENED)
        events = storage.get_events_for_job("job-1", event_type=OutcomeEventType.VIEWED)
        assert len(events) == 1
        assert events[0].event_type == OutcomeEventType.VIEWED

    def test_events_scoped_to_job_id(self, storage: OutcomeEventStorage) -> None:
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        storage.record_event("job-2", OutcomeEventType.VIEWED)
        assert len(storage.get_events_for_job("job-1")) == 1
        assert len(storage.get_events_for_job("job-2")) == 1

    def test_no_events_returns_empty_list(self, storage: OutcomeEventStorage) -> None:
        assert storage.get_events_for_job("job-unknown") == []

    def test_count_events_for_job(self, storage: OutcomeEventStorage) -> None:
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        assert storage.count_events_for_job("job-1") == 2


class TestDuplicateAndIdempotencyBehavior:
    """VIEWED is intentionally NOT deduplicated — a repeat view is a real,
    meaningful observation, not noise. See OutcomeEventStorage.record_event's
    docstring: idempotency for events like application-status changes is the
    CALLER's responsibility (only call record_event when something actually
    changed), not this storage layer's.
    """

    def test_repeated_viewed_events_are_all_recorded(self, storage: OutcomeEventStorage) -> None:
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        storage.record_event("job-1", OutcomeEventType.VIEWED)
        assert storage.count_events_for_job("job-1") == 3

    def test_each_event_gets_a_distinct_id(self, storage: OutcomeEventStorage) -> None:
        e1 = storage.record_event("job-1", OutcomeEventType.VIEWED)
        e2 = storage.record_event("job-1", OutcomeEventType.VIEWED)
        assert e1.id != e2.id


class TestOutcomeEventModel:
    def test_default_id_is_unique(self) -> None:
        e1 = OutcomeEvent(job_id="job-1", event_type=OutcomeEventType.VIEWED)
        e2 = OutcomeEvent(job_id="job-1", event_type=OutcomeEventType.VIEWED)
        assert e1.id != e2.id

    def test_only_genuinely_supported_event_types_exist(self) -> None:
        assert {e.value for e in OutcomeEventType} == {
            "viewed",
            "apply_link_opened",
            "application_status_changed",
        }


class TestNoProductionCoupling:
    """Structural proof: outcome tracking cannot influence scoring, ranking,
    eligibility, or personalization — none of those modules import it."""

    @pytest.mark.parametrize(
        "production_file",
        [
            "src/pipeline/scoring.py",
            "src/pipeline/orchestrator.py",
            "src/requirements/gate.py",
            "src/personalization/engine.py",
        ],
    )
    def test_module_never_imported_by_production_ranking_code(self, production_file: str) -> None:
        source = (REPO_ROOT / production_file).read_text(encoding="utf-8")
        assert "src.outcomes" not in source
        assert "from src import outcomes" not in source
