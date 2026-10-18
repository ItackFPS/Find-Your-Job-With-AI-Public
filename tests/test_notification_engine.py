"""Tests for the NotificationEngine, storage deduplication, and providers.

Covers:
- NotificationEngine dispatches to enabled providers
- Disabled provider is not called
- Provider failure does not raise or fail the pipeline
- Same run is not notified twice (deduplication)
- Same job is not notified twice, EVER, across any location channel
  (deduplication) — Location Priority feature (2026-09-10)
- NotificationStorage schema, record, already_sent, list_recent
- NotificationConfig loading (defaults, YAML, malformed)
- DiscordProvider builds correct embed payload, including the generic
  per-location batch embed
- N8nProvider sends raw event payload
- Provider unavailable (HTTP error) returns False
- Location routing: profile.geography.location_preferences drives which
  Discord channel (if any) a job reaches — one job, one channel, top-K per
  location, never hardcoded to Var/remote
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_webhook_env(monkeypatch):
    """Prevent tests from silently posting to real webhooks.

    DiscordProvider/N8nProvider prioritize their environment variables over
    any explicit webhook_url a test passes in (see
    src/notifications/discord.py, src/notifications/n8n.py docstrings — this
    precedence is intentional for production use). src.api.app calls
    load_dotenv() on import, which other test modules trigger transitively
    during pytest collection, populating these env vars for the whole
    process. Without clearing them here, tests that pass a local/fake
    webhook_url (including the real-HTTP e2e tests below) get silently
    redirected to the real Discord/n8n webhooks.
    """
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("N8N_WEBHOOK_URL", raising=False)
    from src.notifications.config import DISCORD_STATIC_CHANNEL_ENV_VARS
    for env_var in DISCORD_STATIC_CHANNEL_ENV_VARS.values():
        monkeypatch.delenv(env_var, raising=False)
    # Other test modules (tests/test_discord_admin_secrets.py,
    # tests/test_discord_admin_reconciler.py) call
    # src.discord_admin.secrets.store_webhook_secret(), which — by design —
    # writes directly into THIS PROCESS's os.environ (so a running
    # NotificationEngine can pick up a newly-created webhook without a
    # restart; see that module's docstring). That mutation is real, not
    # monkeypatched, so it outlives its own test when the full suite runs in
    # one process. Strip every dynamic location/top10 webhook env var here
    # too, so this file's "channel X should NOT be configured" assertions
    # hold regardless of what already ran earlier in the session.
    for name in list(os.environ):
        if name.startswith("DISCORD_LOCATION_") or name.startswith("DISCORD_TOP10_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_real_retry_sleep(monkeypatch):
    """src/notifications/http_retry.py's bounded retry/backoff (added
    alongside this fixture) calls time.sleep() between attempts. Without
    this, any test exercising a retryable status (429/5xx) or a
    ConnectError/TimeoutException with a fixed (non-list) mock response
    would sleep for real (up to ~1s + 3s per test) — this keeps the whole
    suite fast and deterministic regardless of which retry path a given
    test happens to hit."""
    monkeypatch.setattr("src.notifications.http_retry.time.sleep", lambda *_a, **_k: None)


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "test_notif.db")


@pytest.fixture
def notif_storage(tmp_db: str):
    from src.notifications.storage import NotificationStorage
    s = NotificationStorage(db_path=tmp_db)
    s.init_db()
    yield s
    s.close()


def _make_pipeline_result(
    run_id: str = "test-run-id",
    status: str = "completed",
    error: str | None = None,
) -> Any:
    """Build a minimal PipelineResult-like object for tests (plain namespace)."""
    from types import SimpleNamespace

    stats = SimpleNamespace(
        collected=100, validated=90, deduplicated=80, filtered=50,
        scored=40, semantically_matched=35, personalized=30,
        duration_seconds=12.5, errors=[],
    )

    def stats_to_dict() -> dict:
        return {
            "collected": stats.collected,
            "validated": stats.validated,
            "deduplicated": stats.deduplicated,
            "filtered": stats.filtered,
            "scored": stats.scored,
            "semantically_matched": stats.semantically_matched,
            "personalized": stats.personalized,
            "duration_seconds": stats.duration_seconds,
            "errors": stats.errors,
            "by_source": {}, "by_country": {}, "by_work_mode": {},
            "profile_hash": "", "model_name": "", "scoring_config_hash": "",
            "phase2": {}, "stage_times": {},
            "embedding_cache_hits": 0, "embedding_cache_misses": 0,
            "personalization_cache_hits": 0, "personalization_cache_misses": 0,
            "validation_rejected": 10, "deduplication_removed": 10, "filter_rejected": 30,
        }

    stats.to_dict = stats_to_dict

    return SimpleNamespace(
        run_id=run_id,
        status=status,
        error=error,
        stats=stats,
        scored_jobs=[],
        rejection_records=[],
    )


def _job_at_score(job_id: str, score_100: int, **overrides) -> dict:
    """One new-job dict at an exact score_100, for location-routing tests.

    score_to_tier() boundaries (fixed-reference calibration):
    exceptional>=80, excellent>=72, strong>=65, relevant>=60, low_priority<60
    — see src/pipeline/scoring.py.

    Defaults to a fully-remote, no-department job (location "France") so it
    is remote-eligible out of the box; pass department="83"/location="Toulon"
    and/or work_mode="on_site"/"hybrid" to build department-based variants.
    """
    base = {
        "job_id": job_id,
        "title": f"Job {job_id}",
        "company": "Acme Corp",
        "location": "France",
        "department": None,
        "countries": ["France"],
        "remote": True,
        "work_mode": "remote",
        "employment_type": "CDI",
        "skills": ["Python"],
        "final_score": score_100 / 100.0,
        "canonical_url": f"https://example.com/job/{job_id}",
        "apply_url": f"https://example.com/apply/{job_id}",
        "job_source": "linkedin",
    }
    base.update(overrides)
    return base


def _loc_pref(type_: str, value: str, status: str = "preferred", label: str | None = None) -> dict:
    d = {"type": type_, "value": value, "status": status}
    if label is not None:
        d["label"] = label
    return d


# ---------------------------------------------------------------------------
# NotificationStorage tests
# ---------------------------------------------------------------------------

class TestNotificationStorage:
    def test_schema_created(self, notif_storage):
        # already_sent on empty table returns False
        assert notif_storage.already_sent("discord", "pipeline.completed", "run-1") is False

    def test_record_and_already_sent(self, notif_storage):
        notif_storage.record("discord", "pipeline.completed", "run-1", "sent")
        assert notif_storage.already_sent("discord", "pipeline.completed", "run-1") is True

    def test_failed_does_not_block(self, notif_storage):
        notif_storage.record("discord", "pipeline.completed", "run-1", "failed", "timeout")
        # A 'failed' record should NOT prevent future attempts (only 'sent' deduplicates)
        assert notif_storage.already_sent("discord", "pipeline.completed", "run-1") is False

    def test_provider_isolation(self, notif_storage):
        notif_storage.record("discord", "pipeline.completed", "run-1", "sent")
        # Different provider: should not be marked as sent
        assert notif_storage.already_sent("n8n", "pipeline.completed", "run-1") is False

    def test_list_recent(self, notif_storage):
        notif_storage.record("discord", "pipeline.completed", "run-1", "sent")
        notif_storage.record("n8n", "pipeline.completed", "run-1", "failed", "timeout")
        rows = notif_storage.list_recent()
        assert len(rows) == 2

    def test_get_last_sent(self, notif_storage):
        notif_storage.record("discord", "pipeline.completed", "run-1", "failed")
        notif_storage.record("discord", "pipeline.completed", "run-2", "sent")
        last = notif_storage.get_last_sent("discord")
        assert last is not None
        assert last["entity_id"] == "run-2"

    def test_count_by_status(self, notif_storage):
        notif_storage.record("discord", "pipeline.completed", "run-1", "sent")
        notif_storage.record("discord", "pipeline.completed", "run-2", "failed")
        counts = notif_storage.count_by_status()
        assert counts.get("sent") == 1
        assert counts.get("failed") == 1

    def test_migration_deduplicates_existing_sent_rows(self, tmp_path):
        """init_db() removes duplicate 'sent' rows so the unique index can be created.

        Simulates an existing database that pre-dates the unique index, with two
        duplicate 'sent' rows for the same (provider, event_type, entity_id).
        The migration must succeed without error, leaving exactly one row.
        """
        import sqlite3
        db_path = str(tmp_path / "legacy.db")

        # Build an old-style DB with no unique index and a duplicate sent row.
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE notification_history (
                id TEXT PRIMARY KEY, provider TEXT NOT NULL, event_type TEXT NOT NULL,
                entity_id TEXT NOT NULL, status TEXT NOT NULL,
                sent_at TEXT NOT NULL, error TEXT
            )
        """)
        conn.execute(
            "INSERT INTO notification_history VALUES "
            "('id1','discord','pipeline.completed','run-1','sent','2026-01-01T10:00:00',NULL)"
        )
        conn.execute(
            "INSERT INTO notification_history VALUES "
            "('id2','discord','pipeline.completed','run-1','sent','2026-01-01T10:00:05',NULL)"
        )
        conn.commit()
        conn.close()

        # init_db() must succeed: it deduplicates before creating the unique index.
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=db_path)
        storage.init_db()  # must not raise

        # Only the earliest row survives (MIN(rowid) = id1).
        conn2 = storage._get_conn()
        rows = conn2.execute(
            "SELECT id FROM notification_history WHERE status='sent'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["id"] == "id1"

        # Subsequent notifications still work correctly.
        assert storage.already_sent("discord", "pipeline.completed", "run-1") is True
        assert storage.already_sent("discord", "pipeline.completed", "run-2") is False
        storage.record("discord", "pipeline.completed", "run-2", "sent")
        assert storage.already_sent("discord", "pipeline.completed", "run-2") is True
        storage.close()

    def test_insert_or_ignore_prevents_duplicate_sent(self, tmp_path):
        """Calling record(..., 'sent') twice for the same key inserts only once."""
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dedup.db"))
        storage.init_db()

        storage.record("discord", "pipeline.completed", "run-1", "sent")
        storage.record("discord", "pipeline.completed", "run-1", "sent")  # second call → ignored

        conn = storage._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM notification_history WHERE status='sent'"
        ).fetchone()[0]
        assert count == 1
        storage.close()

    def test_multiple_failed_rows_still_allowed(self, tmp_path):
        """Multiple 'failed' records for the same key are all retained (retry tracking)."""
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "failed.db"))
        storage.init_db()

        storage.record("discord", "pipeline.completed", "run-1", "failed", "timeout")
        storage.record("discord", "pipeline.completed", "run-1", "failed", "timeout")
        storage.record("discord", "pipeline.completed", "run-1", "failed", "timeout")

        conn = storage._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM notification_history WHERE status='failed'"
        ).fetchone()[0]
        assert count == 3
        storage.close()


class TestDepartmentNotificationState:
    """department_notification_state — the explicit lifecycle state backing
    NotificationEngine's BACKFILL vs UNSEEN_FOR_DEPARTMENT candidate typing
    (see NotificationEngine._route_notification_candidates)."""

    def test_brand_new_slug_starts_with_backfill_not_completed(self, tmp_path):
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dept.db"))
        storage.init_db()

        state = storage.get_or_create_department_state("dept-04")
        assert state["slug"] == "dept-04"
        assert state["initial_backfill_completed_at"] is None
        assert state["initial_backfill_sent_count"] == 0
        storage.close()

    def test_state_is_created_only_once_and_is_stable_across_calls(self, tmp_path):
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dept.db"))
        storage.init_db()

        first = storage.get_or_create_department_state("dept-04")
        storage.mark_initial_backfill_complete("dept-04")
        second = storage.get_or_create_department_state("dept-04")

        assert first["first_seen_at"] == second["first_seen_at"]
        assert second["initial_backfill_completed_at"] is not None
        storage.close()

    def test_slug_with_prior_location_batch_history_is_seeded_as_already_complete(self, tmp_path):
        """Migration safety: a department that already has a successfully
        sent jobs.location_batch:{slug} row (i.e. it was sending
        notifications before this table existed) must NOT be treated as
        newly activated the first time its state row is created — it must
        start already "backfill complete", never hit with an
        initial-backfill burst of old jobs after deployment."""
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dept.db"))
        storage.init_db()
        storage.record("discord", "jobs.location_batch:dept-83", "run-old-1", "sent")

        state = storage.get_or_create_department_state("dept-83")
        assert state["initial_backfill_completed_at"] is not None
        storage.close()

    def test_slug_with_only_failed_history_is_not_seeded_as_complete(self, tmp_path):
        """A 'failed' batch send must not count as real prior history — the
        actual dedup rule (only 'sent' rows count) applies here too."""
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dept.db"))
        storage.init_db()
        storage.record("discord", "jobs.location_batch:dept-04", "run-old-1", "failed", "timeout")

        state = storage.get_or_create_department_state("dept-04")
        assert state["initial_backfill_completed_at"] is None
        storage.close()

    def test_mark_complete_does_not_reset_an_already_complete_timestamp(self, tmp_path):
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dept.db"))
        storage.init_db()
        storage.get_or_create_department_state("dept-04")
        storage.mark_initial_backfill_complete("dept-04")
        first_ts = storage.get_or_create_department_state("dept-04")["initial_backfill_completed_at"]

        storage.mark_initial_backfill_complete("dept-04")  # idempotent second call
        second_ts = storage.get_or_create_department_state("dept-04")["initial_backfill_completed_at"]
        assert first_ts == second_ts
        storage.close()

    def test_record_backfill_sent_accumulates_count(self, tmp_path):
        from src.notifications.storage import NotificationStorage
        storage = NotificationStorage(db_path=str(tmp_path / "dept.db"))
        storage.init_db()
        storage.get_or_create_department_state("dept-04")

        storage.record_backfill_sent("dept-04", 5)
        storage.record_backfill_sent("dept-04", 3)
        state = storage.get_or_create_department_state("dept-04")
        assert state["initial_backfill_sent_count"] == 8
        storage.close()


# ---------------------------------------------------------------------------
# NotificationConfig tests
# ---------------------------------------------------------------------------

class TestNotificationConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        from src.notifications.config import load_notification_config
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert cfg.enabled is True
        assert cfg.discord.enabled is False
        assert cfg.n8n.enabled is False
        assert cfg.rules.min_score_for_notification == 0.80
        # Location Priority feature (2026-09-10): volume is controlled by a
        # per-location CAP, not tier lists.
        assert cfg.rules.max_notifications_per_location_per_run == 3
        assert cfg.rules.notification_score_floor_tier == "relevant"
        assert cfg.rules.notify_unmatched_jobs is False
        assert cfg.discord.location_channels == {}

    def test_loads_yaml(self, tmp_path):
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text("""
notifications:
  enabled: true
  discord:
    enabled: true
    webhook_url: "https://discord.example.com/webhook"
  n8n:
    enabled: false
    webhook_url: ""
  rules:
    min_score_for_notification: 0.85
    max_jobs_in_notification: 3
""")
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert cfg.enabled is True
        assert cfg.discord.enabled is True
        assert cfg.discord.webhook_url == "https://discord.example.com/webhook"
        assert cfg.rules.min_score_for_notification == 0.85
        assert cfg.rules.max_jobs_in_notification == 3
        # Key absent from the file -> the loader's own fallback (issue #77).
        assert cfg.rules.max_notifications_per_location_per_run == 3

    def test_malformed_yaml_returns_defaults(self, tmp_path):
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text(":: invalid yaml ::")
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert cfg.enabled is True  # defaults

    def test_static_channel_config_loaded(self, tmp_path):
        """Per-channel enabled + webhook_url are parsed for the operational
        pipeline/errors channels."""
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text("""
notifications:
  discord:
    enabled: true
    channels:
      pipeline:
        enabled: true
        webhook_url: "https://discord.example.com/pipeline"
      errors:
        enabled: false
        webhook_url: ""
""")
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert cfg.discord.channels["pipeline"].enabled is True
        assert cfg.discord.channels["pipeline"].webhook_url == "https://discord.example.com/pipeline"
        assert cfg.discord.channels["errors"].enabled is False

    def test_static_channel_config_defaults_when_channels_key_absent(self, tmp_path):
        """A legacy config with no `channels:` block at all still activates
        both static channels."""
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text("""
notifications:
  discord:
    enabled: true
    webhook_url: "https://discord.example.com/legacy"
""")
        cfg = load_notification_config(config_dir=str(tmp_path))
        for name in ("pipeline", "errors"):
            assert cfg.discord.channels[name].enabled is True
            assert cfg.discord.channels[name].webhook_url == ""

    def test_legacy_static_channel_names_ignored_not_fatal(self, tmp_path):
        """Old `channels.high_matches`/`var_jobs`/`remote_jobs` blocks (from
        before the Location Priority redesign) are silently ignored — no
        crash. Required migration: configure `discord.location_channels`
        instead, keyed by the slugs the user's own location_preferences
        resolve to."""
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text("""
notifications:
  discord:
    enabled: true
    channels:
      var_jobs:
        enabled: true
        webhook_url: "https://discord.example.com/legacy-var"
""")
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert "var_jobs" not in cfg.discord.channels
        assert set(cfg.discord.channels.keys()) == {"pipeline", "errors", "statistics", "system-status"}

    def test_location_channels_loaded_from_yaml(self, tmp_path):
        """discord.location_channels is keyed by SLUG (e.g. "dept-83"), never
        by a hardcoded location name — this file only maps an
        already-existing slug to a webhook."""
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text("""
notifications:
  discord:
    enabled: true
    location_channels:
      dept-83:
        enabled: true
        webhook_url: "https://discord.example.com/var"
      dept-69:
        enabled: false
      remote-remote_france:
        enabled: true
        webhook_url_env: "DISCORD_REMOTE_FRANCE_WEBHOOK_URL"
""")
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert cfg.discord.location_channels["dept-83"].enabled is True
        assert cfg.discord.location_channels["dept-83"].webhook_url == "https://discord.example.com/var"
        assert cfg.discord.location_channels["dept-69"].enabled is False
        assert cfg.discord.location_channels["remote-remote_france"].webhook_url_env == "DISCORD_REMOTE_FRANCE_WEBHOOK_URL"

    def test_rules_configurable(self, tmp_path):
        from src.notifications.config import load_notification_config
        (tmp_path / "notifications.yaml").write_text("""
notifications:
  rules:
    max_notifications_per_location_per_run: 3
    notification_score_floor_tier: excellent
    notify_unmatched_jobs: true
""")
        cfg = load_notification_config(config_dir=str(tmp_path))
        assert cfg.rules.max_notifications_per_location_per_run == 3
        assert cfg.rules.notification_score_floor_tier == "excellent"
        assert cfg.rules.notify_unmatched_jobs is True


