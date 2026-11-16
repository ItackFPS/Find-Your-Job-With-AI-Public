"""One list, one count, one score (issue #253): the list, its count, its country facets, the
Dashboard and the list status agree; scores come from the run that orders the list; saved and
tracked offers stay reachable; turning a source off or on acts at once. Temporary databases."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.models.job import Job
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage


def _sources(cfg: Path, beta: bool = True) -> None:
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "sources.yaml").write_text(
        "sources:\n  - {name: alpha, type: alpha, active: true}\n"
        f"  - {{name: beta, type: beta, active: {str(beta).lower()}}}\n", encoding="utf-8")


def _jobs(db: str, specs: list[tuple[str, str, list[str]]]) -> None:
    s = Storage(db_path=db)
    s.init_db()
    for job_id, source, countries in specs:
        s.upsert_job(Job(id=job_id, source=source, title=f"Offre {job_id}", sources=[source], countries=countries))
    s.close()


def _run(db: str, run_id: str, scores: dict[str, float], status: str = "completed",
         stats: dict[str, Any] | None = None) -> None:
    rs = RunStorage(db_path=db)
    rs.init_db()
    rs.create_run(run_id)
    rs.store_job_scores(run_id, [{"job_id": j, "deterministic_score": v, "semantic_score": v, "final_score": v,
                                  "score_breakdown": {}} for j, v in scores.items()])
    if status == "completed":
        rs.complete_run(run_id, stats or {})
    elif status == "failed":
        rs.fail_run(run_id, "boom", stats or {})
    rs.close()
    time.sleep(0.01)                       # distinct started_at


@pytest.fixture
def api(isolated_app):  # noqa: ANN001, ANN201
    _sources(Path(isolated_app.state.config_dir))
    return TestClient(isolated_app), isolated_app.state.db_path


def _pages(client: TestClient, limit: int, **params: Any) -> list[dict[str, Any]]:
    out, offset = [], 0
    while True:
        page = client.get("/api/jobs", params={**params, "limit": limit, "offset": offset}).json()
        out.extend(page)
        if len(page) < limit:
            return out
        offset += limit


def test_list_count_facets_dashboard_and_status_agree(api) -> None:  # noqa: ANN001
    client, db = api
    _jobs(db, [("a1", "alpha", ["France"]), ("a2", "alpha", ["France", "Belgique"]), ("a3", "alpha", []),
               ("b1", "beta", ["France"]), ("x1", "remoteok", ["France"]), ("i1", "indeed", ["France"]),
               ("e1", "alpha", ["France"])])
    s = Storage(db_path=db)
    s.set_reported_expired("e1", True, datetime.now(UTC))
    s.close()
    _run(db, "r1", {"a1": .9, "a2": .8, "a3": .7, "b1": .6, "x1": .5, "i1": .4, "e1": .3})
    assert client.post("/api/jobs/a3/feedback", json={"type": "hidden"}).status_code == 201

    listed = _pages(client, 2)
    ids = [j["id"] for j in listed]
    # hidden a3, expired e1, retired source x1, indeed (switched off: not chosen) all left out
    assert ids == ["a1", "a2", "b1"]
    assert client.get("/api/jobs/count").json()["count"] == 3
    countries = client.get("/api/jobs/countries").json()
    assert countries["total"] == 3
    assert {f["name"]: f["count"] for f in countries["facets"]} == {"France": 3, "Belgique": 1}
    assert client.get("/api/dashboard").json()["listed_total"] == 3
    status = client.get("/api/jobs/list-status").json()
    assert status["listed"] == 3 and status["visible_run_id"] == "r1"
    assert status["hidden_by_user"] == 1
    assert status["source_off"] == {"indeed": 1, "remoteok": 1}
    assert status["expired"] == {"user_report": 1}


def test_hiding_an_offer_without_a_content_hash_hides_only_it(api) -> None:  # noqa: ANN001
    client, db = api
    _jobs(db, [("a1", "alpha", []), ("a2", "alpha", [])])          # stored with content_hash ""
    _run(db, "r1", {"a1": 0.9, "a2": 0.8})
    assert client.post("/api/jobs/a1/feedback", json={"type": "hidden"}).status_code == 201
    assert [j["id"] for j in client.get("/api/jobs").json()] == ["a2"]


def test_tied_scores_page_without_duplicates_or_gaps(api) -> None:  # noqa: ANN001
    client, db = api
    _jobs(db, [(f"t{i}", "alpha", []) for i in range(7)])
    _run(db, "r1", {f"t{i}": 0.5 for i in range(7)})
    whole = [j["id"] for j in client.get("/api/jobs", params={"limit": 50}).json()]
    paged = [j["id"] for j in _pages(client, 3)]
    assert paged == whole and len(set(paged)) == 7
    assert whole == sorted(whole)          # ties broken by id: stable across queries


def test_the_score_shown_is_the_visible_runs(api) -> None:  # noqa: ANN001
    client, db = api
    _jobs(db, [("a1", "alpha", [])])
    _run(db, "good", {"a1": 0.9})
    _run(db, "down", {"a1": 0.1}, stats={"outcome": "no_successful_source"})
    _run(db, "running", {"a1": 0.2}, status="running")
    job = client.get("/api/jobs").json()[0]
    assert job["score"]["final_score"] == pytest.approx(0.9)
    assert client.get("/api/jobs/list-status").json()["previous_list_kept"] is False  # latest is running
    s = RunStorage(db_path=db)
    s.fail_run("running", "boom")
    s.close()
    status = client.get("/api/jobs/list-status").json()
    assert status["visible_run_id"] == "good" and status["previous_list_kept"] is True


def test_saved_and_tracked_offers_stay_reachable(api) -> None:  # noqa: ANN001
    client, db = api
    _jobs(db, [("a1", "alpha", []), ("old", "alpha", []), ("i1", "indeed", []), ("b1", "beta", [])])
    _run(db, "r1", {"a1": 0.9, "b1": 0.8})                 # "old" and "i1" are not in the visible run
    assert client.post("/api/jobs/old/feedback", json={"type": "saved"}).status_code == 201
    assert client.patch("/api/jobs/i1/application-status", json={"application_status": "applied"}).status_code == 200
    assert client.patch("/api/jobs/b1/application-status", json={"application_status": "interview"}).status_code == 200
    s = Storage(db_path=db)
    s.set_reported_expired("old", True, datetime.now(UTC))
    s.close()

    saved = {j["id"]: j for j in client.get("/api/jobs", params={"saved_only": True}).json()}
    assert set(saved) == {"old"} and saved["old"]["status"] == "expired"
    tracked = {j["id"]: j for j in client.get("/api/jobs", params={"tracked_only": True}).json()}
    assert set(tracked) == {"i1", "b1"}
    assert tracked["i1"]["source_off"] is True and tracked["b1"]["source_off"] is False
    assert client.get("/api/jobs/count", params={"tracked_only": True}).json()["count"] == 2

    _sources(Path(client.app.state.config_dir), beta=False)  # b1's source off: still tracked
    tracked = {j["id"]: j for j in client.get("/api/jobs", params={"tracked_only": True}).json()}
    assert tracked["b1"]["source_off"] is True
    assert [j["id"] for j in client.get("/api/jobs").json()] == ["a1", "b1"]   # tracked: listed (D1)


def test_turning_a_source_off_and_on_acts_at_once(api) -> None:  # noqa: ANN001
    client, db = api
    _jobs(db, [("a1", "alpha", []), ("b1", "beta", []), ("b2", "beta", [])])
    _run(db, "r1", {"a1": 0.9, "b1": 0.8, "b2": 0.7})
    cfg = Path(client.app.state.config_dir)
    assert [j["id"] for j in client.get("/api/jobs").json()] == ["a1", "b1", "b2"]
    _sources(cfg, beta=False)
    assert [j["id"] for j in client.get("/api/jobs").json()] == ["a1"]
    assert client.get("/api/jobs/list-status").json()["source_off"] == {"beta": 2}
    _sources(cfg, beta=True)
    assert [j["id"] for j in client.get("/api/jobs").json()] == ["a1", "b1", "b2"]
