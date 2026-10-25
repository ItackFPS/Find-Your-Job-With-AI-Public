"""Guided Ollama install (plan F8.2): official URL only, consent by size, verified, mocked.

Nothing here touches the network or launches anything: HTTP is a fake session and the
download / signature / launch steps are injected.
"""

from __future__ import annotations

import hashlib
import json
from functools import partial
from pathlib import Path

import pytest
import requests
from fastapi.testclient import TestClient

from src.ai.runtime import ollama_install
from src.ai.runtime.jobs import Job, JobFailed, JobRegistry
from src.ai.runtime.ollama_install import InstallerInfo, install_worker, resolve_installer
from src.api.routes import ai as ai_routes

GITHUB_DL = "https://github.com/ollama/ollama/releases/download/v9.9.9/OllamaSetup.exe"
ASSET_URL = "https://release-assets.githubusercontent.com/asset/123?sig=x"
PAYLOAD = b"MZ" + b"\x00" * 1000
PAYLOAD_SHA = hashlib.sha256(PAYLOAD).hexdigest()


class _Resp:
    def __init__(self, status: int, headers: dict[str, str] | None = None,
                 body: bytes = b"") -> None:
        self.status_code = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.content = body
        self.closed = False

    def iter_content(self, size: int):
        for i in range(0, len(self.content), 100):
            yield self.content[i:i + 100]

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class _Session:
    """Answers ``(method, url)`` from a table; records every request."""

    def __init__(self, table: dict[tuple[str, str], _Resp]) -> None:
        self.table = table
        self.calls: list[tuple[str, str, bool]] = []

    def request(self, method, url, allow_redirects=True, timeout=None, stream=False):
        self.calls.append((method, url, allow_redirects))
        if (method, url) not in self.table:
            raise requests.ConnectionError(url)
        return self.table[(method, url)]

    def close(self) -> None:
        pass


def _release_json(size: int, digest: str | None) -> bytes:
    asset = {"name": "OllamaSetup.exe", "size": size}
    if digest:
        asset["digest"] = digest
    return json.dumps({"tag_name": "v9.9.9", "assets": [
        {"name": "sha256sum.txt", "size": 10}, asset]}).encode()


def _chain(size: int = len(PAYLOAD), digest: str | None = f"sha256:{PAYLOAD_SHA}",
           final_status: int = 200) -> dict[tuple[str, str], _Resp]:
    api = ollama_install.RELEASE_API.format(version="v9.9.9")
    table = {}
    for method in ("HEAD", "GET"):
        table[(method, ollama_install.OFFICIAL_URL)] = _Resp(307, {"Location": GITHUB_DL})
        table[(method, GITHUB_DL)] = _Resp(302, {"Location": ASSET_URL})
        table[(method, ASSET_URL)] = _Resp(final_status, {"Content-Length": str(size)},
                                           PAYLOAD if method == "GET" else b"")
    table[("GET", api)] = _Resp(200, {}, _release_json(size, digest))
    return table


# --- resolve -----------------------------------------------------------------------------


def test_resolve_follows_allowed_redirects_and_reads_published_hash():
    session = _Session(_chain())
    info = resolve_installer(session)  # type: ignore[arg-type]
    assert info.url == ollama_install.OFFICIAL_URL
    assert (info.size_bytes, info.version, info.sha256) == (len(PAYLOAD), "v9.9.9", PAYLOAD_SHA)
    assert info.final_url_host == "release-assets.githubusercontent.com"
    assert all(not follow for _, _, follow in session.calls)  # redirects followed by hand


def test_resolve_without_published_hash():
    info = resolve_installer(_Session(_chain(digest=None)))  # type: ignore[arg-type]
    assert info.sha256 is None and info.size_bytes == len(PAYLOAD)


def test_resolve_refuses_a_redirect_to_another_host():
    table = _chain()
    table[("HEAD", GITHUB_DL)] = _Resp(302, {"Location": "https://evil.example/OllamaSetup.exe"})
    with pytest.raises(JobFailed) as exc:
        resolve_installer(_Session(table))  # type: ignore[arg-type]
    assert exc.value.code == "redirect_not_allowed"