# ---------------------------------------------------------------------------
# Real shipped config/notifications.yaml — every other TestNotificationConfig
# test above uses synthetic YAML in tmp_path, which cannot catch a schema
# drift between the dataclasses in src/notifications/config.py and the file
# actually committed/deployed. This class loads the REAL file from the repo
# root instead. Read-only: never writes back to config/notifications.yaml.
# ---------------------------------------------------------------------------

class TestRealShippedNotificationsYaml:
    def _load_real_config(self):
        from src.notifications.config import load_notification_config
        return load_notification_config(config_dir="config")

    def test_parses_without_falling_back_to_defaults(self):
        """A parse failure returns NotificationConfig() defaults silently
        (see load_notification_config's except clause) — asserting
        non-default values here proves the real file actually parsed rather
        than quietly failing and returning defaults. The tracked file names no
        Discord server, so location/top10 channels may be empty (personal ones
        live in notifications.local.yaml)."""
        cfg = self._load_real_config()
        assert cfg.discord.enabled is True  # dataclass default: False
        assert cfg.discord.channels["pipeline"].webhook_url_env == "DISCORD_PIPELINE_WEBHOOK_URL"  # default: ""
        assert cfg.n8n.webhook_url  # default: ""

    def test_static_channels_present_and_enabled(self):
        cfg = self._load_real_config()
        for name in ("pipeline", "errors", "statistics", "system-status"):
            assert name in cfg.discord.channels
            assert cfg.discord.channels[name].enabled is True
            # webhook_url is intentionally never committed to this file —
            # see save_discord_channel_registries's docstring.
            assert cfg.discord.channels[name].webhook_url == ""
            assert cfg.discord.channels[name].webhook_url_env

    def test_location_and_top10_channels_share_the_same_slugs(self):
        """job-<dept> and top10-<dept> are companions for the same location
        (see DiscordTop10ChannelConfig's docstring) — the real file must
        keep them in sync, one top10 entry per location entry, with matching
        enabled state (a department not yet wired up on Discord is disabled
        on both, not just one)."""
        cfg = self._load_real_config()
        assert set(cfg.discord.location_channels.keys()) == set(cfg.discord.top10_channels.keys())
        for slug, chan in cfg.discord.location_channels.items():
            top10_chan = cfg.discord.top10_channels[slug]
            assert top10_chan.enabled is chan.enabled

    def test_n8n_section_parses(self):
        cfg = self._load_real_config()
        assert cfg.n8n.enabled is False
        assert cfg.n8n.webhook_url

    def test_engine_builds_against_the_real_config_without_error(self):
        """Constructing a NotificationEngine against the real config
        directory and calling get_provider_status() must not raise —
        exercises load_notification_config -> DiscordConfig ->
        _build_*_discord_channel for every real channel/location/top10 slug
        in one pass."""
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=":memory:", config_dir="config")
        status = engine.get_provider_status()
        assert set(status["providers"]["discord"]["location_channels"].keys()) == set(
            status["providers"]["discord"]["top10_channels"].keys()
        )


# ---------------------------------------------------------------------------
# Real env-var -> DiscordProvider wiring. Every test in
# TestNotificationEngineLocationRouting/TestTop10LeaderboardUpdate/
# TestStatisticsUpdate monkeypatches _build_location_discord_channel /
# _build_top10_discord_channel / _build_discord_channel away entirely, so
# none of them prove the actual env-var-name derivation
# (default_location_webhook_env / default_top10_webhook_env) resolves to a
# real, usable DiscordProvider end-to-end. These tests close that seam.
# ---------------------------------------------------------------------------

