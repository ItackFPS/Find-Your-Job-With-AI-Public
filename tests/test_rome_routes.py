"""ROME autocomplete API (src/api/routes/rome.py): only a router on a bare app, tmp databases."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import rome as rome_module
from src.rome.db import RomeDb, RomeEntry

ENTRIES = [
    RomeEntry("C12", "Comptabilité et gestion", "domain"),
    RomeEntry("M1203", "Comptable", "occupation", group="Comptabilité et gestion"),
    RomeEntry("M1855", "Développeur / Développeuse web", "occupation", group="Informatique"),
]


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fill: bool) -> TestClient:
    path = str(tmp_path / "rome.db")
    monkeypatch.setattr(rome_module, "_DB_PATH", path)
    if fill:
        RomeDb(path).replace_all(ENTRIES, {"version": "62", "published": "19/10/2026",
                                           "source": "France Travail, ROME 4.0"})
    app = FastAPI()
    app.include_router(rome_module.router)
    return TestClient(app)


def test_occupations_search_returns_the_profile_keyword(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    client = _client(tmp_path, monkeypatch, fill=True)
    body = client.get("/api/rome/occupations", params={"q": "developp"}).json()
    assert body == [{"code": "M1855", "label": "Développeur / Développeuse web", "keyword": "Développeur web",
                     "kind": "occupation", "group": "Informatique", "is_rome": True}]


def test_domains_are_searched_too(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    client = _client(tmp_path, monkeypatch, fill=True)
    kinds = {r["kind"] for r in client.get("/api/rome/occupations", params={"q": "comptab"}).json()}
    assert kinds == {"domain", "occupation"}


def test_not_set_up_is_an_empty_list_and_a_status_with_the_command(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    client = _client(tmp_path, monkeypatch, fill=False)
    assert client.get("/api/rome/occupations", params={"q": "comptable"}).json() == []
    status = client.get("/api/rome/status").json()
    assert status["ready"] is False and status["count"] == 0
    assert status["setup_command"] == "python scripts/setup_rome.py"


def test_status_carries_the_attribution(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    client = _client(tmp_path, monkeypatch, fill=True)
    status = client.get("/api/rome/status").json()
    assert status["ready"] is True and status["count"] == 3
    assert status["attribution"] == "France Travail, ROME 4.0 (version 62, published 19/10/2026)"


@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "x" * 101}, {"q": "a", "limit": 0}, {"q": "a", "limit": 51}])
def test_bad_queries_are_rejected(tmp_path, monkeypatch, params) -> None:  # noqa: ANN001
    assert _client(tmp_path, monkeypatch, fill=True).get("/api/rome/occupations", params=params).status_code == 422
