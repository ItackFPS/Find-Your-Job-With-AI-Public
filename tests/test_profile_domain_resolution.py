"""How the search reads line-of-work labels, shown before saving (Phase B B5-C)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.api.routes.profile as profile_route

    config = tmp_path / "config"
    config.mkdir()
    (config / "filters.yaml").write_text(
        "domain_keywords:\n"
        "  systems:\n    title: [administrateur systèmes, administration systèmes]\n"
        "  healthcare:\n    title: [infirmier, aide-soignant]\n",
        encoding="utf-8")
    monkeypatch.setattr(profile_route, "_CONFIG_DIR", config)
    from src.api.app import app
    return TestClient(app)


def test_each_label_says_how_the_search_reads_it(client: TestClient) -> None:
    r = client.post("/api/profile/domains/resolve",
                    json={"labels": ["systems", "Administration systèmes", "Boulangerie", "  ", "Infirmier"]})
    assert r.status_code == 200
    assert r.json()["labels"] == [
        {"label": "systems", "status": "known", "families": ["systems"]},
        {"label": "Administration systèmes", "status": "resolved", "families": ["systems"]},
        {"label": "Boulangerie", "status": "unresolved", "families": []},
        {"label": "Infirmier", "status": "unresolved", "families": []},
    ]


def test_nothing_is_saved(client: TestClient, tmp_path: Path) -> None:
    client.post("/api/profile/domains/resolve", json={"labels": ["Boulangerie"]})
    assert sorted(p.name for p in (tmp_path / "config").iterdir()) == ["filters.yaml"]


def test_the_answer_matches_the_resolver_the_filter_uses(client: TestClient) -> None:
    from src.discovery.presets import load_domain_keywords
    from src.pipeline.domain_vocabulary import resolve_domains

    import src.api.routes.profile as profile_route

    vocabulary = load_domain_keywords(profile_route._CONFIG_DIR)
    labels = ["Administration systèmes", "Boulangerie"]
    body = client.post("/api/profile/domains/resolve", json={"labels": labels}).json()
    assert [x["families"] for x in body["labels"]] == [
        list(resolve_domains([label], vocabulary).families) for label in labels]


def test_malformed_requests_are_rejected(client: TestClient) -> None:
    assert client.post("/api/profile/domains/resolve", json={"labels": "x"}).status_code == 422
    assert client.post("/api/profile/domains/resolve", json={"labels": ["a"] * 201}).status_code == 422
