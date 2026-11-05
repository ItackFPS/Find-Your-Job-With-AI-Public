"""INV-2 and the log rule of ADR 0015: personal data never leaves the install, and never reaches logs.

1. **Socket guard.** Recording personal activity through the API and running the pipeline over a
   database that holds it opens no non-loopback network connection.
2. **Log lint.** A UI-like session (explicit feedback with reasons and a note, status changes, the
   feedback list) followed by a full offline pipeline run logs neither the note nor the reasons of
   the user's answers. (Discovery queries built from profile skills are logged on purpose: they are
   sent to the sources anyway. The test profile has no free-text field to check.)
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.test_decision_journal_orchestrator import DS, _config_dir, _FakeEmbeddingService

NOTE = "zq-private-note-7f3a"  # distinctive text the user typed; must never appear in a log


def _is_loopback(address: object) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str):
        return True  # AF_UNIX path or similar local endpoint
    if host in ("localhost", "testserver"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False  # a host name: resolving it would already be a network lookup


@pytest.fixture
def outbound(monkeypatch):
    """Record (and refuse) every non-loopback connection attempt."""
    attempts: list[object] = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        if not _is_loopback(address):
            attempts.append(address)
            raise OSError("network disabled by INV-2 test")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _is_loopback(address):
            attempts.append(address)
            return 111
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and not _is_loopback(host):
            attempts.append(host)
            raise OSError("name resolution disabled by INV-2 test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return attempts


def _run_pipeline(tmp_path, db: str):
    from src.evaluation.dataset import load_dataset
    from src.pipeline.orchestrator import run_pipeline

    cfg = _config_dir(tmp_path, None)
    with patch("src.embeddings.embedding.EmbeddingService", _FakeEmbeddingService):
        result = run_pipeline(db_path=db, config_dir=str(cfg),
                              prefetched_linkedin_jobs=load_dataset(DS).build_jobs())
    assert result.status == "completed", result.error
    return cfg


def _personal_session(client: TestClient, job_ids: list[str]) -> None:
    for i, job_id in enumerate(job_ids):
        if i % 2:
            r = client.post(f"/api/jobs/{job_id}/feedback", json={
                "type": "not_interested", "reasons": ["too_far", "salary"], "note": NOTE, "source": "ui"})
        else:
            r = client.post(f"/api/jobs/{job_id}/feedback", json={
                "type": "interested", "reasons": ["interesting_despite_low_score"], "note": NOTE,
                "source": "ui"})
        assert r.status_code == 201, r.text
        assert client.get(f"/api/jobs/{job_id}/feedback").status_code == 200
    r = client.patch(f"/api/jobs/{job_ids[0]}/application-status", json={"application_status": "applied"})
    assert r.status_code in (200, 204), r.text


def _stored_job_ids(db: str, n: int) -> list[str]:
    import sqlite3

    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT id FROM jobs ORDER BY id LIMIT ?", (n,))]
    finally:
        conn.close()


def test_personal_activity_and_pipeline_open_no_outbound_socket(tmp_path, isolated_app, outbound):
    db = isolated_app.state.db_path
    _run_pipeline(tmp_path, db)
    with TestClient(isolated_app) as client:
        _personal_session(client, _stored_job_ids(db, 6))
    # a second run now reads the user's feedback (personalization bridge, journal stances)
    (tmp_path / "config").rename(tmp_path / "config-1")
    _run_pipeline(tmp_path, db)
    assert outbound == [], f"non-loopback connections attempted: {outbound}"


def test_guard_detects_an_outbound_connection(outbound):
    with pytest.raises(OSError):
        socket.create_connection(("192.0.2.1", 9), timeout=0.5)
    assert outbound


def test_logs_hold_no_note_or_reasons(tmp_path, isolated_app, caplog):
    caplog.set_level(logging.DEBUG)
    db = isolated_app.state.db_path
    _run_pipeline(tmp_path, db)
    with TestClient(isolated_app) as client:
        _personal_session(client, _stored_job_ids(db, 6))
    (tmp_path / "config").rename(tmp_path / "config-1")
    _run_pipeline(tmp_path, db)

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert NOTE not in text
    assert '["too_far", "salary"]' not in text and "['too_far', 'salary']" not in text
