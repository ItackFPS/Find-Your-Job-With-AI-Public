"""Issue #66: a job is recorded as sent only if its complete block was in a
Discord message that was actually delivered.

Every test drives the real NotificationEngine lifecycle with a real
DiscordProvider and a real NotificationStorage (tmp DB). Only the HTTP layer
is faked, by an endpoint that rejects (HTTP 400) any message breaking
Discord's documented embed limits, as the real API does:

  title 256 · description 4096 · 25 fields · field name 256 ·
  field value 1024 · footer 2048 · all embed text combined 6000.

The fake counts code points: the real history shows Discord accepting
1024-code-point fields that contain emoji, i.e. more than 1024 UTF-16 units.
The implementation counts UTF-16 units, which is never less, so it keeps a
small safety margin; the tests assert that stricter bound.

Behaviour under test (Option B, approved on #66): one message per location
per run, with the job blocks spread over as many fields as needed. A job
block is never cut. A job that does not fit in the message is left out of
it, is not recorded as sent, and stays eligible for a later run.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import httpx
import pytest

from src.notifications.discord import DiscordProvider
from src.notifications.storage import NotificationStorage
from tests.test_notification_engine import (  # noqa: F401 - autouse fixtures
    _clear_webhook_env,
    _discord_config,
    _engine_with_backfill,
    _engine_with_location_channels,
    _job_at_score,
    _loc_pref,
    _make_pipeline_result,
    _no_real_retry_sleep,
    _store_active_jobs_batch,
)

_DEPT_URL = "https://discord.test/api/webhooks/dept-83"
_REMOTE_URL = "https://discord.test/api/webhooks/remote"


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _limit_violations(body: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    total = 0
    for embed in body.get("embeds", []):
        for key, limit in (("title", 256), ("description", 4096)):
            size = len(embed.get(key, ""))
            total += size
            if size > limit:
                errors.append(f"{key} {size} > {limit}")
        fields = embed.get("fields", [])
        if len(fields) > 25:
            errors.append(f"{len(fields)} fields > 25")
        for field in fields:
            name, value = len(field["name"]), len(field["value"])
            total += name + value
            if not field["name"] or not field["value"]:
                errors.append("empty field name/value")
            if name > 256:
                errors.append(f"field name {name} > 256")
            if value > 1024:
                errors.append(f"field value {value} > 1024")
        footer = len(embed.get("footer", {}).get("text", ""))
        total += footer
        if footer > 2048:
            errors.append(f"footer {footer} > 2048")
    if total > 6000:
        errors.append(f"embed total {total} > 6000")
    return errors


class _FakeDiscord:
    """Stands in for Discord's webhook endpoint (httpx.post)."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, dict[str, Any]]] = []
        self.rejected: list[tuple[str, list[str]]] = []
        self.down: set[str] = set()  # webhook URLs answering HTTP 500

    def post(self, url: str, json: dict[str, Any], timeout: float) -> httpx.Response:  # noqa: A002
        request = httpx.Request("POST", url)
        if url in self.down:
            return httpx.Response(500, request=request)
        errors = _limit_violations(json)
        if errors:
            self.rejected.append((url, errors))
            return httpx.Response(400, json={"embeds": errors}, request=request)
        self.delivered.append((url, json))
        return httpx.Response(204, request=request)

    def messages(self, url: str) -> list[dict[str, Any]]:
        return [body for u, body in self.delivered if u == url]

    def visible_text(self, url: str) -> str:
        return "\n\n".join(
            field["value"]
            for body in self.messages(url)
            for embed in body["embeds"]
            for field in embed.get("fields", [])
        )


@pytest.fixture
def tmp_db(tmp_path) -> str:
    return str(tmp_path / "test_notif.db")


@pytest.fixture
def discord(monkeypatch) -> _FakeDiscord:
    fake = _FakeDiscord()
    monkeypatch.setattr(httpx, "post", fake.post)
    return fake


def _long_url(job_id: str, length: int) -> str:
    base = f"https://jobs.example.com/offer/{job_id}?ref="
    return base + "x" * (length - len(base))


