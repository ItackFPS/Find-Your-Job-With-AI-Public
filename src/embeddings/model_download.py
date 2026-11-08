"""Download the embedding model on request, with progress, integrity check and retry (M3).

Before this, the first pipeline run downloaded about 470 MB with no sign of it. The pipeline still
fetches the model if it has to, and fails open to the deterministic ranking if it cannot; this
module only lets the user do it first and see it happen.

Exactly one download runs at a time. Progress comes from the partial files Hugging Face writes in
its cache, so nothing here reimplements the transfer.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.embeddings.onnx_encoder import SUPPORTED_MODELS, OnnxModelSpec

logger = logging.getLogger(__name__)

SPEC: OnnxModelSpec = SUPPORTED_MODELS["paraphrase-multilingual-MiniLM-L12-v2"]


@dataclass(frozen=True)
class ModelFile:
    filename: str
    size: int
    sha256: str


# The pinned revision's files. A file that does not match is deleted, never used.
FILES: tuple[ModelFile, ...] = (
    ModelFile("tokenizer.json", 9_081_518, "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8"),
    ModelFile(SPEC.onnx_file, 470_301_610, "10f7a088420252b26caf819236ca2c9d2987afd0fc06fec7553b542a5655a05a"),
)
TOTAL_BYTES = sum(f.size for f in FILES)


def _default_fetch(filename: str, local_only: bool) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(SPEC.repo_id, filename, revision=SPEC.revision, local_files_only=local_only)


def _default_partial_bytes() -> int:
    from huggingface_hub import constants

    blobs = Path(constants.HF_HUB_CACHE) / f"models--{SPEC.repo_id.replace('/', '--')}" / "blobs"
    try:
        return sum(p.stat().st_size for p in blobs.glob("*.incomplete"))
    except OSError:
        return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _error_code(exc: BaseException) -> str:
    """A stable code the interface translates. Never a raw exception message."""
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "disk_full"
    try:
        import httpx
        from huggingface_hub.errors import LocalEntryNotFoundError

        if isinstance(exc, LocalEntryNotFoundError | httpx.TransportError | ConnectionError | TimeoutError):
            return "offline"
    except ImportError:  # the dependency is missing: nothing was reachable anyway
        return "failed"
    return "failed"


class ModelDownloader:
    """The download's state. Dependencies are injectable so tests never touch the network."""

    def __init__(self, fetch: Callable[[str, bool], str] = _default_fetch,
                 partial_bytes: Callable[[], int] = _default_partial_bytes,
                 files: tuple[ModelFile, ...] = FILES) -> None:
        self._fetch = fetch
        self._partial_bytes = partial_bytes
        self._files = files
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._done_bytes = 0
        self._error: str | None = None

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self._files)

    def _cached(self, file: ModelFile) -> Path | None:
        """The local copy of ``file`` when it is complete. Hashing is done once, at download."""
        try:
            path = Path(self._fetch(file.filename, True))
            return path if path.stat().st_size == file.size else None
        except Exception:  # not cached, or a cache that cannot be read: same answer
            return None

    def is_ready(self) -> bool:
        return all(self._cached(f) is not None for f in self._files)

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            done, error = self._done_bytes, self._error
        total = self.total_bytes
        if running:
            return {"state": "downloading", "total_bytes": total, "error": None,
                    "downloaded_bytes": min(total, done + self._partial_bytes())}
        if self.is_ready():
            return {"state": "ready", "total_bytes": total, "downloaded_bytes": total, "error": None}
        return {"state": "failed" if error else "missing", "total_bytes": total,
                "downloaded_bytes": 0, "error": error}

    def start(self) -> dict[str, Any]:
        """Start the download unless it runs already or the model is here. Returns the status."""
        with self._lock:
            already = self._thread is not None and self._thread.is_alive()
        if not already and not self.is_ready():
            with self._lock:
                if self._thread is None or not self._thread.is_alive():
                    self._done_bytes, self._error = 0, None
                    self._thread = threading.Thread(target=self._run, name="embedding-download", daemon=True)
                    self._thread.start()
        return self.status()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        try:
            for file in self._files:
                path = Path(self._fetch(file.filename, False))
                if _sha256(path) != file.sha256:
                    self._discard(path)
                    raise ValueError(f"{file.filename} does not match its pinned hash")
                with self._lock:
                    self._done_bytes += file.size
        except ValueError as exc:
            logger.warning("Embedding model rejected: %s", exc)
            with self._lock:
                self._error = "corrupt"
        except Exception as exc:  # a failed download must never take the app down
            code = _error_code(exc)
            logger.warning("Embedding model download failed (%s): %s", code, type(exc).__name__)
            with self._lock:
                self._error = code

    @staticmethod
    def _discard(path: Path) -> None:
        for target in {path.resolve(), path}:
            try:
                os.unlink(target)
            except OSError:
                logger.warning("Could not remove the rejected file %s", target)
