"""Phase NOW / I4 regression tests: the collector registry must never
misleadingly imply a dead/broken collector is available, while genuinely
working-but-unconfigured collectors stay registered exactly as before.

Background: src/collectors/registry.py registered 5 collector types with no
corresponding entry anywhere in config/sources.yaml. Four of them
(greenhouse, lever, smartrecruiters, workingnomads) are real, verified,
contract-compliant implementations — just not configured with a
board/company identifier yet. The fifth, "linkedin", targeted a LinkedIn
endpoint that doesn't exist and violated the collector base contract by
constructing Job instances directly — genuinely broken, unlike the other
four, and unrelated to the project's real LinkedIn integration
(src.linkedin.scanner.LinkedInScanner). It has been moved to
src/collectors/experimental/ and removed from the registry; the other four
are untouched.
"""

from __future__ import annotations

import pytest

from src.collectors.registry import CollectorRegistry


class TestDeadCollectorQuarantine:
    def test_linkedin_is_not_a_registered_collector_type(self) -> None:
        """No sources.yaml entry can reference the broken collector — a typo
        like `type: linkedin` in config must fail loudly (KeyError via
        get_collector_class), not silently resolve to dead code."""
        reg = CollectorRegistry()
        assert "linkedin" not in reg.list_collectors()
        with pytest.raises(KeyError):
            reg.get_collector_class("linkedin")

    def test_experimental_linkedin_module_still_importable(self) -> None:
        """Quarantined, not deleted — the code is preserved for reference."""
        from src.collectors.experimental.linkedin import LinkedInCollector

        assert LinkedInCollector is not None

    def test_experimental_linkedin_collector_never_runs_via_pipeline(self) -> None:
        """Even if instantiated directly (bypassing the registry entirely,
        which no pipeline code path does), it must still honor the base
        collector contract of never raising out of collect()."""
        from src.collectors.experimental.linkedin import LinkedInCollector

        collector = LinkedInCollector()
        result = collector.collect()
        assert result.source_name == "linkedin"
        assert isinstance(result.jobs, list)
        assert isinstance(result.errors, list)


class TestDormantButWorkingCollectorsUnaffected:
    """These four are legitimate, working, just unconfigured — I4 must not
    remove or otherwise change their availability."""

    @pytest.mark.parametrize(
        "collector_type",
        ["greenhouse", "lever", "smartrecruiters", "workingnomads"],
    )
    def test_still_registered(self, collector_type: str) -> None:
        reg = CollectorRegistry()
        assert collector_type in reg.list_collectors()
        # Must resolve to an actual class, not raise.
        cls = reg.get_collector_class(collector_type)
        assert cls is not None

    def test_none_of_the_dormant_four_have_a_sources_yaml_entry(self) -> None:
        """Confirms the premise of this whole item: these are dormant
        because nothing configures them, not because they were disabled —
        distinguishing 'unconfigured' from 'active: false'."""
        from pathlib import Path

        from src.discovery.registry import SourceRegistry

        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.yaml"
        registry = SourceRegistry(config_path=str(config_path))
        configured_types = {s.get("type") for s in registry.all_sources}
        for dormant_type in ("greenhouse", "lever", "smartrecruiters", "workingnomads", "linkedin"):
            assert dormant_type not in configured_types