def _dept_job(job_id: str, score_100: int = 77, url_len: int = 200, **overrides: Any) -> dict:
    job = _job_at_score(
        job_id, score_100, department="83", location="Toulon",
        work_mode="on_site", remote=False, **overrides,
    )
    job["canonical_url"] = job["apply_url"] = _long_url(job_id, url_len)
    return job


def _apply_link(job: dict) -> str:
    return f"[Apply]({job['canonical_url']})"


def _sent_ids(tmp_db: str) -> set[str]:
    conn = sqlite3.connect(tmp_db)
    try:
        rows = conn.execute(
            "SELECT entity_id FROM notification_history "
            "WHERE provider='discord' AND event_type='jobs.location_notify' AND status='sent'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _visible_ids(discord: _FakeDiscord, url: str, jobs: list[dict]) -> set[str]:
    text = discord.visible_text(url)
    return {j["job_id"] for j in jobs if _apply_link(j) in text}


def _dept_engine(tmp_db: str, config=None, extra: dict[str, str] | None = None):
    urls = {"dept-83": _DEPT_URL, **(extra or {})}
    prefs = [_loc_pref("department", "83")]
    if extra:
        prefs.append(_loc_pref("remote", "remote_france"))
    providers = {slug: DiscordProvider(webhook_url=url) for slug, url in urls.items()}
    return _engine_with_location_channels(tmp_db, prefs, providers, config=config)


def _config(**rules: Any):
    config = _discord_config()
    for key, value in rules.items():
        setattr(config.rules, key, value)
    return config


# ---------------------------------------------------------------------------
# One message, whole blocks, sent state == what was visible
# ---------------------------------------------------------------------------

def test_small_batch_fits_in_one_field(tmp_db, discord):
    jobs = [_dept_job(f"job-{i}", url_len=60) for i in range(3)]
    _dept_engine(tmp_db).notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=jobs)

    [message] = discord.messages(_DEPT_URL)
    fields = message["embeds"][0]["fields"]
    assert [f["name"] for f in fields] == ["Jobs"]
    assert _visible_ids(discord, _DEPT_URL, jobs) == {j["job_id"] for j in jobs}
    assert _sent_ids(tmp_db) == {j["job_id"] for j in jobs}
    assert discord.rejected == []


def test_batch_previously_truncated_is_now_split_over_several_fields(tmp_db, discord):
    """A cap of 10 jobs with ~200-char URLs: the old single field
    was cut at 1024 characters and the last jobs lost their apply link while
    still being recorded as sent."""
    jobs = [_dept_job(f"job-{i:02d}", score_100=90 - i) for i in range(10)]
    _dept_engine(tmp_db, config=_config(max_notifications_per_location_per_run=10)).notify_pipeline_completed(
        _make_pipeline_result("run-1"), new_jobs=jobs
    )

    [message] = discord.messages(_DEPT_URL)
    fields = message["embeds"][0]["fields"]
    assert len(fields) >= 2
    assert all(_utf16_len(f["value"]) <= 1024 for f in fields)
    # Every job is delivered whole, including the tail the old field lost.
    assert _visible_ids(discord, _DEPT_URL, jobs) == {j["job_id"] for j in jobs}
    assert _apply_link(jobs[-1]) in discord.visible_text(_DEPT_URL)
    assert _sent_ids(tmp_db) == {j["job_id"] for j in jobs}
    # No block is split across two fields.
    for job in jobs:
        assert sum(_apply_link(job) in f["value"] for f in fields) == 1
        field = next(f for f in fields if _apply_link(job) in f["value"])
        assert job["title"] in field["value"]


