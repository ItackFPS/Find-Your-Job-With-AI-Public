"""Parser canary: "the site changed" instead of a silent 0 (plan §C1, F6.1).

A collector that scrapes pages or app endpoints ships a recorded fixture
(``BaseCollector.fixture()``); a test parses it to at least one job, so a parser change
that breaks the known format fails CI. At run time, when such a collector returns no job
although the source answered (HTTP 200 with a non-trivial body), the run reports
``parser_changed`` for it rather than an unexplained 0 (v1's HelloWork bug was exactly
that: pages were served, the parser no longer found the offers).
"""

from __future__ import annotations

from typing import Any

from src.collectors.base import BaseCollector

PARSER_CHANGED = "parser_changed"


def has_canary(collector: BaseCollector) -> bool:
    """Whether the collector ships a fixture (the canary only covers those collectors:
    for a documented API an empty answer can be genuine)."""
    if type(collector).fixture is BaseCollector.fixture:
        return False
    try:
        return collector.fixture() is not None
    except OSError:  # a fixture file missing from an installation: no canary, never a crash
        return False


def parser_canary(collector: BaseCollector, jobs_count: int) -> str | None:
    """``"parser_changed"`` when a canary-covered collector returned no job although at
    least one page parsed for offers was HTTP 200 and ``canary_min_bytes`` or larger, and
    none was recognised as a genuine empty result; otherwise ``None``. robots.txt,
    sitemaps and taxonomy indexes never count as pages served."""
    if jobs_count > 0 or not has_canary(collector):
        return None
    health = collector.health()
    if health.get("empty_results", 0):
        return None  # the collector recognised a genuine "no offer" answer
    if health.get("ok_responses", 0) and health.get("largest_ok_bytes", 0) >= collector.canary_min_bytes:
        return PARSER_CHANGED
    return None


def canary_message(collector: BaseCollector) -> str:
    health = collector.health()
    return (f"{PARSER_CHANGED}: the source answered ({health.get('ok_responses', 0)} page(s) with HTTP 200, "
            f"up to {health.get('largest_ok_bytes', 0)} bytes) but no offer could be read from it; "
            "its page format has probably changed")


def parse_fixture(collector: BaseCollector) -> list[dict[str, Any]]:
    """The raw jobs the collector's parser reads from its recorded fixture ([] without one)."""
    fixture = collector.fixture()
    return fixture.parse(fixture.content) if fixture is not None else []
