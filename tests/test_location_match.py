"""Tests for src.pipeline.location_match.resolve_job_location — the single
shared resolver scoring and Discord notification routing both call to
answer "which configured location preference does this job match, and at
what priority?"
"""
from __future__ import annotations

from src.pipeline.location_match import LocationMatch, resolve_job_location


def _pref(type_: str, value: str, status: str = "preferred", label: str | None = None) -> dict:
    d = {"type": type_, "value": value, "status": status}
    if label is not None:
        d["label"] = label
    return d


class TestNoPreferencesConfigured:
    def test_empty_list_matches_nothing(self):
        assert resolve_job_location("83", False, ["France"], []) is None


class TestDepartmentMatching:
    def test_matching_department_resolves(self):
        match = resolve_job_location("83", False, ["France"], [_pref("department", "83")])
        assert match is not None
        assert match.slug == "dept-83"
        assert match.priority == 0
        assert match.status == "preferred"

    def test_non_matching_department_resolves_to_none(self):
        assert resolve_job_location("69", False, ["France"], [_pref("department", "83")]) is None

    def test_none_department_never_matches_department_preference(self):
        assert resolve_job_location(None, False, ["France"], [_pref("department", "83")]) is None


class TestRemoteMatching:
    def test_remote_job_matches_remote_preference(self):
        match = resolve_job_location(None, True, ["France"], [_pref("remote", "remote_france")])
        assert match is not None
        assert match.slug == "remote-remote_france"

    def test_non_remote_job_does_not_match_remote_preference(self):
        assert resolve_job_location(None, False, ["France"], [_pref("remote", "remote_france")]) is None

    def test_remote_with_no_country_data_still_matches(self):
        """Many remote listings omit countries entirely — absence of
        country data should not block a remote match."""
        match = resolve_job_location(None, True, [], [_pref("remote", "remote_france")])
        assert match is not None

    def test_remote_with_non_france_country_does_not_match(self):
        assert resolve_job_location(None, True, ["Germany"], [_pref("remote", "remote_france")]) is None


class TestPriorityOrdering:
    def test_first_matching_preference_in_list_order_wins(self):
        prefs = [_pref("department", "06"), _pref("department", "83")]
        match = resolve_job_location("83", False, ["France"], prefs)
        assert match is not None
        assert match.value == "83"
        assert match.priority == 1

    def test_job_matching_multiple_prefs_resolves_to_highest_priority(self):
        """A job whose department AND remote status both match configured
        preferences resolves to whichever is EARLIER in the list —
        regardless of which match is 'better' in any other sense."""
        prefs = [_pref("department", "69"), _pref("remote", "remote_france")]
        match = resolve_job_location("69", True, ["France"], prefs)
        assert match is not None
        assert match.slug == "dept-69"
        assert match.priority == 0

    def test_reordering_changes_the_winner(self):
        """Same job, same preferences, different order -> different winner —
        proves priority is driven by list position, not preference content."""
        prefs_var_first = [_pref("department", "69"), _pref("remote", "remote_france")]
        prefs_remote_first = [_pref("remote", "remote_france"), _pref("department", "69")]
        match_a = resolve_job_location("69", True, ["France"], prefs_var_first)
        match_b = resolve_job_location("69", True, ["France"], prefs_remote_first)
        assert match_a.slug == "dept-69"
        assert match_b.slug == "remote-remote_france"


class TestExcludedIsAPerMatchVeto:
    def test_excluded_only_match_resolves_to_none(self):
        match = resolve_job_location("75", False, ["France"], [_pref("department", "75", "excluded")])
        assert match is None

    def test_excluded_does_not_block_a_different_non_excluded_match(self):
        """Exclusion vetoes only ITS OWN entry — a job that also matches a
        different, non-excluded preference (at ANY priority, including
        lower priority than the excluded one) still resolves to that match."""
        prefs = [_pref("department", "75", "excluded"), _pref("remote", "remote_france")]
        match = resolve_job_location(None, True, ["France"], prefs)
        assert match is not None
        assert match.slug == "remote-remote_france"

    def test_excluded_higher_priority_does_not_shadow_lower_priority_preferred_department(self):
        prefs = [_pref("department", "75", "excluded"), _pref("department", "69", "preferred")]
        match = resolve_job_location("69", False, ["France"], prefs)
        assert match is not None
        assert match.value == "69"

    def test_acceptable_still_wins_over_no_match(self):
        match = resolve_job_location("83", False, ["France"], [_pref("department", "83", "acceptable")])
        assert match is not None
        assert match.status == "acceptable"


class TestSlugStability:
    def test_slug_depends_on_value_not_list_position(self):
        """Slugs must stay stable across reorders (so Discord channel config
        keyed by slug doesn't break when the user reprioritizes)."""
        prefs_a = [_pref("department", "06"), _pref("department", "83")]
        prefs_b = [_pref("department", "83"), _pref("department", "06")]
        match_a = resolve_job_location("83", False, ["France"], prefs_a)
        match_b = resolve_job_location("83", False, ["France"], prefs_b)
        assert match_a.slug == match_b.slug == "dept-83"


class TestLabelPassthrough:
    def test_custom_label_is_carried_through(self):
        match = resolve_job_location("83", False, ["France"], [_pref("department", "83", label="Var")])
        assert match is not None
        assert match.label == "Var"

    def test_missing_label_is_none(self):
        match = resolve_job_location("83", False, ["France"], [_pref("department", "83")])
        assert match is not None
        assert match.label is None


class TestReturnTypeIsFrozenDataclass:
    def test_returns_location_match_instance(self):
        match = resolve_job_location("83", False, ["France"], [_pref("department", "83")])
        assert isinstance(match, LocationMatch)
