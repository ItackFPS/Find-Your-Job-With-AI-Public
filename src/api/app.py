"""FastAPI application for Find Your Job With AI.

Starts the web server with:
    uvicorn src.api.app:app --reload --port 8000

During development the Vite dev server (port 5173) proxies /api/* to
port 8000. In production the built frontend is served from FastAPI.

Production note (--reload): --reload is a development convenience — it
restarts this worker process on every source file change. A pipeline run
triggered from the dashboard executes as a background thread INSIDE this
process (see src/api/runner.py), so a mid-run restart genuinely kills that
thread; RunStorage.recover_stale_runs() now only recovers a 'running' row
once its owning PID is actually gone and its heartbeat has gone stale
(src/storage/runs.py), so a restart no longer *misreports* an unrelated,
still-alive process as failed — but it does not make --reload safe to run a
long AI-enabled pipeline under. Run without --reload in production
(`uvicorn src.api.app:app --port 8000`), and avoid triggering runs from a
--reload-enabled dev server while editing files in this repository.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Configured before any other src.* import so every module logger (all of
# which propagate to root) is captured from the very first line, regardless
# of whether this process was launched via scripts/Start_backend.bat or a
# manual `uvicorn --reload` dev run — see src/logging_config.py docstring.
from src.logging_config import configure_logging

configure_logging()

from src.api.origin_guard import LocalHostGuardMiddleware, OriginGuardMiddleware
from src.api.peer_guard import SameUserPeerGuardMiddleware, enabled_from_env as same_user_only
from src.api.routes import (
    dashboard,
    decisions,
    departments,
    esco,
    feedback,
    hellowork,
    jobs,
    lab,
    linkedin,
    pipeline,
    profile,
    scheduler as scheduler_routes,
    skills,
)
from src.api.progress_stream import ProgressBus
from src.api.runner import PipelineRunner
from src.hellowork.scanner import HelloWorkScanner
from src.hellowork.staging import HelloWorkStagingStorage
from src.linkedin.scanner import LinkedInConfig, LinkedInScanner
from src.linkedin.staging import LinkedInStagingStorage
from src.notifications.discord_reconciler_worker import DiscordReconcilerWorker
from src.notifications.engine import NotificationEngine
from src.notifications.system_status_worker import SystemStatusWorker
from src.scheduler.service import (
    HelloWorkHandler,
    LinkedInHandler,
    SchedulerService,
    SourceHandler,
)
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(application: FastAPI) -> AsyncIterator[None]:
    logger.info(
        "backend_start pid=%d timestamp=%s",
        os.getpid(), datetime.now(UTC).isoformat(),
    )
    db_path = application.state.db_path
    config_dir = application.state.config_dir
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    # Bind the running event loop so PipelineRunner (on its background
    # thread) can publish SSE progress events thread-safely. Purely
    # observational — see src/api/progress_stream.py.
    application.state.progress_bus.bind_loop(asyncio.get_running_loop())

    # -- Core database init --------------------------------------------------
    s = Storage(db_path=db_path)
    s.init_db()
    s.close()

    rs = RunStorage(db_path=db_path)
    rs.init_db()
    recovered = rs.recover_stale_runs()
    if recovered:
        logger.warning("Recovered %d stale pipeline run(s) — marked 'failed'.", recovered)
    rs.close()
    logger.info("Database initialised at %s", db_path)

    # -- Discord channel health check (startup only) -------------------------
    # Surfaces any enabled-but-unusable channel (webhook secret missing from
    # this environment's .env) as a log warning so it's visible immediately
    # instead of discovered later as "why didn't this channel ever send
    # anything". Purely observational — never blocks/delays startup.
    try:
        application.state.notification_engine.log_unconfigured_channels()
    except Exception as exc:  # noqa: BLE001 - health reporting must never block startup
        logger.warning("Startup Discord channel health check failed: %s", exc)

    # -- LinkedIn staging init (always runs so recovery is always safe) ------
    li_staging = LinkedInStagingStorage(db_path=db_path)
    li_staging.init_db()
    li_staging.recover_stale_scans()
    # Reset after pipeline recovery: any job snapshotted by a now-failed
    # pipeline run must be returned to 'waiting'.
    li_staging.reset_stale_snapshots()
    application.state.linkedin_staging = li_staging

    # -- LinkedIn config and scanner (only when enabled) ---------------------
    li_config = LinkedInConfig.load(config_dir)
    application.state.linkedin_config = li_config
    application.state.linkedin_scanner = None

    # Shared by every scanner so a single shutdown signal stops all of them
    # cooperatively -- see LinkedInScanner's docstring on _shutdown_event.
    scanner_shutdown_event = threading.Event()

    li_scanner: LinkedInScanner | None = None
    if li_config.enabled:
        li_scanner = LinkedInScanner(
            config=li_config, staging=li_staging, config_dir=config_dir,
            shutdown_event=scanner_shutdown_event, db_path=db_path,
        )
        application.state.linkedin_scanner = li_scanner
    else:
        logger.debug("LinkedIn scanner disabled (set enabled: true in config/linkedin.yaml).")

    # -- HelloWork staging init (always runs so recovery is always safe) -----
    hw_staging = HelloWorkStagingStorage(db_path=db_path)
    hw_staging.init_db()
    hw_staging.recover_stale_scans()
    hw_staging.reset_stale_snapshots()
    application.state.hellowork_staging = hw_staging

    # -- HelloWork scanner (runs decoupled from the main pipeline, like
    # LinkedIn -- see src/collectors/hellowork.py for why its collection is
    # slow: ~30 minutes for the full metier taxonomy, one request per job).
    # Reads the same 'hellowork' entry's config sub-dict from sources.yaml
    # that the collector itself accepts; that entry is set active: false so
    # the normal per-run collector loop never invokes it synchronously.
    hw_scanner: HelloWorkScanner | None = None
    try:
        from src.discovery.registry import SourceRegistry
        from src.pipeline.orchestrator import _build_collector_config

        sources_path = Path(config_dir) / "sources.yaml"
        hw_source_cfg = SourceRegistry(config_path=str(sources_path)).get_source("hellowork")
        if hw_source_cfg is not None:
            hw_scanner = HelloWorkScanner(
                config=_build_collector_config(hw_source_cfg), staging=hw_staging,
                shutdown_event=scanner_shutdown_event,
            )
            application.state.hellowork_scanner = hw_scanner
        else:
            application.state.hellowork_scanner = None
            logger.debug("HelloWork: no 'hellowork' entry in sources.yaml — scanner disabled.")
    except Exception as exc:  # noqa: BLE001
        application.state.hellowork_scanner = None
        logger.warning("HelloWork: failed to initialise scanner — disabled: %s", exc)

    # -- Generic scheduler (always starts; dispatches per config/schedules.yaml) --
    handlers: dict[str, SourceHandler] = {}
    if li_scanner is not None:
        handlers["linkedin"] = LinkedInHandler(scanner=li_scanner)
    if hw_scanner is not None:
        handlers["hellowork"] = HelloWorkHandler(scanner=hw_scanner)
    scheduler = SchedulerService(
        db_path=db_path,
        config_dir=config_dir,
        handlers=handlers,
        pipeline_runner=application.state.runner,
    )
    scheduler.start()
    application.state.scheduler = scheduler

    # -- Graceful stop for a launcher without a console (the tray app) -------
    from src.api.stop_signal import start_stop_signal_watcher
    stop_signal_event = start_stop_signal_watcher()

    # -- Discord location-channel reconciliation (automatic, independent of
    # Claude/MCP/any LLM — see src/notifications/discord_reconciler_worker.py
    # for the reload/duplicate-start safety reasoning). No-ops harmlessly on
    # every tick until DISCORD_BOT_TOKEN/DISCORD_GUILD_ID are configured.
    discord_worker = DiscordReconcilerWorker(config_dir=config_dir)
    discord_worker.start()
    application.state.discord_reconciler_worker = discord_worker

    # -- System status heartbeat (independent of the reconciler above — see
    # src/notifications/system_status_worker.py's docstring for why this is
    # a separate worker rather than a tick added onto the reconciler's).
    # No-ops harmlessly whenever the "system-status" Discord channel isn't
    # enabled/configured — same "safe to run always" property as the
    # reconciler worker above.
    system_status_worker = SystemStatusWorker(notification_engine=application.state.notification_engine)
    system_status_worker.start()
    application.state.system_status_worker = system_status_worker

    # -- ESCO seed -----------------------------------------------------------
    # Auto-seed the ESCO skill database with bundled data if not yet populated.
    # This requires no network access — seed data is embedded in the codebase.
    # Run `python scripts/setup_esco.py` to also fetch the full ESCO taxonomy.
    try:
        from src.esco.db import EscoDb
        from src.esco.loader import load_seed_data
        esco_db = EscoDb(db_path=application.state.esco_db_path)
        if not esco_db.is_ready():
            esco_db.init_db()
            count = load_seed_data(esco_db)
            logger.info("ESCO: seeded %d concepts from bundled data (run scripts/setup_esco.py for full taxonomy)", count)
        else:
            logger.debug("ESCO: database already populated (%d concepts)", esco_db.count())
    except Exception as exc:  # noqa: BLE001
        logger.warning("ESCO: failed to seed database: %s", exc)

    shutdown_reason = "normal"
    try:
        yield
    except asyncio.CancelledError:
        shutdown_reason = "cancelled"
        raise
    except BaseException as exc:  # reported below, then re-raised as-is
        shutdown_reason = f"unhandled_exception: {exc!r}"
        raise
    finally:
        # -- Shutdown ----------------------------------------------------------
        # try/finally so cleanup always runs even if an exception propagates
        # through the yield point.
        logger.info(
            "backend_shutdown pid=%d reason=%s timestamp=%s",
            os.getpid(), shutdown_reason, datetime.now(UTC).isoformat(),
        )
        # Signal any in-flight scan to stop at its next checkpointed
        # boundary (end of current page/slug) instead of being abandoned
        # mid-request when this process actually exits. This does not make
        # shutdown wait indefinitely -- worst case is one in-flight network
        # call finishing, which is already what the scanner was doing.
        scanner_shutdown_event.set()
        scheduler.stop()
        discord_worker.stop()
        system_status_worker.stop()
        li_staging.close()
        hw_staging.close()
        if stop_signal_event is not None:
            stop_signal_event.close()


app = FastAPI(
    title="Find Your Job With AI",
    description="Local-first job discovery pipeline with web UI.",
    version="1.0.0",  # = pyproject.toml [project] version (tests/test_windows_package.py)
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    lifespan=_lifespan,
)

# Web origins other than the backend itself that may call the API: the Vite
# dev server. Shared by CORS and the cross-origin write guard.
DEV_WEB_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
)


def allowed_web_origins(same_user_only_enabled: bool) -> tuple[str, ...]:
    """None under API_SAME_USER_ONLY: the dev server must not run then (it would
    relay other accounts' requests as yours), and while its port is free another
    account could serve a page there that your browser would treat as allowed."""
    return () if same_user_only_enabled else DEV_WEB_ORIGINS


ALLOWED_WEB_ORIGINS: tuple[str, ...] = allowed_web_origins(same_user_only())

# CORS: allow the Vite dev server to reach the API during development.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(ALLOWED_WEB_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Refuse POST/PUT/PATCH/DELETE under /api sent by other websites (issue #12);
# same-origin UI calls, the dev server and Origin-less clients are unaffected.
app.add_middleware(OriginGuardMiddleware, allowed_origins=ALLOWED_WEB_ORIGINS)

# Refuse any request whose Host is not this machine
# (issue #59, DNS rebinding). API_ALLOWED_HOSTS (comma-separated) adds names
# such as host.docker.internal for a containerised n8n.
app.add_middleware(
    LocalHostGuardMiddleware,
    extra_hosts=os.environ.get("API_ALLOWED_HOSTS", "").split(","),
)

# Opt-in, outermost: API_SAME_USER_ONLY=1 serves only processes of the OS account
# running this server, so a lab agent under its own account cannot make the API
# write production state for it (ADR 0011, "Promotion authority"). Off by default.
app.add_middleware(SameUserPeerGuardMiddleware, enabled=same_user_only())

# ---------------------------------------------------------------------------
# Shared state (attached to app.state so routes can access via Request)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DB_PATH = str(_PROJECT_ROOT / "data" / "jobs.db")
_ESCO_DB_PATH = str(_PROJECT_ROOT / "data" / "esco.db")
_CONFIG_DIR = str(_PROJECT_ROOT / "config")

app.state.db_path = _DB_PATH
app.state.config_dir = _CONFIG_DIR
# Read by _lifespan's ESCO auto-seed step so a test can point it at an
# isolated tmp db before triggering lifespan via `with TestClient(app)`.
# Independent of src/api/routes/esco.py's own `_DB_PATH` (the route
# handlers' path), which a test must override separately — see
# tests/test_esco.py.
app.state.esco_db_path = _ESCO_DB_PATH

_notification_engine = NotificationEngine(db_path=_DB_PATH, config_dir=_CONFIG_DIR)
app.state.notification_engine = _notification_engine
app.state.progress_bus = ProgressBus()
app.state.runner = PipelineRunner(
    db_path=_DB_PATH,
    config_dir=_CONFIG_DIR,
    notification_engine=_notification_engine,
    progress_bus=app.state.progress_bus,
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

app.include_router(profile.router)
app.include_router(pipeline.router)
app.include_router(jobs.router)
app.include_router(feedback.router)
app.include_router(decisions.router)
app.include_router(dashboard.router)
app.include_router(esco.router)
app.include_router(departments.router)
app.include_router(linkedin.router)
app.include_router(hellowork.router)
app.include_router(scheduler_routes.router)
app.include_router(skills.router)
app.include_router(lab.router)

# Must match src/launcher/core.py APP_ID: the launcher recognises this backend by it.
APP_ID = "find-your-job-with-ai"


@app.get("/api/health")
def health() -> dict[str, str | int]:
    """Liveness for the launcher and monitors. Touches no database and no network.
    ``pid`` lets the launcher tell the backend it started from one started elsewhere."""
    return {"status": "ok", "app": APP_ID, "version": app.version, "pid": os.getpid()}


# ---------------------------------------------------------------------------
# Static files (production: serve the built Vite frontend)
# ---------------------------------------------------------------------------

# Anchored to the project root, not the working directory, so the dashboard is
# served however the backend was started.
_FRONTEND_DIST = _PROJECT_ROOT / "frontend" / "dist"


def spa_file(dist: Path, full_path: str) -> Path | None:
    """File answering a non-API path: a real file at the build root (favicon,
    icons) as itself, anything else the SPA's index.html. None for /api/* paths,
    which must 404 as JSON instead of returning the page with a 200."""
    if full_path == "api" or full_path.startswith("api/"):
        return None
    root = dist.resolve()
    candidate = (root / full_path).resolve()
    if full_path and candidate.is_file() and candidate.is_relative_to(root):
        return candidate
    return root / "index.html"


if _FRONTEND_DIST.exists():
    # Serve static assets (JS, CSS, images) under /assets
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str) -> FileResponse:
        """Serve the React SPA for all non-API routes."""
        path = spa_file(_FRONTEND_DIST, full_path)
        if path is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(str(path))
