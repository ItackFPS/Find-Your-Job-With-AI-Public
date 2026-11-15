"""Durations kept apart (Phase B B7-B): internal pipeline, post-processing, stages, sources.

Temporary database, fake collector, no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.collectors.base import BaseCollector, CollectorResult


class _Source(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        return CollectorResult(source_name="src", jobs=[{
            "source_job_id": "d1", "url": "https://example.test/d1", "title": "Python Developer",
            "company": "Acme", "location": "Paris", "description": "Python developer position."}])


def _run(tmp_path: Path, monkeypatch) -> dict[str, Any]:  # noqa: ANN001
    from src.collectors.registry import registry
    from src.pipeline.orchestrator import run_pipeline

    monkeypatch.setattr(registry, "get_collector_class", lambda _type: _Source)
    config = tmp_path / "config"
    config.mkdir()
    (config / "profile.yaml").write_text("target_domains: [software_development]\ntarget_skills: [python]\n",
                                         encoding="utf-8")
    (config / "filters.yaml").write_text("domain_keywords:\n  software_development:\n    title: [developer]\n",
                                         encoding="utf-8")
    (config / "sources.yaml").write_text("sources:\n  - {name: src, type: src, active: true}\n", encoding="utf-8")
    result = run_pipeline(db_path=str(tmp_path / "jobs.db"), config_dir=str(config))
    assert result.status == "completed", result.error
    return result.stats.to_dict()


def test_post_processing_and_each_source_are_timed(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    stats = _run(tmp_path, monkeypatch)
    assert stats["post_seconds"] > 0
    assert {"journal", "expiry"} <= set(stats["stage_times"])
    assert stats["post_seconds"] >= stats["stage_times"]["journal"]
    assert stats["by_source"]["src"]["seconds"] >= 0
    # the internal duration is taken before post-processing; the two are reported apart
    assert stats["duration_seconds"] > 0
