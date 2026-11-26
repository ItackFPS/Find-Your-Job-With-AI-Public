"""The per-collector validation criteria stay complete and point at real tests (Phase B B8-G, D7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.collectors.freeze import STAGED_SOURCE_TYPES, VALIDATED_COLLECTORS
from src.collectors.registry import registry
from src.collectors.validation import COLLECTORS, CRITERIA, report

ROOT = Path(__file__).resolve().parent.parent


def _registered_types() -> set[str]:
    return set(registry.list_types()) if hasattr(registry, "list_types") else set(registry._collectors)  # noqa: SLF001


def test_every_collector_has_a_validation_entry() -> None:
    assert set(COLLECTORS) == _registered_types() | set(STAGED_SOURCE_TYPES)


def test_every_configured_collector_covers_the_eight_criteria() -> None:
    assert CRITERIA == ("coverage", "reliability", "latency", "deduplication", "pagination", "error_handling",
                        "source_enablement", "access_rules")
    for c in COLLECTORS.values():
        if c.configured:
            assert set(c.criteria) == set(CRITERIA), c.source_type


def test_every_shipped_source_type_is_a_configured_entry() -> None:
    import yaml

    shipped = {s["type"] for s in yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))["sources"]}
    assert shipped | set(STAGED_SOURCE_TYPES) <= {t for t, c in COLLECTORS.items() if c.configured}


_EVIDENCE = sorted({e for c in COLLECTORS.values() for crit in c.criteria.values() for e in crit.evidence})


@pytest.mark.parametrize("ref", _EVIDENCE)
def test_each_evidence_reference_names_an_existing_test(ref: str) -> None:
    path, _, name = ref.partition("::")
    text = (ROOT / path).read_text(encoding="utf-8")
    assert re.search(rf"^\s*def {re.escape(name)}\(", text, re.M), ref


def test_no_collector_is_validated_while_it_has_open_items_or_unrecorded_supervised_checks() -> None:
    # Validation needs every criterion backed and no open item (D7); supervised runs of 2026-11-24 (#258).
    assert set(VALIDATED_COLLECTORS) == {"france_travail", "indeed", "hellowork"}
    for source_type in VALIDATED_COLLECTORS:
        assert COLLECTORS[source_type].ready_for_supervised_check()


def test_the_report_lists_each_configured_criterion_once() -> None:
    rows = report()
    configured = [c for c in COLLECTORS.values() if c.configured]
    assert len([r for r in rows if r["criterion"] != "-"]) == len(configured) * len(CRITERIA)
    assert {r["status"] for r in rows} <= {"open", "no evidence", "offline ok", "offline ok, supervised pending",
                                           "not configured"}
