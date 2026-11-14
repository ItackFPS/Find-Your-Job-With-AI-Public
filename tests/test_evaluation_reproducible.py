"""Phase B B3-R: the same dataset, code and strategy give the same evaluation output, run after run."""

from __future__ import annotations

import json
from pathlib import Path

from src.evaluation.dataset import load_dataset
from src.evaluation.models import StrategyRef
from src.evaluation.runner import run_evaluation

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
# Fields that describe the run itself (when, where, how long), not what it decided or measured.
VOLATILE = ("run_id", "created_at", "timings", "environment", "code")


def _stable(result) -> str:
    data = result.model_dump()
    for key in VOLATILE:
        data.pop(key, None)
    metrics = data.get("metrics") or {}
    metrics.pop("timings", None)
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def test_an_evaluation_run_twice_gives_identical_output() -> None:
    ds = load_dataset(DS)
    strategy = StrategyRef(config_dir=str(ROOT / "config"))
    first, second = run_evaluation(ds, strategy, assess=True), run_evaluation(ds, strategy, assess=True)
    assert first.run_id != second.run_id
    assert _stable(first) == _stable(second)
    assert first.decisions and first.metrics["overall"]["scored"] > 0
