"""Removed sources still listed in an installed sources.yaml (owner decision D10, Phase B B8-D).

Temporary folders and databases; fake collectors; no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.collectors.base import BaseCollector, CollectorResult
from src.discovery.registry import RETIRED_SOURCE_TYPES, SourceRegistry

V1_SOURCES = Path(__file__).parent / "fixtures" / "config_v1" / "sources.yaml"

_EDITED = """sources:
  - {name: src, type: src, active: true}
  - {name: remoteok, type: remoteok, active: true, config: {tags: [python]}}
  - {name: jobicy, type: jobicy, active: false}
  - {name: weworkremotely, type: weworkremotely, active: true}
"""


def test_the_removed_types_are_the_three_boards() -> None:
    assert RETIRED_SOURCE_TYPES == {"remoteok", "jobicy", "weworkremotely"}


def test_a_v1_sources_file_loads_without_the_removed_boards() -> None:
    names = {s["type"] for s in SourceRegistry(config_path=str(V1_SOURCES)).all_sources}
    assert names and not names & RETIRED_SOURCE_TYPES


def test_an_edited_file_keeps_its_removed_entries_when_saved(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(_EDITED, encoding="utf-8")
    reg = SourceRegistry(config_path=str(path))
    assert [s["name"] for s in reg.all_sources] == ["src"]
    assert reg.get_source("remoteok") is None
    reg.save(str(path))
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))["sources"]
    assert [s["name"] for s in saved] == ["src", "remoteok", "jobicy", "weworkremotely"]
    assert saved[1]["config"] == {"tags": ["python"]}          # the user's entry is unchanged


class _Source(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        job = {"source_job_id": "a1", "url": "https://example.test/a1", "title": "Python Developer",
               "company": "Acme", "location": "Paris", "description": "Python developer position."}
        return CollectorResult(source_name="src", jobs=[job], errors=[])


def _collector_class(src_type: str) -> type:
    if src_type != "src":
        raise KeyError(src_type)
    return _Source


@pytest.fixture
def run(tmp_path: Path, monkeypatch):  # noqa: ANN201
    from src.api.runner import PipelineRunner
    from src.collectors import freeze
    from src.collectors.registry import registry
    from src.storage.runs import RunStorage

    monkeypatch.setattr(registry, "get_collector_class", _collector_class)
    monkeypatch.setattr(freeze, "ENFORCED", False)  # only the fake collector above runs
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(
        "target_domains: [software_development]\ntarget_skills: [python]\n", encoding="utf-8")
    (config_dir / "filters.yaml").write_text(
        "domain_keywords:\n  software_development:\n    title: [developer]\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text(_EDITED, encoding="utf-8")
    db = str(tmp_path / "jobs.db")

    def _run() -> dict[str, Any]:
        rs = RunStorage(db_path=db)
        rs.init_db()
        rs.create_run("r1")
        rs.close()
        PipelineRunner(db_path=db, config_dir=str(config_dir))._execute("r1")
        rs = RunStorage(db_path=db)
        try:
            return (rs.get_run("r1") or {}).get("stats") or {}
        finally:
            rs.close()

    return _run


def test_a_run_neither_tries_nor_reports_a_removed_source(run) -> None:  # noqa: ANN001
    stats = run()
    assert set(stats["by_source"]) == {"src"}
    assert not [e for e in stats.get("errors", []) if "Unknown source type" in e]
    assert stats["outcome"] in ("successful", "first_run_empty")   # never "partial" because of them
