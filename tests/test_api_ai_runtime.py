"""GET /api/ai/runtimes and /api/ai/hardware (plan F8.1)."""

from __future__ import annotations

from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from src.ai.endpoint import ALLOW_REMOTE_ENV
from src.ai.runtime import detect
from src.ai.runtime.hardware import GpuInfo, HardwareProbe
from src.api.routes import ai as ai_routes


@pytest.fixture()
def client(isolated_app, monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)
    monkeypatch.setattr(detect, "find_ollama", lambda: None)
    monkeypatch.setattr(detect, "find_lmstudio", lambda: None)
    return TestClient(isolated_app)


def test_runtimes_route_reports_codes(client, monkeypatch):
    def _refused(*a, **k):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "get", _refused)
    resp = client.get("/api/ai/runtimes")
    assert resp.status_code == 200
    body = resp.json()
    assert [(r["runtime"], r["status"], r["error"], r["reachable"]) for r in body["runtimes"]] == [
        ("ollama", "not_found", "unreachable", False),
        ("lmstudio", "not_found", "unreachable", False),
    ]
    assert body["runtimes"][0]["base_url"] == "http://127.0.0.1:11434"


def test_runtimes_route_probes_a_local_base_url(client, monkeypatch):
    seen = []

    class _Resp:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_content(self, size):
            yield b'{"data": [{"id": "local-model"}]}'

    def _fake_get(self, url, **kwargs):
        seen.append(url)
        if url == "http://127.0.0.1:8080/v1/models":
            return _Resp()
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    resp = client.get("/api/ai/runtimes", params={"base_url": "http://127.0.0.1:8080/v1"})
    custom = resp.json()["runtimes"][2]
    assert custom["runtime"] == "openai_compatible" and custom["status"] == "running"
    assert custom["models"][0]["name"] == "local-model"
    assert "http://127.0.0.1:8080/v1/models" in seen


def test_runtimes_route_refuses_a_remote_base_url(client, monkeypatch):
    seen = []

    def _fake_get(self, url, **kwargs):
        seen.append(url)
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    resp = client.get("/api/ai/runtimes", params={"base_url": "https://api.openai.com/v1"})
    assert resp.status_code == 200
    custom = resp.json()["runtimes"][2]
    assert (custom["status"], custom["error"]) == ("error", "endpoint_not_local")
    assert not any("openai.com" in url for url in seen)


def test_runtimes_route_rejects_an_empty_base_url(client):
    assert client.get("/api/ai/runtimes", params={"base_url": ""}).status_code == 422


def test_hardware_route_probes_the_app_data_dir(client, isolated_app, monkeypatch):
    seen = []
    probe = HardwareProbe(platform="win32", cpu_logical_cores=8, avx2=True,
                          gpus=[GpuInfo(name="RTX", vendor="nvidia", vram_bytes=8 * 1024**3,
                                        source="nvidia-smi")])

    def _fake_probe(data_dir):
        seen.append(Path(data_dir))
        return probe

    monkeypatch.setattr(ai_routes, "probe_hardware", _fake_probe)
    resp = client.get("/api/ai/hardware")
    assert resp.status_code == 200
    assert resp.json()["gpus"][0]["vram_bytes"] == 8 * 1024**3
    assert resp.json()["ram_total_bytes"] is None
    assert seen == [Path(isolated_app.state.db_path).parent]
