"""The orchestrator hands each collector what the profile's line of work supplies.

A discovery preset (config/presets/) supplies collector config keys (``queries``,
``categories``, ``occupations`` …) only for keys the source's own config leaves empty;
a profile outside every preset gets none of it, only its own discovery queries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.collectors.base import BaseCollector, CollectorResult

_SEEN: dict[str, dict[str, Any]] = {}


class _Recorder(BaseCollector):
    def __init__(self, config: dict | None = None) -> None:
        self._config = dict(config or {})

    def collect(self) -> CollectorResult:
        _SEEN["config"] = self._config
        return CollectorResult(source_name="eures", jobs=[])


def _run(tmp_path: Path, monkeypatch, profile_yaml: str, source_config: str = "") -> dict[str, Any]:
    from src.collectors.registry import registry
    from src.pipeline.orchestrator import run_pipeline

    _SEEN.clear()
    monkeypatch.setattr(registry, "get_collector_class", lambda src_type: _Recorder)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.yaml").write_text(profile_yaml, encoding="utf-8")
    (config_dir / "filters.yaml").write_text("{}\n", encoding="utf-8")
    (config_dir / "sources.yaml").write_text(
        "sources:\n  - {name: eures, type: eures, active: true" + source_config + "}\n",
        encoding="utf-8",
    )
    result = run_pipeline(db_path=str(tmp_path / "t.db"), config_dir=str(config_dir))
    assert result.status == "completed", result.error
    return _SEEN["config"]


def test_it_profile_gets_the_preset_queries(tmp_path, monkeypatch) -> None:
    config = _run(tmp_path, monkeypatch, "target_domains: [software_development]\n")
    assert {"keyword": "software engineer", "location_codes": ["be", "lu", "ch"]} in config["queries"]


def test_the_source_config_wins_over_the_preset(tmp_path, monkeypatch) -> None:
    config = _run(
        tmp_path, monkeypatch, "target_domains: [software_development]\n",
        ", config: {queries: [{keyword: mine, location_codes: [fr]}]}",
    )
    assert config["queries"] == [{"keyword": "mine", "location_codes": ["fr"]}]


def test_a_nurse_gets_her_own_queries_and_no_it_config(tmp_path, monkeypatch) -> None:
    config = _run(tmp_path, monkeypatch, "target_domains: [infirmier]\n")
    assert "queries" not in config
    assert config["discovery_queries"] == ["infirmier"]


def test_target_occupations_are_searched(tmp_path, monkeypatch) -> None:
    config = _run(
        tmp_path, monkeypatch,
        "target_occupations:\n  - {label: electrician, keyword: electrician, label_fr: électricien}\n",
    )
    assert config["discovery_queries"] == ["électricien"]


@pytest.mark.parametrize("profile", ["target_domains: []\n", "{}\n"])
def test_an_empty_profile_searches_nothing_by_default(tmp_path, monkeypatch, profile) -> None:
    config = _run(tmp_path, monkeypatch, profile)
    assert "queries" not in config
    assert not config.get("discovery_queries")
