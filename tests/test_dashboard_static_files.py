"""How the built dashboard is served."""

from __future__ import annotations

from src.api.app import spa_file


def _dist(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    return dist


def test_spa_routes_get_index(tmp_path):
    dist = _dist(tmp_path)
    for path in ("", "jobs", "jobs/123", "profile"):
        assert spa_file(dist, path) == dist.resolve() / "index.html"


def test_build_root_files_are_served_as_themselves(tmp_path):
    dist = _dist(tmp_path)
    assert spa_file(dist, "favicon.svg") == (dist / "favicon.svg").resolve()


def test_unknown_api_paths_are_not_the_spa(tmp_path):
    dist = _dist(tmp_path)
    assert spa_file(dist, "api") is None
    assert spa_file(dist, "api/does-not-exist") is None


def test_no_file_outside_the_build_is_served(tmp_path):
    dist = _dist(tmp_path)
    assert spa_file(dist, "../secret.txt") == dist.resolve() / "index.html"
    assert spa_file(dist, "assets/../../secret.txt") == dist.resolve() / "index.html"


def test_the_page_is_revalidated_on_every_load(tmp_path):
    from src.api.app import NO_CACHE, spa_response

    dist = _dist(tmp_path)
    for path in ("", "jobs/123", "favicon.svg"):
        assert spa_response(dist, path).headers["cache-control"] == NO_CACHE


def test_hashed_assets_are_immutable(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.api.app import IMMUTABLE, HashedAssets

    dist = _dist(tmp_path)
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    app = FastAPI()
    app.mount("/assets", HashedAssets(directory=str(dist / "assets")), name="assets")
    with TestClient(app) as client:
        r = client.get("/assets/index-abc123.js")
        assert r.status_code == 200 and r.headers["cache-control"] == IMMUTABLE
        assert client.get("/assets/missing.js").status_code == 404