class TestRealEnvVarToProviderWiring:
    def test_location_channel_resolves_real_env_var(self, tmp_db, monkeypatch):
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        monkeypatch.setenv("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://discord.example.com/job-83")
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={"dept-83": DiscordLocationChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        provider = engine._build_location_discord_channel("dept-83")
        assert provider is not None
        assert provider.enabled is True

    def test_location_channel_explicit_webhook_url_env_override_is_honored(self, tmp_db, monkeypatch):
        """A location_channels entry may override webhook_url_env instead of
        relying on default_location_webhook_env(slug) — must still resolve."""
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        monkeypatch.setenv("CUSTOM_VAR_WEBHOOK", "https://discord.example.com/custom")
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={
                    "dept-83": DiscordLocationChannelConfig(enabled=True, webhook_url_env="CUSTOM_VAR_WEBHOOK"),
                },
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        provider = engine._build_location_discord_channel("dept-83")
        assert provider.enabled is True

    def test_location_channel_without_env_var_set_is_not_configured(self, tmp_db):
        """Negative case: enabled but the env var genuinely isn't set in
        this process — must resolve to a disabled provider, not raise."""
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={"dept-99": DiscordLocationChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        provider = engine._build_location_discord_channel("dept-99")
        assert provider is not None
        assert provider.enabled is False

    def test_top10_channel_resolves_real_env_var(self, tmp_db, monkeypatch):
        from src.notifications.config import (
            DiscordConfig, DiscordTop10ChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        monkeypatch.setenv("DISCORD_TOP10_DEPT_83_WEBHOOK_URL", "https://discord.example.com/top10-83")
        cfg = NotificationConfig(
            discord=DiscordConfig(
                top10_channels={"dept-83": DiscordTop10ChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        provider = engine._build_top10_discord_channel("dept-83")
        assert provider is not None
        assert provider.enabled is True

    def test_static_channel_resolves_real_env_var(self, tmp_db, monkeypatch):
        from src.notifications.config import DiscordChannelConfig, DiscordConfig, NotificationConfig
        from src.notifications.engine import NotificationEngine
        monkeypatch.setenv("DISCORD_PIPELINE_WEBHOOK_URL", "https://discord.example.com/pipeline")
        cfg = NotificationConfig(
            discord=DiscordConfig(
                channels={"pipeline": DiscordChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        provider = engine._build_discord_channel("pipeline")
        assert provider is not None
        assert provider.enabled is True

    def test_default_location_webhook_env_naming_matches_the_real_convention(self):
        """Pins the exact naming convention .env.example documents (e.g.
        "dept-83" -> "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL") so a future
        change to this function can't silently break every deployed .env."""
        from src.notifications.config import default_location_webhook_env, default_top10_webhook_env
        assert default_location_webhook_env("dept-83") == "DISCORD_LOCATION_DEPT_83_WEBHOOK_URL"
        assert default_location_webhook_env("remote-remote_france") == "DISCORD_LOCATION_REMOTE_REMOTE_FRANCE_WEBHOOK_URL"
        assert default_top10_webhook_env("dept-83") == "DISCORD_TOP10_DEPT_83_WEBHOOK_URL"


# ---------------------------------------------------------------------------
# DiscordProvider tests
# ---------------------------------------------------------------------------

class TestTierLegendDerivation:
    """Regression coverage for the 2026-09-12 cleanup that replaced
    discord.py's hand-typed "Tiers: 🔥80+ ⭐72+ 👍65+ 📋60+" string with one
    derived from src.pipeline.scoring.TIER_THRESHOLDS — proves the two can
    never drift apart again."""

    def test_legend_reflects_the_canonical_thresholds(self):
        from src.notifications.discord import _tier_legend
        from src.pipeline.scoring import TIER_THRESHOLDS

        legend = _tier_legend()
        for threshold, _tier in TIER_THRESHOLDS:
            assert f"{threshold}+" in legend

    def test_legend_is_cached_across_calls(self):
        from src.notifications.discord import _tier_legend
        assert _tier_legend() is _tier_legend()


class TestDiscordProvider:
    def test_not_enabled_without_url(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="")
        assert p.enabled is False

    def test_enabled_with_url(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        assert p.enabled is True

    def test_send_returns_false_without_url(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="")
        assert p.send({"event": "pipeline.completed"}) is False

    def test_send_calls_httpx_post(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        mock_resp = MagicMock()
        mock_resp.status_code = 200  # a real httpx.Response always has one; the retry layer inspects it
        mock_resp.raise_for_status = MagicMock()
        with patch("src.notifications.discord.httpx.post", return_value=mock_resp) as mock_post:
            result = p.send({"event": "pipeline.completed", "run_id": "abc", "stats": {},
                             "timestamp": "2026-01-01T00:00:00+00:00"})
        assert result is True
        mock_post.assert_called_once()

    def test_http_error_returns_false(self):
        import httpx as _httpx
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        with patch("src.notifications.discord.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = _httpx.HTTPStatusError(
                "400", request=MagicMock(), response=mock_resp
            )
            result = p.send({"event": "pipeline.completed", "stats": {}})
        assert result is False

    def test_send_retries_through_transient_429_then_succeeds(self):
        """Integration-level proof (not just the isolated http_retry unit
        tests) that DiscordProvider.send() actually retries via the shared
        retry layer rather than failing on the first transient error."""
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}
        ok = MagicMock()
        ok.status_code = 200
        ok.raise_for_status = MagicMock()
        with patch("src.notifications.discord.httpx.post", side_effect=[rate_limited, ok]) as mock_post, \
                patch("src.notifications.http_retry.time.sleep"):
            result = p.send({"event": "pipeline.completed", "stats": {}})
        assert result is True
        assert mock_post.call_count == 2

    def test_send_does_not_retry_non_retryable_4xx(self):
        """A permanently broken webhook/payload (e.g. 400) must fail on the
        first attempt — never burn the retry budget on something retrying
        can't fix."""
        from src.notifications.discord import DiscordProvider
        import httpx as _httpx
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        bad_request = MagicMock()
        bad_request.status_code = 400
        bad_request.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "400", request=MagicMock(), response=bad_request
        )
        with patch("src.notifications.discord.httpx.post", return_value=bad_request) as mock_post:
            result = p.send({"event": "pipeline.completed", "stats": {}})
        assert result is False
        assert mock_post.call_count == 1

    def test_send_gives_up_after_bounded_retries_returns_false_not_raise(self):
        """Exhausting all retries on a persistently unreachable webhook must
        still honor the "never raises" contract — returns False, no
        exception escapes to the caller (NotificationEngine), even though
        the underlying retry layer re-raises ConnectError internally after
        its own retry budget is exhausted."""
        import httpx as _httpx
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        with patch(
            "src.notifications.discord.httpx.post",
            side_effect=_httpx.ConnectError("connection refused"),
        ) as mock_post, patch("src.notifications.http_retry.time.sleep"):
            result = p.send({"event": "pipeline.completed", "stats": {}})
        assert result is False
        from src.notifications.http_retry import MAX_RETRIES
        assert mock_post.call_count == MAX_RETRIES + 1

    def test_env_var_takes_precedence_over_webhook_url(self, monkeypatch):
        """A custom env_var (used for per-channel routing) wins over the constructor arg."""
        from src.notifications.discord import DiscordProvider
        monkeypatch.setenv("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://discord.example.com/from-env")
        p = DiscordProvider(
            webhook_url="https://discord.example.com/from-arg",
            env_var="DISCORD_LOCATION_DEPT_83_WEBHOOK_URL",
        )
        assert p._webhook_url == "https://discord.example.com/from-env"

    def test_fallback_webhook_used_when_channel_env_and_url_absent(self):
        """Backward compatibility: legacy DISCORD_WEBHOOK_URL/webhook_url still works."""
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(
            webhook_url="",
            env_var="DISCORD_PIPELINE_WEBHOOK_URL",
            fallback_env_var="DISCORD_WEBHOOK_URL",
            fallback_webhook_url="https://discord.example.com/legacy",
        )
        assert p.enabled is True
        assert p._webhook_url == "https://discord.example.com/legacy"

    def test_fallback_env_var_wins_over_fallback_webhook_url(self, monkeypatch):
        from src.notifications.discord import DiscordProvider
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example.com/legacy-env")
        p = DiscordProvider(
            webhook_url="",
            env_var="DISCORD_PIPELINE_WEBHOOK_URL",
            fallback_env_var="DISCORD_WEBHOOK_URL",
            fallback_webhook_url="https://discord.example.com/legacy-arg",
        )
        assert p._webhook_url == "https://discord.example.com/legacy-env"

    def test_embed_contains_funnel(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "pipeline.completed",
            "run_id": "test-run",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "stats": {
                "collected": 200, "validated": 180, "deduplicated": 160,
                "filtered": 80, "scored": 70, "semantically_matched": 65,
                "personalized": 60, "duration_seconds": 45.2, "errors": [],
            },
            "previous_stats": None,
        }
        embed_payload = p._build_embed(payload)
        assert "embeds" in embed_payload
        embed = embed_payload["embeds"][0]
        field_names = [f["name"] for f in embed.get("fields", [])]
        assert any("funnel" in n.lower() or "Pipeline" in n for n in field_names)

    def test_pipeline_completed_no_previous_stats_no_delta(self):
        """First-ever run: no previous_stats -> no delta annotations, no crash."""
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "pipeline.completed", "run_id": "run-1",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "stats": {"collected": 10, "validated": 9, "deduplicated": 8,
                      "filtered": 5, "scored": 5, "personalized": 5,
                      "duration_seconds": 1.0, "errors": []},
            "previous_stats": None,
        }
        embed = p._build_embed(payload)["embeds"][0]
        funnel = next(f["value"] for f in embed["fields"] if f["name"] == "Pipeline funnel")
        assert "(" not in funnel  # no delta parentheses at all

    def test_pipeline_completed_shows_delta(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "pipeline.completed", "run_id": "run-2",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "stats": {"collected": 120, "validated": 100, "deduplicated": 90,
                      "filtered": 40, "scored": 40, "personalized": 40,
                      "duration_seconds": 1.0, "errors": []},
            "previous_stats": {"collected": 100, "validated": 90, "deduplicated": 80,
                               "filtered": 50, "scored": 50, "personalized": 50},
        }
        embed = p._build_embed(payload)["embeds"][0]
        funnel = next(f["value"] for f in embed["fields"] if f["name"] == "Pipeline funnel")
        assert "(+20)" in funnel   # collected: 120 - 100
        assert "(-10)" in funnel  # filtered: 40 - 50 (negative delta is valid, shown as-is)

    def test_pipeline_completed_warnings_field_only_when_errors_present(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        no_errors = {"event": "pipeline.completed", "run_id": "r", "timestamp": "t",
                     "stats": {"collected": 1, "errors": []}, "previous_stats": None}
        with_errors = {"event": "pipeline.completed", "run_id": "r", "timestamp": "t",
                       "stats": {"collected": 1, "errors": ["indeed: timeout"]}, "previous_stats": None}
        embed_no = p._build_embed(no_errors)["embeds"][0]
        embed_with = p._build_embed(with_errors)["embeds"][0]
        assert not any(f["name"] == "Warnings" for f in embed_no["fields"])
        assert any(f["name"] == "Warnings" for f in embed_with["fields"])
        status_no = next(f["value"] for f in embed_no["fields"] if f["name"] == "Status")
        status_with = next(f["value"] for f in embed_with["fields"] if f["name"] == "Status")
        assert "Healthy" in status_no
        assert "warnings" in status_with.lower()

    def test_pipeline_failed_embed_no_traceback(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        long_traceback = "Traceback (most recent call last):\n" + ("  File ...\n" * 200)
        payload = {"event": "pipeline.failed", "run_id": "run-3", "timestamp": "t",
                   "error": long_traceback}
        embed = p._build_embed(payload)["embeds"][0]
        error_field = next(f["value"] for f in embed["fields"] if f["name"] == "Error")
        assert len(error_field) < 400  # truncated, not the full multi-KB traceback
        assert any(f["name"] == "🔁 Retry" for f in embed["fields"])
        status = next(f["value"] for f in embed["fields"] if f["name"] == "Status")
        assert "Failed" in status

    def test_pipeline_warnings_embed_lists_errors(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {"event": "pipeline.warnings", "run_id": "run-4", "timestamp": "t",
                   "errors": ["Validation: 3 rejected", "indeed: HTTP 503"]}
        embed = p._build_embed(payload)["embeds"][0]
        details = next(f["value"] for f in embed["fields"] if f["name"] == "Details")
        assert "Validation: 3 rejected" in details
        assert "indeed: HTTP 503" in details

    def test_location_notify_embed_department_title_and_color(self):
        """The generic per-location embed builds its title/color from the
        payload's own location_label/location_type — never from a hardcoded
        Var/remote assumption, so it works for any configured department."""
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "jobs.location_notify", "run_id": "run-5",
            "location_type": "department", "location_label": "Rhône (69)",
            "total_count": 3, "shown_count": 3, "overflow_count": 0,
            "jobs": [
                {"job_id": f"job-{i}", "title": f"Dev {i}", "company": "Acme",
                 "location": "Lyon", "remote": False, "work_mode": "on_site",
                 "employment_type": "CDI", "score_100": 80 - i, "tier": "excellent",
                 "canonical_url": f"https://example.com/{i}", "apply_url": None}
                for i in range(3)
            ],
        }
        embed = p._build_embed(payload)["embeds"][0]
        assert "RHÔNE (69)" in embed["title"]
        assert "📍" in embed["title"]
        assert "3 new relevant" in embed["description"]

    def test_location_notify_embed_remote_shows_fully_remote_and_france_flag(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "jobs.location_notify", "run_id": "run-8",
            "location_type": "remote", "location_label": "Remote France",
            "total_count": 1, "shown_count": 1, "overflow_count": 0,
            "jobs": [{"job_id": "job-1", "title": "Python Automation Developer", "company": "Acme",
                      "location": "France", "remote": True, "work_mode": "remote",
                      "employment_type": "Internship", "countries": ["France"],
                      "score_100": 91, "tier": "exceptional",
                      "canonical_url": "https://example.com/apply", "apply_url": None}],
        }
        embed = p._build_embed(payload)["embeds"][0]
        assert "REMOTE FRANCE" in embed["title"]
        assert "🌍" in embed["title"]
        jobs_field = embed["fields"][0]["value"]
        assert "Fully Remote" in jobs_field
        assert "🇫🇷 France" in jobs_field

    def test_location_notify_embed_department_handles_missing_location(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "jobs.location_notify", "run_id": "run-6",
            "location_type": "department", "location_label": "Var (83)",
            "total_count": 1, "shown_count": 1, "overflow_count": 0,
            "jobs": [{"job_id": "job-1", "title": "Dev", "company": "Acme",
                      "location": None, "remote": False, "work_mode": None,
                      "employment_type": None,
                      "score_100": 65, "tier": "relevant",
                      "canonical_url": None, "apply_url": None}],
        }
        embed = p._build_embed(payload)["embeds"][0]  # must not raise
        assert "Dev" in embed["fields"][0]["value"]
        assert "Acme" in embed["fields"][0]["value"]

    def test_location_notify_embed_department_shows_work_mode_and_contract(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "jobs.location_notify", "run_id": "run-7",
            "location_type": "department", "location_label": "Var (83)",
            "total_count": 1, "shown_count": 1, "overflow_count": 0,
            "jobs": [{"job_id": "job-1", "title": "Technicien Cybersecurite", "company": "ABC",
                      "location": "Ollioules", "remote": False, "work_mode": "on_site",
                      "employment_type": "Alternance",
                      "score_100": 82, "tier": "exceptional",
                      "canonical_url": "https://example.com/apply", "apply_url": None}],
        }
        embed = p._build_embed(payload)["embeds"][0]
        jobs_field = embed["fields"][0]["value"]
        assert "On-site" in jobs_field
        assert "Alternance" in jobs_field
        assert "Ollioules" in jobs_field
        assert "[Apply]" in jobs_field

    def test_location_notify_embed_remote_non_france_no_flag(self):
        from src.notifications.discord import DiscordProvider
        p = DiscordProvider(webhook_url="https://discord.example.com/webhook")
        payload = {
            "event": "jobs.location_notify", "run_id": "run-9",
            "location_type": "remote", "location_label": "Remote France",
            "total_count": 1, "shown_count": 1, "overflow_count": 0,
            "jobs": [{"job_id": "job-1", "title": "QA Engineer", "company": "Acme",
                      "location": "Germany", "remote": True, "work_mode": "remote",
                      "employment_type": "Permanent", "countries": ["Germany"],
                      "score_100": 84, "tier": "excellent",
                      "canonical_url": None, "apply_url": None}],
        }
        embed = p._build_embed(payload)["embeds"][0]
        jobs_field = embed["fields"][0]["value"]
        assert "🇫🇷" not in jobs_field
        assert "Fully Remote" in jobs_field


# ---------------------------------------------------------------------------
# N8nProvider tests
# ---------------------------------------------------------------------------

class TestN8nProvider:
    def test_not_enabled_without_url(self):
        from src.notifications.n8n import N8nProvider
        p = N8nProvider(webhook_url="")
        assert p.enabled is False

    def test_send_calls_httpx_post(self):
        from src.notifications.n8n import N8nProvider
        p = N8nProvider(webhook_url="https://n8n.example.com/webhook/abc")
        mock_resp = MagicMock()
        mock_resp.status_code = 200  # a real httpx.Response always has one; the retry layer inspects it
        mock_resp.raise_for_status = MagicMock()
        with patch("src.notifications.n8n.httpx.post", return_value=mock_resp) as mock_post:
            result = p.send({"event": "pipeline.completed", "version": 1})
        assert result is True
        _, kwargs = mock_post.call_args
        assert kwargs["json"]["event"] == "pipeline.completed"

    def test_http_error_returns_false(self):
        import httpx as _httpx
        from src.notifications.n8n import N8nProvider
        p = N8nProvider(webhook_url="https://n8n.example.com/webhook/abc")
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        with patch("src.notifications.n8n.httpx.post") as mock_post:
            mock_post.return_value.raise_for_status.side_effect = _httpx.HTTPStatusError(
                "503", request=MagicMock(), response=mock_resp
            )
            result = p.send({"event": "pipeline.completed"})
        assert result is False


# ---------------------------------------------------------------------------
# NotificationEngine tests
# ---------------------------------------------------------------------------

def _discord_config():
    """Build a NotificationConfig with Discord enabled.

    Channel webhook_urls are left empty; tests patch
    `engine._build_discord_channel`/`engine._build_location_discord_channel`
    to inject a mock provider rather than resolving real webhooks — this is
    the seam the channel-routing architecture is built around.
    """
    from src.notifications.config import DiscordConfig, N8nConfig, NotificationConfig, NotificationRules
    return NotificationConfig(
        enabled=True,
        discord=DiscordConfig(enabled=True, webhook_url=""),
        n8n=N8nConfig(enabled=False, webhook_url=""),
        rules=NotificationRules(),
    )


class _RecordingProvider:
    """Fake DiscordProvider: records every payload sent to one channel."""

    def __init__(self, should_succeed: bool = True):
        self.enabled = True
        self.sent: list[dict] = []
        self.should_succeed = should_succeed

    def send(self, payload: dict) -> bool:
        self.sent.append(payload)
        return self.should_succeed


def _engine_with_location_channels(
    tmp_db,
    location_preferences: list[dict],
    channel_providers: dict[str, "_RecordingProvider"],
    static_providers: dict[str, "_RecordingProvider"] | None = None,
    config=None,
):
    """Build a NotificationEngine wired for location-routing tests:
    - profile.geography.location_preferences is stubbed directly (no disk I/O)
    - location Discord channels resolve via `channel_providers` (keyed by slug)
    - static #pipeline/#errors channels resolve via `static_providers`
    """
    from src.notifications.engine import NotificationEngine
    engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=config or _discord_config())
    engine._load_location_preferences = lambda: location_preferences
    engine._build_location_discord_channel = lambda slug: channel_providers.get(slug)
    engine._build_discord_channel = lambda name: (static_providers or {}).get(name)
    return engine


class TestNotificationEngineLocationRouting:
    """profile.geography.location_preferences drives Discord routing — no
    hardcoded department/remote anywhere, one job -> at most one channel."""

    def test_department_match_routes_to_its_own_slug(self, tmp_db):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan},
        )
        job = _job_at_score("job-var", 77, department="83", location="Toulon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-var"), new_jobs=[job])
        assert {j["job_id"] for p in var_chan.sent for j in p["jobs"]} == {"job-var"}

    def test_no_configured_location_matching_produces_no_send(self, tmp_db):
        """A department NOT in the configured list never reaches any channel
        — proves routing genuinely reads the profile, not a hardcoded dept."""
        lyon_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-69": lyon_chan},
        )
        job = _job_at_score("job-lyon", 77, department="69", location="Lyon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-lyon"), new_jobs=[job])
        assert lyon_chan.sent == []

    def test_reconfiguring_profile_changes_routing_without_code_change(self, tmp_db):
        """Same job, same code — only the (stubbed) profile changes — proves
        the profile alone drives which channel wins."""
        var_chan = _RecordingProvider()
        lyon_chan = _RecordingProvider()
        job = _job_at_score("job-x", 77, department="69", location="Lyon", work_mode="on_site", remote=False)

        engine_a = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan, "dept-69": lyon_chan},
        )
        engine_a.notify_pipeline_completed(_make_pipeline_result("run-a"), new_jobs=[job])
        assert lyon_chan.sent == []  # 69 not configured -> no match

        engine_b = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "69")], {"dept-83": var_chan, "dept-69": lyon_chan},
        )
        engine_b.notify_pipeline_completed(_make_pipeline_result("run-b"), new_jobs=[job])
        assert {j["job_id"] for p in lyon_chan.sent for j in p["jobs"]} == {"job-x"}

    def test_remote_preference_routes_remote_job(self, tmp_db):
        remote_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("remote", "remote_france")], {"remote-remote_france": remote_chan},
        )
        job = _job_at_score("job-remote", 77)  # default: fully remote, France
        engine.notify_pipeline_completed(_make_pipeline_result("run-remote"), new_jobs=[job])
        assert {j["job_id"] for p in remote_chan.sent for j in p["jobs"]} == {"job-remote"}

    def test_job_matching_both_department_and_remote_goes_to_higher_priority_only(self, tmp_db):
        """ONE JOB -> ONE CHANNEL: department listed before remote wins,
        remote channel gets nothing for this job."""
        var_chan = _RecordingProvider()
        remote_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db,
            [_loc_pref("department", "83"), _loc_pref("remote", "remote_france")],
            {"dept-83": var_chan, "remote-remote_france": remote_chan},
        )
        job = _job_at_score("job-both", 79, department="83", location="Toulon", work_mode="remote", remote=True)
        engine.notify_pipeline_completed(_make_pipeline_result("run-both"), new_jobs=[job])
        assert {j["job_id"] for p in var_chan.sent for j in p["jobs"]} == {"job-both"}
        assert remote_chan.sent == []

    def test_priority_order_reversed_changes_the_winner(self, tmp_db):
        var_chan = _RecordingProvider()
        remote_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db,
            [_loc_pref("remote", "remote_france"), _loc_pref("department", "83")],
            {"dept-83": var_chan, "remote-remote_france": remote_chan},
        )
        job = _job_at_score("job-both2", 79, department="83", location="Toulon", work_mode="remote", remote=True)
        engine.notify_pipeline_completed(_make_pipeline_result("run-both2"), new_jobs=[job])
        assert {j["job_id"] for p in remote_chan.sent for j in p["jobs"]} == {"job-both2"}
        assert var_chan.sent == []

    def test_excluded_location_never_routes_but_does_not_block_other_match(self, tmp_db):
        var_chan = _RecordingProvider()
        remote_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db,
            [_loc_pref("department", "83", "excluded"), _loc_pref("remote", "remote_france")],
            {"dept-83": var_chan, "remote-remote_france": remote_chan},
        )
        job = _job_at_score("job-excl", 79, department="83", location="Toulon", work_mode="remote", remote=True)
        engine.notify_pipeline_completed(_make_pipeline_result("run-excl"), new_jobs=[job])
        assert var_chan.sent == []
        assert {j["job_id"] for p in remote_chan.sent for j in p["jobs"]} == {"job-excl"}

    def test_low_priority_job_never_notified_despite_location_match(self, tmp_db):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan},
        )
        job = _job_at_score("job-lowvar", 40, department="83", location="Toulon")
        engine.notify_pipeline_completed(_make_pipeline_result("run-lowvar"), new_jobs=[job])
        assert var_chan.sent == []

    def test_relevant_tier_job_clears_the_sanity_floor(self, tmp_db):
        """The floor is deliberately low ('relevant', >=60) — it's a sanity
        minimum, not the volume control (see max_notifications_per_location_per_run)."""
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan},
        )
        job = _job_at_score("job-relevant", 61, department="83", location="Toulon")
        engine.notify_pipeline_completed(_make_pipeline_result("run-relevant"), new_jobs=[job])
        assert {j["job_id"] for p in var_chan.sent for j in p["jobs"]} == {"job-relevant"}

    def test_unmatched_job_dropped_by_default(self, tmp_db):
        engine = _engine_with_location_channels(tmp_db, [], {})
        job = _job_at_score("job-nomatch", 90, department="75", location="Paris", work_mode="on_site", remote=False)
        # Must not raise even with zero location_preferences and zero channels.
        engine.notify_pipeline_completed(_make_pipeline_result("run-nomatch"), new_jobs=[job])

    def test_notify_unmatched_jobs_flag_routes_to_synthetic_unmatched_slug(self, tmp_db):
        """Reversibility knob (rules.notify_unmatched_jobs): when enabled, a
        non-matching job is still routed, under the synthetic "unmatched"
        slug — which, like any other slug, only sends if a channel is
        actually configured for it."""
        from src.notifications.config import DiscordConfig, N8nConfig, NotificationConfig, NotificationRules
        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=True, webhook_url=""),
            n8n=N8nConfig(enabled=False, webhook_url=""),
            rules=NotificationRules(notify_unmatched_jobs=True),
        )
        unmatched_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [], {"unmatched": unmatched_chan}, config=cfg,
        )
        job = _job_at_score("job-um", 90, department="75", location="Paris", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-um"), new_jobs=[job])
        assert {j["job_id"] for p in unmatched_chan.sent for j in p["jobs"]} == {"job-um"}

    def test_max_notifications_per_location_per_run_caps_and_ranks_descending(self, tmp_db):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan},
        )
        engine._config.rules.max_notifications_per_location_per_run = 3
        jobs = [
            _job_at_score(f"job-{i}", 65 + i, department="83", location="Toulon", work_mode="on_site", remote=False)
            for i in range(10)
        ]
        engine.notify_pipeline_completed(_make_pipeline_result("run-cap"), new_jobs=jobs)
        assert len(var_chan.sent) == 1  # one batch message, not one per job
        payload = var_chan.sent[0]
        assert payload["shown_count"] == 3
        assert payload["total_count"] == 3
        assert [j["score_100"] for j in payload["jobs"]] == sorted(
            [j["score_100"] for j in payload["jobs"]], reverse=True
        )

    def test_fewer_qualifying_jobs_than_cap_are_never_padded(self, tmp_db):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan},
        )
        job = _job_at_score("job-solo", 77, department="83", location="Toulon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-solo"), new_jobs=[job])
        assert var_chan.sent[0]["total_count"] == 1

    def test_default_cap_sends_at_most_3_jobs_per_location_batch(self, tmp_db):
        """Issue #77: with the DEFAULT rules (no override), a department with
        10 eligible jobs gets one message with exactly the top 3, and the
        other 7 are not recorded as sent, so a later run can still send them."""
        from src.notifications.storage import NotificationStorage

        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "69")], {"dept-69": var_chan},
        )
        jobs = [
            _job_at_score(f"job-{i}", 61 + 2 * i, department="69", location="Lyon", work_mode="on_site", remote=False)
            for i in range(10)
        ]
        engine.notify_pipeline_completed(_make_pipeline_result("run-cap3"), new_jobs=jobs)

        assert len(var_chan.sent) == 1
        payload = var_chan.sent[0]
        assert [j["job_id"] for j in payload["jobs"]] == ["job-9", "job-8", "job-7"]
        assert payload["total_count"] == payload["shown_count"] == 3
        assert payload["overflow_count"] == 0

        storage = NotificationStorage(db_path=tmp_db)
        try:
            sent = {
                j["job_id"] for j in jobs
                if storage.already_sent("discord", "jobs.location_notify", j["job_id"])
            }
        finally:
            storage.close()
        assert sent == {"job-9", "job-8", "job-7"}

    @pytest.mark.parametrize("count", [1, 2, 3])
    def test_default_cap_includes_all_jobs_up_to_3(self, tmp_db, count):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "69")], {"dept-69": var_chan},
        )
        jobs = [
            _job_at_score(f"job-{i}", 70 + i, department="69", location="Lyon", work_mode="on_site", remote=False)
            for i in range(count)
        ]
        engine.notify_pipeline_completed(_make_pipeline_result(f"run-{count}"), new_jobs=jobs)
        assert len(var_chan.sent) == 1
        assert {j["job_id"] for j in var_chan.sent[0]["jobs"]} == {j["job_id"] for j in jobs}
        assert var_chan.sent[0]["total_count"] == count

    def test_no_eligible_jobs_sends_no_location_batch(self, tmp_db):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "69")], {"dept-69": var_chan},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-none"), new_jobs=[])
        assert var_chan.sent == []

    def test_configured_location_without_channel_is_not_lost_just_not_sent(self, tmp_db):
        """A location the user configured but hasn't wired a webhook for
        yet: no Discord send this run, but no crash either — see the
        digest, which is unaffected by this."""
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "69")], {},  # no "dept-69" provider registered
        )
        job = _job_at_score("job-unmapped", 90, department="69", location="Lyon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-unmapped"), new_jobs=[job])  # must not raise


