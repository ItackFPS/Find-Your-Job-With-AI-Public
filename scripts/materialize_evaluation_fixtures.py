"""Regenerate the committed evaluation datasets from the Python benchmark fixtures.

``tests/fixtures/ai_filter_benchmark/{jobs,ground_truth}.py`` stay the source of
truth; this script writes them in the evaluation harness' dataset format under
``tests/fixtures/evaluation/datasets/``. ``tests/test_evaluation_dataset.py``
fails when the committed files drift from the fixtures.

Usage:  python scripts/materialize_evaluation_fixtures.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.evaluation.dataset import write_dataset
from src.evaluation.models import LabelRecord
from tests.fixtures.ai_filter_benchmark.ground_truth import GROUND_TRUTH
from tests.fixtures.ai_filter_benchmark.jobs import JOBS

ROOT = Path(__file__).parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
CREATED_AT = "2026-09-17T00:00:00+00:00"   # date of the pinned profile snapshot
AS_OF = "2026-09-17T00:00:00+00:00"
_EXPECTED = {"SHOULD_ACCEPT": "accept", "SHOULD_REJECT": "reject", "BORDERLINE": "borderline"}


def build_ai_filter_50(dest: Path) -> None:
    labels = [
        LabelRecord(
            job_id=jid, expected=_EXPECTED[gt["expected"]], category=gt["category"],
            reason=gt["reason"], label_source="human", labeler="repo-maintainer",
            labeled_at=CREATED_AT,
        )
        for jid, gt in GROUND_TRUTH.items()
    ]
    write_dataset(
        dest, dataset_id="synthetic-ai-filter-50", version="1", kind="synthetic",
        created_at=CREATED_AT, as_of=AS_OF, jobs=[dict(j) for j in JOBS], labels=labels,
        profile_yaml=(FIXTURES / "ai_filter_benchmark" / "profile.yaml").read_bytes(),
        notes=("50 synthetic French postings, hand-labelled; pinned profile snapshot 2026-09-17, "
               "location migrated to the distance model 2026-10-24."),
    )


def main() -> None:
    build_ai_filter_50(FIXTURES / "evaluation" / "datasets" / "synthetic-ai-filter-50")


if __name__ == "__main__":
    main()
