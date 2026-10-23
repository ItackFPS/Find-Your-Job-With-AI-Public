"""What the launcher checks, starts and reports — no process management here.

Standard library only (python-dotenv is used when installed), so the launcher can
still explain what is missing in a half-installed environment.

Component roles (what the launcher may do with each):

=============  ==========================================  ==========================
component      role                                        launcher
=============  ==========================================  ==========================
backend        FastAPI API + built dashboard (required)    starts it, or reuses ours
dashboard      ``frontend/dist`` served by the backend     builds it when stale
local LLM      LM Studio / Ollama, only for --ai runs      never starts it; reports
=============  ==========================================  ==========================
"""

from __future__ import annotations

import enum
import importlib.util
import json
import os
import shutil
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

APP_ID = "find-your-job-with-ai"
APP_TITLE = "Find Your Job With AI"
LOOPBACK = "127.0.0.1"

DEFAULT_BACKEND_PORT = 8000
DEFAULT_VITE_PORT = 5173
DEFAULT_LLM_ENDPOINT = "http://localhost:1234"

# Imports the backend cannot start without (module name -> pip distribution).
REQUIRED_MODULES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "pydantic": "pydantic",
    "yaml": "pyyaml",
    "httpx": "httpx",
    "dotenv": "python-dotenv",
    "sentence_transformers": "sentence-transformers",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


# -- configuration ---------------------------------------------------------------------------


