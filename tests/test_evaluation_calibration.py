"""In-app blind calibration (completion plan P2.3): copy, blind items, representative order, saved versions."""

from __future__ import annotations

import json
from collections import Counter

import pytest

from src.evaluation import calibration as cal
from src.evaluation.dataset import load_dataset, write_dataset
from src.evaluation.export import EXPORT_META

STRATA = {"random_control": 40, "top_scored": 20, "pipeline_rejected": 30, "near_tier": 10}


@pytest.fixture()
def export_dir(tmp_path):
    """A small exported real dataset with its private sidecar, as `export` writes it."""
    src = tmp_path / "exports" / "real-x"
    jobs, strata = [], {}
    for stratum, n in STRATA.items():
        for i in range(n):
            jid = f"{stratum[:3]}{i:03d}"
            jobs.append({"id": jid, "title": f"Data analyst {jid}", "company": "Acme", "location": "Lyon",
                         "source": "france_travail", "description": "Analyse de données. " * 5})
            strata[jid] = stratum
    write_dataset(src, dataset_id="real-x", version="1", kind="real", created_at="2026-10-17T00:00:00+00:00",
                  as_of="2026-10-17T00:00:00+00:00", jobs=jobs, labels=[])
    (src / EXPORT_META).write_text(json.dumps({"strata": strata, "salt": "s", "id_map": {}}), encoding="utf-8")
    return src


@pytest.fixture()
def root(tmp_path, export_dir):
    r = tmp_path / "data" / cal.CALIBRATION_DIR
    cal.import_dataset(export_dir, r)
    return r


def _files(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob("*") if p.is_file()}


def test_import_copies_and_never_writes_the_original(export_dir, root):
    before = _files(export_dir)
    items = cal.next_items(root, "real-x", 5)
    cal.record_answer(root, "real-x", items[0]["job_id"], "relevant")
    cal.save_session(root, "real-x")
    assert _files(export_dir) == before
    assert load_dataset(root / "real-x", require_complete_labels=False).manifest.version == "2"


def test_import_refuses_a_second_copy_and_synthetic_datasets(tmp_path, export_dir, root):
    with pytest.raises(cal.CalibrationError) as exc:
        cal.import_dataset(export_dir, root)
    assert exc.value.code == "already_imported"
    synth = tmp_path / "synth"
    write_dataset(synth, dataset_id="s", version="1", kind="synthetic", created_at="2026-10-17T00:00:00+00:00",
                  as_of="2026-10-17T00:00:00+00:00", jobs=[{"id": "a", "title": "t", "company": "c"}], labels=[])
    with pytest.raises(cal.CalibrationError) as exc:
        cal.import_dataset(synth, tmp_path / "other")
    assert exc.value.code == "not_real"
    with pytest.raises(cal.CalibrationError) as exc:
        cal.import_dataset(tmp_path / "missing", tmp_path / "other")
    assert exc.value.code == "invalid_dataset"


def test_items_are_blind(root):
    item = cal.next_items(root, "real-x", 1)[0]
    assert set(item) == {"job_id", "title", "company", "location", "remote", "source", "description",
                         "description_truncated"}
    text = json.dumps(item)
    assert not any(s in text for s in STRATA)      # no stratum, score or decision leaks into what is shown


def test_any_prefix_of_the_order_is_representative(root, export_dir):
    strata = json.loads((export_dir / EXPORT_META).read_text(encoding="utf-8"))["strata"]
    order = cal.labelling_order("real-x", strata, strata)
    assert sorted(order) == sorted(strata)
    first = Counter(strata[j] for j in order[:20])
    total = sum(STRATA.values())
    for stratum, n in STRATA.items():
        assert abs(first[stratum] - 20 * n / total) <= 1, (stratum, first)
    assert order == cal.labelling_order("real-x", reversed(list(strata)), strata)   # ids only, not input order


def test_answers_survive_until_saved_then_become_human_labels(root):
    items = cal.next_items(root, "real-x", 3)
    cal.record_answer(root, "real-x", items[0]["job_id"], "relevant")
    cal.record_answer(root, "real-x", items[1]["job_id"], "not_relevant", "location")
    s = cal.record_answer(root, "real-x", items[2]["job_id"], "not_sure")
    assert s["pending"] == 3 and s["labelled"] == 0
    assert [i["job_id"] for i in cal.next_items(root, "real-x", 1)] != [items[0]["job_id"]]
    saved = cal.save_session(root, "real-x")
    assert saved["changed"] == 3 and saved["labelled"] == 3 and saved["pending"] == 0
    labels = load_dataset(root / "real-x", require_complete_labels=False).labels
    assert labels[items[0]["job_id"]].expected == "accept"
    rejected = labels[items[1]["job_id"]]
    assert (rejected.expected, rejected.category) == ("reject", "location")
    assert labels[items[2]["job_id"]].expected == "borderline"
    assert {lab.label_source for lab in labels.values()} == {"human"}
    assert {lab.labeler for lab in labels.values()} == {cal.LABELER}
    assert (root / "real-x" / "versions" / "1" / "manifest.json").is_file()   # previous version archived
    with pytest.raises(cal.CalibrationError) as exc:
        cal.record_answer(root, "real-x", items[0]["job_id"], "relevant")
    assert exc.value.code == "already_labelled"


