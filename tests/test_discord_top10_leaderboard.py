"""Tests for the top10-leaderboard-specific additions to
src.notifications.discord.DiscordProvider:
- _top10_leaderboard_embed: the standing-snapshot embed builder.
- send_or_update: create-or-edit-one-message transport, used ONLY by the
  top10 leaderboard update path (never by the fire-and-forget send() used
  everywhere else).

Mocks httpx exactly like the existing DiscordProvider tests in
tests/test_notification_engine.py (patch src.notifications.discord.httpx.*)
— no real network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx as _httpx

from src.notifications.discord import DiscordProvider


def _entry(rank, title, company="Acme", score_100=80, location="Toulon", url="https://example/apply", tier=None):
    entry = {
        "rank": rank,
        "title": title,
        "company": company,
        "location": location,
        "final_score": score_100 / 100,
        "score_100": score_100,
        "canonical_url": url,
        "apply_url": url,
    }
    if tier is not None:
        entry["tier"] = tier
    return entry


class TestTop10LeaderboardEmbed:
    def test_renders_ranked_entries(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": [_entry(1, "DevOps Engineer"), _entry(2, "Backend Dev")],
        })
        description = payload["embeds"][0]["description"]
        assert "1." in description and "DevOps Engineer" in description
        assert "2." in description and "Backend Dev" in description
        assert "VAR" in payload["embeds"][0]["title"]

    def test_remote_location_uses_globe_icon_and_no_location_suffix(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Remote France",
            "location_type": "remote",
            "entries": [_entry(1, "Remote Dev", location="Should not appear")],
        })
        assert "🌍" in payload["embeds"][0]["title"]
        assert "Should not appear" not in payload["embeds"][0]["description"]

    def test_empty_entries_shows_placeholder_not_crash(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": [],
        })
        assert "No active jobs" in payload["embeds"][0]["description"]

    def test_no_delta_language_present(self):
        """Unlike the new-arrivals batch embed, this is a complete
        standing snapshot re-rendered in place — it must never claim
        '+N more this run' (that concept doesn't apply to an edited message)."""
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": [_entry(1, "Dev")],
        })
        assert "more this run" not in payload["embeds"][0]["description"]

    def test_long_entries_stay_within_the_description_limit_and_whole(self):
        """Issue #66: the description was cut at 6000 characters, but
        Discord's description limit is 4096 — and a cut could land inside an
        entry. Entries are now kept whole, leading ranks first."""
        entries = [
            _entry(rank, f"Poste {rank}", url=f"https://example.com/{rank}?q=" + "x" * 480)
            for rank in range(1, 11)
        ]
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        description = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": entries,
        })["embeds"][0]["description"]

        assert len(description.encode("utf-16-le")) // 2 <= 4096
        shown = [e for e in entries if f"[Apply]({e['canonical_url']})" in description]
        assert 0 < len(shown) < 10
        assert shown == entries[: len(shown)]  # leading ranks, no gaps
        assert f"{10 - len(shown)} more not shown" in description

    def test_entries_that_fit_are_unchanged(self):
        entries = [_entry(rank, f"Poste {rank}") for rank in range(1, 11)]
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        description = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": entries,
        })["embeds"][0]["description"]
        assert "more not shown" not in description
        assert all(f"Poste {rank}\n" in description for rank in range(1, 11))


class TestTierEmojiReflectsActualTier:
    """Regression coverage for the bug where every leaderboard entry always
    fell back to the "relevant" (📋) emoji because compute_top10_by_slug
    never populated a `tier` key (fixed in src/notifications/top10_query.py).
    These tests prove the embed builder actually branches on a real tier
    when one is present — the pre-fix test suite only ever exercised the
    `entry.get("tier") or "relevant"` fallback path, which is exactly how
    the bug went unnoticed.
    """

    def test_exceptional_tier_renders_fire_emoji_not_relevant_fallback(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": [_entry(1, "Staff Engineer", score_100=90, tier="exceptional")],
        })
        description = payload["embeds"][0]["description"]
        assert "🔥" in description
        assert "📋" not in description

    def test_low_priority_tier_renders_archive_emoji(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": [_entry(1, "Intern", score_100=40, tier="low_priority")],
        })
        assert "🗄" in payload["embeds"][0]["description"]

    def test_missing_tier_key_still_falls_back_to_relevant(self):
        """Defensive fallback stays intact for any payload that somehow
        omits tier — never a crash, never a blank icon."""
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = p._build_embed({
            "event": "jobs.top10_leaderboard",
            "location_label": "Var",
            "location_type": "department",
            "entries": [_entry(1, "Unknown Tier Job", score_100=80)],
        })
        assert "📋" in payload["embeds"][0]["description"]


class TestSendOrUpdate:
    def test_no_webhook_url_returns_none(self):
        p = DiscordProvider(webhook_url="")
        assert p.send_or_update({"event": "jobs.top10_leaderboard", "entries": []}, "") is None

    def test_empty_message_id_creates_and_returns_new_id(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        mock_resp = MagicMock()
        mock_resp.status_code = 200  # a real httpx.Response always has one; the retry layer inspects it
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": "999"}
        with patch("src.notifications.discord.httpx.post", return_value=mock_resp) as mock_post:
            result = p.send_or_update({"event": "jobs.top10_leaderboard", "entries": []}, "")
        assert result == "999"
        assert "wait=true" in mock_post.call_args[0][0]

    def test_create_with_malformed_json_body_returns_none_not_raises(self):
        """The message genuinely posts (2xx) but the response body can't be
        parsed for an id — must return None (not raise, not a garbage id)
        so the caller correctly treats it as a failure and never persists a
        bogus message_id. This is a real orphan-message risk (the post did
        succeed, so a duplicate is created on the next update), caught by
        forensic failure-injection audit; regression-tested here to prove
        the failure mode returns a clean None rather than propagating a
        raw exception out of send_or_update (which must never raise)."""
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.side_effect = ValueError("not valid JSON")
        with patch("src.notifications.discord.httpx.post", return_value=mock_resp):
            result = p.send_or_update({"event": "jobs.top10_leaderboard", "entries": []}, "")
        assert result is None

    def test_existing_message_id_patches_and_returns_same_id(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        with patch("src.notifications.discord.httpx.patch", return_value=mock_resp) as mock_patch:
            result = p.send_or_update({"event": "jobs.top10_leaderboard", "entries": []}, "12345")
        assert result == "12345"
        assert "12345" in mock_patch.call_args[0][0]

    def test_deleted_message_falls_back_to_creating_a_new_one(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        patch_resp = MagicMock()
        patch_resp.status_code = 404
        post_resp = MagicMock()
        post_resp.status_code = 200  # a real httpx.Response always has one; the retry layer inspects it
        post_resp.raise_for_status = MagicMock()
        post_resp.json.return_value = {"id": "new-id"}
        with patch("src.notifications.discord.httpx.patch", return_value=patch_resp):
            with patch("src.notifications.discord.httpx.post", return_value=post_resp) as mock_post:
                result = p.send_or_update({"event": "jobs.top10_leaderboard", "entries": []}, "stale-id")
        assert result == "new-id"
        mock_post.assert_called_once()

    def test_http_error_on_create_returns_none(self):
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        with patch("src.notifications.discord.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = _httpx.HTTPStatusError(
                "400", request=MagicMock(), response=mock_resp
            )
            result = p.send_or_update({"event": "jobs.top10_leaderboard", "entries": []}, "")
        assert result is None
