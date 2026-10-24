"""Local AI runtime detection (plan F8.1): mocked loopback servers, never a real runtime."""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from src.ai.endpoint import ALLOW_REMOTE_ENV
from src.ai.runtime import detect
from src.ai.runtime.detect import (
    detect_runtimes,
    find_lmstudio,
    find_ollama,
    probe_lmstudio,
    probe_ollama,
    probe_openai_compatible,
)

OLLAMA_TAGS = {"models": [
    {"name": "qwen2.5:7b", "model": "qwen2.5:7b", "size": 4683087332,
     "details": {"format": "gguf", "family": "qwen2", "parameter_size": "7.6B",
                 "quantization_level": "Q4_K_M"}},
    {"name": "llama3.2:3b", "size": 2019393189,
     "details": {"parameter_size": "3.2B", "quantization_level": "Q4_K_M"}},
    {"no_name": True},
]}
OLLAMA_PS = {"models": [{"name": "qwen2.5:7b", "size": 6000000000, "size_vram": 6000000000}]}
OPENAI_MODELS = {"object": "list", "data": [{"id": "qwen2.5-7b-instruct", "object": "model"},
                                            {"id": "text-embedding-nomic", "object": "model"}]}


@pytest.fixture(autouse=True)
def _no_opt_in(monkeypatch):
    monkeypatch.delenv(ALLOW_REMOTE_ENV, raising=False)


@pytest.fixture(autouse=True)
def _no_real_install_lookup(monkeypatch):
    """Keep detection from reporting this machine's real Ollama / LM Studio installs."""
    monkeypatch.setattr(detect, "find_ollama", lambda: None)
    monkeypatch.setattr(detect, "find_lmstudio", lambda: None)


class _Server:
    """A loopback HTTP server answering ``routes[path] = (status, body, delay_s)``."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, float]] = {}
        self.hits: list[str] = []
        routes, hits = self.routes, self.hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                hits.append(self.path)
                status, body, delay = routes.get(self.path, (404, b"{}", 0.0))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    if status in (301, 302):
                        self.send_header("Location", "http://192.0.2.1/models")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up (timeout test)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def json(self, path: str, payload: object, status: int = 200, delay: float = 0.0) -> None:
        self.routes[path] = (status, json.dumps(payload).encode(), delay)

    def raw(self, path: str, body: bytes, status: int = 200, delay: float = 0.0) -> None:
        self.routes[path] = (status, body, delay)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)


@pytest.fixture()
def server():
    srv = _Server()
    yield srv
    srv.close()


@pytest.fixture()
def closed_url():
    """A loopback URL with nothing listening."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_ollama_running_lists_models_with_size_quantization_and_loaded(server):
    server.json("/api/tags", OLLAMA_TAGS)
    server.json("/api/ps", OLLAMA_PS)
    result = probe_ollama(server.url, executable="C:/x/ollama.exe")
    assert result.status == "running" and result.reachable and result.error is None
    assert result.installed is True and result.executable == "C:/x/ollama.exe"
    assert [m.model_dump() for m in result.models] == [
        {"name": "qwen2.5:7b", "size_bytes": 4683087332, "quantization": "Q4_K_M",
         "parameter_size": "7.6B", "loaded": True},
        {"name": "llama3.2:3b", "size_bytes": 2019393189, "quantization": "Q4_K_M",
         "parameter_size": "3.2B", "loaded": False},
    ]


def test_ollama_without_ps_keeps_loaded_unknown(server):
    server.json("/api/tags", OLLAMA_TAGS)
    result = probe_ollama(server.url)
    assert result.status == "running"
    assert all(m.loaded is None for m in result.models)


def test_lmstudio_running_lists_models(server):
    server.json("/v1/models", OPENAI_MODELS)
    result = probe_lmstudio(server.url)
    assert result.runtime == "lmstudio" and result.status == "running"
    assert result.base_url == server.url and result.installed is False
    assert [m.name for m in result.models] == ["qwen2.5-7b-instruct", "text-embedding-nomic"]
    assert result.models[0].size_bytes is None and result.models[0].quantization is None


