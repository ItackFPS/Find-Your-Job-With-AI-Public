"""Crash restart and checkpoint resume (update plan F2.5, §C7 "Recovery").

The agent's backend is killed (TerminateProcess / SIGKILL: no cleanup, no cooperative stop) in the
middle of a scheduled HelloWork scan driven by a fake collector. A new "backend" on the same
database then runs the startup recovery the real one runs (src/api/app.py ``_lifespan``) and its
scheduler: the scan must resume from its checkpoint as the same logical scan, without duplicate
jobs, and the scheduler must catch up at most one missed run however long the app was down.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.collectors.base import CollectorResult
from src.hellowork.scanner import HelloWorkScanner
from src.hellowork.staging import HelloWorkStagingStorage
from src.scheduler.service import HelloWorkHandler, SchedulerService

ROOT = Path(__file__).resolve().parents[1]
SLUGS = ["slug-a", "slug-b", "slug-c"]
SCHEDULES = """\
schedules:
  - id: hellowork_daily
    source_id: hellowork
    label: "HelloWork daily scan"
    enabled: true
    startup_policy: run_once_per_day
    retry_max: 0
    retry_delay_seconds: 300
"""


def raw_job(slug: str, n: int) -> dict:
    jid = f"{slug}-{n}"
    return {"title": f"Developpeur {jid}", "company": "Acme", "description": "Build things",
            "location": "Paris, France", "countries": ["FR"],
            "canonical_url": f"https://www.hellowork.com/fr-fr/emplois/{jid}.html", "source_job_id": jid,
            "published_at": "2026-09-01T00:00:00Z", "type": "full-time", "remote": False,
            "work_mode": None, "skills": []}


# The agent's backend before the crash: its scheduler dispatches the daily scan at startup; the
# collector finishes slug-a and slug-b (each checkpointed), then hangs inside slug-c until killed.
AGENT = """\
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[4])
from tests.test_crash_resume import SLUGS, raw_job
from src.collectors.base import CollectorResult
import src.collectors.hellowork as hw
from src.hellowork.scanner import HelloWorkScanner
from src.hellowork.staging import HelloWorkStagingStorage
from src.scheduler.service import HelloWorkHandler, SchedulerService

db, config_dir, marker = sys.argv[1:4]

class HangingCollector:
    def __init__(self, config):
        pass
    def collect(self, on_slug_done=None, skip_slugs=None, shutdown_event=None):
        for slug in SLUGS:
            if slug == "slug-c":
                Path(marker).write_text("in slug-c")
                time.sleep(600)  # killed here, mid-slug
            on_slug_done(slug, [raw_job(slug, n) for n in range(3)])
        return CollectorResult(source_name="hellowork", jobs=[], errors=[], duration_seconds=1.0)

hw.HelloWorkCollector = HangingCollector
staging = HelloWorkStagingStorage(db_path=db)
staging.init_db()
staging.recover_stale_scans()
scanner = HelloWorkScanner(config={"metier_slugs": SLUGS}, staging=staging)
SchedulerService(db_path=db, config_dir=config_dir, handlers={"hellowork": HelloWorkHandler(scanner)}).start()
time.sleep(600)
"""


def _kill_mid_scan(tmp_path: Path, db: str, config_dir: Path) -> None:
    marker = tmp_path / "in-slug-c"
    script = tmp_path / "agent.py"
    script.write_text(AGENT, encoding="utf-8")
    agent = subprocess.Popen([sys.executable, str(script), db, str(config_dir), str(marker), str(ROOT)],
                             cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 120
        while not marker.exists():
            assert agent.poll() is None, agent.stderr.read().decode(errors="replace") if agent.stderr else ""
            assert time.monotonic() < deadline, "the agent never reached slug-c"
            time.sleep(0.1)
    finally:
        agent.kill()  # TerminateProcess on Windows: no shutdown handler runs
        agent.wait(30)


def test_a_scan_killed_mid_run_resumes_from_its_checkpoint_without_duplicates(tmp_path, monkeypatch):
    db = str(tmp_path / "jobs.db")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "schedules.yaml").write_text(SCHEDULES, encoding="utf-8")
    _kill_mid_scan(tmp_path, db, config_dir)

    # The machine was off for three days (a reboot after the crash, then a long weekend).
    later = datetime.now(UTC) + timedelta(days=3)
    seen_skips: list[set[str]] = []

    class ResumingCollector:
        def __init__(self, config):  # noqa: ANN001
            pass

        def collect(self, on_slug_done=None, skip_slugs=None, shutdown_event=None):  # noqa: ANN001
            seen_skips.append(set(skip_slugs or ()))
            for slug in SLUGS:
                if slug in (skip_slugs or ()):
                    continue
                on_slug_done(slug, [raw_job(slug, n) for n in range(3)])
            # a source that re-lists an offer it already gave (slug-b's first job) must not duplicate it
            on_slug_done("slug-c", [raw_job("slug-b", 0)])
            return CollectorResult(source_name="hellowork", jobs=[], errors=[], duration_seconds=1.0)

    import src.collectors.hellowork as hw

    monkeypatch.setattr(hw, "HelloWorkCollector", ResumingCollector)

    # The restarted backend: the recovery steps of src/api/app.py _lifespan, then its scheduler.
    staging = HelloWorkStagingStorage(db_path=db)
    staging.init_db()
    staging.recover_stale_scans()
    staging.reset_stale_snapshots()
    checkpoint = staging.get_scan_checkpoint()
    assert checkpoint is not None and checkpoint["state"] == "interrupted"
    killed_scan = checkpoint["scan_id"]
    scanner = HelloWorkScanner(config={"metier_slugs": SLUGS}, staging=staging)
    scheduler = SchedulerService(db_path=db, config_dir=str(config_dir),
                                 handlers={"hellowork": HelloWorkHandler(scanner)}, clock=lambda: later)
    scheduler.start()
    try:
        deadline = time.monotonic() + 60
        while not seen_skips or scanner.is_scanning():
            assert time.monotonic() < deadline, "the scheduler never resumed the scan"
            time.sleep(0.05)
        scheduler.join_dispatches(30)
        for _ in range(5):                       # later ticks of the same (catch-up) day
            scheduler._tick()
        scheduler.join_dispatches(30)
    finally:
        scheduler.stop()

    assert seen_skips == [{"slug-a", "slug-b"}]                 # resumed after the last checkpoint, once
    conn = staging._get_conn()
    rows = conn.execute("SELECT source_job_id, COUNT(*) FROM hellowork_staging GROUP BY source_job_id").fetchall()
    assert len(rows) == 9 and all(count == 1 for _id, count in rows)   # 3 slugs x 3 jobs, no duplicate
    run = staging.get_scan_run(killed_scan)
    assert run is not None and run["status"] == "completed"           # the same logical scan finished
    assert staging.get_scan_checkpoint() is None
    executions = conn.execute(
        "SELECT status, trigger FROM schedule_executions ORDER BY started_at").fetchall()
    # before the crash: the killed run (recovered as interrupted); after: ONE catch-up run, not three
    assert [tuple(e) for e in executions] == [("interrupted", "startup"), ("completed", "startup")]
    staging.close()