def read_dotenv(root: Path) -> dict[str, str]:
    """Values from ``.env`` (never printed). Empty if absent or python-dotenv is missing."""
    path = root / ".env"
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def _port(value: str | None, default: int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return port if 0 < port < 65536 else default


@dataclass(frozen=True)
class Settings:
    root: Path
    backend_port: int = DEFAULT_BACKEND_PORT
    vite_port: int = DEFAULT_VITE_PORT
    llm_endpoint: str = DEFAULT_LLM_ENDPOINT
    startup_timeout_s: float = 180.0

    @classmethod
    def load(cls, root: Path, environ: Mapping[str, str] | None = None,
             backend_port: int | None = None) -> Settings:
        """Shell environment over ``.env`` over defaults."""
        env = {**read_dotenv(root), **(os.environ if environ is None else environ)}
        return cls(
            root=root,
            backend_port=backend_port or _port(env.get("FYJ_PORT"), DEFAULT_BACKEND_PORT),
            vite_port=_port(env.get("FYJ_VITE_PORT"), DEFAULT_VITE_PORT),
            llm_endpoint=(env.get("PHASE2_LLM_ENDPOINT") or DEFAULT_LLM_ENDPOINT).rstrip("/"),
        )

    @property
    def backend_url(self) -> str:
        return f"http://{LOOPBACK}:{self.backend_port}"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs" / "launcher"

    @property
    def state_file(self) -> Path:
        return self.logs_dir / "state.json"

    @property
    def stop_event_name(self) -> str:
        # one launcher per checkout: two clones get two independent apps
        import hashlib

        digest = hashlib.sha256(str(self.root).lower().encode()).hexdigest()[:16]
        return f"FindYourJobWithAI.launcher.{digest}"


# -- probes ----------------------------------------------------------------------------------


def port_in_use(port: int, host: str = LOOPBACK, timeout_s: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def http_get(url: str, timeout_s: float = 2.0, max_bytes: int = 65536) -> tuple[int, bytes] | None:
    """(status, first ``max_bytes`` of the body) or None when nothing answers.
    Never goes through a proxy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, headers={"Accept": "application/json"}),
                         timeout=timeout_s) as resp:
            return resp.status, resp.read(max_bytes)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(max_bytes) if exc.fp else b""
    except (OSError, ValueError):
        return None


class Backend(enum.Enum):
    FREE = "free"          # nothing listening: the launcher may start one
    OURS = "ours"          # this application answers on the port
    FOREIGN = "foreign"    # something else holds the port
    STARTING = "starting"  # port open, not answering yet


def _json(body: bytes) -> dict | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def probe_backend(port: int, timeout_s: float = 2.0) -> Backend:
    base = f"http://{LOOPBACK}:{port}"
    health = http_get(f"{base}/api/health", timeout_s)
    if health is None:
        return Backend.STARTING if port_in_use(port) else Backend.FREE
    status, body = health
    data = _json(body) if status == 200 else None
    if data is not None and data.get("app") == APP_ID:
        return Backend.OURS
    if status == 403 and b"OS account" in body:  # peer guard refusal: our app, other account
        return Backend.OURS
    # A backend from before /api/health existed answers it with the SPA (or 404):
    # recognise it by its OpenAPI title.
    openapi = http_get(f"{base}/api/openapi.json", timeout_s, max_bytes=16_000_000)
    if openapi and openapi[0] == 200:
        info = (_json(openapi[1]) or {}).get("info") or {}
        if isinstance(info, dict) and info.get("title") == APP_TITLE:
            return Backend.OURS
    return Backend.FOREIGN


def backend_pid(port: int, timeout_s: float = 2.0) -> int | None:
    """PID reported by this application's /api/health on ``port``, if it answers."""
    result = http_get(f"http://{LOOPBACK}:{port}/api/health", timeout_s)
    data = _json(result[1]) if result and result[0] == 200 else None
    if not data or data.get("app") != APP_ID:
        return None
    pid = data.get("pid")
    return pid if isinstance(pid, int) else None


def probe_llm(endpoint: str, timeout_s: float = 2.0) -> bool:
    """True if an OpenAI-compatible server answers ``/v1/models`` at the endpoint."""
    result = http_get(f"{endpoint.rstrip('/')}/v1/models", timeout_s)
    return result is not None and result[0] == 200


def is_loopback_url(url: str) -> bool:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


# -- prerequisites ---------------------------------------------------------------------------


class Level(enum.IntEnum):
    OK = 0
    INFO = 1
    WARN = 2
    ERROR = 3


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    message: str
    fix: str = ""


def missing_modules(modules: Iterable[str] = REQUIRED_MODULES) -> list[str]:
    return [m for m in modules if importlib.util.find_spec(m) is None]


def in_virtualenv() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def bundled_runtime(root: Path) -> bool:
    """True when this interpreter is the private Python the Windows installer ships in
    ``<root>/runtime`` (packaging/windows): an installed copy, not a development checkout."""
    try:
        return Path(sys.prefix).resolve() == (root / "runtime").resolve()
    except OSError:
        return False


@dataclass(frozen=True)
class Frontend:
    built: bool
    stale: bool
    node: str | None
    deps_installed: bool

    @property
    def can_build(self) -> bool:
        return self.node is not None and self.deps_installed


_FRONTEND_INPUTS = ("src", "public", "index.html", "package.json", "package-lock.json",
                    "vite.config.ts", "tsconfig.json", "tsconfig.app.json")


def _newest_mtime(paths: Iterable[Path]) -> float:
    newest = 0.0
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and "__tests__" not in f.parts:
                    newest = max(newest, f.stat().st_mtime)
        elif p.is_file():
            newest = max(newest, p.stat().st_mtime)
    return newest


def frontend_state(root: Path) -> Frontend:
    fe = root / "frontend"
    index = fe / "dist" / "index.html"
    built = index.is_file()
    stale = built and _newest_mtime(fe / name for name in _FRONTEND_INPUTS) > index.stat().st_mtime
    return Frontend(
        built=built,
        stale=stale,
        node=shutil.which("node"),
        deps_installed=(fe / "node_modules" / "vite" / "bin" / "vite.js").is_file(),
    )


REINSTALL_FIX = "Run the Find Your Job With AI installer again (your data is kept)"


def prerequisite_checks(root: Path) -> list[Check]:
    checks: list[Check] = []
    installed = bundled_runtime(root)
    py = sys.version_info
    if py < (3, 12):
        checks.append(Check("python", Level.ERROR, f"Python {py.major}.{py.minor} is too old",
                            REINSTALL_FIX if installed else "Install Python 3.12 and recreate .venv"))
    else:
        where = " (bundled)" if installed else ""
        checks.append(Check("python", Level.OK, f"Python {py.major}.{py.minor}.{py.micro}{where}"))
    if not installed and not in_virtualenv():
        checks.append(Check("venv", Level.WARN, "not running inside a virtual environment",
                            "Create one: py -3.12 -m venv .venv"))
    missing = missing_modules()
    if missing:
        dists = " ".join(REQUIRED_MODULES[m] for m in missing)
        checks.append(Check("packages", Level.ERROR, f"missing Python packages: {dists}",
                            REINSTALL_FIX if installed else ".venv\\Scripts\\python -m pip install -e ."))
    else:
        checks.append(Check("packages", Level.OK, "backend packages installed"))
    fe = frontend_state(root)
    if installed and not fe.built:
        checks.append(Check("dashboard", Level.ERROR, "dashboard files are missing", REINSTALL_FIX))
    elif fe.built and not fe.stale:
        checks.append(Check("dashboard", Level.OK, "dashboard build is up to date"))
    elif fe.can_build:
        what = "out of date" if fe.built else "not built yet"
        checks.append(Check("dashboard", Level.INFO, f"dashboard {what}; it will be built now"))
    elif fe.built:
        checks.append(Check("dashboard", Level.WARN, "dashboard build is older than its sources",
                            "cd frontend && npm ci && npm run build"))
    else:
        fix = ("Install Node.js 22 LTS (or 20.19+), then: cd frontend && npm ci && npm run build"
               if fe.node is None else "cd frontend && npm ci && npm run build")
        checks.append(Check("dashboard", Level.ERROR, "dashboard is not built", fix))
    if not (root / ".env").is_file():
        checks.append(Check(".env", Level.INFO, "no .env file: optional sources and notifications are off",
                            f"copy .env.example to .env in {root}, then fill in only what you use"
                            if installed else "copy .env.example .env, then fill in only what you use"))
    return checks


# -- commands --------------------------------------------------------------------------------


def backend_command(python: str, port: int, reload: bool = False) -> list[str]:
    # an open dashboard keeps its live-events stream open, and uvicorn otherwise waits for it until
    # the launcher kills the backend (run.GRACEFUL_STOP_S): close it after 5 s instead
    cmd = [python, "-m", "uvicorn", "src.api.app:app", "--host", LOOPBACK, "--port", str(port),
           "--timeout-graceful-shutdown", "5"]
    if reload:
        cmd.append("--reload")
    return cmd


def frontend_build_command(root: Path, node: str) -> list[str]:
    """``vite build`` run by node directly (no npm/cmd.exe in between: a direct child is
    easier to own and stop). Type-checking (``npm run build``'s ``tsc -b``) is a
    development check and does not gate starting the app."""
    fe = root / "frontend"
    return [node, str(fe / "node_modules" / "vite" / "bin" / "vite.js"), "build"]


def vite_dev_command(root: Path, node: str, port: int) -> list[str]:
    vite = root / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
    return [node, str(vite), "--host", LOOPBACK, "--port", str(port), "--strictPort"]


# -- state -----------------------------------------------------------------------------------


@dataclass
class Owned:
    pid: int
    created: int | None
    port: int | None = None


@dataclass
class State:
    launcher_pid: int
    launcher_created: int | None
    mode: str
    components: dict[str, Owned] = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "launcher_pid": self.launcher_pid,
            "launcher_created": self.launcher_created,
            "mode": self.mode,
            "components": {k: vars(v) for k, v in self.components.items()},
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> State | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                launcher_pid=int(data["launcher_pid"]),
                launcher_created=data.get("launcher_created"),
                mode=str(data.get("mode", "")),
                components={k: Owned(**v) for k, v in (data.get("components") or {}).items()},
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


def rotate_log(path: Path, max_bytes: int = 5_000_000) -> None:
    """Keep one previous generation once a child's log grows past ``max_bytes``."""
    try:
        if path.stat().st_size > max_bytes:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass


def log_tail(path: Path, lines: int = 15) -> list[str]:
    try:
        text = path.read_bytes()[-16384:].decode("utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]