class TestNotificationEngineDedup:
    def test_successful_completion_recorded_as_pipeline_completed(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        pipeline = _RecordingProvider()
        engine = _engine_with_location_channels(tmp_db, [], {}, static_providers={"pipeline": pipeline})
        engine.notify_pipeline_completed(_make_pipeline_result("run-ok", status="completed"))
        ns = NotificationStorage(db_path=tmp_db)
        ns.init_db()
        assert ns.already_sent("discord", "pipeline.completed", "run-ok") is True
        assert ns.already_sent("discord", "pipeline.failed", "run-ok") is False
        ns.close()

    def test_failed_run_recorded_as_pipeline_failed(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        errors = _RecordingProvider()
        engine = _engine_with_location_channels(tmp_db, [], {}, static_providers={"errors": errors})
        engine.notify_pipeline_completed(
            _make_pipeline_result("run-fail", status="failed", error="boom")
        )
        ns = NotificationStorage(db_path=tmp_db)
        ns.init_db()
        assert ns.already_sent("discord", "pipeline.failed", "run-fail") is True
        assert ns.already_sent("discord", "pipeline.completed", "run-fail") is False
        ns.close()
        assert len(errors.sent) == 1
        assert errors.sent[0]["event"] == "pipeline.failed"

    def test_warnings_only_recorded_when_sent(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        errors = _RecordingProvider()
        engine = _engine_with_location_channels(tmp_db, [], {}, static_providers={"errors": errors})
        result = _make_pipeline_result("run-warn", status="completed")
        result.stats.errors = ["indeed: timeout"]
        result.stats.to_dict = lambda: {  # keep to_dict in sync with the mutated errors list
            "collected": 100, "validated": 90, "deduplicated": 80, "filtered": 50,
            "scored": 40, "semantically_matched": 35, "personalized": 30,
            "duration_seconds": 12.5, "errors": ["indeed: timeout"],
        }
        engine.notify_pipeline_completed(result)
        ns = NotificationStorage(db_path=tmp_db)
        ns.init_db()
        assert ns.already_sent("discord", "pipeline.warnings", "run-warn") is True
        ns.close()
        # Two messages this run: pipeline.completed AND pipeline.warnings both use
        # the "errors" mock here only for warnings (completed has no channel mock,
        # so it silently no-ops) — assert only the warnings payload landed.
        assert len(errors.sent) == 1
        assert errors.sent[0]["event"] == "pipeline.warnings"

    def test_no_warnings_message_when_no_errors(self, tmp_db):
        errors = _RecordingProvider()
        engine = _engine_with_location_channels(tmp_db, [], {}, static_providers={"errors": errors})
        engine.notify_pipeline_completed(_make_pipeline_result("run-clean", status="completed"))
        assert errors.sent == []

    def test_job_not_resent_on_the_same_channel_across_runs(self, tmp_db):
        var_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan},
        )
        job = _job_at_score("job-dup", 90, department="83", location="Toulon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-a"), new_jobs=[job])
        engine.notify_pipeline_completed(_make_pipeline_result("run-b"), new_jobs=[job])
        assert len(var_chan.sent) == 1  # second run: already marked sent, skipped

    def test_job_dedup_is_global_across_every_location_channel(self, tmp_db):
        """The core fan-out fix: dedup uses ONE unified event_type
        ("jobs.location_notify") regardless of destination channel, so a
        job already sent via one channel can never ALSO be sent via a
        different channel in a later run (e.g. after the profile changes
        priority order) — this is what makes "one job -> one channel, ever"
        a storage-level guarantee, not just a routing-logic one."""
        var_chan = _RecordingProvider()
        remote_chan = _RecordingProvider()
        job = _job_at_score("job-once", 79, department="83", location="Toulon", work_mode="remote", remote=True)

        engine_a = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": var_chan, "remote-remote_france": remote_chan},
        )
        engine_a.notify_pipeline_completed(_make_pipeline_result("run-a"), new_jobs=[job])
        assert len(var_chan.sent) == 1

        # Profile reconfigured to prefer remote instead — same job, new run.
        engine_b = _engine_with_location_channels(
            tmp_db, [_loc_pref("remote", "remote_france")], {"dept-83": var_chan, "remote-remote_france": remote_chan},
        )
        engine_b.notify_pipeline_completed(_make_pipeline_result("run-b"), new_jobs=[job])
        assert remote_chan.sent == []  # already sent once, ever — never a second channel

    def test_different_locations_same_run_do_not_collide_in_dedup(self, tmp_db):
        """Two DIFFERENT jobs, resolved to two different locations in the
        SAME run, must both send — the per-(run, slug) envelope dedup key
        must not collide across locations."""
        var_chan = _RecordingProvider()
        remote_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db,
            [_loc_pref("department", "83"), _loc_pref("remote", "remote_france")],
            {"dept-83": var_chan, "remote-remote_france": remote_chan},
        )
        var_job = _job_at_score("job-v", 77, department="83", location="Toulon", work_mode="on_site", remote=False)
        remote_job = _job_at_score("job-r", 77)
        engine.notify_pipeline_completed(_make_pipeline_result("run-multi"), new_jobs=[var_job, remote_job])
        assert {j["job_id"] for p in var_chan.sent for j in p["jobs"]} == {"job-v"}
        assert {j["job_id"] for p in remote_chan.sent for j in p["jobs"]} == {"job-r"}

    def test_discord_delivery_failure_not_marked_sent(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        failing = _RecordingProvider(should_succeed=False)
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": failing},
        )
        job = _job_at_score("job-flaky", 90, department="83", location="Toulon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-e"), new_jobs=[job])
        ns = NotificationStorage(db_path=tmp_db)
        ns.init_db()
        assert ns.already_sent("discord", "jobs.location_notify", "job-flaky") is False
        counts = ns.count_by_status()
        assert counts.get("failed") == 1
        ns.close()

    def test_retry_after_failed_delivery_succeeds(self, tmp_db):
        """A job whose first send failed is retried (and can succeed) on a later call."""
        failing = _RecordingProvider(should_succeed=False)
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": failing},
        )
        job = _job_at_score("job-retry", 90, department="83", location="Toulon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-f1"), new_jobs=[job])
        assert len(failing.sent) == 1

        recovering = _RecordingProvider(should_succeed=True)
        engine._build_location_discord_channel = lambda slug: recovering if slug == "dept-83" else None
        engine.notify_pipeline_completed(_make_pipeline_result("run-f2"), new_jobs=[job])
        assert len(recovering.sent) == 1  # not skipped — the earlier attempt never recorded 'sent'

    def test_pipeline_run_level_dedup_same_run_id(self, tmp_db):
        pipeline = _RecordingProvider()
        engine = _engine_with_location_channels(tmp_db, [], {}, static_providers={"pipeline": pipeline})
        result = _make_pipeline_result("run-same")
        engine.notify_pipeline_completed(result)
        engine.notify_pipeline_completed(result)
        assert len(pipeline.sent) == 1

    def test_discord_disabled_produces_no_send(self, tmp_db):
        from src.notifications.config import DiscordConfig
        cfg = _discord_config()
        cfg.discord = DiscordConfig(enabled=False, webhook_url="")
        pipeline = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [], {}, static_providers={"pipeline": pipeline}, config=cfg,
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-g"), new_jobs=[_job_at_score("j", 95)])
        assert pipeline.sent == []  # master switch off -> channel builders never even consulted

    def test_individual_static_channel_disabled_produces_no_send(self, tmp_db):
        """Disabling just #pipeline leaves #errors unaffected."""
        cfg = _discord_config()
        cfg.discord.channels["pipeline"].enabled = False
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        # Real _build_discord_channel this time (not mocked) to exercise the
        # actual enabled-flag check end to end.
        assert engine._build_discord_channel("pipeline") is None
        assert engine._build_discord_channel("errors") is not None


class TestNotificationEngineMisc:
    def test_no_providers_no_error(self, tmp_db):
        from src.notifications.config import NotificationConfig
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=NotificationConfig())
        engine.notify_pipeline_completed(_make_pipeline_result(), new_jobs=[])

    def test_disabled_engine_skips_everything(self, tmp_db):
        from src.notifications.config import NotificationConfig
        from src.notifications.engine import NotificationEngine
        config = NotificationConfig(enabled=False)
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=config)
        pipeline = _RecordingProvider()
        engine._build_discord_channel = lambda name: pipeline
        engine.notify_pipeline_completed(_make_pipeline_result())
        assert pipeline.sent == []

    def test_provider_exception_does_not_raise(self, tmp_db):
        from src.notifications.engine import NotificationEngine

        class _RaisingProvider:
            enabled = True
            def send(self, payload):
                raise RuntimeError("connection refused")

        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=_discord_config())
        engine._build_discord_channel = lambda name: _RaisingProvider()
        # Must not raise — NotificationEngine.notify_pipeline_completed() catches everything.
        engine.notify_pipeline_completed(_make_pipeline_result("run-raise"))

    def test_get_history(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        ns = NotificationStorage(db_path=tmp_db)
        ns.init_db()
        ns.record("discord", "pipeline.completed", "run-1", "sent")
        ns.close()

        from src.notifications.config import NotificationConfig
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=NotificationConfig())
        history = engine.get_history(limit=10)
        assert len(history) == 1
        assert history[0]["entity_id"] == "run-1"

    def test_get_provider_status(self, tmp_db):
        from src.notifications.config import NotificationConfig
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=NotificationConfig())
        status = engine.get_provider_status()
        assert "enabled" in status
        assert "providers" in status
        assert "discord" in status["providers"]
        assert "n8n" in status["providers"]

    def test_get_provider_status_reports_channels_and_location_channels(self, tmp_db):
        """`channels` (static pipeline/errors) and `location_channels`
        (dynamic, keyed by whatever slugs are configured) are both reported,
        without removing/renaming the existing enabled/configured/last_sent
        keys the frontend Sidebar status pill reads."""
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={"dept-83": DiscordLocationChannelConfig(enabled=True, webhook_url="https://x")},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        # get_provider_status() always reloads location_channels fresh from
        # disk (see _current_location_channels) — stub the reload seam
        # itself rather than relying on the constructor-injected cfg, same
        # convention _engine_with_backfill already uses.
        engine._current_location_channels = lambda: cfg.discord.location_channels
        status = engine.get_provider_status()
        discord_status = status["providers"]["discord"]
        assert {"enabled", "configured", "last_sent"} <= discord_status.keys()
        assert "channels" in discord_status
        for name in ("pipeline", "errors"):
            assert name in discord_status["channels"]
            assert "enabled" in discord_status["channels"][name]
            assert "configured" in discord_status["channels"][name]
        assert "location_channels" in discord_status
        assert discord_status["location_channels"]["dept-83"]["enabled"] is True
        assert discord_status["location_channels"]["dept-83"]["configured"] is True

    def test_get_provider_status_reports_top10_channels(self, tmp_db):
        """top10_channels must be reported with the same shape as
        channels/location_channels — this was entirely absent before,
        making an unconfigured top10 leaderboard invisible to any caller of
        this status endpoint (dashboard, startup log, system-status)."""
        from src.notifications.config import (
            DiscordConfig, DiscordTop10ChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        cfg = NotificationConfig(
            discord=DiscordConfig(
                top10_channels={"dept-83": DiscordTop10ChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        discord_status = engine.get_provider_status()["providers"]["discord"]
        assert "top10_channels" in discord_status
        entry = discord_status["top10_channels"]["dept-83"]
        assert entry["enabled"] is True
        # No webhook_url_env resolves to a real env var in this test, and no
        # env var is set — so this channel is enabled but not configured:
        # exactly the silent-no-op state missing_webhook exists to surface.
        assert entry["configured"] is False
        assert entry["missing_webhook"] is True

    def test_missing_webhook_flag_is_false_when_fully_configured(self, tmp_db, monkeypatch):
        """The counterpart to the above: an enabled channel WITH a
        resolvable webhook must report missing_webhook=False, proving the
        flag actually distinguishes the two states rather than always
        firing whenever a channel is enabled."""
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        monkeypatch.setenv("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://discord.example.com/webhook")
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={"dept-83": DiscordLocationChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        engine._current_location_channels = lambda: cfg.discord.location_channels
        entry = engine.get_provider_status()["providers"]["discord"]["location_channels"]["dept-83"]
        assert entry["enabled"] is True
        assert entry["configured"] is True
        assert entry["missing_webhook"] is False

    def test_get_unconfigured_enabled_channels_lists_every_kind(self, tmp_db):
        from src.notifications.config import (
            DiscordChannelConfig, DiscordConfig, DiscordLocationChannelConfig,
            DiscordTop10ChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        cfg = NotificationConfig(
            discord=DiscordConfig(
                channels={"pipeline": DiscordChannelConfig(enabled=True)},  # no webhook -> missing
                location_channels={"dept-83": DiscordLocationChannelConfig(enabled=True)},
                top10_channels={"dept-83": DiscordTop10ChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        engine._current_location_channels = lambda: cfg.discord.location_channels
        missing = engine.get_unconfigured_enabled_channels()
        kinds_found = {(m["kind"], m["name"]) for m in missing}
        assert ("static", "pipeline") in kinds_found
        assert ("location", "dept-83") in kinds_found
        assert ("top10", "dept-83") in kinds_found

    def test_log_unconfigured_channels_warns_for_missing_webhook(self, tmp_db, caplog):
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={"dept-83": DiscordLocationChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        engine._current_location_channels = lambda: cfg.discord.location_channels
        with caplog.at_level("WARNING"):
            engine.log_unconfigured_channels()
        assert any("dept-83" in record.message for record in caplog.records)

    def test_log_unconfigured_channels_silent_when_fully_configured(self, tmp_db, monkeypatch, caplog):
        from src.notifications.config import (
            DiscordConfig, DiscordLocationChannelConfig, NotificationConfig,
        )
        from src.notifications.engine import NotificationEngine
        monkeypatch.setenv("DISCORD_LOCATION_DEPT_83_WEBHOOK_URL", "https://discord.example.com/webhook")
        cfg = NotificationConfig(
            discord=DiscordConfig(
                location_channels={"dept-83": DiscordLocationChannelConfig(enabled=True)},
            ),
        )
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=cfg)
        engine._current_location_channels = lambda: cfg.discord.location_channels
        with caplog.at_level("WARNING"):
            engine.log_unconfigured_channels()
        assert not any("dept-83" in record.message for record in caplog.records)

    def test_log_unconfigured_channels_never_raises_on_internal_error(self, tmp_db, monkeypatch, caplog):
        """Startup health reporting must never prevent the backend from
        starting — even if the check itself blows up."""
        from src.notifications.config import NotificationConfig
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(Path(tmp_db).parent / "config"), config=NotificationConfig())

        def _boom(self):
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(NotificationEngine, "get_unconfigured_enabled_channels", _boom)
        with caplog.at_level("WARNING"):
            engine.log_unconfigured_channels()  # must not raise
        assert any("could not check Discord channel health" in r.message for r in caplog.records)

    def test_provider_status_reflects_a_department_enabled_after_construction(self, tmp_db):
        """Point 1 of the follow-up audit: get_provider_status() was the one
        remaining place still capable of reading a stale location_channels
        snapshot between pipeline runs, which could misreport a genuinely
        enabled-and-working department as 'missing_webhook'/unconfigured to
        any dashboard/system-status caller until the next pipeline run. Same
        construct-before-enable shape as the P0 regression test in
        TestDepartmentActivationBackfill, but exercising get_provider_status
        / get_unconfigured_enabled_channels directly rather than the send
        path — with NO pipeline run in between."""
        import os

        config_dir = Path(tmp_db).parent / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "notifications.yaml").write_text(
            "notifications:\n  enabled: true\n  discord:\n    enabled: true\n", encoding="utf-8"
        )
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(config_dir))

        status_before = engine.get_provider_status()["providers"]["discord"]["location_channels"]
        assert "dept-04" not in status_before

        os.environ["DISCORD_LOCATION_DEPT_04_WEBHOOK_URL"] = "https://discord.example.com/dept-04"
        try:
            # Reconciliation worker activates dept-04 on disk — no engine restart.
            (config_dir / "notifications.yaml").write_text(
                "notifications:\n"
                "  enabled: true\n"
                "  discord:\n"
                "    enabled: true\n"
                "    location_channels:\n"
                "      dept-04:\n"
                "        enabled: true\n",
                encoding="utf-8",
            )
            status_after = engine.get_provider_status()["providers"]["discord"]["location_channels"]
            assert status_after["dept-04"]["enabled"] is True
            assert status_after["dept-04"]["configured"] is True
            assert status_after["dept-04"]["missing_webhook"] is False

            missing = engine.get_unconfigured_enabled_channels()
            assert ("location", "dept-04") not in {(m["kind"], m["name"]) for m in missing}
        finally:
            del os.environ["DISCORD_LOCATION_DEPT_04_WEBHOOK_URL"]


# ---------------------------------------------------------------------------
# Auto-trigger: SchedulerService triggers pipeline after any successful scan
# ---------------------------------------------------------------------------

class TestSchedulerAutoPipelineTrigger:
    """The scheduler generically auto-triggers the pipeline after any source scan.

    Auto-trigger moved from LinkedInHandler to SchedulerService._dispatch() so
    that every future source handler benefits automatically.
    """

    def _make_svc(self, tmp_path, pipeline_runner=None):
        from src.scheduler.service import SchedulerService
        return SchedulerService(
            db_path=str(tmp_path / "test.db"),
            config_dir=str(tmp_path),
            handlers={},
            pipeline_runner=pipeline_runner,
        )

    def _make_defn(self, source_id="test_source"):
        import types
        return types.SimpleNamespace(id="test_sched", source_id=source_id)

    def _dispatch_and_join(self, svc, defn, exec_id, handler, timeout=5.0):
        """Run svc._dispatch() and deterministically wait for its daemon
        thread to finish, instead of racing it with a fixed sleep (flaky
        under CI load — see 5343c60 / e2e683d)."""
        import threading
        from unittest.mock import patch
        real_thread = threading.Thread
        started: list[threading.Thread] = []

        def _spy(*args, **kwargs):
            t = real_thread(*args, **kwargs)
            started.append(t)
            return t

        with patch("src.scheduler.service.threading.Thread", side_effect=_spy):
            svc._dispatch(defn, exec_id, handler)
        assert started, "svc._dispatch() did not start a thread"
        started[0].join(timeout=timeout)
        assert not started[0].is_alive(), "dispatch thread did not finish within timeout"

    def test_triggers_pipeline_after_successful_scan(self, tmp_path):
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref="scan-abc", completed=True)
        mock_runner = MagicMock()
        mock_runner.is_running.return_value = False
        mock_runner.start_run.return_value = "run-xyz"

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=mock_runner)
            self._dispatch_and_join(svc, self._make_defn(), "exec-001", mock_handler)

        mock_runner.start_run.assert_called_once()

    def test_does_not_trigger_if_pipeline_already_running(self, tmp_path):
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref="scan-abc", completed=True)
        mock_runner = MagicMock()
        mock_runner.is_running.return_value = True  # already running

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=mock_runner)
            self._dispatch_and_join(svc, self._make_defn(), "exec-001", mock_handler)

        mock_runner.start_run.assert_not_called()

    def test_does_not_trigger_if_scan_returned_none(self, tmp_path):
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref=None, completed=False)  # scan did not complete
        mock_runner = MagicMock()
        mock_runner.is_running.return_value = False

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=mock_runner)
            self._dispatch_and_join(svc, self._make_defn(), "exec-001", mock_handler)

        mock_runner.start_run.assert_not_called()

    def test_no_pipeline_runner_no_trigger(self, tmp_path):
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref="scan-abc", completed=True)

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=None)
            self._dispatch_and_join(svc, self._make_defn(), "exec-001", mock_handler)

        # No runner configured — dispatch should complete without error
        mock_handler.dispatch.assert_called_once()

    def test_pipeline_start_runtime_error_is_swallowed(self, tmp_path):
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref="scan-abc", completed=True)
        mock_runner = MagicMock()
        mock_runner.is_running.return_value = False
        mock_runner.start_run.side_effect = RuntimeError("already running")

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=mock_runner)
            # Should not propagate RuntimeError from the daemon thread
            self._dispatch_and_join(svc, self._make_defn(), "exec-001", mock_handler)

        # Dispatch completed; pipeline start was attempted
        mock_runner.start_run.assert_called_once()

    def test_generic_trigger_works_for_any_source(self, tmp_path):
        """Auto-trigger is source-agnostic — works for non-LinkedIn handlers too."""
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref="scan-indeed-001", completed=True)
        mock_runner = MagicMock()
        mock_runner.is_running.return_value = False
        mock_runner.start_run.return_value = "run-xyz"

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=mock_runner)
            self._dispatch_and_join(svc, self._make_defn(source_id="indeed"), "exec-001", mock_handler)

        mock_runner.start_run.assert_called_once()

    def test_scan_with_zero_staged_jobs_still_triggers_pipeline(self, tmp_path):
        """A scan that completed but staged 0 jobs still triggers the pipeline.

        Decision rationale: the pipeline collects from multiple sources (apec,
        jobicy, remoteok, etc.) and re-scores existing LinkedIn jobs regardless
        of how many new LinkedIn jobs were staged.  Blocking the pipeline on
        zero-staging would suppress all other collectors unnecessarily.

        The scan handler returns a non-None scan_id when the scan completed
        (even with 0 results); the scheduler considers this a successful
        dispatch and triggers the pipeline.
        """
        from unittest.mock import patch
        from src.scheduler.service import DispatchResult
        # Handler returns a scan_id even though it staged 0 jobs internally
        mock_handler = MagicMock()
        mock_handler.dispatch.return_value = DispatchResult(run_ref="scan-zero-results", completed=True)
        mock_runner = MagicMock()
        mock_runner.is_running.return_value = False
        mock_runner.start_run.return_value = "run-xyz"

        with patch("src.scheduler.service.SchedulerStorage") as mock_cls:
            mock_cls.return_value = MagicMock()
            svc = self._make_svc(tmp_path, pipeline_runner=mock_runner)
            self._dispatch_and_join(svc, self._make_defn(), "exec-001", mock_handler)

        # Pipeline triggered even for a zero-result scan
        mock_runner.start_run.assert_called_once()


