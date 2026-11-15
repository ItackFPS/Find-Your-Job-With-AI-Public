"""Run summary labels (Phase B B7-C): a skipped AI stage says so (P-016); the headline count is
the number the Jobs list shows (P-017). Temporary databases, fake collector, no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.collectors.base import BaseCollector, CollectorResult


def _job(n: int) -> dict[str, Any]:
    return {"source_job_id": f"j{n}", "url": f"https://example.test/j{n}", "title": f"Python Developer {n}",
            "company": f"Acme {n}", "location": "Paris", "description": "Python developer position, Django."}


class _Source(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        super().__init__(rate_limit_delay=0)

    def collect(self) -> CollectorResult:
        return CollectorResult(source_name="src", jobs=[_job(n) for n in range(6)])


@pytest.fixture
def ran(tmp_path: Path, monkeypatch):  # noqa: ANN201
    from src.api.runner import PipelineRunner
    from src.collectors import freeze
    from src.collectors.registry import registry
    from src.storage.runs import RunStorage

    monkeypatch.setattr(registry, "get_collector_class", lambda _type: _Source)
    monkeypatch.setattr(freeze, "ENFORCED", False)  # only the fake collector above runs
    config = tmp_path / "config"
    config.mkdir()
    (config / "profile.yaml").write_text("target_domains: [software_development]\ntarget_skills: [python]\n",
                                         encoding="utf-8")
    (config / "filters.yaml").write_text("domain_keywords:\n  software_development:\n    title: [developer]\n",
                                         encoding="utf-8")
    (config / "sources.yaml").write_text("sources:\n  - {name: src, type: src, active: true}\n", encoding="utf-8")
    db = str(tmp_path / "jobs.db")
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.create_run("r1")
    rs.close()
    PipelineRunner(db_path=db, config_dir=str(config))._execute("r1")
    rs = RunStorage(db_path=db)
    stats = (rs.get_run("r1") or {}).get("stats") or {}
    rs.close()
    return db, str(config), stats


def test_a_skipped_ai_stage_is_recorded_as_not_run(ran) -> None:  # noqa: ANN001
    _db, _cfg, stats = ran
    assert stats["llm_eligibility"]["ran"] is False


@pytest.mark.parametrize("min_tier", [None, "relevant", "low_priority"])
def test_the_count_is_the_number_the_list_shows(ran, isolated_app, monkeypatch, min_tier) -> None:  # noqa: ANN001
    from fastapi.testclient import TestClient

    db, cfg, _ = ran
    monkeypatch.setitem(isolated_app.state._state, "db_path", db)
    monkeypatch.setitem(isolated_app.state._state, "config_dir", cfg)
    params = {"min_tier": min_tier} if min_tier else {}
    with TestClient(isolated_app) as client:
        listed = client.get("/api/jobs", params={**params, "limit": 500}).json()
        count = client.get("/api/jobs/count", params=params).json()["count"]
    assert count == len(listed)
    if min_tier is None:
        assert count > 0
