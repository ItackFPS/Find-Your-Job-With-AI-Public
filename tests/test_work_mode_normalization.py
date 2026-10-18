"""Regression tests for work-mode normalization.

Canonical values: 'remote', 'hybrid', 'on_site', None (unknown).

CRITICAL invariant: None (unknown) != 'on_site'.
Absence of remote information must never be promoted to on-site.
"""

from __future__ import annotations

import pytest

from src.pipeline.normalize import normalize_work_mode


# ---------------------------------------------------------------------------
# Remote — English
# ---------------------------------------------------------------------------

class TestRemoteEnglish:
    @pytest.mark.parametrize("raw", [
        "remote",
        "Remote",
        "REMOTE",
        "full remote",
        "Full Remote",
        "fully remote",
        "100% remote",
        "100% Remote",
        "remote work",
        "work from home",
        "wfh",
        "distributed",
    ])
    def test_english_remote(self, raw):
        assert normalize_work_mode(raw) == "remote", f"Expected 'remote' for {raw!r}"


# ---------------------------------------------------------------------------
# Remote — French
# ---------------------------------------------------------------------------

class TestRemoteFrench:
    @pytest.mark.parametrize("raw", [
        "télétravail",
        "Télétravail",
        "TÉLÉTRAVAIL",
        "teletravail",
        "télétravail complet",
        "teletravail complet",
        "full télétravail",
        "full teletravail",
        "à distance",
        "a distance",
    ])
    def test_french_remote(self, raw):
        assert normalize_work_mode(raw) == "remote", f"Expected 'remote' for {raw!r}"


# ---------------------------------------------------------------------------
# Hybrid — English
# ---------------------------------------------------------------------------

class TestHybridEnglish:
    @pytest.mark.parametrize("raw", [
        "hybrid",
        "Hybrid",
        "HYBRID",
        "partial remote",
        "partially remote",
        "semi-remote",
        "flexible",
    ])
    def test_english_hybrid(self, raw):
        assert normalize_work_mode(raw) == "hybrid", f"Expected 'hybrid' for {raw!r}"


# ---------------------------------------------------------------------------
# Hybrid — French
# ---------------------------------------------------------------------------

class TestHybridFrench:
    @pytest.mark.parametrize("raw", [
        "hybride",
        "Hybride",
        "HYBRIDE",
        "télétravail partiel",
        "teletravail partiel",
        "remote partiel",
    ])
    def test_french_hybrid(self, raw):
        assert normalize_work_mode(raw) == "hybrid", f"Expected 'hybrid' for {raw!r}"


# ---------------------------------------------------------------------------
# On-site — English
# ---------------------------------------------------------------------------

class TestOnSiteEnglish:
    @pytest.mark.parametrize("raw", [
        "on-site",
        "On-Site",
        "ON-SITE",
        "onsite",
        "Onsite",
        "on site",
        "in office",
        "in-office",
        "on_site",
    ])
    def test_english_on_site(self, raw):
        assert normalize_work_mode(raw) == "on_site", f"Expected 'on_site' for {raw!r}"


# ---------------------------------------------------------------------------
# On-site — French
# ---------------------------------------------------------------------------

class TestOnSiteFrench:
    @pytest.mark.parametrize("raw", [
        "présentiel",
        "Présentiel",
        "PRÉSENTIEL",
        "presentiel",
        "sur site",
        "Sur site",
        "bureau",
    ])
    def test_french_on_site(self, raw):
        assert normalize_work_mode(raw) == "on_site", f"Expected 'on_site' for {raw!r}"


# ---------------------------------------------------------------------------
# Unknown — None is returned; on_site is NOT inferred from absence
# ---------------------------------------------------------------------------

class TestUnknown:
    @pytest.mark.parametrize("raw", [
        None,
        "",
        "   ",
        "non précisé",
        "à définir",
        "see job description",
        "varies",
        "negotiable",
        "TBD",
    ])
    def test_missing_or_ambiguous_is_none(self, raw):
        result = normalize_work_mode(raw)
        assert result is None, (
            f"Expected None (unknown) for {raw!r}, got {result!r}. "
            "Absence of remote information must never become 'on_site'."
        )

    def test_unknown_is_not_on_site(self):
        """The most critical invariant: unknown != on_site."""
        assert normalize_work_mode(None) != "on_site"
        assert normalize_work_mode("") != "on_site"
        assert normalize_work_mode("non précisé") != "on_site"

    def test_unknown_is_not_remote(self):
        assert normalize_work_mode(None) != "remote"
        assert normalize_work_mode("") != "remote"

    def test_unknown_is_not_hybrid(self):
        assert normalize_work_mode(None) != "hybrid"


# ---------------------------------------------------------------------------
# Already-canonical values pass through unchanged
# ---------------------------------------------------------------------------

class TestCanonicalPassthrough:
    def test_canonical_remote(self):
        assert normalize_work_mode("remote") == "remote"

    def test_canonical_hybrid(self):
        assert normalize_work_mode("hybrid") == "hybrid"

    def test_canonical_on_site(self):
        assert normalize_work_mode("on_site") == "on_site"