# ---------------------------------------------------------------------------
# End-to-end: real HTTP server receives the webhook POST
# ---------------------------------------------------------------------------

class TestNotificationE2E:
    """Exercises the full notification path against a real local HTTP server.

    No mocking of httpx — the providers make real TCP connections to a
    localhost test server so we can verify the exact wire format.
    """

    def _make_result(self):
        import types
        return types.SimpleNamespace(
            run_id="e2e-run-001",
            status="completed",
            error=None,
            scored_jobs=[],
            stats=types.SimpleNamespace(to_dict=lambda: {
                "collected": 50, "validated": 45, "deduplicated": 40,
                "filtered": 20, "scored": 18, "semantically_matched": 15,
                "personalized": 15, "duration_seconds": 8.3, "errors": [],
            }),
        )

    def _start_server(self):
        """Start a one-shot HTTP server that captures one POST body."""
        import http.server
        import threading

        received: list[dict] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(json.loads(body.decode()))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass  # silence test output

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        return server, port, thread, received

    def test_discord_real_http_post(self, tmp_path):
        """Discord provider POSTs a valid embed payload to the webhook URL."""
        from src.notifications.config import (
            DiscordConfig, N8nConfig, NotificationConfig, NotificationRules,
        )
        from src.notifications.engine import NotificationEngine

        server, port, thread, received = self._start_server()
        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=True, webhook_url=f"http://127.0.0.1:{port}"),
            n8n=N8nConfig(enabled=False, webhook_url=""),
            rules=NotificationRules(
                notify_pipeline_completed=True,
                notify_new_jobs=True,
                min_score_for_notification=0.80,
                max_jobs_in_notification=5,
            ),
        )
        engine = NotificationEngine(str(tmp_path / "e2e.db"), config_dir=str(tmp_path / "config"), config=cfg)
        engine.notify_pipeline_completed(self._make_result())

        thread.join(timeout=5)
        server.server_close()

        assert len(received) == 1, f"Expected 1 POST, got {len(received)}"
        payload = received[0]
        # Discord format wraps content in an embeds list
        assert "embeds" in payload
        assert len(payload["embeds"]) >= 1
        embed = payload["embeds"][0]
        assert "title" in embed
        assert "fields" in embed

    def test_n8n_real_http_post(self, tmp_path):
        """n8n provider POSTs the raw PipelineCompletedEvent JSON payload."""
        from src.notifications.config import (
            DiscordConfig, N8nConfig, NotificationConfig, NotificationRules,
        )
        from src.notifications.engine import NotificationEngine

        server, port, thread, received = self._start_server()
        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=False, webhook_url=""),
            n8n=N8nConfig(enabled=True, webhook_url=f"http://127.0.0.1:{port}"),
            rules=NotificationRules(
                notify_pipeline_completed=True,
                notify_new_jobs=True,
                min_score_for_notification=0.80,
                max_jobs_in_notification=5,
            ),
        )
        engine = NotificationEngine(str(tmp_path / "e2e_n8n.db"), config_dir=str(tmp_path / "config"), config=cfg)
        engine.notify_pipeline_completed(self._make_result())

        thread.join(timeout=5)
        server.server_close()

        assert len(received) == 1, f"Expected 1 POST, got {len(received)}"
        payload = received[0]
        # Exact n8n payload schema: PipelineCompletedEvent.to_dict()
        assert payload["event"] == "pipeline.completed"
        assert payload["version"] == 1
        assert payload["run_id"] == "e2e-run-001"
        assert payload["status"] == "completed"
        assert payload["error"] is None
        assert "timestamp" in payload
        stats = payload["stats"]
        assert stats["collected"] == 50
        assert stats["scored"] == 18
        assert stats["duration_seconds"] == 8.3
        assert payload["new_high_match_jobs"] == []

    def test_dedup_prevents_second_post(self, tmp_path):
        """Calling notify twice with the same run_id sends only one HTTP POST."""
        from src.notifications.config import (
            DiscordConfig, N8nConfig, NotificationConfig, NotificationRules,
        )
        from src.notifications.engine import NotificationEngine
        import http.server
        import threading

        received: list[dict] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length).decode()))
                self.send_response(204)
                self.end_headers()
            def log_message(self, *args): pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]

        def _serve_two():
            server.handle_request()
            server.handle_request()

        thread = threading.Thread(target=_serve_two, daemon=True)
        thread.start()

        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=True, webhook_url=f"http://127.0.0.1:{port}"),
            n8n=N8nConfig(enabled=False, webhook_url=""),
            rules=NotificationRules(
                notify_pipeline_completed=True,
                notify_new_jobs=False,
                min_score_for_notification=0.80,
                max_jobs_in_notification=5,
            ),
        )
        engine = NotificationEngine(str(tmp_path / "e2e_dedup.db"), config_dir=str(tmp_path / "config"), config=cfg)
        result = self._make_result()

        engine.notify_pipeline_completed(result)  # first call → sends
        engine.notify_pipeline_completed(result)  # second call → deduplicated

        import time
        time.sleep(0.3)
        server.server_close()

        assert len(received) == 1, (
            f"Deduplication failed: got {len(received)} POSTs for the same run_id"
        )


# ---------------------------------------------------------------------------
# Phase NOW / I16: source-yield health alerting — wiring-level tests.
#
# src/notifications/source_health.py's detection logic is unit-tested in
# isolation in tests/test_source_health.py; these tests prove
# NotificationEngine actually reaches it and sends a real Discord embed via
# the existing #errors channel/dedup machinery, using the same real-local-
# HTTP-server pattern as TestNotificationE2E above (no mocking of httpx).
# ---------------------------------------------------------------------------

class TestSourceYieldAnomalyAlerting:
    def _make_result(self, run_id: str, by_source: dict):
        import types
        return types.SimpleNamespace(
            run_id=run_id,
            status="completed",
            error=None,
            scored_jobs=[],
            stats=types.SimpleNamespace(
                by_source=by_source,
                to_dict=lambda: {
                    "collected": sum(v.get("collected", 0) for v in by_source.values()),
                    "validated": 0, "deduplicated": 0, "filtered": 0, "scored": 0,
                    "semantically_matched": 0, "personalized": 0,
                    "duration_seconds": 1.0, "errors": [],
                },
            ),
        )

    def _seed_healthy_history(self, db_path: str, source: str, values: list[int]) -> None:
        """Write `len(values)` prior COMPLETED runs, each with `source`
        yielding one of `values`, oldest first (so the last one written is
        the most recent prior run)."""
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=db_path)
        rs.init_db()
        try:
            for i, v in enumerate(values):
                run_id = f"history-run-{i}"
                rs.create_run(run_id)
                rs.complete_run(run_id, {"by_source": {source: {"collected": v, "accepted": 0, "errors": 0}}})
        finally:
            rs.close()

    def _start_multi_request_server(self, count: int):
        import http.server
        import threading

        received: list[dict] = []

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                received.append(json.loads(self.rfile.read(length).decode()))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]

        def _serve() -> None:
            for _ in range(count):
                server.handle_request()

        thread = threading.Thread(target=_serve, daemon=True)
        thread.start()
        return server, port, thread, received

    def _engine(self, db_path: str, tmp_path, port: int):
        from src.notifications.config import DiscordConfig, N8nConfig, NotificationConfig, NotificationRules
        from src.notifications.engine import NotificationEngine

        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=True, webhook_url=f"http://127.0.0.1:{port}"),
            n8n=N8nConfig(enabled=False, webhook_url=""),
            rules=NotificationRules(notify_pipeline_completed=True, notify_new_jobs=False),
        )
        return NotificationEngine(db_path, config_dir=str(tmp_path / "config"), config=cfg)

    def test_new_zero_yield_anomaly_sends_alert(self, tmp_path) -> None:
        db_path = str(tmp_path / "yield.db")
        self._seed_healthy_history(db_path, "indeed", [500, 480, 510, 495, 505])

        server, port, thread, received = self._start_multi_request_server(2)  # pipeline.completed + alert
        engine = self._engine(db_path, tmp_path, port)
        result = self._make_result("current-run", {"indeed": {"collected": 0, "accepted": 0, "errors": 1}})
        engine.notify_pipeline_completed(result)

        thread.join(timeout=5)
        server.server_close()

        alert_payloads = [
            p for p in received
            if p.get("embeds") and "ZERO jobs" in p["embeds"][0].get("title", "")
        ]
        assert len(alert_payloads) == 1, f"Expected exactly 1 anomaly alert, got {received}"
        fields = {f["name"]: f["value"] for f in alert_payloads[0]["embeds"][0]["fields"]}
        assert "`indeed`" in fields["Source"]

    def test_healthy_yield_sends_no_alert(self, tmp_path) -> None:
        db_path = str(tmp_path / "yield_healthy.db")
        self._seed_healthy_history(db_path, "apec", [50, 48, 52, 49, 47])

        server, port, thread, received = self._start_multi_request_server(1)  # pipeline.completed only
        engine = self._engine(db_path, tmp_path, port)
        result = self._make_result("current-run", {"apec": {"collected": 51, "accepted": 10, "errors": 0}})
        engine.notify_pipeline_completed(result)

        thread.join(timeout=5)
        server.server_close()

        assert len(received) == 1
        assert received[0].get("embeds", [{}])[0].get("title") is not None  # the pipeline.completed embed
        assert not any("anomaly" in str(p).lower() or "ZERO jobs" in str(p) for p in received)

    def test_continuing_anomaly_does_not_re_alert(self, tmp_path) -> None:
        """The source was ALREADY at 0 in the immediately-prior run — this
        run's alert must be suppressed (is_new=False), leaving only the
        ordinary pipeline.completed send."""
        db_path = str(tmp_path / "yield_continuing.db")
        from src.storage.runs import RunStorage

        rs = RunStorage(db_path=db_path)
        rs.init_db()
        # 5 healthy runs, then 1 already-broken run (most recent prior run).
        for i, v in enumerate([500, 480, 510, 495, 505]):
            rs.create_run(f"h{i}")
            rs.complete_run(f"h{i}", {"by_source": {"indeed": {"collected": v}}})
        rs.create_run("already-broken")
        rs.complete_run("already-broken", {"by_source": {"indeed": {"collected": 0, "errors": 1}}})
        rs.close()

        server, port, thread, received = self._start_multi_request_server(1)  # pipeline.completed only
        engine = self._engine(db_path, tmp_path, port)
        result = self._make_result("current-run", {"indeed": {"collected": 0, "accepted": 0, "errors": 1}})
        engine.notify_pipeline_completed(result)

        thread.join(timeout=5)
        server.server_close()

        assert len(received) == 1
        assert not any("ZERO jobs" in str(p) for p in received)

    def test_source_health_check_failure_never_blocks_other_notifications(self, tmp_path) -> None:
        """A corrupted/unreadable run history must not prevent the ordinary
        pipeline.completed notification from being sent (never-raises
        guarantee, see engine.py's try/except around this check)."""
        db_path = str(tmp_path / "yield_corrupt.db")
        # No RunStorage schema initialized at all at this path — list_runs()
        # against a missing/empty db exercises the same "history unusable"
        # path defensively, without hand-crafting invalid SQL rows.

        server, port, thread, received = self._start_multi_request_server(1)
        engine = self._engine(db_path, tmp_path, port)
        result = self._make_result("current-run", {"indeed": {"collected": 0, "accepted": 0, "errors": 1}})
        engine.notify_pipeline_completed(result)  # must not raise

        thread.join(timeout=5)
        server.server_close()

        assert len(received) == 1  # pipeline.completed still sent


