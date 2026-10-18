"""Issue #67: one collector failing must not fail the whole pipeline run.

CLAUDE.md §5 — "Do not let one collector failure crash the pipeline." A
collector whose constructor raises (e.g. an unparsable config value) used to
escape the per-source error handling and mark the entire run failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.collectors.base import BaseCollector, CollectorResult


class _ConstructorFails(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        raise ValueError("invalid literal for int() with base 10: '30s'")

    def collect(self) -> CollectorResult:  # pragma: no cover - never constructed
        raise AssertionError("unreachable")


class _CollectFails(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        pass

    def collect(self) -> CollectorResult:
        raise RuntimeError("upstream API down")


class _Healthy(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        pass

    def collect(self) -> CollectorResult:
        job = {
            "source_job_id": "1", "url": "https://example.test/1",
            "title": "Python Developer", "company": "Acme", "location": "Paris",
            "description": "Python developer position.",
        }
        return CollectorResult(source_name="healthy", jobs=[job])


_CLASSES = {"constructor_fails": _ConstructorFails, "collect_fails": _CollectFails, "healthy": _Healthy}


def _config_dir(tmp_path: Path, broken_type: str) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(
        "target_domains: [software_development]\ntarget_skills: [python]\n", encoding="utf-8"
    )
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text(
        "sources:\n"
        f"  - {{name: broken, type: {broken_type}, active: true}}\n"
        "  - {name: healthy, type: healthy, active: true}\n",
        encoding="utf-8",
    )
    return config_dir


@pytest.mark.parametrize("broken_type", ["constructor_fails", "collect_fails"])
def test_one_failing_collector_does_not_fail_the_run(tmp_path, monkeypatch, broken_type):
    from src.collectors.registry import registry
    from src.pipeline.orchestrator import run_pipeline

    monkeypatch.setattr(registry, "get_collector_class", lambda src_type: _CLASSES[src_type])

    result = run_pipeline(
        db_path=str(tmp_path / "t.db"), config_dir=str(_config_dir(tmp_path, broken_type))
    )

    assert result.status == "completed", result.error
    assert result.stats.by_source["healthy"]["collected"] == 1
    assert result.stats.by_source["broken"] == {"collected": 0, "accepted": 0, "errors": 1}
    assert any(e.startswith("Failed to collect from broken:") for e in result.stats.errors)