def test_resolve_refuses_a_plain_http_hop():
    table = _chain()
    table[("HEAD", GITHUB_DL)] = _Resp(302, {"Location": "http://github.com/x/OllamaSetup.exe"})
    with pytest.raises(JobFailed) as exc:
        resolve_installer(_Session(table))  # type: ignore[arg-type]
    assert exc.value.code == "redirect_not_allowed"


def test_resolve_needs_a_size():
    table = _chain()
    table[("HEAD", ASSET_URL)] = _Resp(200, {})
    with pytest.raises(JobFailed) as exc:
        resolve_installer(_Session(table))  # type: ignore[arg-type]
    assert exc.value.code == "size_unknown"


def test_resolve_reports_http_errors_and_unreachable():
    with pytest.raises(JobFailed) as exc:
        resolve_installer(_Session(_chain(final_status=404)))  # type: ignore[arg-type]
    assert (exc.value.code, exc.value.params) == ("http_status", {"http_status": 404})
    with pytest.raises(JobFailed) as exc:
        resolve_installer(_Session({}))  # type: ignore[arg-type]
    assert exc.value.code == "download_unreachable"


# --- download ----------------------------------------------------------------------------


def _info(size: int = len(PAYLOAD), sha: str | None = PAYLOAD_SHA) -> InstallerInfo:
    return InstallerInfo(url=ollama_install.OFFICIAL_URL, final_url_host="x", size_bytes=size,
                         version="v9.9.9", sha256=sha)


def test_download_writes_the_verified_file(tmp_path):
    job = Job(id="j", kind="install")
    dest = tmp_path / "dl" / "OllamaSetup.exe"
    sha = ollama_install.download(_info(), dest, job, _Session(_chain()))  # type: ignore[arg-type]
    assert sha == PAYLOAD_SHA and dest.read_bytes() == PAYLOAD
    assert job.state["completed_bytes"] == len(PAYLOAD)
    assert not list(dest.parent.glob("*.part"))


def test_download_rejects_a_hash_mismatch_and_leaves_nothing(tmp_path):
    dest = tmp_path / "OllamaSetup.exe"
    with pytest.raises(JobFailed) as exc:
        ollama_install.download(_info(sha="0" * 64), dest, Job(id="j", kind="install"),
                                _Session(_chain()))  # type: ignore[arg-type]
    assert exc.value.code == "sha256_mismatch"
    assert list(tmp_path.iterdir()) == []


def test_download_rejects_a_size_mismatch(tmp_path):
    with pytest.raises(JobFailed) as exc:
        ollama_install.download(_info(size=len(PAYLOAD) + 5), tmp_path / "x.exe",
                                Job(id="j", kind="install"),
                                _Session(_chain()))  # type: ignore[arg-type]
    assert exc.value.code == "size_mismatch"
    assert list(tmp_path.iterdir()) == []


# --- worker ------------------------------------------------------------------------------


class _Steps:
    def __init__(self, size: int = len(PAYLOAD), signature: tuple[str, str] = (
            "Valid", "CN=\"Ollama, Inc.\", O=\"Ollama, Inc.\"")) -> None:
        self.size = size
        self.signature = signature
        self.launched: list[Path] = []
        self.fetched = False

    def resolve(self) -> InstallerInfo:
        return _info(size=self.size)

    def fetch(self, info: InstallerInfo, dest: Path, job: Job) -> str:
        self.fetched = True
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(PAYLOAD)
        return PAYLOAD_SHA

    def verify(self, path: Path) -> tuple[str, str]:
        return self.signature

    def start(self, path: Path) -> None:
        self.launched.append(path)

    def kwargs(self) -> dict:
        return {"platform": "win32", "find": lambda: None, "resolve": self.resolve,
                "fetch": self.fetch, "verify": self.verify, "start": self.start}