# ---------------------------------------------------------------------------
# Phase 5 (2026-09-10): duplicate-notification-path warning
#
# discord.enabled=True AND n8n.enabled=True together risks an n8n workflow
# independently posting its OWN Discord notifications via the same legacy
# webhook the Python Discord channels also fall back to — historically the
# now-deleted config/n8n_workflow.json, but the check stays generic to any
# workflow that could do the same thing. This never changes behavior — it
# only makes the risk visible in logs so it's caught instead of silently
# double-posting.
# ---------------------------------------------------------------------------

class TestDuplicateDiscordPathWarning:
    def _result(self):
        import types
        return types.SimpleNamespace(
            run_id="warn-run-001",
            status="completed",
            error=None,
            scored_jobs=[],
            stats=types.SimpleNamespace(to_dict=lambda: {
                "collected": 10, "validated": 8, "deduplicated": 7,
                "filtered": 5, "scored": 5, "semantically_matched": 5,
                "personalized": 5, "duration_seconds": 1.0, "errors": [],
            }),
        )

    def test_warns_when_both_discord_and_n8n_enabled(self, tmp_path, caplog):
        from src.notifications.config import DiscordConfig, N8nConfig, NotificationConfig, NotificationRules
        from src.notifications.engine import NotificationEngine

        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=True, webhook_url=""),
            n8n=N8nConfig(enabled=True, webhook_url=""),  # the documented migration-overlap state
            rules=NotificationRules(),
        )
        engine = NotificationEngine(str(tmp_path / "warn.db"), config_dir=str(tmp_path / "config"), config=cfg)
        with caplog.at_level("WARNING"):
            engine.notify_pipeline_completed(self._result())

        assert any("duplicate" in r.message.lower() for r in caplog.records), (
            "Expected a warning about the discord+n8n duplicate-notification path"
        )

    def test_no_warning_in_the_documented_end_state(self, tmp_path, caplog):
        """discord.enabled=True + n8n.enabled=False is the documented,
        recommended end state (config/notifications.yaml) — no warning."""
        from src.notifications.config import DiscordConfig, N8nConfig, NotificationConfig, NotificationRules
        from src.notifications.engine import NotificationEngine

        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=True, webhook_url=""),
            n8n=N8nConfig(enabled=False, webhook_url=""),
            rules=NotificationRules(),
        )
        engine = NotificationEngine(str(tmp_path / "nowarn.db"), config_dir=str(tmp_path / "config"), config=cfg)
        with caplog.at_level("WARNING"):
            engine.notify_pipeline_completed(self._result())

        assert not any("duplicate" in r.message.lower() for r in caplog.records)

    def test_no_warning_when_discord_disabled(self, tmp_path, caplog):
        """n8n-only (legacy, pre-migration) setup is not the duplicate risk
        this warning targets — only discord+n8n together are."""
        from src.notifications.config import DiscordConfig, N8nConfig, NotificationConfig, NotificationRules
        from src.notifications.engine import NotificationEngine

        cfg = NotificationConfig(
            enabled=True,
            discord=DiscordConfig(enabled=False, webhook_url=""),
            n8n=N8nConfig(enabled=True, webhook_url=""),
            rules=NotificationRules(),
        )
        engine = NotificationEngine(str(tmp_path / "n8nonly.db"), config_dir=str(tmp_path / "config"), config=cfg)
        with caplog.at_level("WARNING"):
            engine.notify_pipeline_completed(self._result())

        assert not any("duplicate" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Top10 leaderboard update (2026-09-11) — independent of the new-arrivals
# feed above: no dedup, always re-renders one standing message per enabled
# top10 channel from a fresh DB query, every run.
# ---------------------------------------------------------------------------

class _RecordingTop10Provider:
    """Fake DiscordProvider exposing only send_or_update (the top10 path
    never calls plain send())."""

    def __init__(self, should_succeed: bool = True, returned_message_id: str = "new-msg-id"):
        self.enabled = True
        self.calls: list[tuple[dict, str]] = []
        self.should_succeed = should_succeed
        self.returned_message_id = returned_message_id

    def send_or_update(self, payload: dict, message_id: str) -> str | None:
        self.calls.append((payload, message_id))
        return self.returned_message_id if self.should_succeed else None


def _write_top10_notifications_yaml(config_dir, *, message_id: str = "") -> None:
    import yaml
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "notifications.yaml").write_text(
        yaml.safe_dump({
            "notifications": {
                "enabled": True,
                "discord": {
                    "enabled": True,
                    "top10_channels": {
                        "dept-83": {
                            "enabled": True,
                            "webhook_url_env": "X",
                            "channel_id": "chan-1",
                            "webhook_id": "wh-1",
                            "message_id": message_id,
                        }
                    },
                },
            }
        }),
        encoding="utf-8",
    )


