"""Tests for src.discord_admin.matcher — the 0/1/many ambiguity-safe
resolution every channel/webhook lookup goes through. Must never pick a
candidate when more than one match exists.
"""

from __future__ import annotations

from src.discord_admin.matcher import (
    MatchOutcome,
    match_channel_by_name,
    match_webhook_by_name_prefix,
    resolve_unique,
)


class TestResolveUnique:
    def test_no_matches_is_none_outcome(self):
        result = resolve_unique([1, 2, 3], lambda x: x == 99)
        assert result.outcome is MatchOutcome.NONE
        assert result.match is None

    def test_single_match_is_one_outcome(self):
        result = resolve_unique([1, 2, 3], lambda x: x == 2)
        assert result.outcome is MatchOutcome.ONE
        assert result.match == 2

    def test_multiple_matches_is_ambiguous_never_picks_one(self):
        result = resolve_unique([1, 2, 2, 3], lambda x: x == 2)
        assert result.outcome is MatchOutcome.AMBIGUOUS
        assert result.match is None
        assert result.candidates == (2, 2)


class TestMatchChannelByName:
    def test_no_channel_named_that(self):
        result = match_channel_by_name([{"name": "job-01"}], "job-83")
        assert result.outcome is MatchOutcome.NONE

    def test_exact_single_match(self):
        channels = [{"id": "1", "name": "job-83"}, {"id": "2", "name": "job-01"}]
        result = match_channel_by_name(channels, "job-83")
        assert result.outcome is MatchOutcome.ONE
        assert result.match["id"] == "1"

    def test_duplicate_channel_names_are_ambiguous(self):
        channels = [{"id": "1", "name": "job-83"}, {"id": "2", "name": "job-83"}]
        result = match_channel_by_name(channels, "job-83")
        assert result.outcome is MatchOutcome.AMBIGUOUS
        assert len(result.candidates) == 2

    def test_partial_name_is_not_a_match(self):
        """job-830 must not match a search for job-83 — exact match only."""
        result = match_channel_by_name([{"name": "job-830"}], "job-83")
        assert result.outcome is MatchOutcome.NONE


class TestMatchWebhookByNamePrefix:
    def test_no_webhooks_at_all(self):
        result = match_webhook_by_name_prefix([], "job-83-webhook")
        assert result.outcome is MatchOutcome.NONE

    def test_single_prefix_match(self):
        webhooks = [{"id": "1", "name": "job-83-webhook"}]
        result = match_webhook_by_name_prefix(webhooks, "job-83-webhook")
        assert result.outcome is MatchOutcome.ONE

    def test_renamed_webhook_still_matches_by_prefix(self):
        webhooks = [{"id": "1", "name": "job-83-webhook (renamed)"}]
        result = match_webhook_by_name_prefix(webhooks, "job-83-webhook")
        assert result.outcome is MatchOutcome.ONE

    def test_unrelated_webhook_on_channel_is_ignored(self):
        webhooks = [{"id": "1", "name": "some-other-integration"}]
        result = match_webhook_by_name_prefix(webhooks, "job-83-webhook")
        assert result.outcome is MatchOutcome.NONE

    def test_two_webhooks_matching_prefix_are_ambiguous(self):
        webhooks = [
            {"id": "1", "name": "job-83-webhook"},
            {"id": "2", "name": "job-83-webhook-copy"},
        ]
        result = match_webhook_by_name_prefix(webhooks, "job-83-webhook")
        assert result.outcome is MatchOutcome.AMBIGUOUS