def test_worker_downloads_verifies_then_launches(tmp_path):
    steps, job = _Steps(), Job(id="j", kind="install")
    install_worker(job, len(PAYLOAD), tmp_path, **steps.kwargs())
    assert steps.launched == [tmp_path / "OllamaSetup-v9.9.9.exe"]
    assert job.state["phase"] == "launched" and job.state["signature_status"] == "Valid"
    assert job.state["sha256_published"] is True


def test_worker_needs_the_confirmed_size_before_downloading(tmp_path):
    steps = _Steps(size=len(PAYLOAD) + 1)
    with pytest.raises(JobFailed) as exc:
        install_worker(Job(id="j", kind="install"), len(PAYLOAD), tmp_path, **steps.kwargs())
    assert exc.value.code == "size_mismatch"
    assert not steps.fetched and not steps.launched


@pytest.mark.parametrize("signature", [("NotSigned", ""), ("HashMismatch", "CN=Ollama"),
                                       ("Valid", "CN=Someone Else")])
def test_worker_never_launches_an_unverified_installer(tmp_path, signature):
    steps = _Steps(signature=signature)
    with pytest.raises(JobFailed) as exc:
        install_worker(Job(id="j", kind="install"), len(PAYLOAD), tmp_path, **steps.kwargs())
    assert exc.value.code == "signature_invalid"
    assert not steps.launched and list(tmp_path.iterdir()) == []


def test_worker_refuses_other_platforms_and_an_existing_install(tmp_path):
    steps = _Steps()
    kwargs = steps.kwargs()
    with pytest.raises(JobFailed) as exc:
        install_worker(Job(id="j", kind="install"), 1, tmp_path, **{**kwargs, "platform": "linux"})
    assert exc.value.code == "unsupported_platform"
    with pytest.raises(JobFailed) as exc:
        install_worker(Job(id="j", kind="install"), 1, tmp_path,
                       **{**kwargs, "find": lambda: "C:/Ollama/ollama.exe"})
    assert exc.value.code == "already_installed"
    assert not steps.fetched and not steps.launched


# --- routes ------------------------------------------------------------------------------


@pytest.fixture()
def client(isolated_app, monkeypatch):
    monkeypatch.setattr(ai_routes, "INSTALL_JOBS", JobRegistry("install"))
    return TestClient(isolated_app)


def test_installer_info_route(client, monkeypatch):
    monkeypatch.setattr(ollama_install, "resolve_installer", lambda: _info())
    body = client.get("/api/ai/ollama/installer").json()
    assert body["size_bytes"] == len(PAYLOAD) and body["url"] == ollama_install.OFFICIAL_URL


def test_installer_info_route_reports_failure_codes(client, monkeypatch):
    def _fail():
        raise JobFailed("download_unreachable")

    monkeypatch.setattr(ollama_install, "resolve_installer", _fail)
    resp = client.get("/api/ai/ollama/installer")
    assert resp.status_code == 502 and resp.json()["detail"]["error"] == "download_unreachable"


def test_install_route_runs_the_job_and_reports_status(client, monkeypatch, isolated_app):
    steps = _Steps()
    monkeypatch.setattr(ollama_install, "install_worker",
                        partial(install_worker, **steps.kwargs()))
    resp = client.post("/api/ai/ollama/install", json={"confirm_size_bytes": len(PAYLOAD)})
    assert resp.status_code == 202
    job_id = resp.json()["id"]
    ai_routes.INSTALL_JOBS.wait(job_id, 5)
    status = client.get(f"/api/ai/ollama/install/{job_id}").json()
    assert status["status"] == "succeeded" and status["phase"] == "launched"
    expected_dir = Path(isolated_app.state.db_path).parent / "downloads" / "ollama"
    assert steps.launched == [expected_dir / "OllamaSetup-v9.9.9.exe"]


def test_install_route_requires_a_positive_confirmed_size(client):
    assert client.post("/api/ai/ollama/install", json={}).status_code == 422
    assert client.post("/api/ai/ollama/install",
                       json={"confirm_size_bytes": 0}).status_code == 422


def test_install_status_unknown_job(client):
    assert client.get("/api/ai/ollama/install/nope").status_code == 404
    assert client.delete("/api/ai/ollama/install/nope").status_code == 404