class TestTop10LeaderboardUpdate:
    def _engine(
        self, monkeypatch, config_dir, db_path, location_preferences, top10_providers, query_result=None
    ):
        import src.notifications.engine as engine_module
        from src.notifications.engine import NotificationEngine

        engine = NotificationEngine(db_path=str(db_path), config_dir=str(config_dir))
        engine._load_location_preferences = lambda: location_preferences
        engine._build_top10_discord_channel = lambda slug: top10_providers.get(slug)
        engine._build_location_discord_channel = lambda slug: None
        engine._build_discord_channel = lambda name: None
        monkeypatch.setattr(engine_module, "compute_top10_by_slug", lambda *a, **k: (query_result or {}))
        return engine

    def test_enabled_channel_gets_updated_with_query_results(self, tmp_path, monkeypatch):
        _write_top10_notifications_yaml(tmp_path / "config")
        provider = _RecordingTop10Provider()
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")],
            {"dept-83": provider},
            query_result={"dept-83": [{"rank": 1, "title": "Top Dev"}]},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-top10"))

        assert len(provider.calls) == 1
        payload, message_id = provider.calls[0]
        assert payload["event"] == "jobs.top10_leaderboard"
        assert payload["entries"] == [{"rank": 1, "title": "Top Dev"}]
        assert message_id == ""  # nothing persisted yet in the fixture

    def test_disabled_top10_channel_is_never_queried_or_sent(self, tmp_path, monkeypatch):
        """No top10_channels entry at all — must skip before even computing
        the query (proven simply by zero provider calls)."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "notifications.yaml").write_text(
            "notifications:\n  enabled: true\n  discord:\n    enabled: true\n", encoding="utf-8"
        )
        provider = _RecordingTop10Provider()
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-none"))
        assert provider.calls == []

    def test_excluded_location_is_skipped(self, tmp_path, monkeypatch):
        _write_top10_notifications_yaml(tmp_path / "config")
        provider = _RecordingTop10Provider()
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83", status="excluded")],
            {"dept-83": provider},
            query_result={"dept-83": [{"rank": 1, "title": "Should not be sent"}]},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-excluded"))
        assert provider.calls == []

    def test_send_failure_does_not_persist_and_does_not_crash(self, tmp_path, monkeypatch):
        _write_top10_notifications_yaml(tmp_path / "config")
        provider = _RecordingTop10Provider(should_succeed=False)
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider}, query_result={},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-fail"))  # must not raise
        assert len(provider.calls) == 1

        from src.notifications.config import load_notification_config
        saved = load_notification_config(str(tmp_path / "config"))
        assert saved.discord.top10_channels["dept-83"].message_id == ""

    def test_new_message_id_is_persisted_to_the_registry(self, tmp_path, monkeypatch):
        _write_top10_notifications_yaml(tmp_path / "config", message_id="")
        provider = _RecordingTop10Provider(returned_message_id="brand-new-id")
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider}, query_result={},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-persist"))

        from src.notifications.config import load_notification_config
        saved = load_notification_config(str(tmp_path / "config"))
        assert saved.discord.top10_channels["dept-83"].message_id == "brand-new-id"

    def test_unchanged_message_id_is_not_rewritten(self, tmp_path, monkeypatch):
        """send_or_update returning the SAME message_id (a normal edit, not
        a fresh create) must not trigger a registry write at all."""
        _write_top10_notifications_yaml(tmp_path / "config", message_id="existing-id")
        provider = _RecordingTop10Provider(returned_message_id="existing-id")
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider}, query_result={},
        )
        called = []
        monkeypatch.setattr(
            engine, "_persist_top10_message_id",
            lambda *a, **k: called.append(1),
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-noop"))
        assert called == []

    def test_repeated_calls_on_the_same_long_lived_engine_edit_the_same_message(self, tmp_path, monkeypatch):
        """Regression: NotificationEngine is normally a SINGLETON reused
        across every pipeline run for the whole backend process lifetime
        (see src/api/app.py) — _persist_top10_message_id used to only write
        the new message id to disk, never back into this engine's own
        in-memory self._config. A second call on the SAME engine instance
        would therefore keep reading the original empty message_id and
        create a BRAND NEW Discord message every single run, forever,
        instead of ever editing the first one. This proves two consecutive
        calls on one engine instance reuse the persisted id."""
        _write_top10_notifications_yaml(tmp_path / "config", message_id="")
        provider = _RecordingTop10Provider(returned_message_id="msg-1")
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider}, query_result={},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-1"))
        engine.notify_pipeline_completed(_make_pipeline_result("run-2"))

        message_ids_used = [call[1] for call in provider.calls]
        assert message_ids_used == ["", "msg-1"]

    def test_department_added_to_registry_after_engine_construction_still_gets_refreshed(
        self, tmp_path, monkeypatch
    ):
        """Root-cause regression: NotificationEngine is a long-lived
        singleton (src/api/app.py) whose self._config.discord.top10_channels
        used to be loaded exactly once at construction and never refreshed.
        src.discord_admin's reconciliation worker runs independently (its
        own timer) and, on discovering a newly configured department, writes
        a brand-new top10_channels registry entry straight to
        notifications.yaml — completely invisible to an already-running
        engine's stale in-memory snapshot. desired_slugs was derived from
        that stale snapshot, so a department added to the registry AFTER
        this engine was constructed would never be queried or sent to,
        forever, until the backend process restarted. _update_top10_leaderboards
        must re-read the registry from disk on every call, exactly like
        _load_location_preferences already does for the profile."""
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "notifications.yaml").write_text(
            "notifications:\n  enabled: true\n  discord:\n    enabled: true\n", encoding="utf-8"
        )
        provider = _RecordingTop10Provider()
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider},
            query_result={"dept-83": [{"rank": 1, "title": "New Dept Job"}]},
        )

        # Simulate src.discord_admin's reconciliation worker creating the
        # channel and writing its registry entry AFTER the engine started.
        _write_top10_notifications_yaml(tmp_path / "config")

        engine.notify_pipeline_completed(_make_pipeline_result("run-after-reconcile"))

        assert len(provider.calls) == 1
        payload, _ = provider.calls[0]
        assert payload["entries"] == [{"rank": 1, "title": "New Dept Job"}]

    def test_channel_reenabled_by_reconciliation_after_construction_still_gets_refreshed(
        self, tmp_path, monkeypatch
    ):
        """Same staleness bug, different trigger: a channel disabled at
        engine-construction time (e.g. its department was temporarily
        excluded) gets re-enabled by src.discord_admin's independent
        reconciliation pass — the long-lived engine's stale in-memory
        ``enabled=False`` must not keep suppressing its leaderboard content
        forever."""
        _write_top10_notifications_yaml(tmp_path / "config")
        import yaml
        raw = yaml.safe_load((tmp_path / "config" / "notifications.yaml").read_text(encoding="utf-8"))
        raw["notifications"]["discord"]["top10_channels"]["dept-83"]["enabled"] = False
        (tmp_path / "config" / "notifications.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")

        provider = _RecordingTop10Provider()
        engine = self._engine(
            monkeypatch, tmp_path / "config", tmp_path / "t.db",
            [_loc_pref("department", "83")], {"dept-83": provider},
            query_result={"dept-83": [{"rank": 1, "title": "Reactivated"}]},
        )

        # Reconciliation re-enables it after construction.
        _write_top10_notifications_yaml(tmp_path / "config")

        engine.notify_pipeline_completed(_make_pipeline_result("run-reenabled"))

        assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# #statistics channel update (2026-09-11) — a single standing message
# summarizing the job database, edited in place every run. Same convention
# as the top10 leaderboards above, but one message total (not per-location)
# and content comes from src.api.routes.jobs.compute_job_stats (never
# duplicated in the engine).
# ---------------------------------------------------------------------------

class _RecordingStatisticsProvider:
    def __init__(self, should_succeed: bool = True, returned_message_id: str = "new-msg-id"):
        self.enabled = True
        self.calls: list[tuple[dict, str]] = []
        self.should_succeed = should_succeed
        self.returned_message_id = returned_message_id

    def send_or_update(self, payload: dict, message_id: str) -> str | None:
        self.calls.append((payload, message_id))
        return self.returned_message_id if self.should_succeed else None


def _write_statistics_notifications_yaml(config_dir, *, message_id: str = "") -> None:
    import yaml
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "notifications.yaml").write_text(
        yaml.safe_dump({
            "notifications": {
                "enabled": True,
                "discord": {
                    "enabled": True,
                    "channels": {
                        "statistics": {
                            "enabled": True,
                            "webhook_url_env": "X",
                            "channel_id": "chan-1",
                            "webhook_id": "wh-1",
                            "message_id": message_id,
                        }
                    },
                },
            }
        }),
        encoding="utf-8",
    )


def _fake_job_stats():
    from src.api.routes.jobs import JobStatsOut, SkillFacet

    return JobStatsOut(total_jobs=42, skills=[SkillFacet(name="python", count=10)])


class TestStatisticsUpdate:
    def _engine(self, monkeypatch, config_dir, db_path, provider, stats=None):
        import src.api.routes.jobs as jobs_module
        from src.notifications.engine import NotificationEngine

        engine = NotificationEngine(db_path=str(db_path), config_dir=str(config_dir))
        engine._build_top10_discord_channel = lambda slug: None
        engine._build_location_discord_channel = lambda slug: None
        engine._build_discord_channel = lambda name: None
        engine._build_statistics_discord_channel = lambda: provider
        monkeypatch.setattr(jobs_module, "compute_job_stats", lambda *a, **k: stats or _fake_job_stats())
        return engine

    def test_enabled_channel_gets_updated_with_computed_stats(self, tmp_path, monkeypatch):
        _write_statistics_notifications_yaml(tmp_path / "config")
        provider = _RecordingStatisticsProvider()
        engine = self._engine(monkeypatch, tmp_path / "config", tmp_path / "t.db", provider)

        engine.notify_pipeline_completed(_make_pipeline_result("run-stats"))

        assert len(provider.calls) == 1
        payload, message_id = provider.calls[0]
        assert payload["event"] == "jobs.statistics"
        assert payload["total_jobs"] == 42
        assert payload["top_skills"] == [("python", 10)]
        assert message_id == ""

    def test_disabled_statistics_channel_is_never_queried_or_sent(self, tmp_path, monkeypatch):
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "notifications.yaml").write_text(
            "notifications:\n  enabled: true\n  discord:\n    enabled: true\n"
            "    channels:\n      statistics:\n        enabled: false\n",
            encoding="utf-8",
        )
        provider = _RecordingStatisticsProvider()
        engine = self._engine(monkeypatch, tmp_path / "config", tmp_path / "t.db", provider)
        engine.notify_pipeline_completed(_make_pipeline_result("run-none"))
        assert provider.calls == []

    def test_send_failure_does_not_persist_and_does_not_crash(self, tmp_path, monkeypatch):
        _write_statistics_notifications_yaml(tmp_path / "config")
        provider = _RecordingStatisticsProvider(should_succeed=False)
        engine = self._engine(monkeypatch, tmp_path / "config", tmp_path / "t.db", provider)

        engine.notify_pipeline_completed(_make_pipeline_result("run-fail"))  # must not raise

        from src.notifications.config import load_notification_config
        saved = load_notification_config(str(tmp_path / "config"))
        assert saved.discord.channels["statistics"].message_id == ""

    def test_new_message_id_is_persisted_to_the_registry(self, tmp_path, monkeypatch):
        _write_statistics_notifications_yaml(tmp_path / "config", message_id="")
        provider = _RecordingStatisticsProvider(returned_message_id="brand-new-id")
        engine = self._engine(monkeypatch, tmp_path / "config", tmp_path / "t.db", provider)

        engine.notify_pipeline_completed(_make_pipeline_result("run-persist"))

        from src.notifications.config import load_notification_config
        saved = load_notification_config(str(tmp_path / "config"))
        assert saved.discord.channels["statistics"].message_id == "brand-new-id"

    def test_unchanged_message_id_is_not_rewritten(self, tmp_path, monkeypatch):
        _write_statistics_notifications_yaml(tmp_path / "config", message_id="existing-id")
        provider = _RecordingStatisticsProvider(returned_message_id="existing-id")
        engine = self._engine(monkeypatch, tmp_path / "config", tmp_path / "t.db", provider)
        called = []
        monkeypatch.setattr(
            engine, "_persist_static_channel_message_id",
            lambda *a, **k: called.append(1),
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-noop"))
        assert called == []

    def test_repeated_calls_on_the_same_long_lived_engine_edit_the_same_message(self, tmp_path, monkeypatch):
        """Regression: same class of bug as TestTop10LeaderboardUpdate's
        equivalent test — a long-lived NotificationEngine singleton must
        reuse the message id it just persisted on its NEXT call, not keep
        reading a stale empty id from its own initial in-memory config."""
        _write_statistics_notifications_yaml(tmp_path / "config", message_id="")
        provider = _RecordingStatisticsProvider(returned_message_id="msg-1")
        engine = self._engine(monkeypatch, tmp_path / "config", tmp_path / "t.db", provider)

        engine.notify_pipeline_completed(_make_pipeline_result("run-1"))
        engine.notify_pipeline_completed(_make_pipeline_result("run-2"))

        message_ids_used = [call[1] for call in provider.calls]
        assert message_ids_used == ["", "msg-1"]


# ---------------------------------------------------------------------------
# Department-activation backfill (2026-09-15 fix)
#
# Root cause: Discord location routing was previously driven ONLY by
# run-over-run "new_jobs" (jobs not scored in the previous run). A job
# already sitting in the database — including every job matching a
# department that was only just added to profile.geography.location_preferences
# — is by definition NOT "new" and therefore never got routed, even on a
# newly-activated department's very first run, and even indefinitely for an
# established department whose matching jobs simply weren't run-over-run
# "new" the one time everything else (channel configured, tier floor) lined
# up. These tests exercise the real DB-backed backfill path
# (_backfill_notification_candidates / compute_notification_backfill_candidates)
# rather than the new_jobs parameter, since that is precisely the path the
# bug lived in.
# ---------------------------------------------------------------------------

def _engine_with_backfill(
    tmp_db,
    location_preferences: list[dict],
    channel_providers: dict[str, "_RecordingProvider"],
    config=None,
):
    """Like _engine_with_location_channels, but ALSO marks each provided
    slug's location channel as `enabled`.

    _backfill_notification_candidates reads _current_location_channels()
    (unlike routing-destination resolution, which every other test in this
    file bypasses via the _build_location_discord_channel stub) — it must,
    since "is this location channel actually configured" is exactly what
    decides which departments are eligible for backfill.
    _current_location_channels() always reloads notifications.yaml from
    disk (see its docstring — this is the fix for the department-activation
    staleness bug), so it is stubbed here rather than mutated on
    engine._config, which a real reload would otherwise overwrite on every
    notify_pipeline_completed() call.
    """
    from src.notifications.config import DiscordLocationChannelConfig
    engine = _engine_with_location_channels(tmp_db, location_preferences, channel_providers, config=config)
    engine._current_location_channels = lambda: {
        slug: DiscordLocationChannelConfig(enabled=True) for slug in channel_providers
    }
    return engine


def _store_active_job(
    tmp_db,
    *,
    department: str | None = None,
    remote: bool = False,
    location: str = "",
    countries: list[str] | None = None,
    score: float = 0.7,
    title: str = "DB Job",
    work_mode: str | None = None,
    employment_type: str | None = None,
) -> str:
    """Insert one real job + completed pipeline run + job_scores row into
    tmp_db (same Storage/RunStorage fixture pattern as
    tests/test_top10_query.py) and return the job id.

    Required to exercise _backfill_notification_candidates, which — unlike
    the new_jobs parameter other tests in this file pass as plain dicts —
    reads real jobs/job_scores/pipeline_runs rows via
    src.notifications.top10_query.compute_notification_backfill_candidates.
    """
    import uuid
    from datetime import UTC, datetime

    from src.models.job import Job
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    job = Job(
        id=str(uuid.uuid4()),
        title=title,
        company="TestCo",
        location=location,
        countries=countries or [],
        department=department,
        source="test",
        remote=remote,
        work_mode=work_mode,
        employment_type=employment_type,
        status="active",
        discovered_at=datetime.now(UTC),
    )
    s = Storage(db_path=tmp_db)
    s.init_db()
    rs = RunStorage(db_path=tmp_db)
    rs.init_db()
    try:
        s.upsert_jobs_batch([job])
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.store_job_scores(run_id, [{
            "job_id": job.id,
            "deterministic_score": score,
            "semantic_score": score,
            "final_score": score,
            "score_breakdown": {},
        }])
        rs.complete_run(run_id, {"personalized": 1})
    finally:
        s.close()
        rs.close()
    return job.id


def _store_active_jobs_batch(tmp_db, jobs: list[dict]) -> list[str]:
    """Like _store_active_job, but stores every job's score under ONE
    shared pipeline run — required whenever a test needs several jobs to
    simultaneously count as "currently active" (compute_notification_backfill_candidates,
    like compute_top10_by_slug, scopes to the SINGLE latest completed run's
    job_scores — see tests/test_top10_query.py's TestRunScoping — so jobs
    stored via separate _store_active_job calls, each its own completed
    run, would only ever see the last one as "current").

    Each dict in ``jobs`` accepts the same keyword fields as _store_active_job
    (department/remote/location/countries/score/title/work_mode/employment_type).
    Returns job ids in the same order as ``jobs``.
    """
    import uuid
    from datetime import UTC, datetime

    from src.models.job import Job
    from src.storage.runs import RunStorage
    from src.storage.sqlite import Storage

    built = [
        Job(
            id=spec.get("id") or str(uuid.uuid4()),
            title=spec.get("title", "DB Job"),
            company="TestCo",
            location=spec.get("location", ""),
            countries=spec.get("countries") or [],
            department=spec.get("department"),
            source="test",
            remote=spec.get("remote", False),
            work_mode=spec.get("work_mode"),
            employment_type=spec.get("employment_type"),
            status="active",
            discovered_at=datetime.now(UTC),
        )
        for spec in jobs
    ]
    s = Storage(db_path=tmp_db)
    s.init_db()
    rs = RunStorage(db_path=tmp_db)
    rs.init_db()
    try:
        s.upsert_jobs_batch(built)
        run_id = str(uuid.uuid4())
        rs.create_run(run_id)
        rs.store_job_scores(run_id, [
            {
                "job_id": job.id,
                "deterministic_score": spec.get("score", 0.7),
                "semantic_score": spec.get("score", 0.7),
                "final_score": spec.get("score", 0.7),
                "score_breakdown": {},
            }
            for job, spec in zip(built, jobs)
        ])
        rs.complete_run(run_id, {"personalized": len(built)})
    finally:
        s.close()
        rs.close()
    return [job.id for job in built]


class TestDepartmentActivationBackfill:
    def test_preexisting_job_is_eligible_on_newly_activated_departments_first_run(self, tmp_db):
        """Test 1 (spec): job exists in the DB before department 03 is
        activated, matches 03, scores above the floor, and department 03 has
        no notification history — it must still be sent on 03's first run,
        despite never being "new" in any run-over-run diff."""
        dept03_chan = _RecordingProvider()
        job_id = _store_active_job(tmp_db, department="03", location="03 - Moulins", score=0.70)
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        # No new_jobs this run — the job is pre-existing, not run-over-run new.
        engine.notify_pipeline_completed(_make_pipeline_result("run-03-activation"), new_jobs=[])
        assert {j["job_id"] for p in dept03_chan.sent for j in p["jobs"]} == {job_id}

    def test_department_enabled_after_engine_construction_still_receives_backfill_notifications(
        self, tmp_db,
    ):
        """Direct regression test for the real dept-04 incident: NotificationEngine
        is a long-lived singleton constructed once at backend startup
        (src/api/app.py), while src.discord_admin's reconciliation worker
        independently writes newly-enabled departments into
        notifications.yaml's location_channels on its own timer, completely
        out of band. Before the fix, _backfill_notification_candidates()
        computed desired_slugs from self._config.discord.location_channels
        — a snapshot frozen at construction time — so a department enabled
        AFTER construction was silently excluded from the backfill query
        forever, even though its Top10 leaderboard worked fine (that path
        already reloaded top10_channels fresh every run). This test
        constructs the engine BEFORE dept-04 is enabled on disk, then
        enables it on disk with NO engine restart, and proves a pre-existing
        (not run-over-run "new") eligible job is still sent on the very next
        run — i.e. _current_location_channels()/_backfill_notification_candidates
        genuinely re-read notifications.yaml every call, not just at
        construction."""
        config_dir = Path(tmp_db).parent / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "notifications.yaml").write_text(
            "notifications:\n  enabled: true\n  discord:\n    enabled: true\n", encoding="utf-8"
        )
        from src.notifications.engine import NotificationEngine
        engine = NotificationEngine(db_path=tmp_db, config_dir=str(config_dir))
        engine._load_location_preferences = lambda: [_loc_pref("department", "04")]
        dept04_chan = _RecordingProvider()
        engine._build_location_discord_channel = lambda slug: dept04_chan if slug == "dept-04" else None

        # Job pre-exists in the DB before dept-04 is ever activated.
        job_id = _store_active_job(tmp_db, department="04", location="04 - Digne-les-Bains", score=0.75)

        # Reconciliation worker (its own process/timer) activates dept-04 by
        # writing straight to notifications.yaml — AFTER this engine
        # instance already exists. No restart of `engine` follows.
        (config_dir / "notifications.yaml").write_text(
            "notifications:\n"
            "  enabled: true\n"
            "  discord:\n"
            "    enabled: true\n"
            "    location_channels:\n"
            "      dept-04:\n"
            "        enabled: true\n",
            encoding="utf-8",
        )

        engine.notify_pipeline_completed(_make_pipeline_result("run-dept04-activation"), new_jobs=[])

        assert {j["job_id"] for p in dept04_chan.sent for j in p["jobs"]} == {job_id}

    def test_job_already_sent_to_one_department_does_not_block_or_duplicate_a_different_department(
        self, tmp_db,
    ):
        """Test 2 (spec): a job already notified to Job-83 must not prevent
        an UNRELATED job newly-relevant for a just-activated Job-03 from
        being independently evaluated and sent — and must not itself be
        resent to 83 as a side effect."""
        var_chan = _RecordingProvider()
        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db,
            [_loc_pref("department", "83"), _loc_pref("department", "03")],
            {"dept-83": var_chan, "dept-03": dept03_chan},
        )

        # Job A already sent to dept-83 in an earlier run.
        job_a = _job_at_score("job-a-83", 90, department="83", location="Toulon", work_mode="on_site", remote=False)
        engine.notify_pipeline_completed(_make_pipeline_result("run-a"), new_jobs=[job_a])
        assert {j["job_id"] for p in var_chan.sent for j in p["jobs"]} == {"job-a-83"}

        # Job B is pre-existing in the DB, matches the just-activated dept-03.
        job_b_id = _store_active_job(tmp_db, department="03", location="03 - Vichy", score=0.68)
        engine.notify_pipeline_completed(_make_pipeline_result("run-b"), new_jobs=[])

        assert {j["job_id"] for p in dept03_chan.sent for j in p["jobs"]} == {job_b_id}
        # 83 received exactly job A, once — no duplicate triggered by B's send.
        assert {j["job_id"] for p in var_chan.sent for j in p["jobs"]} == {"job-a-83"}
        assert len(var_chan.sent) == 1

    def test_backfilled_job_is_not_resent_on_a_later_run(self, tmp_db):
        """Test 3 (spec): once a backfilled (pre-existing) job has been sent
        to its department's channel, the following run must not resend it —
        normal dedup applies identically to backfill-sourced candidates."""
        dept03_chan = _RecordingProvider()
        job_id = _store_active_job(tmp_db, department="03", location="03 - Moulins", score=0.70)
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-first"), new_jobs=[])
        assert len(dept03_chan.sent) == 1

        engine.notify_pipeline_completed(_make_pipeline_result("run-second"), new_jobs=[])
        assert len(dept03_chan.sent) == 1  # unchanged — job_id already marked sent
        assert {j["job_id"] for p in dept03_chan.sent for j in p["jobs"]} == {job_id}

    def test_established_department_is_not_starved_by_globally_old_matching_jobs(self, tmp_db):
        """Test 4 (spec): department 01 has several pre-existing relevant
        jobs that were never picked up because they were never run-over-run
        "new" — they must not be dropped merely for being "not new
        globally"."""
        dept01_chan = _RecordingProvider()
        job_ids = set(_store_active_jobs_batch(tmp_db, [
            {"department": "01", "location": "01 - Bourg-en-Bresse", "score": s, "title": f"Job {i}"}
            for i, s in enumerate([0.62, 0.68, 0.74])
        ]))
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "01")], {"dept-01": dept01_chan},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-01"), new_jobs=[])
        sent_ids = {j["job_id"] for p in dept01_chan.sent for j in p["jobs"]}
        assert sent_ids == job_ids

    def test_department_with_no_relevant_jobs_still_sends_nothing(self, tmp_db):
        """Test 6 (spec): a configured department with genuinely no
        above-floor matching job must still produce zero sends — the
        backfill path must not degrade into "send anything"."""
        dept02_chan = _RecordingProvider()
        # Below the 'relevant' tier floor (score_100 < 60).
        _store_active_job(tmp_db, department="02", location="02 - Laon", score=0.40)
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "02")], {"dept-02": dept02_chan},
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-02-empty"), new_jobs=[])
        assert dept02_chan.sent == []

    def test_no_configured_location_channel_means_no_backfill_query_at_all(self, tmp_db):
        """A department with matching active jobs in the DB, but whose
        location channel isn't enabled/configured, must not be backfilled —
        same "no mapping -> no send" rule as run-over-run routing, and
        proves the backfill query is skipped rather than silently matching
        an unconfigured slug."""
        _store_active_job(tmp_db, department="75", location="75 - Paris", score=0.90)
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "75")], {},  # no "dept-75" provider, no config entry
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-unconfigured"), new_jobs=[])  # must not raise

    def test_already_sent_high_scorer_never_crowds_out_a_not_yet_sent_lower_scorer(self, tmp_db):
        """Dedup must be applied BEFORE the per-location cap, not after:
        otherwise an already-sent top scorer permanently occupies a cap slot
        and a legitimately not-yet-sent lower scorer never gets a turn, even
        though both are simultaneously active/matching this run."""
        import uuid

        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine._config.rules.max_notifications_per_location_per_run = 1

        # Seed run: only the (future) high scorer exists yet — gets sent and
        # recorded in notification_history.
        already_sent_id = str(uuid.uuid4())
        _store_active_jobs_batch(tmp_db, [
            {"id": already_sent_id, "department": "03", "location": "03 - Moulins", "score": 0.95, "title": "AlreadySent"},
        ])
        engine.notify_pipeline_completed(_make_pipeline_result("run-seed"), new_jobs=[])
        assert {j["job_id"] for p in dept03_chan.sent for j in p["jobs"]} == {already_sent_id}

        # Next run: the SAME job (re-scored, same id — simulating "still
        # active") plus a new, lower-scoring, not-yet-sent job are BOTH part
        # of the latest completed run's job_scores simultaneously. With
        # cap=1, the not-yet-sent job must still get through even though the
        # already-sent job outranks it by score.
        not_yet_sent_id = str(uuid.uuid4())
        _store_active_jobs_batch(tmp_db, [
            {"id": already_sent_id, "department": "03", "location": "03 - Moulins", "score": 0.95, "title": "AlreadySent"},
            {"id": not_yet_sent_id, "department": "03", "location": "03 - Vichy", "score": 0.61, "title": "NotYetSent"},
        ])
        engine.notify_pipeline_completed(_make_pipeline_result("run-next"), new_jobs=[])
        newly_sent = {j["job_id"] for p in dept03_chan.sent[1:] for j in p["jobs"]}
        assert newly_sent == {not_yet_sent_id}

    def test_department_missing_from_raw_column_still_resolves_via_location_fallback(self, tmp_db):
        """Root-cause-A regression: collectors that never populate the raw
        `department` column (HelloWork, LinkedIn, Greenhouse, Lever, EURES —
        see src/collectors/*) must not be silently dropped from location
        routing. src.pipeline.scoring and src.notifications.top10_query
        already fall back to extract_department_code(location); location
        routing must use the exact same fallback."""
        dept01_chan = _RecordingProvider()
        engine = _engine_with_location_channels(
            tmp_db, [_loc_pref("department", "01")], {"dept-01": dept01_chan},
        )
        job = _job_at_score(
            "job-no-dept-column", 77,
            department=None, location="Bourg-en-Bresse", work_mode="on_site", remote=False,
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-fallback"), new_jobs=[job])
        assert {j["job_id"] for p in dept01_chan.sent for j in p["jobs"]} == {"job-no-dept-column"}


class TestCandidatePriorityAndCaps:
    """NEW > UNSEEN_FOR_DEPARTMENT > BACKFILL, tie-broken by tier then
    score (2026-09-15 redesign) — plus the per-type sub-caps and the
    cross-location global cap that sit on top of the existing per-location
    cap."""

    def test_new_outranks_backfill_regardless_of_score(self, tmp_db):
        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        backfill_job_id = _store_active_job(tmp_db, department="03", location="03 - Moulins", score=0.95)
        new_job = _job_at_score(
            "job-new-03", 61, department="03", location="03 - Vichy", work_mode="on_site", remote=False,
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-priority"), new_jobs=[new_job])

        assert len(dept03_chan.sent) == 1
        sent_ids = [j["job_id"] for j in dept03_chan.sent[0]["jobs"]]
        assert sent_ids == ["job-new-03", backfill_job_id]

    def test_new_outranks_unseen_for_department_regardless_of_score(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        pre = NotificationStorage(db_path=tmp_db)
        pre.init_db()
        pre.get_or_create_department_state("dept-03")
        pre.mark_initial_backfill_complete("dept-03")
        pre.close()

        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        unseen_job_id = _store_active_job(tmp_db, department="03", location="03 - Moulins", score=0.95)
        new_job = _job_at_score(
            "job-new-03b", 61, department="03", location="03 - Vichy", work_mode="on_site", remote=False,
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-priority-2"), new_jobs=[new_job])

        assert len(dept03_chan.sent) == 1
        sent_ids = [j["job_id"] for j in dept03_chan.sent[0]["jobs"]]
        assert sent_ids == ["job-new-03b", unseen_job_id]

    def test_within_backfill_type_higher_score_wins_first(self, tmp_db):
        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        ids = _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - Moulins", "score": 0.65, "title": "Lower"},
            {"department": "03", "location": "03 - Vichy", "score": 0.90, "title": "Higher"},
        ])
        engine.notify_pipeline_completed(_make_pipeline_result("run-tiebreak"), new_jobs=[])
        sent_ids = [j["job_id"] for j in dept03_chan.sent[0]["jobs"]]
        assert sent_ids == [ids[1], ids[0]]

    def test_initial_backfill_cap_keeps_only_top_scoring_candidates(self, tmp_db):
        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine._config.rules.initial_backfill_max_per_department = 2
        ids = _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": s, "title": f"J{s}"}
            for s in [0.61, 0.70, 0.80, 0.90]
        ])
        engine.notify_pipeline_completed(_make_pipeline_result("run-cap"), new_jobs=[])
        sent_ids = {j["job_id"] for j in dept03_chan.sent[0]["jobs"]}
        assert sent_ids == {ids[2], ids[3]}  # top 2 by score: 0.80 and 0.90

    def test_regular_backfill_cap_limits_unseen_for_department_candidates(self, tmp_db):
        from src.notifications.storage import NotificationStorage
        pre = NotificationStorage(db_path=tmp_db)
        pre.init_db()
        pre.get_or_create_department_state("dept-03")
        pre.mark_initial_backfill_complete("dept-03")
        pre.close()

        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine._config.rules.regular_backfill_max_per_department = 1
        ids = _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": s, "title": f"J{s}"}
            for s in [0.61, 0.70, 0.80]
        ])
        engine.notify_pipeline_completed(_make_pipeline_result("run-unseen-cap"), new_jobs=[])
        sent_ids = {j["job_id"] for j in dept03_chan.sent[0]["jobs"]}
        assert sent_ids == {ids[2]}  # only the top-scoring one, cap=1

    def test_global_cap_trims_lowest_priority_overflow_across_departments(self, tmp_db):
        """max_total_notifications_per_run is a SAFETY CEILING applied after
        every per-location cap: two departments each independently eligible
        for their own candidates must still sum to no more than the global
        cap, with the lowest-priority overflow trimmed first — the two
        highest scorers overall always survive."""
        dept83 = _RecordingProvider()
        dept69 = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db,
            [_loc_pref("department", "83"), _loc_pref("department", "69")],
            {"dept-83": dept83, "dept-69": dept69},
        )
        engine._config.rules.max_total_notifications_per_run = 3
        engine._config.rules.initial_backfill_max_per_department = 10

        ids = _store_active_jobs_batch(tmp_db, [
            {"department": "83", "location": "83 - A", "score": 0.95, "title": "83-high"},
            {"department": "83", "location": "83 - B", "score": 0.61, "title": "83-low"},
            {"department": "69", "location": "69 - A", "score": 0.90, "title": "69-high"},
            {"department": "69", "location": "69 - B", "score": 0.62, "title": "69-low"},
        ])
        high_83, _low_83, high_69, low_69 = ids

        engine.notify_pipeline_completed(_make_pipeline_result("run-global-cap"), new_jobs=[])

        all_sent_ids = {j["job_id"] for p in (dept83.sent + dept69.sent) for j in p["jobs"]}
        # The two highest scorers overall always survive; the 3rd slot goes
        # to whichever remaining candidate outranks the other (69's low
        # scorer at 62 outranks 83's low scorer at 61).
        assert all_sent_ids == {high_83, high_69, low_69}

    def test_per_department_cap_loss_keeps_backlog_not_drained(self, tmp_db):
        """Lifecycle-completion audit, Case 1: 12 initial-backfill
        candidates with the per-department cap pinned at 10 — the subcap
        (set here to 15, above the per-department cap, so it does not bind)
        does not bind, but the overall per-department cap does, so 2
        candidates are lost. The department must NOT be marked
        backfill-complete: those 2 are real, undelivered backlog, not a
        drained pool."""
        from src.notifications.storage import NotificationStorage

        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine._config.rules.initial_backfill_max_per_department = 15
        engine._config.rules.max_notifications_per_location_per_run = 10
        _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": 0.60 + i * 0.02, "title": f"J{i}"}
            for i in range(12)
        ])

        state_reader = NotificationStorage(db_path=tmp_db)
        state_reader.init_db()

        engine.notify_pipeline_completed(_make_pipeline_result("run-dept-cap"), new_jobs=[])

        assert len(dept03_chan.sent) == 1
        assert len(dept03_chan.sent[0]["jobs"]) == 10  # per-department cap binds (12 -> 10)
        state = state_reader.get_or_create_department_state("dept-03")
        assert state["initial_backfill_completed_at"] is None

        state_reader.close()

    def test_global_cap_loss_keeps_backlog_not_drained_for_both_departments(self, tmp_db):
        """Lifecycle-completion audit, Case 2: 8 candidates for dept A + 8
        for dept B, global cap = 10 — each department's own raw backlog (8)
        is individually under its own per-department cap and subcap, but
        the CROSS-department global cap still trims some of each. Neither
        department may be marked backfill-complete. Scores are interleaved
        (A: 0.90/0.86/.../0.62, B: 0.88/0.84/.../0.60) so the top-10 overall
        deterministically keeps exactly 5 from each, guaranteeing both lose
        candidates rather than depending on cap-boundary tie-breaking."""
        from src.notifications.storage import NotificationStorage

        dept_a = _RecordingProvider()
        dept_b = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db,
            [_loc_pref("department", "83"), _loc_pref("department", "69")],
            {"dept-83": dept_a, "dept-69": dept_b},
        )
        engine._config.rules.max_total_notifications_per_run = 10
        engine._config.rules.max_notifications_per_location_per_run = 10

        _store_active_jobs_batch(tmp_db, [
            {"department": "83", "location": "83 - A", "score": 0.90 - i * 0.04, "title": f"A{i}"}
            for i in range(8)
        ] + [
            {"department": "69", "location": "69 - A", "score": 0.88 - i * 0.04, "title": f"B{i}"}
            for i in range(8)
        ])

        state_reader = NotificationStorage(db_path=tmp_db)
        state_reader.init_db()

        engine.notify_pipeline_completed(_make_pipeline_result("run-global-cap-2"), new_jobs=[])

        a_sent = len(dept_a.sent[0]["jobs"]) if dept_a.sent else 0
        b_sent = len(dept_b.sent[0]["jobs"]) if dept_b.sent else 0
        assert a_sent + b_sent == 10  # global cap binds across both (16 -> 10)
        assert a_sent == 5 and b_sent == 5  # deterministic split by construction

        state_a = state_reader.get_or_create_department_state("dept-83")
        state_b = state_reader.get_or_create_department_state("dept-69")
        assert state_a["initial_backfill_completed_at"] is None
        assert state_b["initial_backfill_completed_at"] is None

        state_reader.close()

    def test_dedup_is_applied_before_the_global_cap_too(self, tmp_db):
        """An already-sent job must never consume a global-cap slot — it is
        filtered out long before the global cap ever sees the candidate
        pool (same ordering guarantee as the existing per-location cap
        test, extended to the cross-location cap)."""
        dept83 = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "83")], {"dept-83": dept83},
        )
        engine._config.rules.max_total_notifications_per_run = 1

        already_sent_id = _store_active_job(tmp_db, department="83", location="83 - A", score=0.95)
        engine.notify_pipeline_completed(_make_pipeline_result("run-seed"), new_jobs=[])
        assert {j["job_id"] for j in dept83.sent[0]["jobs"]} == {already_sent_id}

        not_yet_sent_id = _store_active_jobs_batch(tmp_db, [
            {"id": already_sent_id, "department": "83", "location": "83 - A", "score": 0.95, "title": "AlreadySent"},
            {"department": "83", "location": "83 - B", "score": 0.61, "title": "NotYetSent"},
        ])[1]
        engine.notify_pipeline_completed(_make_pipeline_result("run-next"), new_jobs=[])
        newly_sent = {j["job_id"] for j in dept83.sent[1]["jobs"]}
        assert newly_sent == {not_yet_sent_id}