def test_jobs_beyond_one_message_are_deferred_then_delivered_once(tmp_db, discord):
    """More jobs than one message can hold (caps raised): the jobs that do
    not fit are neither shown nor recorded as sent, the message says so, and
    later runs deliver them — each job exactly once."""
    config = _config(max_notifications_per_location_per_run=40, max_total_notifications_per_run=40)
    jobs = [_dept_job(f"job-{i:02d}", score_100=90 - i // 4, url_len=250) for i in range(40)]
    engine = _dept_engine(tmp_db, config=config)

    engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=jobs)
    [first] = discord.messages(_DEPT_URL)
    shown_first = _visible_ids(discord, _DEPT_URL, jobs)
    assert 0 < len(shown_first) < len(jobs)
    assert _sent_ids(tmp_db) == shown_first  # nothing hidden is recorded as sent
    description = first["embeds"][0]["description"]
    assert f"Showing {len(shown_first)} of 40" in description
    assert f"{40 - len(shown_first)} more" in description

    for run in range(2, 6):
        engine.notify_pipeline_completed(_make_pipeline_result(f"run-{run}"), new_jobs=jobs)
        assert _sent_ids(tmp_db) == _visible_ids(discord, _DEPT_URL, jobs)

    assert _sent_ids(tmp_db) == {j["job_id"] for j in jobs}
    text = discord.visible_text(_DEPT_URL)
    assert all(text.count(_apply_link(j)) == 1 for j in jobs)
    assert discord.rejected == []


def test_deferred_jobs_keep_priority_order(tmp_db, discord):
    config = _config(max_notifications_per_location_per_run=40, max_total_notifications_per_run=40)
    jobs = [_dept_job(f"job-{i:02d}", score_100=95 - i, url_len=250) for i in range(30)]
    _dept_engine(tmp_db, config=config).notify_pipeline_completed(
        _make_pipeline_result("run-1"), new_jobs=jobs,
    )
    shown = _visible_ids(discord, _DEPT_URL, jobs)
    assert shown == {j["job_id"] for j in jobs[: len(shown)]}  # the best ones first


# ---------------------------------------------------------------------------
# Size edge cases
# ---------------------------------------------------------------------------

def _job_with_block_len(job_id: str, target: int) -> dict:
    from src.notifications.discord import _location_batch_item

    job = _dept_job(job_id, url_len=100)
    base = _utf16_len(_location_batch_item(_payload_job(job), is_remote_channel=False))
    job["canonical_url"] = job["apply_url"] = _long_url(job_id, 100 + target - base)
    assert _utf16_len(_location_batch_item(_payload_job(job), is_remote_channel=False)) == target
    return job


def _payload_job(job: dict) -> dict:
    from src.pipeline.scoring import score_to_tier

    score_100 = round(job["final_score"] * 100)
    return {**job, "score_100": score_100, "tier": score_to_tier(score_100)}


def test_job_block_exactly_at_the_field_limit_is_delivered(tmp_db, discord):
    job = _job_with_block_len("job-max", 1024)
    _dept_engine(tmp_db).notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[job])

    [message] = discord.messages(_DEPT_URL)
    assert _utf16_len(message["embeds"][0]["fields"][0]["value"]) == 1024
    assert _sent_ids(tmp_db) == {"job-max"}


def test_job_too_large_for_any_field_is_never_cut_or_marked_sent(tmp_db, discord, caplog):
    big = _job_with_block_len("job-big", 1025)
    normal = _dept_job("job-ok", score_100=70, url_len=60)
    engine = _dept_engine(tmp_db)

    with caplog.at_level(logging.WARNING, logger="src.notifications.engine"):
        engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[big, normal])

    assert "job-big" in caplog.text  # reported, never silent
    assert _visible_ids(discord, _DEPT_URL, [big, normal]) == {"job-ok"}
    assert big["job_id"] not in discord.visible_text(_DEPT_URL)
    assert _sent_ids(tmp_db) == {"job-ok"}
    assert "1 more" in discord.messages(_DEPT_URL)[0]["embeds"][0]["description"]

    # Still eligible on the next run; never recorded as sent.
    engine.notify_pipeline_completed(_make_pipeline_result("run-2"), new_jobs=[big, normal])
    assert _sent_ids(tmp_db) == {"job-ok"}


