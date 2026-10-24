"""Detect local LLM runtimes: Ollama, LM Studio and a user-supplied OpenAI-compatible server.

Every probe is a short, read-only GET on a loopback address, checked first by the
local-first guard (:func:`src.ai.endpoint.require_local_endpoint`): a remote address is
reported as ``endpoint_not_local`` and never contacted. Redirects are not followed and
proxy settings from the environment are ignored, so a request cannot leave the machine.

Installed-but-not-running runtimes are found by looking at standard install paths and
``PATH``; nothing is launched.

:func:`detect_runtimes` never raises: every failure becomes an error code on the result.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, Field

from src.ai.endpoint import RemoteLLMRefused, require_local_endpoint

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://127.0.0.1:11434"
LMSTUDIO_URL = "http://127.0.0.1:1234"

CONNECT_TIMEOUT_S = 1.0
READ_TIMEOUT_S = 2.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

RuntimeKind = Literal["ollama", "lmstudio", "openai_compatible"]
RuntimeStatus = Literal["running", "installed", "not_found", "error"]

# Error codes (stable, for the UI to translate).
ERR_INVALID_URL = "invalid_url"
ERR_NOT_LOCAL = "endpoint_not_local"
ERR_UNREACHABLE = "unreachable"
ERR_TIMEOUT = "timeout"
ERR_HTTP_STATUS = "http_status"
ERR_MALFORMED = "malformed_response"
ERR_TOO_LARGE = "response_too_large"
ERR_INTERNAL = "internal_error"


class RuntimeModel(BaseModel):
    name: str
    size_bytes: int | None = None
    quantization: str | None = None
    parameter_size: str | None = None
    loaded: bool | None = None  # Ollama /api/ps; None = unknown


class RuntimeDetection(BaseModel):
    runtime: RuntimeKind
    base_url: str
    status: RuntimeStatus
    reachable: bool = False
    installed: bool | None = None  # None = not checked (a user-supplied server)
    executable: str | None = None
    models: list[RuntimeModel] = Field(default_factory=list)
    error: str | None = None
    http_status: int | None = None


class RuntimeReport(BaseModel):
    runtimes: list[RuntimeDetection]


class _ProbeError(Exception):
    def __init__(self, code: str, http_status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


# --- HTTP --------------------------------------------------------------------------


def _get_json(url: str, timeout: tuple[float, float]) -> Any:
    """GET ``url`` and decode JSON; raise :class:`_ProbeError` with a code on any failure."""
    deadline = time.monotonic() + timeout[0] + timeout[1]
    session = requests.Session()
    session.trust_env = False  # no proxy from the environment: stay on this machine
    try:
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=False, stream=True,
                               headers={"Accept": "application/json"})
        except requests.ConnectTimeout as exc:
            # On loopback a listening server completes the handshake at once; a connect
            # timeout means nothing listens (Windows retries a refused SYN for ~2 s).
            raise _ProbeError(ERR_UNREACHABLE) from exc
        except requests.Timeout as exc:
            raise _ProbeError(ERR_TIMEOUT) from exc
        except requests.RequestException as exc:
            raise _ProbeError(ERR_UNREACHABLE) from exc
        with resp:
            if resp.status_code != 200:
                raise _ProbeError(ERR_HTTP_STATUS, resp.status_code)
            body = bytearray()
            try:
                for chunk in resp.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise _ProbeError(ERR_TOO_LARGE)
                    if time.monotonic() > deadline:
                        raise _ProbeError(ERR_TIMEOUT)
            except requests.Timeout as exc:
                raise _ProbeError(ERR_TIMEOUT) from exc
            except requests.RequestException as exc:
                raise _ProbeError(ERR_UNREACHABLE) from exc
        try:
            return json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _ProbeError(ERR_MALFORMED) from exc
    finally:
        session.close()


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _parse_ollama_models(data: Any) -> list[RuntimeModel]:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise _ProbeError(ERR_MALFORMED)
    models = []
    for item in data["models"]:
        if not isinstance(item, dict):
            continue
        name = _str_or_none(item.get("name")) or _str_or_none(item.get("model"))
        if name is None:
            continue
        raw_details = item.get("details")
        details: dict[str, Any] = raw_details if isinstance(raw_details, dict) else {}
        models.append(RuntimeModel(
            name=name,
            size_bytes=_int_or_none(item.get("size")),
            quantization=_str_or_none(details.get("quantization_level")),
            parameter_size=_str_or_none(details.get("parameter_size")),
        ))
    return models


def _parse_openai_models(data: Any) -> list[RuntimeModel]:
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise _ProbeError(ERR_MALFORMED)
    models = []
    for item in data["data"]:
        if isinstance(item, dict) and (name := _str_or_none(item.get("id"))):
            models.append(RuntimeModel(name=name))
    return models


def _check_base_url(url: str) -> str:
    """Return the normalised base URL, or raise :class:`_ProbeError` (invalid / not local)."""
    try:
        parts = urlsplit(url.strip())
        _ = parts.port  # raises ValueError on a bad port
    except ValueError as exc:
        raise _ProbeError(ERR_INVALID_URL) from exc
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise _ProbeError(ERR_INVALID_URL)
    base = url.strip().rstrip("/")
    try:
        require_local_endpoint(base)
    except RemoteLLMRefused as exc:
        raise _ProbeError(ERR_NOT_LOCAL) from exc
    return base


# --- Installed runtimes ----------------------------------------------------------


def _first_existing(candidates: list[Path]) -> str | None:
    for path in candidates:
        try:
            if path.is_file() or (path.suffix == ".app" and path.is_dir()):
                return str(path)
        except OSError:
            continue
    return None


def _home(env: Mapping[str, str]) -> Path | None:
    home = env.get("USERPROFILE") or env.get("HOME")
    return Path(home) if home else None


def find_ollama(env: Mapping[str, str] | None = None, platform: str | None = None) -> str | None:
    """Path of an installed Ollama executable (not launched), or ``None``."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    candidates: list[Path] = []
    if platform == "win32":
        if local := env.get("LOCALAPPDATA"):
            candidates.append(Path(local) / "Programs" / "Ollama" / "ollama.exe")
        for root in (env.get("ProgramFiles"), env.get("ProgramW6432")):
            if root:
                candidates.append(Path(root) / "Ollama" / "ollama.exe")
    elif platform == "darwin":
        candidates += [Path("/Applications/Ollama.app"), Path("/usr/local/bin/ollama"),
                       Path("/opt/homebrew/bin/ollama")]
    else:
        candidates += [Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")]
    return _first_existing(candidates) or shutil.which("ollama")


def find_lmstudio(env: Mapping[str, str] | None = None, platform: str | None = None) -> str | None:
    """Path of LM Studio's ``lms`` CLI, else of the LM Studio app (not launched), or ``None``."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    exe = ".exe" if platform == "win32" else ""
    candidates: list[Path] = []
    if home := _home(env):
        candidates += [home / ".lmstudio" / "bin" / f"lms{exe}",
                       home / ".cache" / "lm-studio" / "bin" / f"lms{exe}"]
    found = _first_existing(candidates) or shutil.which("lms")
    if found:
        return found
    apps: list[Path] = []
    if platform == "win32":
        if local := env.get("LOCALAPPDATA"):
            apps += [Path(local) / "Programs" / "LM Studio" / "LM Studio.exe",
                     Path(local) / "Programs" / "lm-studio" / "LM Studio.exe"]
    elif platform == "darwin":
        apps.append(Path("/Applications/LM Studio.app"))
    return _first_existing(apps)


# --- Probes ------------------------------------------------------------------------


def _finish(result: RuntimeDetection) -> RuntimeDetection:
    """``running``; nothing listening: ``installed`` / ``not_found``; anything else: ``error``."""
    if result.reachable:
        result.status = "running"
    elif result.error == ERR_UNREACHABLE:
        result.status = "installed" if result.installed else "not_found"
    else:
        result.status = "error"
    return result


def probe_ollama(base_url: str = OLLAMA_URL,
                 timeout: tuple[float, float] = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                 executable: str | None = None) -> RuntimeDetection:
    result = RuntimeDetection(runtime="ollama", base_url=base_url, status="not_found",
                              installed=executable is not None, executable=executable)
    try:
        base = _check_base_url(base_url)
        models = _parse_ollama_models(_get_json(f"{base}/api/tags", timeout))
        result.reachable = True
        try:
            loaded = {m.name for m in _parse_ollama_models(_get_json(f"{base}/api/ps", timeout))}
            for model in models:
                model.loaded = model.name in loaded
        except _ProbeError:
            pass  # tags answered; "loaded" stays unknown
        result.models = models
    except _ProbeError as exc:
        result.error, result.http_status = exc.code, exc.http_status
    except Exception:  # noqa: BLE001 - detection must never raise
        logger.exception("Ollama detection failed")
        result.error = ERR_INTERNAL
    return _finish(result)


def probe_openai_compatible(base_url: str, runtime: RuntimeKind = "openai_compatible",
                            timeout: tuple[float, float] = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                            executable: str | None = None,
                            check_install: bool = False) -> RuntimeDetection:
    """``GET <base_url>/models``. LM Studio is probed this way at ``<root>/v1``."""
    result = RuntimeDetection(runtime=runtime, base_url=base_url, status="not_found",
                              installed=(executable is not None) if check_install else None,
                              executable=executable)
    try:
        base = _check_base_url(base_url)
        result.models = _parse_openai_models(_get_json(f"{base}/models", timeout))
        result.reachable = True
    except _ProbeError as exc:
        result.error, result.http_status = exc.code, exc.http_status
    except Exception:  # noqa: BLE001 - detection must never raise
        logger.exception("OpenAI-compatible detection failed for %s", base_url)
        result.error = ERR_INTERNAL
    return _finish(result)


def probe_lmstudio(base_url: str = LMSTUDIO_URL,
                   timeout: tuple[float, float] = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                   executable: str | None = None) -> RuntimeDetection:
    result = probe_openai_compatible(f"{base_url.rstrip('/')}/v1", runtime="lmstudio",
                                     timeout=timeout, executable=executable, check_install=True)
    result.base_url = base_url
    return result


def detect_runtimes(base_url: str | None = None, *,
                    timeout: tuple[float, float] = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                    ollama_url: str | None = None,
                    lmstudio_url: str | None = None) -> RuntimeReport:
    """Probe Ollama, LM Studio and (when given) ``base_url``, concurrently. Never raises."""
    ollama_url = ollama_url or OLLAMA_URL
    lmstudio_url = lmstudio_url or LMSTUDIO_URL

    def _safe_find(finder: Callable[[], str | None]) -> str | None:
        try:
            return finder()
        except Exception:  # noqa: BLE001
            logger.exception("Runtime install lookup failed")
            return None

    jobs: list[Any] = [
        lambda: probe_ollama(ollama_url, timeout, _safe_find(find_ollama)),
        lambda: probe_lmstudio(lmstudio_url, timeout, _safe_find(find_lmstudio)),
    ]
    if base_url is not None:
        jobs.append(lambda: probe_openai_compatible(base_url, timeout=timeout))
    kinds: list[RuntimeKind] = ["ollama", "lmstudio", "openai_compatible"]
    urls = [ollama_url, lmstudio_url, base_url or ""]
    results: list[RuntimeDetection] = []
    with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="ai-detect") as pool:
        futures = [pool.submit(job) for job in jobs]
        for i, future in enumerate(futures):
            try:
                results.append(future.result())
            except Exception:  # noqa: BLE001 - belt and braces: the probes already catch
                logger.exception("Runtime detection failed")
                results.append(RuntimeDetection(runtime=kinds[i], base_url=urls[i],
                                                status="error", error=ERR_INTERNAL))
    return RuntimeReport(runtimes=results)
