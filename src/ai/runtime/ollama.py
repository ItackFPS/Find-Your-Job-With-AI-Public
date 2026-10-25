"""Minimal Ollama HTTP client for setup: pull a model (streamed) and run one chat call.

Every call goes through the local-first guard (:func:`src.ai.endpoint.require_local_endpoint`)
first: a non-loopback ``base_url`` raises :class:`~src.ai.runtime.jobs.JobFailed` with
``endpoint_not_local`` and nothing is sent. Redirects are not followed and proxy settings
from the environment are ignored, so a request cannot leave the machine.

Resume: Ollama keeps the layers it already downloaded, so re-issuing ``POST /api/pull`` for
the same model continues where a cancelled or interrupted pull stopped.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from src.ai.endpoint import RemoteLLMRefused, is_loopback_endpoint, require_local_endpoint
from src.ai.runtime.detect import OLLAMA_URL
from src.ai.runtime.jobs import JobCancelled, JobFailed

logger = logging.getLogger(__name__)

PULL_TIMEOUT_S = (2.0, 120.0)  # connect, then max silence between progress lines
MAX_LINE_BYTES = 64 * 1024

ERR_NOT_LOCAL = "endpoint_not_local"
ERR_UNREACHABLE = "unreachable"
ERR_TIMEOUT = "timeout"
ERR_HTTP_STATUS = "http_status"
ERR_MALFORMED = "malformed_response"
ERR_OLLAMA = "ollama_error"


def checked_base(base_url: str | None) -> str:
    base = (base_url or OLLAMA_URL).strip().rstrip("/")
    try:
        require_local_endpoint(base)
    except RemoteLLMRefused as exc:
        raise JobFailed(ERR_NOT_LOCAL) from exc
    return base


def endpoint_key(base: str) -> str:
    """One key per server: ``localhost``, ``127.0.0.1`` and ``[::1]`` on a port are the same."""
    parts = urlsplit(base)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = (parts.hostname or "").lower()
    return f"loopback:{port}" if is_loopback_endpoint(base) else f"{host}:{port}"


def models_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where Ollama stores models: ``OLLAMA_MODELS``, else ``~/.ollama/models``."""
    env = os.environ if env is None else env
    if custom := env.get("OLLAMA_MODELS", "").strip():
        return Path(custom)
    home = env.get("USERPROFILE") or env.get("HOME")
    return (Path(home) if home else Path.home()) / ".ollama" / "models"


def free_disk_bytes(path: Path) -> int | None:
    """Free bytes on the drive holding ``path`` (its nearest existing parent), or ``None``."""
    for candidate in (path, *path.parents):
        try:
            if candidate.exists():
                return shutil.disk_usage(candidate).free
        except OSError:
            return None
    return None


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False  # no proxy from the environment: stay on this machine
    return session


def _post(session: requests.Session, url: str, payload: dict[str, Any],
          timeout: tuple[float, float], stream: bool) -> requests.Response:
    try:
        resp = session.post(url, json=payload, timeout=timeout, allow_redirects=False,
                            stream=stream)
    except requests.ConnectTimeout as exc:
        raise JobFailed(ERR_UNREACHABLE) from exc
    except requests.Timeout as exc:
        raise JobFailed(ERR_TIMEOUT) from exc
    except requests.RequestException as exc:
        raise JobFailed(ERR_UNREACHABLE) from exc
    if resp.status_code != 200:
        message = _error_message(resp)
        resp.close()
        raise JobFailed(ERR_HTTP_STATUS, http_status=resp.status_code, message=message)
    return resp


def _error_message(resp: requests.Response) -> str | None:
    try:
        data = json.loads(resp.content[:MAX_LINE_BYTES].decode("utf-8", "replace"))
    except (ValueError, requests.RequestException):
        return None
    error = data.get("error") if isinstance(data, dict) else None
    return error[:500] if isinstance(error, str) else None


def _lines(resp: requests.Response, cancelled: Callable[[], bool]) -> Iterator[dict[str, Any]]:
    try:
        for raw in resp.iter_lines(chunk_size=1024):
            if cancelled():
                raise JobCancelled()
            if not raw:
                continue
            if len(raw) > MAX_LINE_BYTES:
                raise JobFailed(ERR_MALFORMED)
            try:
                item = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise JobFailed(ERR_MALFORMED) from exc
            if not isinstance(item, dict):
                raise JobFailed(ERR_MALFORMED)
            yield item
    except requests.Timeout as exc:
        raise JobFailed(ERR_TIMEOUT) from exc
    except requests.RequestException as exc:
        raise JobFailed(ERR_UNREACHABLE) from exc


class PullProgress:
    """Aggregates Ollama's per-layer progress lines into bytes completed / total."""

    def __init__(self) -> None:
        self.layers: dict[str, tuple[int, int]] = {}
        self.status: str | None = None

    def feed(self, item: dict[str, Any]) -> None:
        status = item.get("status")
        if isinstance(status, str):
            self.status = status[:200]
        digest, total, completed = item.get("digest"), item.get("total"), item.get("completed")
        if isinstance(digest, str) and isinstance(total, int) and total >= 0:
            done = completed if isinstance(completed, int) and completed >= 0 else 0
            self.layers[digest] = (min(done, total), total)

    @property
    def completed_bytes(self) -> int:
        return sum(done for done, _ in self.layers.values())

    @property
    def total_bytes(self) -> int:
        return sum(total for _, total in self.layers.values())


def pull(model: str, *, base_url: str | None = None,
         on_progress: Callable[[PullProgress], None] = lambda _p: None,
         cancelled: Callable[[], bool] = lambda: False,
         timeout: tuple[float, float] = PULL_TIMEOUT_S) -> PullProgress:
    """``POST /api/pull`` with ``stream: true`` until ``success``; raises on error or cancel.

    Cancelling closes the connection: Ollama stops downloading and keeps the partial layers.
    """
    base = checked_base(base_url)
    progress = PullProgress()
    session = _session()
    try:
        resp = _post(session, f"{base}/api/pull", {"model": model, "stream": True},
                     timeout, stream=True)
        with resp:
            for item in _lines(resp, cancelled):
                if isinstance(item.get("error"), str):
                    raise JobFailed(ERR_OLLAMA, message=item["error"][:500])
                progress.feed(item)
                on_progress(progress)
                if progress.status == "success":
                    return progress
        if cancelled():
            raise JobCancelled()
        raise JobFailed(ERR_MALFORMED, message="stream ended without success")
    finally:
        session.close()


def chat(model: str, messages: list[dict[str, str]], *, base_url: str | None = None,
         format_schema: dict[str, Any] | None = None, think: bool | str | None = False,
         options: dict[str, Any] | None = None,
         timeout: tuple[float, float] = (2.0, 600.0)) -> dict[str, Any]:
    """One non-streamed ``POST /api/chat``; returns Ollama's JSON reply (with its timings)."""
    base = checked_base(base_url)
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False,
                               "options": options or {}}
    if format_schema is not None:
        payload["format"] = format_schema
    if think is not None:
        payload["think"] = think
    session = _session()
    try:
        resp = _post(session, f"{base}/api/chat", payload, timeout, stream=False)
        try:
            data = resp.json()
        except ValueError as exc:
            raise JobFailed(ERR_MALFORMED) from exc
        if not isinstance(data, dict):
            raise JobFailed(ERR_MALFORMED)
        if isinstance(data.get("error"), str):
            raise JobFailed(ERR_OLLAMA, message=data["error"][:500])
        return data
    finally:
        session.close()