def test_openai_compatible_base_url_appends_models(server):
    server.json("/v1/models", OPENAI_MODELS)
    result = probe_openai_compatible(f"{server.url}/v1/")
    assert result.status == "running" and result.installed is None
    assert server.hits == ["/v1/models"]
    assert len(result.models) == 2


def test_unreachable_runtime_installed_vs_not_found(closed_url):
    installed = probe_ollama(closed_url, executable="/usr/bin/ollama")
    assert (installed.status, installed.error, installed.reachable) == ("installed", "unreachable", False)
    missing = probe_lmstudio(closed_url)
    assert (missing.status, missing.error) == ("not_found", "unreachable")
    custom = probe_openai_compatible(closed_url)
    assert (custom.status, custom.error) == ("not_found", "unreachable")


def test_connect_timeout_counts_as_nothing_listening(monkeypatch):
    def _connect_timeout(*a, **k):
        raise requests.ConnectTimeout("SYN retried")

    monkeypatch.setattr(requests.Session, "get", _connect_timeout)
    result = probe_ollama("http://127.0.0.1:11434", executable="ollama")
    assert (result.status, result.error) == ("installed", "unreachable")


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "http://192.168.1.20:1234/v1",
                                 "http://127.0.0.1.nip.io:1234/v1", "http://10.0.0.5:11434"])
def test_non_loopback_base_url_is_refused_without_a_request(url, monkeypatch):
    def _no_request(*a, **k):
        raise AssertionError("a request was attempted")

    monkeypatch.setattr(requests.Session, "get", _no_request)
    result = probe_openai_compatible(url)
    assert (result.status, result.error, result.reachable) == ("error", "endpoint_not_local", False)


@pytest.mark.parametrize("url", ["not a url", "ftp://127.0.0.1/v1", "http://user:pw@127.0.0.1:1/v1",
                                 "http://127.0.0.1:1/v1?x=1", "http://127.0.0.1:99999/v1", "http:///v1"])
def test_invalid_base_url_is_refused(url):
    result = probe_openai_compatible(url)
    assert (result.status, result.error) == ("error", "invalid_url")


@pytest.mark.parametrize("body", [b"not json", b"\xff\xfe", b"[]", b'{"models": "x"}', b'{"data": 3}'])
def test_malformed_responses(server, body):
    server.raw("/api/tags", body)
    server.raw("/v1/models", body)
    for result in (probe_ollama(server.url), probe_openai_compatible(f"{server.url}/v1")):
        assert (result.status, result.error, result.reachable) == ("error", "malformed_response", False)
        assert result.models == []


def test_http_error_status_is_reported(server):
    server.json("/api/tags", {"error": "boom"}, status=500)
    result = probe_ollama(server.url)
    assert (result.status, result.error, result.http_status) == ("error", "http_status", 500)


def test_redirects_are_not_followed(server):
    server.raw("/v1/models", b"", status=302)
    result = probe_openai_compatible(f"{server.url}/v1")
    assert (result.error, result.http_status) == ("http_status", 302)
    assert server.hits == ["/v1/models"]


def test_read_timeout_is_reported_quickly(server):
    server.json("/api/tags", OLLAMA_TAGS, delay=1.5)
    started = time.monotonic()
    result = probe_ollama(server.url, timeout=(0.5, 0.3))
    assert time.monotonic() - started < 1.4
    assert (result.status, result.error) == ("error", "timeout")


def test_oversized_response_is_cut(server, monkeypatch):
    monkeypatch.setattr(detect, "MAX_RESPONSE_BYTES", 100)
    server.json("/v1/models", {"data": [{"id": "m" * 500}]})
    result = probe_openai_compatible(f"{server.url}/v1")
    assert result.error == "response_too_large"


