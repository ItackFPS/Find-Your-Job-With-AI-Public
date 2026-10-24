"""Semantic matching only refines the ranking: if the embedding model cannot be
downloaded or loaded, the run keeps the deterministic ranking instead of failing.
"""

from __future__ import annotations

from pathlib import Path

from src.collectors.base import BaseCollector, CollectorResult


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


class _ModelUnavailable:
    def __init__(self, *args: object, **kwargs: object) -> None:
        raise OSError("could not download the embedding model")


def test_embedding_failure_keeps_the_deterministic_ranking(tmp_path: Path, monkeypatch) -> None:
    from src.collectors.registry import registry
    from src.embeddings import embedding
    from src.pipeline import orchestrator

    monkeypatch.setattr(registry, "get_collector_class", lambda _src_type: _Healthy)
    monkeypatch.setattr(embedding, "EmbeddingService", _ModelUnavailable)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(
        "target_domains: [software_development]\ntarget_skills: [python]\n", encoding="utf-8"
    )
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text(
        "sources:\n  - {name: healthy, type: healthy, active: true}\n", encoding="utf-8"
    )

    result = orchestrator.run_pipeline(db_path=str(tmp_path / "t.db"), config_dir=str(config_dir))

    assert result.status == "completed", result.error
    assert result.stats.semantically_matched == 0
    assert any("Semantic matching skipped: OSError" in e for e in result.stats.errors)
    assert result.stats.by_source["healthy"]["collected"] == 1