def test_batch_of_only_oversized_jobs_sends_nothing(tmp_db, discord):
    big = _job_with_block_len("job-big", 1100)
    _dept_engine(tmp_db).notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[big])
    assert discord.delivered == []
    assert discord.rejected == []
    assert _sent_ids(tmp_db) == set()


def test_direct_send_of_a_batch_that_cannot_fit_fails_instead_of_truncating(discord):
    """The embed builder never drops content: a batch that was not fitted
    first is refused (send() returns False) and nothing is posted."""
    jobs = [
        {**_payload_job(_dept_job(f"job-{i}", url_len=250)), "countries": []}
        for i in range(40)
    ]
    provider = DiscordProvider(webhook_url=_DEPT_URL)
    ok = provider.send({
        "event": "jobs.location_notify", "run_id": "run-x",
        "location_type": "department", "location_label": "Var (83)",
        "total_count": 40, "shown_count": 40, "overflow_count": 0, "jobs": jobs,
    })
    assert ok is False
    assert discord.delivered == []


# ---------------------------------------------------------------------------
# Failures and retries
# ---------------------------------------------------------------------------

def test_discord_failure_records_nothing_and_the_next_run_delivers(tmp_db, discord):
    jobs = [_dept_job(f"job-{i}") for i in range(10)]
    engine = _dept_engine(tmp_db, config=_config(max_notifications_per_location_per_run=10))

    discord.down.add(_DEPT_URL)
    engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=jobs)
    assert _sent_ids(tmp_db) == set()

    discord.down.clear()
    engine.notify_pipeline_completed(_make_pipeline_result("run-2"), new_jobs=jobs)
    assert _sent_ids(tmp_db) == {j["job_id"] for j in jobs}
    assert len(discord.messages(_DEPT_URL)) == 1


def test_one_location_failing_does_not_affect_another_and_retry_does_not_duplicate(
    tmp_db, discord,
):
    """Two locations in one run, one webhook down. Only the delivered
    location's jobs are recorded; retrying the same run re-sends only the
    failed location, never the delivered one."""
    dept_jobs = [_dept_job(f"dept-{i}") for i in range(10)]
    remote_jobs = [
        {**_job_at_score(f"remote-{i}", 77), "canonical_url": _long_url(f"remote-{i}", 200)}
        for i in range(10)
    ]
    engine = _dept_engine(
        tmp_db,
        config=_config(max_notifications_per_location_per_run=10),
        extra={"remote-remote_france": _REMOTE_URL},
    )
    result = _make_pipeline_result("run-1")

    discord.down.add(_REMOTE_URL)
    engine.notify_pipeline_completed(result, new_jobs=dept_jobs + remote_jobs)
    assert _sent_ids(tmp_db) == {j["job_id"] for j in dept_jobs}
    assert _visible_ids(discord, _DEPT_URL, dept_jobs) == {j["job_id"] for j in dept_jobs}

    discord.down.clear()
    engine.notify_pipeline_completed(result, new_jobs=dept_jobs + remote_jobs)  # same run retried
    assert len(discord.messages(_DEPT_URL)) == 1
    assert len(discord.messages(_REMOTE_URL)) == 1
    assert _visible_ids(discord, _REMOTE_URL, remote_jobs) == {j["job_id"] for j in remote_jobs}
    assert _sent_ids(tmp_db) == {j["job_id"] for j in dept_jobs + remote_jobs}

    engine.notify_pipeline_completed(result, new_jobs=dept_jobs + remote_jobs)  # and again
    assert len(discord.delivered) == 2


def test_each_location_gets_only_its_own_jobs(tmp_db, discord):
    dept_jobs = [_dept_job(f"dept-{i}") for i in range(4)]
    remote_jobs = [_job_at_score(f"remote-{i}", 77) for i in range(4)]
    engine = _dept_engine(
        tmp_db,
        config=_config(max_notifications_per_location_per_run=10),
        extra={"remote-remote_france": _REMOTE_URL},
    )
    engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=dept_jobs + remote_jobs)

    assert _visible_ids(discord, _DEPT_URL, dept_jobs + remote_jobs) == {j["job_id"] for j in dept_jobs}
    assert _visible_ids(discord, _REMOTE_URL, dept_jobs + remote_jobs) == {
        j["job_id"] for j in remote_jobs
    }


