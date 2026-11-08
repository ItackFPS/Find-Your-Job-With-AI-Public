"""M3: the embedding model is downloaded on request, visibly, safely. No network in these tests."""

from __future__ import annotations

import errno
import hashlib
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src.embeddings.model_download import ModelDownloader, ModelFile


class Hub:
    """A fake Hugging Face cache."""

    def __init__(self, root: Path, content: bytes = b"model-bytes") -> None:
        self.root, self.content = root, content
        self.fail: BaseException | None = None
        self.remote_content = content
        self.calls: list[str] = []
        self.gate: threading.Event | None = None
        self.partial = 0

    @property
    def file(self) -> ModelFile:
        return ModelFile("onnx/model.onnx", len(self.content), hashlib.sha256(self.content).hexdigest())

    def path(self) -> Path:
        return self.root / "model.onnx"

    def fetch(self, filename: str, local_only: bool) -> str:
        if local_only:
            if not self.path().exists():
                raise FileNotFoundError(filename)
            return str(self.path())
        self.calls.append(filename)
        if self.gate:
            self.gate.wait(5)
        if self.fail:
            raise self.fail
        self.path().write_bytes(self.remote_content)
        return str(self.path())

    def downloader(self) -> ModelDownloader:
        return ModelDownloader(fetch=self.fetch, partial_bytes=lambda: self.partial, files=(self.file,))


@pytest.fixture
def hub(tmp_path: Path) -> Hub:
    return Hub(tmp_path)


def _run(d: ModelDownloader) -> dict:
    d.start()
    d.join(5)
    return d.status()


def test_missing_before_any_download(hub: Hub):
    status = hub.downloader().status()
    assert status["state"] == "missing" and status["total_bytes"] == len(hub.content) and hub.calls == []


def test_a_verified_download_is_ready(hub: Hub):
    assert _run(hub.downloader())["state"] == "ready"
    assert hub.calls == ["onnx/model.onnx"]


def test_a_model_already_cached_is_ready_without_a_download(hub: Hub):
    hub.path().write_bytes(hub.content)
    d = hub.downloader()
    assert d.start()["state"] == "ready" and hub.calls == []


def test_a_truncated_cached_file_is_not_ready(hub: Hub):
    hub.path().write_bytes(hub.content[:-1])
    assert hub.downloader().status()["state"] == "missing"


def test_progress_is_reported_while_downloading(hub: Hub):
    hub.gate = threading.Event()
    hub.partial = 4
    d = hub.downloader()
    status = d.start()
    assert (status["state"], status["downloaded_bytes"]) == ("downloading", 4)
    hub.gate.set()
    d.join(5)


def test_only_one_download_runs_at_a_time(hub: Hub):
    hub.gate = threading.Event()
    d = hub.downloader()
    d.start()
    d.start()
    hub.gate.set()
    d.join(5)
    assert len(hub.calls) == 1


def test_a_corrupt_file_is_deleted_and_reported(hub: Hub):
    hub.remote_content = b"tampered-bytes"
    status = _run(hub.downloader())
    assert (status["state"], status["error"]) == ("failed", "corrupt")
    assert not hub.path().exists()


@pytest.mark.parametrize("exc,code", [
    (httpx.ConnectError("no route"), "offline"),
    (ConnectionError("reset"), "offline"),
    (TimeoutError(), "offline"),
    (OSError(errno.ENOSPC, "No space left on device"), "disk_full"),
    (RuntimeError("boom"), "failed"),
])
def test_a_failure_gives_a_code_never_a_raw_message(hub: Hub, exc: BaseException, code: str):
    hub.fail = exc
    status = _run(hub.downloader())
    assert (status["state"], status["error"]) == ("failed", code)


def test_retry_after_a_failure_succeeds(hub: Hub):
    hub.fail = httpx.ConnectError("no route")
    d = hub.downloader()
    assert _run(d)["state"] == "failed"
    hub.fail = None
    assert _run(d)["state"] == "ready"


def test_the_pinned_files_are_the_reviewed_revision():
    from src.embeddings.model_download import FILES, SPEC, TOTAL_BYTES

    assert SPEC.revision == "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
    assert {f.filename for f in FILES} == {"tokenizer.json", SPEC.onnx_file}
    assert TOTAL_BYTES > 470_000_000


def test_the_api_reports_and_starts(hub: Hub, isolated_app, monkeypatch):
    d = hub.downloader()
    monkeypatch.setitem(isolated_app.state._state, "model_downloader", d)
    client = TestClient(isolated_app)
    assert client.get("/api/embedding-model").json()["state"] == "missing"
    assert client.post("/api/embedding-model/download").status_code == 202
    d.join(5)
    assert client.get("/api/embedding-model").json()["state"] == "ready"
