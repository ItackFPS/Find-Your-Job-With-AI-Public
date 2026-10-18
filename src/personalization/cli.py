"""CLI helper for recording Phase 4 user feedback on jobs.

Usage:
    python -m src.personalization.cli --job-id <id> --type interested
    python -m src.personalization.cli --job-id <id> --type not_interested --note "wrong seniority"
"""

from __future__ import annotations

import argparse
import logging

from src.models.job import Job
from src.personalization.models import Feedback, FeedbackType
from src.personalization.storage import PersonalizationStorage
from src.storage.sqlite import Storage

logger = logging.getLogger(__name__)


def record_feedback(
    job_storage: Storage,
    feedback_storage: PersonalizationStorage,
    job_id: str,
    feedback_type: FeedbackType,
    note: str | None = None,
) -> Feedback:
    """Look up a job by id and record feedback for it.

    Raises:
        KeyError: If no job with the given id exists in ``job_storage``.
    """
    job: Job | None = job_storage.get_job_by_id(job_id)
    if job is None:
        raise KeyError(f"No job found with id {job_id!r}")
    return feedback_storage.add_feedback(job, feedback_type, note=note)


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse args and record one piece of feedback."""
    parser = argparse.ArgumentParser(description="Record feedback for a job.")
    parser.add_argument("--job-id", required=True, help="ID of the job to give feedback on.")
    parser.add_argument(
        "--type",
        required=True,
        choices=[t.value for t in FeedbackType],
        help="Feedback type.",
    )
    parser.add_argument("--note", default=None, help="Optional free-text note.")
    parser.add_argument("--db-path", default="data/jobs.db", help="Path to the SQLite database.")
    args = parser.parse_args(argv)

    job_storage = Storage(db_path=args.db_path)
    job_storage.init_db()
    feedback_storage = PersonalizationStorage(db_path=args.db_path)
    feedback_storage.init_db()

    try:
        feedback = record_feedback(
            job_storage,
            feedback_storage,
            job_id=args.job_id,
            feedback_type=FeedbackType(args.type),
            note=args.note,
        )
    except KeyError as exc:
        print(str(exc))
        return
    finally:
        job_storage.close()
        feedback_storage.close()

    print(f"Recorded '{feedback.feedback_type.value}' feedback for job {feedback.job_id}.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
