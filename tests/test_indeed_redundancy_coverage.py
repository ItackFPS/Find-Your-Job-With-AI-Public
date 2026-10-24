"""Phase NEXT / I12 regression tests: FR/EU redundancy for Indeed.

I12's decision (see the phase report / commit for the full evaluation): NO
new collector was added. France Travail (official Pôle Emploi API, active,
exhaustive, unrestricted by keyword or seniority) already provides
authoritative, ToS-clean, general-purpose FR coverage independent of
Indeed, with EURES as a secondary/overlapping FR source. (APEC, once a
second FR source, was removed in 2.0: its bot protection blocks every
request, and its offers also reach EURES.)

These tests exist to keep that finding TRUE over time — each one would fail
if a future change quietly removed the exact property the redundancy
argument depends on (e.g. someone adds a `max_jobs` cap to france-travail,
or disables it, without realizing that also removes Indeed's redundancy).
They assert against the real, committed config/sources.yaml and the real
collector source, not a fixture — this is deliberately a "config/contract"
test, not a unit test of collection logic.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _load_sources() -> dict[str, dict]:
    with open("config/sources.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {src["name"]: src for src in data["sources"]}


class TestFranceTravailProvidesUnrestrictedRedundancy:
    """France Travail is the primary redundancy source for Indeed: an
    official government API, not a reverse-engineered one, with no ToS risk
    and no seniority/category restriction baked into its config."""

    def test_france_travail_is_active(self) -> None:
        sources = _load_sources()
        assert sources["france-travail"]["active"] is True

    def test_france_travail_config_has_no_max_jobs_cap(self) -> None:
        """Absence of max_jobs is what makes collection 'exhaustive' per its
        own config comment — a cap here would silently narrow coverage."""
        sources = _load_sources()
        assert "max_jobs" not in sources["france-travail"]["config"]

    def test_france_travail_config_has_no_keyword_or_category_restriction(self) -> None:
        """France Travail's collector config carries no keyword list,
        category filter, or seniority restriction — it searches the general
        French job market, including junior/entry-level roles."""
        config = _load_sources()["france-travail"]["config"]
        restrictive_keys = {"keywords", "category", "cadre", "seniority", "max_jobs"}
        assert not (restrictive_keys & config.keys())


class TestRemovedSourcesStayRemoved:
    """2.0 plan F1.4: APEC (bot protection with a CAPTCHA on every request)
    and the Welcome to the Jungle browser collector (behind a firewall) were
    removed deliberately. The shipped config must not list them again, and
    the pipeline must recognise them as removed so an old config that still
    lists them is skipped with a reason rather than failing."""

    def test_shipped_config_lists_no_removed_source(self) -> None:
        from src.collectors.registry import REMOVED_SOURCE_TYPES

        source_types = {src["type"] for src in _load_sources().values()}
        assert not (source_types & REMOVED_SOURCE_TYPES.keys())
        assert {"apec", "wttj"} <= REMOVED_SOURCE_TYPES.keys()


class TestEuresOverlapsFranceTravailByDesign:
    """EURES is documented as substantially overlapping France Travail for
    French postings (deduplication handles it) — it must not be
    double-counted as INDEPENDENT redundancy on top of France Travail."""

    def test_eures_is_active(self) -> None:
        sources = _load_sources()
        assert sources["eures"]["active"] is True


class TestIndeedRiskIsDocumentedNotSilentlyResolved:
    """I12 explicitly requires documenting Indeed's operational/legal
    limitation rather than pretending a technical change resolves it — this
    guards against that documentation silently disappearing later."""

    def test_indeed_collector_documents_tos_risk(self) -> None:
        source = Path("src/collectors/indeed.py").read_text(encoding="utf-8")
        assert "Terms of Service" in source
        assert "prohibited" in source

    def test_indeed_sources_yaml_entry_still_marked_active(self) -> None:
        """Confirms the redundancy question is live (Indeed is actually in
        use, not already disabled) — if this ever flips to False, I12's
        'what if Indeed disappears' premise no longer needs redundancy at
        all, and these tests' purpose should be revisited."""
        sources = _load_sources()
        assert sources["indeed"]["active"] is True


class TestNoRedundantCollectorWasAdded:
    """Guards the I12 decision itself: exactly the sources known before I12
    remain configured — no new FR/EU collector type was introduced merely
    to "check the box" on redundancy."""

    def test_source_types_match_pre_i12_baseline(self) -> None:
        sources = _load_sources()
        source_types = {src["type"] for src in sources.values()}
        # The complete set of collector types this project ships, as of
        # I12 — France Travail/EURES (redundancy sources) plus
        # every other already-existing type. Adding a name here later means
        # a new collector was introduced and this test should be updated
        # deliberately, not accidentally.
        # france_travail_web (2.0 plan F6.2): France Travail without credentials,
        # since its API no longer accepts new subscribers. apec and wttj were
        # removed in 2.0 (plan F1.4): blocked by a CAPTCHA and a firewall.
        expected_types = {
            "france_travail", "france_travail_web", "remoteok", "jobicy", "bundesagentur",
            "arbetsformedlingen", "eures", "hellowork",
            "indeed", "weworkremotely",
        }
        assert source_types == expected_types