def test_remote_opt_in_uses_the_existing_guard(monkeypatch):
    """FYJ_ALLOW_REMOTE_LLM=1 is the existing, explicit opt-in: the guard is reused, not redone."""
    monkeypatch.setenv(ALLOW_REMOTE_ENV, "1")
    seen = []

    def _fake_get(self, url, **kwargs):
        seen.append((url, kwargs["allow_redirects"], self.trust_env))
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests.Session, "get", _fake_get)
    result = probe_openai_compatible("http://192.168.1.20:1234/v1")
    assert result.error == "unreachable"
    assert seen == [("http://192.168.1.20:1234/v1/models", False, False)]


def test_detect_runtimes_never_raises(monkeypatch, closed_url):
    def _boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(detect, "_get_json", _boom)
    monkeypatch.setattr(detect, "find_ollama", _boom)
    report = detect_runtimes("http://127.0.0.1:1/v1", ollama_url=closed_url, lmstudio_url=closed_url)
    assert [(r.runtime, r.status, r.error) for r in report.runtimes] == [
        ("ollama", "error", "internal_error"),
        ("lmstudio", "error", "internal_error"),
        ("openai_compatible", "error", "internal_error"),
    ]


def test_detect_runtimes_reports_all_three(server, closed_url, monkeypatch):
    monkeypatch.setattr(detect, "find_lmstudio", lambda: "/home/u/.lmstudio/bin/lms")
    server.json("/api/tags", OLLAMA_TAGS)
    server.json("/v1/models", OPENAI_MODELS)
    report = detect_runtimes(f"{server.url}/v1", ollama_url=server.url, lmstudio_url=closed_url)
    by_kind = {r.runtime: r for r in report.runtimes}
    assert by_kind["ollama"].status == "running" and len(by_kind["ollama"].models) == 2
    assert by_kind["lmstudio"].status == "installed"
    assert by_kind["lmstudio"].executable == "/home/u/.lmstudio/bin/lms"
    assert by_kind["openai_compatible"].status == "running"


def test_detect_runtimes_without_base_url_probes_two(closed_url):
    report = detect_runtimes(ollama_url=closed_url, lmstudio_url=closed_url)
    assert [r.runtime for r in report.runtimes] == ["ollama", "lmstudio"]
    assert all(r.status == "not_found" for r in report.runtimes)


# --- installed-but-not-running ------------------------------------------------------


@pytest.fixture()
def no_path_lookup(monkeypatch):
    monkeypatch.setattr(detect.shutil, "which", lambda name: None)


def test_find_ollama_windows_standard_path(tmp_path, no_path_lookup):
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert find_ollama({"LOCALAPPDATA": str(tmp_path)}, "win32") == str(exe)
    assert find_ollama({"LOCALAPPDATA": str(tmp_path / "none")}, "win32") is None


def test_find_ollama_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(detect.shutil, "which", lambda name: f"/opt/bin/{name}")
    assert find_ollama({}, "win32") == "/opt/bin/ollama"


def test_find_lmstudio_prefers_the_lms_cli(tmp_path, no_path_lookup):
    lms = tmp_path / ".lmstudio" / "bin" / "lms.exe"
    lms.parent.mkdir(parents=True)
    lms.write_bytes(b"")
    app = tmp_path / "Programs" / "LM Studio" / "LM Studio.exe"
    app.parent.mkdir(parents=True)
    app.write_bytes(b"")
    env = {"USERPROFILE": str(tmp_path), "LOCALAPPDATA": str(tmp_path)}
    assert find_lmstudio(env, "win32") == str(lms)
    lms.unlink()
    assert find_lmstudio(env, "win32") == str(app)
    app.unlink()
    assert find_lmstudio(env, "win32") is None


def test_find_lmstudio_linux_cli(tmp_path, no_path_lookup):
    lms = tmp_path / ".lmstudio" / "bin" / "lms"
    lms.parent.mkdir(parents=True)
    lms.write_bytes(b"")
    assert find_lmstudio({"HOME": str(tmp_path)}, "linux") == str(lms)
