"""F6.4: a 429 pauses a background scan, it resumes after a cooldown, with no burst after.

Fake scrapers and HTTP answers only; temporary databases; no real wait (the scanners'
waits are recorded instead of slept).
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.scan_control import (
    STATE_RATE_LIMITED,
    AdaptivePacer,
    cooling_until,
    interrupted_checkpoint_id,
    rate_limit_cooldown,
    scan_state,
)

# -- cooldown and pacer -----------------------------------------------------------------------


def test_cooldown_doubles_from_one_hour_up_to_a_day():
    assert [rate_limit_cooldown(n) for n in (1, 2, 3, 5, 6, 9)] == [
        timedelta(hours=h) for h in (1, 2, 4, 16, 24, 24)]


def test_pacer_resumes_slowly_and_settles_back_to_base():
    pacer = AdaptivePacer(10.0, resumed=True, jitter=0.0)
    delays = []
    for _ in range(25):
        delays.append(pacer.delay())
        pacer.on_success()
    assert delays[0] == 20.0 and delays == sorted(delays, reverse=True)
    assert delays[-1] == pytest.approx(10.0, abs=0.2)
    pacer.penalize()
    assert pacer.delay() == pytest.approx(20.0, abs=0.4)
    for _ in range(10):
        pacer.penalize()
    assert pacer.delay() == 80.0                                     # capped at 8 x base


def test_pacer_jitter_never_goes_below_the_delay():
    pacer = AdaptivePacer(10.0, rng=random.Random(1))
    assert all(10.0 <= pacer.delay() <= 11.0 for _ in range(50))


def test_rate_limited_checkpoint_waits_out_its_cooldown():
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    cp = {"scan_id": "s", "updated_at": "t", "state": STATE_RATE_LIMITED,
          "resume_after": (now + timedelta(hours=1)).isoformat()}
    assert cooling_until(cp, now) is not None and interrupted_checkpoint_id(cp, now) is None
    later = now + timedelta(hours=1, seconds=1)
    assert cooling_until(cp, later) is None and interrupted_checkpoint_id(cp, later) == "s|t"
    assert scan_state(False, False, cp) == STATE_RATE_LIMITED


# -- LinkedIn ---------------------------------------------------------------------------------


def _row(job_id: str) -> MagicMock:
    row = MagicMock()
    row.get = lambda k, default=None: {
        "id": job_id, "job_url": f"https://www.linkedin.com/jobs/view/{job_id}", "title": "Dev",
        "company": "Acme", "location": "Paris, France", "description": "Great role", "is_remote": False,
        "job_type": "fulltime", "date_posted": None, "currency": "EUR", "site": "linkedin",
    }.get(k, default)
    return row


def _df(rows: list[MagicMock]) -> MagicMock:
    df = MagicMock()
    df.empty = not rows
    df.__len__ = lambda self: len(rows)
    df.iterrows = lambda: enumerate(rows)
    return df


@pytest.fixture
def li_staging(tmp_path: Path):
    from src.linkedin.staging import LinkedInStagingStorage

    store = LinkedInStagingStorage(db_path=str(tmp_path / "jobs.db"))
    store.init_db()
    yield store
    store.close()


def _li_scanner(staging, tmp_path: Path, waits: list[float], monkeypatch):
    from src.linkedin import scanner as mod
    from src.linkedin.scanner import LinkedInConfig, LinkedInQueryConfig, LinkedInScanner

    monkeypatch.setattr(mod, "wait_for_stop", lambda stop, seconds, **kw: waits.append(seconds) or False)
    monkeypatch.setattr(random.Random, "uniform", lambda self, a, b: 0.0)   # no jitter: exact delays
    cfg = LinkedInConfig(enabled=True, rate_limit_seconds=15.0, max_pages_per_query=3, fetch_description=False,
                         queries=[LinkedInQueryConfig(search_term="python", results_wanted=2),
                                  LinkedInQueryConfig(search_term="data", results_wanted=2)])
    return LinkedInScanner(config=cfg, staging=staging, config_dir=str(tmp_path))


class _LinkedIn:
    """Fake jobspy: a page of 2 rows per call, a 429 when the call matches ``limited``."""

    def __init__(self, limited: set[tuple[str, int]]) -> None:
        self.limited = limited
        self.calls: list[tuple[str, int]] = []

    def __call__(self, **kw: Any):
        page = kw["offset"] // kw["results_wanted"]
        self.calls.append((kw["search_term"], page))
        if (kw["search_term"], page) in self.limited:
            logging.getLogger("JobSpy:LinkedIn").error("LinkedIn response status code 429")
            return _df([])
        if page >= 2:
            return _df([])
        return _df([_row(f"{kw['search_term']}{page}{i}{uuid.uuid4().hex[:6]}") for i in range(2)])


def _run(scanner, fake) -> str | None:
    jobspy = MagicMock()
    jobspy.scrape_jobs = fake
    with patch.dict("sys.modules", {"jobspy": jobspy}):
        return scanner.run_scan()


def test_linkedin_429_stops_the_scan_then_resumes_slowly_after_the_cooldown(li_staging, tmp_path, monkeypatch):
    waits: list[float] = []
    scanner = _li_scanner(li_staging, tmp_path, waits, monkeypatch)
    fake = _LinkedIn(limited={("python", 1)})

    scan_id = _run(scanner, fake)

    # stopped on the first 429: the second query was never sent
    assert fake.calls == [("python", 0), ("python", 1)]
    assert scanner.last_scan_rate_limited() and not scanner.last_scan_completed()
    cp = li_staging.get_scan_checkpoint()
    assert (cp["state"], cp["current_page"], cp["rate_limit_strikes"]) == ("rate_limited", 1, "1")
    resume_after = datetime.fromisoformat(cp["resume_after"])
    assert timedelta(minutes=59) < resume_after - datetime.now(UTC) <= timedelta(hours=1)
    assert scanner.rate_limited_until() is not None and scanner.interrupted_checkpoint_id() is None
    run = li_staging.get_scan_run(scan_id)
    assert run["status"] == "interrupted" and "HTTP 429" in run["error"]

    # cooldown passes: the scheduler may resume it, from the saved page, slowly
    li_staging._get_conn().execute("UPDATE linkedin_scan_checkpoint SET resume_after = ?",
                                   ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    li_staging._get_conn().commit()
    assert scanner.rate_limited_until() is None and scanner.interrupted_checkpoint_id() is not None
    waits.clear()
    fake.limited = set()
    assert _run(scanner, fake) == scan_id                            # the same logical scan
    assert fake.calls[2] == ("python", 1)                            # resumed at the saved page
    assert scanner.last_scan_completed() and li_staging.get_scan_checkpoint() is None
    # no burst: every wait after the resume is at least the base delay, the first ones twice it
    assert waits[0] == 30.0 and all(w >= 15.0 for w in waits) and waits == sorted(waits, reverse=True)


def test_consecutive_rate_limits_double_the_cooldown(li_staging, tmp_path, monkeypatch):
    waits: list[float] = []
    scanner = _li_scanner(li_staging, tmp_path, waits, monkeypatch)
    fake = _LinkedIn(limited={("python", 0)})
    _run(scanner, fake)
    li_staging._get_conn().execute("UPDATE linkedin_scan_checkpoint SET resume_after = ?",
                                   ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    li_staging._get_conn().commit()
    _run(scanner, fake)                                              # limited again on resume
    cp = li_staging.get_scan_checkpoint()
    assert cp["rate_limit_strikes"] == "2"
    assert timedelta(hours=1, minutes=59) < datetime.fromisoformat(cp["resume_after"]) - datetime.now(UTC)


def test_description_fetch_429_stops_without_checkpointing_the_page(li_staging, tmp_path, monkeypatch):
    from src.linkedin import scanner as mod

    waits: list[float] = []
    scanner = _li_scanner(li_staging, tmp_path, waits, monkeypatch)
    scanner.config.enrich_descriptions = True
    monkeypatch.setattr(mod, "fetch_linkedin_description_classified", lambda jid: (None, "rate_limited"))
    no_desc = _LinkedIn(limited=set())

    def scrape(**kw):
        df = no_desc(**kw)
        for _i, row in df.iterrows():
            base = row.get
            row.get = lambda k, d=None, _b=base: None if k == "description" else _b(k, d)
        return df

    _run(scanner, scrape)
    cp = li_staging.get_scan_checkpoint()
    assert scanner.last_scan_rate_limited()
    assert cp["state"] == "rate_limited" and not cp["current_page"]   # page 0 re-fetched on resume


# -- HelloWork --------------------------------------------------------------------------------


class _Resp:
    def __init__(self, status: int, text: str = "") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers: dict[str, str] = {}
        self.url = "https://www.hellowork.com/x"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_hellowork_collector_stops_on_the_first_429_without_retrying(monkeypatch):
    from src.collectors import hellowork as hw
    from src.collectors.base import BaseCollector

    calls: list[str] = []
    monkeypatch.setattr(hw.httpx, "get", lambda url, **kw: calls.append(url) or _Resp(429))
    monkeypatch.setattr(BaseCollector, "robots_check", lambda self, url: True)
    monkeypatch.setattr(BaseCollector, "_sleep", staticmethod(lambda s: None))
    monkeypatch.setattr(hw.HelloWorkCollector, "honours_robots", property(lambda self: False))
    collector = hw.HelloWorkCollector({"metier_slugs": ["developpeur", "data-analyst"]})
    collector.stop_on_rate_limit = True
    done: list[str] = []
    result = collector.collect(on_slug_done=lambda slug, jobs: done.append(slug))
    assert len(calls) == 1 and done == [] and result.jobs == []
    assert collector.rate_limited and "429" in collector.rate_limited


def test_hellowork_scanner_records_the_cooldown(tmp_path, monkeypatch):
    from src.collectors.base import CollectorResult
    from src.collectors.hellowork import HelloWorkCollector
    from src.hellowork.scanner import HelloWorkScanner
    from src.hellowork.staging import HelloWorkStagingStorage

    staging = HelloWorkStagingStorage(db_path=str(tmp_path / "jobs.db"))
    staging.init_db()
    seen: dict[str, Any] = {}

    def collect(self, on_slug_done=None, skip_slugs=None, shutdown_event=None):
        seen.update(stop=self.stop_on_rate_limit, pacer=self.pacer)
        on_slug_done("developpeur", [])
        self.rate_limited = "HTTP 429 from https://www.hellowork.com"
        return CollectorResult(source_name="hellowork", jobs=[], errors=[])

    monkeypatch.setattr(HelloWorkCollector, "collect", collect)
    scanner = HelloWorkScanner(staging=staging, config={"metier_slugs": ["developpeur", "data-analyst"]})
    scan_id = scanner.run_scan()
    assert seen["stop"] is True and seen["pacer"] is not None and seen["pacer"].current == seen["pacer"].base
    cp = staging.get_scan_checkpoint()
    assert scanner.last_scan_rate_limited() and cp["state"] == "rate_limited"
    assert cp["completed_slugs_json"] == '["developpeur"]' and scanner.rate_limited_until() is not None
    assert staging.get_scan_run(scan_id)["status"] == "interrupted"
    # after the cooldown the resumed collector starts slower
    staging._get_conn().execute("UPDATE hellowork_scan_checkpoint SET resume_after = ?",
                                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),))
    staging._get_conn().commit()
    scanner.run_scan()
    assert seen["pacer"].current == 2 * seen["pacer"].base
    staging.close()


# -- scheduler --------------------------------------------------------------------------------


class _Handler:
    def __init__(self) -> None:
        self.until: str | None = None
        self.calls = 0

    def is_running(self) -> bool:
        return False

    def rate_limited_until(self) -> str | None:
        return self.until

    def dispatch(self):
        from src.scheduler.service import DispatchResult

        self.calls += 1
        return DispatchResult(run_ref=str(uuid.uuid4()), completed=False, rate_limited=True)


def test_scheduler_waits_out_the_cooldown_and_counts_no_failure(tmp_path):
    from src.scheduler.service import SchedulerService
    from src.scheduler.storage import SchedulerStorage

    db = str(tmp_path / "sched.db")
    storage = SchedulerStorage(db)
    storage.init_db()
    storage.upsert_definition({"id": "li_daily", "source_id": "linkedin", "label": "LinkedIn", "enabled": True,
                               "startup_policy": "when_opted_in", "retry_max": 0, "retry_delay_seconds": 300})
    storage.close()
    handler = _Handler()
    svc = SchedulerService(db_path=db, config_dir=str(tmp_path), handlers={"linkedin": handler},
                           opted_in=lambda s: True)

    svc._tick()
    svc.join_dispatches(timeout=5)
    assert handler.calls == 1
    storage = SchedulerStorage(db)
    assert [e["status"] for e in storage.list_executions("li_daily")] == ["interrupted"]
    storage.close()

    handler.until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    for _ in range(3):
        svc._tick()
    svc.join_dispatches(timeout=5)
    assert handler.calls == 1                                         # cooling down: nothing sent

    handler.until = None                                               # cooldown over
    svc._tick()
    svc.join_dispatches(timeout=5)
    assert handler.calls == 2


def test_not_chosen_source_is_never_dispatched(tmp_path):
    from src.scheduler.service import SchedulerService
    from src.scheduler.storage import SchedulerStorage

    db = str(tmp_path / "sched.db")
    storage = SchedulerStorage(db)
    storage.init_db()
    storage.upsert_definition({"id": "li_daily", "source_id": "linkedin", "label": "LinkedIn", "enabled": True,
                               "startup_policy": "when_opted_in", "retry_max": 0, "retry_delay_seconds": 300})
    storage.close()
    handler = _Handler()
    svc = SchedulerService(db_path=db, config_dir=str(tmp_path), handlers={"linkedin": handler})
    svc._tick(is_startup=True)
    svc.join_dispatches(timeout=5)
    assert handler.calls == 0


def test_opt_in_reads_the_stored_choice_then_the_scan_history(tmp_path, monkeypatch):
    from src.app_settings import unofficial_source_enabled

    settings = tmp_path / "settings.json"
    assert unofficial_source_enabled(settings, "linkedin") is False
    assert unofficial_source_enabled(settings, "linkedin", used_before=True) is True     # observed choice
    settings.write_text('{"unofficial_sources": {"linkedin": false}}', encoding="utf-8")
    assert unofficial_source_enabled(settings, "linkedin", used_before=True) is False    # stored choice wins


def test_a_1x_scan_history_counts_as_chosen_once_then_never_again(tmp_path):
    """Plan rule I1: an install that scanned LinkedIn before 2.0 keeps its daily scans (the
    observed choice is stored, with its date). After that, a scan started by hand ("Run now",
    the Dashboard) is not a choice: daily scans start only once the source is turned on."""
    from src.app_settings import (record_observed_choices, unofficial_source_chosen_at,
                                  unofficial_source_enabled)

    upgraded = tmp_path / "upgraded.json"
    assert record_observed_choices(upgraded, {"linkedin": True, "hellowork": False}) == ["linkedin"]
    assert unofficial_source_enabled(upgraded, "linkedin") is True
    assert unofficial_source_chosen_at(upgraded, "linkedin") is not None
    assert unofficial_source_enabled(upgraded, "hellowork") is False

    fresh = tmp_path / "fresh.json"
    assert record_observed_choices(fresh, {"linkedin": False}) == []           # first start of 2.0
    # later, the user ran one LinkedIn scan by hand: the next start records nothing
    assert record_observed_choices(fresh, {"linkedin": True}) == []
    assert unofficial_source_enabled(fresh, "linkedin") is False


def test_startup_records_the_observed_choices_in_the_apps_own_settings(isolated_app, tmp_path):
    """The lifespan runs record_observed_choices once, on app.state.settings_path (a temp file
    here, never the checkout's data/settings.json)."""
    import json

    from fastapi.testclient import TestClient

    with TestClient(isolated_app):
        pass
    stored = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert stored.get("unofficial_sources_observed_at")
    assert "linkedin" not in stored.get("unofficial_sources", {})      # no scan history in this DB
