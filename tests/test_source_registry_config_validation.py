"""Phase NEXT / I7 regression tests: sources.yaml must fail clearly and early
on malformed entries, instead of raising a raw KeyError/TypeError deep
inside whichever caller first happens to touch the bad field.

Only the envelope every collector relies on (name/active/type) is validated
here — each collector's own ``config`` sub-dict shape legitimately varies
per source and is deliberately left unvalidated (I7's own scope note: "the
goal is validation, not configuration expansion").
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.discovery.registry import SourceRegistry


def _write_sources(tmp_path: Path, data: object) -> str:
    path = tmp_path / "sources.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return str(path)


class TestValidConfiguration:
    def test_missing_file_yields_empty_registry(self, tmp_path: Path) -> None:
        registry = SourceRegistry(config_path=str(tmp_path / "does-not-exist.yaml"))
        assert registry.all_sources == []

    def test_missing_sources_key_yields_empty_registry(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"unrelated": True})
        registry = SourceRegistry(config_path=path)
        assert registry.all_sources == []

    def test_minimal_valid_entry_loads(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": [{"name": "greenhouse"}]})
        registry = SourceRegistry(config_path=path)
        assert registry.get_source("greenhouse") == {"name": "greenhouse"}

    def test_full_valid_entry_loads(self, tmp_path: Path) -> None:
        path = _write_sources(
            tmp_path,
            {"sources": [{"name": "apec", "type": "apec", "active": True, "config": {"pages": 3}}]},
        )
        registry = SourceRegistry(config_path=path)
        src = registry.get_source("apec")
        assert src is not None
        assert src["type"] == "apec"
        assert src["active"] is True

    def test_real_sources_yaml_still_loads(self) -> None:
        """Guard against this change breaking the actual committed config."""
        registry = SourceRegistry(config_path="config/sources.yaml")
        assert len(registry.all_sources) > 0


class TestMalformedTopLevel:
    def test_non_mapping_top_level_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, ["not", "a", "mapping"])
        with pytest.raises(TypeError, match="expected a mapping"):
            SourceRegistry(config_path=path)

    def test_sources_not_a_list_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": {"name": "oops"}})
        with pytest.raises(TypeError, match="must be a list"):
            SourceRegistry(config_path=path)


class TestMalformedEntry:
    def test_entry_missing_name_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": [{"type": "greenhouse"}]})
        with pytest.raises(ValueError, match="'name' is required"):
            SourceRegistry(config_path=path)

    def test_entry_with_empty_name_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": [{"name": "   "}]})
        with pytest.raises(ValueError, match="'name' is required"):
            SourceRegistry(config_path=path)

    def test_non_mapping_entry_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": ["greenhouse"]})
        with pytest.raises(TypeError, match="expected a mapping"):
            SourceRegistry(config_path=path)

    def test_non_bool_active_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": [{"name": "apec", "active": "yes"}]})
        with pytest.raises(ValueError, match="'active' must be true/false"):
            SourceRegistry(config_path=path)

    def test_non_string_type_raises_clear_error(self, tmp_path: Path) -> None:
        path = _write_sources(tmp_path, {"sources": [{"name": "apec", "type": 42}]})
        with pytest.raises(ValueError, match="'type' must be a string"):
            SourceRegistry(config_path=path)

    def test_error_identifies_which_entry_by_index(self, tmp_path: Path) -> None:
        path = _write_sources(
            tmp_path, {"sources": [{"name": "greenhouse"}, {"type": "lever"}]}
        )
        with pytest.raises(ValueError, match=r"sources\[1\]"):
            SourceRegistry(config_path=path)
