"""Model pull through Ollama (plan F8.2): mocked loopback Ollama, consent, cancel, resume."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from src.ai.catalogue import load_catalogue
from src.ai.endpoint import ALLOW_REMOTE_ENV
from src.ai.runtime import ollama
from src.ai.runtime.jobs import JobCancelled, JobFailed, JobRegistry
from src.api.routes import ai as ai_routes

LAYER = 1_000_000
DIGEST = "sha256:" + "a" * 64


class _FakeOllama:
    """Streams ``/api/pull`` progress in ``steps`` chunks; remembers bytes already "on disk"
    so a second pull of the same model resumes from there, like Ollama."""

    def __init__(self) -> None:
        self.downloaded: dict[str, int] = {}
        self.requests: list[dict] = []
        self.mode = "ok"  # ok | error_line | http_500 | no_success
        self.delay = 0.0
        self.steps = 10
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                fake.requests.append({"path": self.path, **body})
                if fake.mode == "http_500":
                    payload = b'{"error":"boom"}'
                    self.send_response(500)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                try:
                    fake.stream(self, body["model"])
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _line(self, handler, item: dict) -> None:
        handler.wfile.write(json.dumps(item).encode() + b"\n")
        handler.wfile.flush()

    def stream(self, handler, model: str) -> None:
        self._line(handler, {"status": "pulling manifest"})
        if self.mode == "error_line":
            self._line(handler, {"error": "pull model manifest: file does not exist"})
            return
        done = self.downloaded.get(model, 0)
        step = LAYER // self.steps
        while True:
            self._line(handler, {"status": f"pulling {DIGEST[7:19]}", "digest": DIGEST,
                                 "total": LAYER, "completed": done})
            if done >= LAYER:
                break
            time.sleep(self.delay)
            done = min(LAYER, done + step)
            self.downloaded[model] = done
        if self.mode == "no_success":
            return
        for status in ("verifying sha256 digest", "writing manifest", "success"):
            self._line(handler, {"status": status})

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake_ollama(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    fake = _FakeOllama()
    monkeypatch.setattr(ollama, "OLLAMA_URL", fake.url)
    yield fake
    fake.close()


@pytest.fixture()
def client(isolated_app, monkeypatch, fake_ollama):
    monkeypatch.setattr(ai_routes, "PULL_JOBS", JobRegistry("pull"))
    monkeypatch.setattr(ollama, "free_disk_bytes", lambda _path: 10**15)
    return TestClient(isolated_app)


def _small():
    return load_catalogue().default("small")


# --- client ------------------------------------------------------------------------------


def test_pull_streams_progress_until_success(fake_ollama):
    seen: list[tuple[int, int]] = []
    progress = ollama.pull("m:1", on_progress=lambda p: seen.append(
        (p.completed_bytes, p.total_bytes)))
    assert progress.status == "success"
    assert (progress.completed_bytes, progress.total_bytes) == (LAYER, LAYER)
    assert seen[1] == (0, LAYER) and seen[-1] == (LAYER, LAYER)
    assert fake_ollama.requests == [{"path": "/api/pull", "model": "m:1", "stream": True}]


def test_pull_reports_an_error_line(fake_ollama):
    fake_ollama.mode = "error_line"
    with pytest.raises(JobFailed) as exc:
        ollama.pull("missing:1")
    assert exc.value.code == "ollama_error"
    assert "does not exist" in exc.value.params["message"]


def test_pull_reports_http_errors(fake_ollama):
    fake_ollama.mode = "http_500"
    with pytest.raises(JobFailed) as exc:
        ollama.pull("m:1")
    assert exc.value.code == "http_status"
    assert exc.value.params == {"http_status": 500, "message": "boom"}


def test_pull_needs_a_success_line(fake_ollama):
    fake_ollama.mode = "no_success"
    with pytest.raises(JobFailed) as exc:
        ollama.pull("m:1")
    assert exc.value.code == "malformed_response"


def test_pull_unreachable(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    with pytest.raises(JobFailed) as exc:
        ollama.pull("m:1", base_url="http://127.0.0.1:9", timeout=(0.5, 0.5))
    assert exc.value.code in ("unreachable", "timeout")


def test_pull_refuses_a_remote_ollama_without_sending(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    sent = []
    monkeypatch.setattr(ollama, "_post", lambda *a, **k: sent.append(a))
    with pytest.raises(JobFailed) as exc:
        ollama.pull("m:1", base_url="http://192.168.1.20:11434")
    assert exc.value.code == "endpoint_not_local" and sent == []


def test_cancel_then_resume_continues_from_the_partial_download(fake_ollama):
    fake_ollama.delay, fake_ollama.steps = 0.01, 50
    stop = {"now": False}

    def on_progress(p):
        if p.completed_bytes >= LAYER // 2:
            stop["now"] = True

    with pytest.raises(JobCancelled):
        ollama.pull("m:1", on_progress=on_progress, cancelled=lambda: stop["now"])
    time.sleep(0.2)  # the server notices the closed connection at its next write
    partial = fake_ollama.downloaded["m:1"]
    assert LAYER // 2 <= partial < LAYER

    first: list[int] = []
    progress = ollama.pull("m:1", on_progress=lambda p: first.append(p.completed_bytes))
    assert progress.status == "success"
    assert first[1] >= LAYER // 2  # the restarted pull starts where the first one stopped


# --- routes ------------------------------------------------------------------------------


def test_pull_route_requires_the_displayed_size(client, fake_ollama):
    small = _small()
    resp = client.post("/api/ai/models/pull",
                       json={"model": small.ollama_tag, "confirm_size_bytes": 1})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"error": "size_mismatch", "expected": small.download_bytes}
    assert client.post("/api/ai/models/pull", json={"model": small.ollama_tag}).status_code == 422
    assert fake_ollama.requests == []


def test_pull_route_only_pulls_catalogue_models(client, fake_ollama):
    resp = client.post("/api/ai/models/pull", json={"model": "random:7b",
                                                    "confirm_size_bytes": 123})
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "model_not_in_catalogue"
    assert fake_ollama.requests == []


def test_pull_route_refuses_a_remote_base_url(client, fake_ollama):
    small = _small()
    resp = client.post("/api/ai/models/pull", json={
        "model": small.ollama_tag, "confirm_size_bytes": small.download_bytes,
        "base_url": "https://ollama.example.com"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "endpoint_not_local"
    assert fake_ollama.requests == []


def test_pull_route_job_progress_and_status(client, fake_ollama):
    small = _small()
    resp = client.post("/api/ai/models/pull", json={
        "model": small.ollama_tag, "confirm_size_bytes": small.download_bytes})
    assert resp.status_code == 202
    job = resp.json()
    assert job["status"] == "running" and job["total_bytes"] == small.download_bytes
    ai_routes.PULL_JOBS.wait(job["id"], 5)
    status = client.get(f"/api/ai/models/pull/{job['id']}").json()
    assert status["status"] == "succeeded" and status["status_text"] == "success"
    assert status["completed_bytes"] == LAYER
    assert fake_ollama.requests[0]["model"] == small.ollama_tag


def test_pull_route_cancel_and_restart_resumes(client, fake_ollama):
    fake_ollama.delay, fake_ollama.steps = 0.02, 50
    small = _small()
    body = {"model": small.ollama_tag, "confirm_size_bytes": small.download_bytes}
    job_id = client.post("/api/ai/models/pull", json=body).json()["id"]
    # A second click while it runs returns the same job instead of a parallel pull.
    assert client.post("/api/ai/models/pull", json=body).json()["id"] == job_id
    deadline = time.monotonic() + 5
    while (client.get(f"/api/ai/models/pull/{job_id}").json()["completed_bytes"] == 0
           and time.monotonic() < deadline):
        time.sleep(0.02)
    assert client.delete(f"/api/ai/models/pull/{job_id}").status_code == 200
    ai_routes.PULL_JOBS.wait(job_id, 5)
    cancelled = client.get(f"/api/ai/models/pull/{job_id}").json()
    assert cancelled["status"] == "cancelled" and 0 < cancelled["completed_bytes"] < LAYER

    fake_ollama.delay = 0.0
    time.sleep(0.2)
    resumed_from = fake_ollama.downloaded[small.ollama_tag]
    new_id = client.post("/api/ai/models/pull", json=body).json()["id"]
    assert new_id != job_id
    ai_routes.PULL_JOBS.wait(new_id, 5)
    done = client.get(f"/api/ai/models/pull/{new_id}").json()
    assert done["status"] == "succeeded" and done["completed_bytes"] == LAYER
    assert resumed_from > 0


def test_pull_route_reports_ollama_errors(client, fake_ollama):
    fake_ollama.mode = "error_line"
    small = _small()
    job_id = client.post("/api/ai/models/pull", json={
        "model": small.ollama_tag, "confirm_size_bytes": small.download_bytes}).json()["id"]
    ai_routes.PULL_JOBS.wait(job_id, 5)
    status = client.get(f"/api/ai/models/pull/{job_id}").json()
    assert (status["status"], status["error"]) == ("failed", "ollama_error")


def test_pull_status_unknown_job(client):
    assert client.get("/api/ai/models/pull/nope").status_code == 404
    assert client.delete("/api/ai/models/pull/nope").status_code == 404


def test_pull_route_refuses_when_the_models_drive_is_too_full(client, fake_ollama, monkeypatch):
    small = _small()
    monkeypatch.setattr(ollama, "free_disk_bytes", lambda _path: small.download_bytes)
    resp = client.post("/api/ai/models/pull", json={
        "model": small.ollama_tag, "confirm_size_bytes": small.download_bytes})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "disk_space_low" and detail["free_bytes"] == small.download_bytes
    assert detail["needed_bytes"] > small.download_bytes
    assert fake_ollama.requests == []


def test_pull_route_one_job_per_server_whatever_the_loopback_spelling(client, fake_ollama):
    fake_ollama.delay, fake_ollama.steps = 0.05, 50
    small = _small()
    port = fake_ollama.url.rsplit(":", 1)[1]
    body = {"model": small.ollama_tag, "confirm_size_bytes": small.download_bytes}
    first = client.post("/api/ai/models/pull",
                        json={**body, "base_url": f"http://127.0.0.1:{port}"}).json()["id"]
    second = client.post("/api/ai/models/pull",
                         json={**body, "base_url": f"http://localhost:{port}/"}).json()["id"]
    assert first == second
    client.delete(f"/api/ai/models/pull/{first}")
    ai_routes.PULL_JOBS.wait(first, 5)


def test_endpoint_key_and_models_dir(tmp_path):
    assert (ollama.endpoint_key("http://127.0.0.1:11434") == ollama.endpoint_key(
        "http://localhost:11434") == ollama.endpoint_key("http://[::1]:11434")
        == "loopback:11434")
    assert ollama.endpoint_key("http://127.0.0.1:8080") == "loopback:8080"
    assert ollama.models_dir({"OLLAMA_MODELS": str(tmp_path)}) == tmp_path
    assert ollama.models_dir({"USERPROFILE": str(tmp_path)}) == tmp_path / ".ollama" / "models"
    assert ollama.free_disk_bytes(tmp_path / "not" / "yet") is not None
