"""Collector registry.

Maps source names to their collector classes. This is the central registry
that allows the pipeline to dynamically load and instantiate collectors by name.
"""

from __future__ import annotations

import logging

from src.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


class CollectorRegistry:
    """Central registry for all job collectors."""

    def __init__(self) -> None:
        self._collectors: dict[str, type[BaseCollector]] = {}
        self._register_default_collectors()

    def _register_default_collectors(self) -> None:
        """Register built-in collector implementations."""
        from src.collectors.apec import APECCollector
        from src.collectors.arbetsformedlingen import ArbetsformedlingenCollector
        from src.collectors.bundesagentur import BundesagenturCollector
        from src.collectors.eures import EURESCollector
        from src.collectors.france_travail import FranceTravailCollector
        from src.collectors.greenhouse import GreenhouseCollector
        from src.collectors.hellowork import HelloWorkCollector
        from src.collectors.jobicy import JobicyCollector
        from src.collectors.lever import LeverCollector
        from src.collectors.remote import RemoteCollector
        from src.collectors.remoteok import RemoteOKCollector
        from src.collectors.smartrecruiters import SmartRecruitersCollector
        from src.collectors.workingnomads import WorkingNomadsCollector
        from src.collectors.weworkremotely import WeWorkRemotelyCollector
        from src.collectors.wttj import WTTJCollector

        # greenhouse/lever/smartrecruiters/workingnomads below are real,
        # working, verified collectors kept registered even though no
        # config/sources.yaml entry currently references any of them —
        # they're dormant/experimental (a source config just hasn't been
        # written yet), not broken. See src/collectors/experimental/ for
        # code that IS broken and was deliberately removed from this
        # registry instead (Phase NOW / I4 audit, 2026-09-13).
        self._collectors["greenhouse"] = GreenhouseCollector
        self._collectors["lever"] = LeverCollector
        self._collectors["remote"] = RemoteCollector
        # 'rss' is an alias for RemoteCollector, which handles RSS internally
        # when the source config contains type='rss' in its config sub-dict.
        # Registering 'rss' here allows sources.yaml to use type: rss directly.
        self._collectors["rss"] = RemoteCollector
        self._collectors["france_travail"] = FranceTravailCollector
        self._collectors["remoteok"] = RemoteOKCollector
        self._collectors["jobicy"] = JobicyCollector
        self._collectors["workingnomads"] = WorkingNomadsCollector
        self._collectors["bundesagentur"] = BundesagenturCollector
        self._collectors["arbetsformedlingen"] = ArbetsformedlingenCollector
        self._collectors["smartrecruiters"] = SmartRecruitersCollector
        # APEC: httpx-based (no browser needed; uses the public JSON search API).
        self._collectors["apec"] = APECCollector
        # EURES: httpx-based (no browser needed; uses the public POST search API).
        self._collectors["eures"] = EURESCollector
        # We Work Remotely: RSS + HTML category-page discovery.
        self._collectors["weworkremotely"] = WeWorkRemotelyCollector
        # Indeed: httpx-based GraphQL API (no browser needed).
        from src.collectors.indeed import IndeedCollector
        self._collectors["indeed"] = IndeedCollector
        # Browser-based collectors (require crawl4ai optional dependency).
        # These classes have no top-level crawl4ai import — the registry always
        # loads successfully.  The ImportError surfaces at collect() time if
        # crawl4ai is not installed, and the orchestrator catches it cleanly.
        self._collectors["wttj"] = WTTJCollector
        # HelloWork: httpx-based, discovers via domain/metier listing pages and
        # extracts via schema.org/JobPosting JSON-LD (no API, no browser needed).
        self._collectors["hellowork"] = HelloWorkCollector

    def register(self, name: str, collector_class: type[BaseCollector]) -> None:
        """Register a new collector class by name."""
        if not issubclass(collector_class, BaseCollector):
            raise TypeError(f"{collector_class.__name__} must be a subclass of BaseCollector")
        self._collectors[name] = collector_class
        logger.info("Registered collector '%s'", name)

    def unregister(self, name: str) -> None:
        """Unregister a collector by name."""
        if name in self._collectors:
            del self._collectors[name]
            logger.info("Unregistered collector '%s'", name)

    def get_collector_class(self, name: str) -> type[BaseCollector]:
        """Get the collector class for a given source name."""
        if name not in self._collectors:
            raise KeyError(f"Unknown collector: {name}")
        return self._collectors[name]

    def list_collectors(self) -> list[str]:
        """List all registered collector names."""
        return list(self._collectors.keys())


# Module-level singleton instance
registry = CollectorRegistry()