def test_saving_nothing_writes_no_version(root):
    assert cal.save_session(root, "real-x")["changed"] == 0
    assert not (root / "real-x" / "versions").exists()


def test_invalid_answers_and_ids_are_refused(root):
    jid = cal.next_items(root, "real-x", 1)[0]["job_id"]
    for args, code in [((jid, "yes"), "invalid_answer"), ((jid, "relevant", "salary"), "invalid_reason"),
                       (("nope", "relevant"), "unknown_job")]:
        with pytest.raises(cal.CalibrationError) as exc:
            cal.record_answer(root, "real-x", *args)
        assert exc.value.code == code
    for bad in ("..", "../real-x", "missing"):
        with pytest.raises(cal.CalibrationError) as exc:
            cal.next_items(root, bad, 1)
        assert exc.value.code == "not_found"


def test_target_starts_at_100_and_grows_by_100_within_the_dataset(root):
    assert cal.summary(root / "real-x")["target"] == 100
    assert cal.extend_target(root, "real-x")["target"] == 100      # the dataset holds 100 jobs
    assert [d["dataset_id"] for d in cal.list_datasets(root)] == ["real-x"]


def test_session_size_is_capped(root):
    assert len(cal.next_items(root, "real-x", 500)) == cal.MAX_SESSION


# -- API ----------------------------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path, export_dir):
    from fastapi.testclient import TestClient

    from src.api.app import app

    saved = getattr(app.state, "db_path", None), getattr(app.state, "calibration_dir", None)
    data = tmp_path / "appdata"
    (data / "benchmarks").mkdir(parents=True)
    app.state.db_path = str(data / "jobs.db")
    app.state.calibration_dir = None
    yield TestClient(app), data
    app.state.db_path, app.state.calibration_dir = saved


def test_api_imports_labels_and_saves_a_session(api, export_dir):
    c, data = api
    over = c.get("/api/quality/calibration").json()
    assert over["datasets"] == [] and over["reasons"] == list(cal.REASONS)
    assert c.post("/api/quality/calibration/import", json={"source": str(export_dir)}).json()["total"] == 100
    assert c.post("/api/quality/calibration/import", json={"source": str(export_dir)}).status_code == 409
    items = c.get("/api/quality/calibration/real-x/next", params={"n": 2}).json()["items"]
    assert len(items) == 2
    r = c.post("/api/quality/calibration/real-x/answers",
               json={"job_id": items[0]["job_id"], "answer": "not_relevant", "reason": "location"})
    assert r.status_code == 200 and r.json()["pending"] == 1
    assert c.post("/api/quality/calibration/real-x/answers",
                  json={"job_id": items[1]["job_id"], "answer": "maybe"}).status_code == 422
    assert c.post("/api/quality/calibration/real-x/answers",
                  json={"job_id": items[1]["job_id"], "answer": "relevant", "reason": "salary"}).json() == {
        "detail": {"code": "invalid_reason"}}
    saved = c.post("/api/quality/calibration/real-x/save").json()
    assert (saved["labelled"], saved["changed"], saved["version"]) == (1, 1, "2")
    assert (data / cal.CALIBRATION_DIR / "real-x" / "labels.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert c.get("/api/quality/calibration/nope/next").status_code == 404


def test_api_offers_exports_found_in_the_data_folder(api, export_dir):
    import shutil

    c, data = api
    shutil.copytree(export_dir, data / "benchmarks" / "real-x")
    found = c.get("/api/quality/calibration").json()["exports_found"]
    assert [(f["dataset_id"], f["total"]) for f in found] == [("real-x", 100)]
    c.post("/api/quality/calibration/import", json={"source": found[0]["source"]})
    assert c.get("/api/quality/calibration").json()["exports_found"] == []


def test_api_refuses_values_that_would_read_as_options(api):
    c, _ = api
    assert c.get("/api/quality/calibration/--help/next").status_code == 400
    assert c.post("/api/quality/calibration/import", json={"source": "--exports"}).status_code == 400