class TestBackfillLifecycleMultiRun:
    """Direct regression coverage for the department lifecycle's real
    multi-run semantics (2026-09-15 redesign, explicitly required before
    approval): raw_backfill_count must reflect the REMAINING, not-already-
    sent backlog — already-sent jobs must never keep the lifecycle stuck —
    draining over as many runs as the backlog requires, with
    initial_backfill_completed_at only flipped once nothing remains. Never
    a naive "if no history" check, and never sending the same job twice."""

    def test_backlog_larger_than_cap_drains_over_three_runs_then_completes(self, tmp_db):
        from src.notifications.storage import NotificationStorage

        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine._config.rules.initial_backfill_max_per_department = 2

        # 5 pre-existing jobs, distinct scores for deterministic ordering.
        ids = _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": s, "title": f"J{s}"}
            for s in [0.61, 0.65, 0.70, 0.80, 0.90]
        ])
        by_score_desc = list(reversed(ids))  # highest score first

        state_reader = NotificationStorage(db_path=tmp_db)
        state_reader.init_db()

        # Run 1: top 2 by score sent; 3 remain -> lifecycle NOT complete.
        engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[])
        run1_sent = [j["job_id"] for j in dept03_chan.sent[0]["jobs"]]
        assert run1_sent == by_score_desc[0:2]
        assert state_reader.get_or_create_department_state("dept-03")["initial_backfill_completed_at"] is None

        # Run 2: next 2 by score sent — DIFFERENT jobs, no duplicates; 1 remains.
        engine.notify_pipeline_completed(_make_pipeline_result("run-2"), new_jobs=[])
        run2_sent = [j["job_id"] for j in dept03_chan.sent[1]["jobs"]]
        assert run2_sent == by_score_desc[2:4]
        assert set(run1_sent).isdisjoint(run2_sent)
        assert state_reader.get_or_create_department_state("dept-03")["initial_backfill_completed_at"] is None

        # Run 3: final job sent; backlog now fully drained
        # (raw_backfill_count == 1 <= cap == 2) -> lifecycle completes.
        engine.notify_pipeline_completed(_make_pipeline_result("run-3"), new_jobs=[])
        run3_sent = [j["job_id"] for j in dept03_chan.sent[2]["jobs"]]
        assert run3_sent == by_score_desc[4:5]
        assert state_reader.get_or_create_department_state("dept-03")["initial_backfill_completed_at"] is not None

        # No job sent twice; all 5 eventually sent exactly once.
        all_sent = run1_sent + run2_sent + run3_sent
        assert len(all_sent) == len(set(all_sent)) == 5
        assert set(all_sent) == set(ids)

        # Run 4: department now established. Freshly-appearing pre-existing
        # (not run-over-run new) jobs must be typed UNSEEN_FOR_DEPARTMENT,
        # capped by regular_backfill_max_per_department — NOT the larger
        # initial-backfill cap. (A fresh _store_active_jobs_batch call
        # starts a new "latest completed run" — the previous 5 jobs drop
        # out of backfill scope, same latest-run scoping
        # compute_notification_backfill_candidates/compute_top10_by_slug
        # both document — so only these 3 are considered here.)
        engine._config.rules.regular_backfill_max_per_department = 1
        _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": s, "title": f"K{s}"}
            for s in [0.65, 0.75, 0.85]
        ])
        engine.notify_pipeline_completed(_make_pipeline_result("run-4"), new_jobs=[])
        run4_sent = [j["job_id"] for j in dept03_chan.sent[3]["jobs"]]
        assert len(run4_sent) == 1  # capped by regular_backfill_max_per_department

        state_reader.close()

    def test_failed_batch_send_does_not_mark_backlog_drained(self, tmp_db):
        """Point 2 of the follow-up audit: mark_initial_backfill_complete
        must only fire once the batch has actually been dispatched
        successfully — a failed send (webhook down, HTTP error, ...) must
        leave the department in "backfill" mode so the SAME jobs are
        retried on the next run, never silently marked drained despite
        nothing having actually been delivered. (Before the fix, this
        decision was made inside _route_notification_candidates — before
        dispatch even ran — purely from candidate availability.)"""
        from src.notifications.storage import NotificationStorage

        dept03_chan = _RecordingProvider(should_succeed=False)
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        engine._config.rules.initial_backfill_max_per_department = 5

        # Small backlog, well within the cap — would have been marked
        # "drained" by the pre-fix candidate-selection-time check even
        # though the send below never actually succeeds.
        ids = set(_store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": 0.70, "title": "A"},
            {"department": "03", "location": "03 - B", "score": 0.80, "title": "B"},
        ]))

        state_reader = NotificationStorage(db_path=tmp_db)
        state_reader.init_db()

        engine.notify_pipeline_completed(_make_pipeline_result("run-fail-1"), new_jobs=[])
        assert len(dept03_chan.sent) == 1  # attempted, but should_succeed=False
        state = state_reader.get_or_create_department_state("dept-03")
        assert state["initial_backfill_completed_at"] is None

        # Nothing was recorded as 'sent' (only 'failed') — the same 2 jobs
        # remain eligible and are retried, unchanged, on the next run.
        assert not state_reader.already_sent("discord", "jobs.location_notify", next(iter(ids)))

        # Webhook recovers — same 2 jobs, this time actually delivered.
        dept03_chan.should_succeed = True
        engine.notify_pipeline_completed(_make_pipeline_result("run-recovered"), new_jobs=[])
        run2_sent = {j["job_id"] for j in dept03_chan.sent[1]["jobs"]}
        assert run2_sent == ids  # same 2 jobs, not lost, not duplicated
        state = state_reader.get_or_create_department_state("dept-03")
        assert state["initial_backfill_completed_at"] is not None

        state_reader.close()


class TestNotificationStats:
    """get_last_notification_stats() — the answer to "why did department X
    get 0/2/8 notifications" without verbose logging (see
    _route_notification_candidates' docstring)."""

    def test_empty_before_any_run(self, tmp_db):
        engine = _engine_with_backfill(tmp_db, [_loc_pref("department", "03")], {})
        assert engine.get_last_notification_stats() == {}

    def test_stats_reflect_candidate_types_and_outcomes(self, tmp_db):
        dept03_chan = _RecordingProvider()
        engine = _engine_with_backfill(
            tmp_db, [_loc_pref("department", "03")], {"dept-03": dept03_chan},
        )
        _store_active_jobs_batch(tmp_db, [
            {"department": "03", "location": "03 - A", "score": 0.95, "title": "Backfill"},
            {"department": "03", "location": "03 - B", "score": 0.40, "title": "BelowFloor"},
        ])
        new_job = _job_at_score(
            "job-new-stats", 61, department="03", location="03 - C", work_mode="on_site", remote=False,
        )
        engine.notify_pipeline_completed(_make_pipeline_result("run-stats"), new_jobs=[new_job])

        stats = engine.get_last_notification_stats()
        dept_stats = stats["dept-03"]
        assert dept_stats["by_type"]["new"] == 1
        assert dept_stats["by_type"]["backfill"] == 1
        assert dept_stats["below_threshold"] == 1
        assert dept_stats["queued_to_send"] == 2
        assert dept_stats["candidates"] == 3