# ---------------------------------------------------------------------------
# NEW / UNSEEN / BACKFILL lifecycle
# ---------------------------------------------------------------------------

_LONG_TITLE = "Ingénieur DevOps / SRE Kubernetes Terraform AWS GCP Azure " * 3


def _dept03_engine(tmp_db, **rules: Any):
    config = _config(**rules)
    provider = DiscordProvider(webhook_url=_DEPT_URL)
    return _engine_with_backfill(
        tmp_db, [_loc_pref("department", "03")], {"dept-03": provider}, config=config,
    )


def _dept03_state(tmp_db) -> dict[str, Any]:
    storage = NotificationStorage(db_path=tmp_db)
    storage.init_db()
    try:
        return storage.get_or_create_department_state("dept-03")
    finally:
        storage.close()


def test_backfill_is_not_complete_while_jobs_were_left_out_of_the_message(tmp_db, discord):
    engine = _dept03_engine(
        tmp_db,
        initial_backfill_max_per_department=40,
        max_notifications_per_location_per_run=40,
        max_total_notifications_per_run=40,
    )
    ids = _store_active_jobs_batch(tmp_db, [
        {"department": "03", "location": "03 - Moulins", "score": 0.70 + i / 200,
         "title": f"{_LONG_TITLE}#{i}"}
        for i in range(30)
    ])

    engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[])
    first = _sent_ids(tmp_db)
    assert 0 < len(first) < 30
    assert _dept03_state(tmp_db)["initial_backfill_completed_at"] is None

    for run in range(2, 6):
        engine.notify_pipeline_completed(_make_pipeline_result(f"run-{run}"), new_jobs=[])
        if _sent_ids(tmp_db) == set(ids):
            break
    assert _sent_ids(tmp_db) == set(ids)
    assert _dept03_state(tmp_db)["initial_backfill_completed_at"] is not None

    text = discord.visible_text(_DEPT_URL)
    for i in range(30):  # every job shown exactly once, whole
        assert text.count(f"{_LONG_TITLE}#{i}\n") == 1


def test_new_job_takes_priority_over_deferred_backfill(tmp_db, discord):
    engine = _dept03_engine(
        tmp_db,
        initial_backfill_max_per_department=40,
        max_notifications_per_location_per_run=40,
        max_total_notifications_per_run=40,
    )
    _store_active_jobs_batch(tmp_db, [
        {"department": "03", "location": "03 - Moulins", "score": 0.90, "title": f"{_LONG_TITLE}#{i}"}
        for i in range(30)
    ])
    new_job = _job_at_score(
        "job-new", 61, department="03", location="03 - Vichy", work_mode="on_site", remote=False,
    )
    engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[new_job])
    assert "job-new" in _sent_ids(tmp_db)
    assert _apply_link(new_job) in discord.visible_text(_DEPT_URL)


def test_unseen_jobs_left_out_are_offered_again(tmp_db, discord):
    pre = NotificationStorage(db_path=tmp_db)
    pre.init_db()
    pre.get_or_create_department_state("dept-03")
    pre.mark_initial_backfill_complete("dept-03")
    pre.close()

    engine = _dept03_engine(
        tmp_db,
        regular_backfill_max_per_department=40,
        max_notifications_per_location_per_run=40,
        max_total_notifications_per_run=40,
    )
    ids = _store_active_jobs_batch(tmp_db, [
        {"department": "03", "location": "03 - Moulins", "score": 0.80, "title": f"{_LONG_TITLE}#{i}"}
        for i in range(30)
    ])
    engine.notify_pipeline_completed(_make_pipeline_result("run-1"), new_jobs=[])
    assert 0 < len(_sent_ids(tmp_db)) < 30
    for run in range(2, 6):
        engine.notify_pipeline_completed(_make_pipeline_result(f"run-{run}"), new_jobs=[])
    assert _sent_ids(tmp_db) == set(ids)
