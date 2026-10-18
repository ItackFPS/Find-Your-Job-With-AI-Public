"""Read-only export, anonymisation and annotation tooling (src/evaluation/export.py)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from src.evaluation.__main__ import main as cli_main
from src.evaluation.dataset import DatasetError, load_dataset
from src.evaluation.export import (
    ExportError, assert_not_committable, export_real_dataset, import_annotations,
    open_readonly, parse_annotations, scrub_text,
)
from src.models.job import Job
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

LONG = "Nous recherchons un développeur Python junior. " * 8


def _build_db(path: Path, n: int = 30) -> Path:
    st = Storage(str(path))
    st.init_db()
    jobs = []
    for i in range(n):
        company = "Acme Corp" if i % 3 else "Globex"
        jobs.append(Job(
            id=f"id{i:03d}", source="src_a" if i % 2 else "src_b", company=company, title=f"Dev {i}",
            description=f"{LONG} Contact: rh{i}@acme.com, tel 06 12 34 56 {i:02d}. "
                        f"https://acme.com/jobs/{i} {company} offre.",
            location="Paris", countries=["France"], skills=["python"]))
    st.upsert_jobs_batch(jobs)
    for i in range(0, n, 10):
        st.update_application_status(f"id{i:03d}", "applied")
    st.close()
    rs = RunStorage(str(path))
    rs.init_db()
    rs.create_run("run1")
    rs.store_job_scores("run1", [
        {"job_id": f"id{i:03d}", "final_score": (0.60 + (i % 5) * 0.01) if i % 2 else 0.30} for i in range(n)
    ])
    rs.close()
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def db(tmp_path):
    return _build_db(tmp_path / "jobs.db")


def test_export_never_modifies_the_database_files(db, tmp_path):
    side = [Path(f"{db}{ext}") for ext in ("-wal", "-shm")]
    before = {p: _sha(p) for p in [db, *side] if p.exists()}
    export_real_dataset(db, tmp_path / "out", n=12, seed=1)
    assert _sha(db) == before[db]
    for p in side:                       # a -wal must be byte-identical (readers never write it)
        if p in before and p.suffix == "-wal":
            assert _sha(p) == before[p]


def test_connection_is_read_only(db):
    conn = open_readonly(db)
    import sqlite3
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|query_only"):
        conn.execute("UPDATE jobs SET title='x'")
    conn.close()


def test_export_is_deterministic_and_loads_as_a_valid_unlabeled_dataset(db, tmp_path):
    kw = dict(n=12, seed=7, salt="s", as_of="2026-10-01T00:00:00+00:00")
    a = export_real_dataset(db, tmp_path / "a", **kw)
    export_real_dataset(db, tmp_path / "b", **kw)
    for name in ("jobs.jsonl", "labels.jsonl", "annotations.csv", "manifest.json"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    assert a["n_jobs"] == 12
    ds = load_dataset(tmp_path / "a", require_complete_labels=False)
    assert ds.manifest.kind == "real" and len(ds.unlabeled_ids) == 12
    assert "survival bias" in ds.manifest.notes.lower()
    export_real_dataset(db, tmp_path / "c", **{**kw, "seed": 8})
    assert (tmp_path / "c" / "jobs.jsonl").read_bytes() != (tmp_path / "a" / "jobs.jsonl").read_bytes()
    # without an explicit salt, anonymised ids are NOT guessable from the (public) seed
    export_real_dataset(db, tmp_path / "d", n=12, seed=7)
    export_real_dataset(db, tmp_path / "e", n=12, seed=7)
    assert (tmp_path / "d" / "jobs.jsonl").read_bytes() != (tmp_path / "e" / "jobs.jsonl").read_bytes()


def test_strata_cover_threshold_actioned_and_sources(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=20, seed=3)
    meta = json.loads((tmp_path / "o" / "PRIVATE_export_meta.json").read_text())
    kinds = set(meta["strata"].values())
    assert {"near_tier_threshold", "user_actioned", "per_source", "random_control"} <= kinds
    assert len(meta["strata"]) <= 20


def test_anonymisation_scrubs_identifiers(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=20, seed=1)        # 20 of 30: sampling is exercised
    text = (tmp_path / "o" / "jobs.jsonl").read_text()
    for leak in ("acme.com", "Acme Corp", "Globex", "06 12 34", "@", "http", '"id": "id0'):
        assert leak not in text, leak
    assert "Company-" in text and "[email]" in text and "[phone]" in text
    meta = json.loads((tmp_path / "o" / "PRIVATE_export_meta.json").read_text())
    assert all(v.startswith("id") for v in meta["id_map"].values())      # private mapping back
    assert meta["salt"] and "PRIVATE" in meta["WARNING"]


def test_raw_payload_and_urls_are_never_exported(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=5, seed=1, anonymize=False)
    row = json.loads((tmp_path / "o" / "jobs.jsonl").read_text().split("\n")[0])
    assert "raw_payload" not in row and "canonical_url" not in row and "apply_url" not in row


def test_scrub_text_cases():
    out = scrub_text("Mail a.b@x.fr, +33 6 12 34 56 78, www.x.fr, ACME corp ok", "Acme Corp", "Company-1")
    assert "@" not in out and "x.fr" not in out and "12 34 56" not in out and "Company-1" in out
    assert scrub_text(None) is None


@pytest.mark.parametrize("phone", ["06 12 34 56 78", "0612345678", "+33 6 12 34 56 78", "+33 (0)6 12 34 56 78",
                                   "06.12.34.56.78", "+1 415 555 2671"])
def test_phone_formats_are_scrubbed(phone):
    assert scrub_text(f"Appelez {phone} svp") == "Appelez [phone] svp"


def test_non_phone_numbers_survive():
    text = "Salaire 45 000 - 55 000 EUR, expérience 2020-2024, publié 2026-09-17, Python 3.11.4, 3 à 5 ans, Bac+5"
    assert scrub_text(text) == text


def test_company_scrub_uses_word_boundaries_and_legal_suffix_variants():
    assert scrub_text("A database message usage", "Data", "X") == "A database message usage"
    assert scrub_text("Sage: message", "Sage", "X") == "X: message"
    out = scrub_text("ACME-corp? Acme Corp SAS recrute; acme  corp", "Acme Corp SAS", "Company-9")
    assert out == "Company-9? Company-9 recrute; Company-9"


def test_refuses_to_overwrite_and_untracked_guard(db, tmp_path):
    export_real_dataset(db, tmp_path / "o", n=5)
    with pytest.raises(ExportError, match="already contains"):
        export_real_dataset(db, tmp_path / "o", n=5)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / ".gitignore").write_text("data/*\n")
    assert_not_committable(repo / "data" / "benchmarks" / "x")            # ignored → ok
    with pytest.raises(ExportError, match="never be committed"):
        assert_not_committable(repo / "tests" / "fixtures" / "x")        # would be tracked
    assert_not_committable(tmp_path / "outside")                          # outside any repo (tmp) → ok
    # a pattern ignoring only jobs.jsonl is NOT enough: every output file is checked
    (repo / ".gitignore").write_text("jobs.jsonl\n")
    with pytest.raises(ExportError, match="never be committed"):
        assert_not_committable(repo / "somewhere")


def test_guard_fails_closed_on_git_errors(tmp_path, monkeypatch):
    import src.evaluation.export as ex

    class _R:
        returncode, stdout, stderr = 128, "", "fatal: detected dubious ownership in repository"

    monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(ExportError, match="cannot determine git status"):
        assert_not_committable(tmp_path / "x")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(ex.subprocess, "run", boom)
    with pytest.raises(ExportError, match="cannot determine git status"):
        assert_not_committable(tmp_path / "x")


@pytest.mark.parametrize("n,n_sources", [(2, 10), (1, 3), (3, 10), (7, 4)])
def test_stratify_never_exceeds_budget_and_keeps_random_control(n, n_sources):
    from src.evaluation.export import _stratify

    cands = [{"id": f"c{i:03d}", "source": f"s{i % n_sources}", "application_status": "applied" if i % 7 == 0 else None}
             for i in range(100)]
    scores = {c["id"]: 60.5 for c in cands[:20]}
    chosen = _stratify(cands, scores, n, 1)
    assert len(chosen) == n
    if n >= 2:
        assert "random_control" in chosen.values()


@pytest.mark.parametrize("bad", [0, -3])
def test_n_must_be_positive(db, tmp_path, bad):
    with pytest.raises(ExportError, match="--n"):
        export_real_dataset(db, tmp_path / "o", n=bad)
    assert cli_main(["export", "--db", str(db), "--out", str(tmp_path / "o2"), "--n", str(bad)]) == 2


def test_cli_refuses_tracked_output_inside_a_repository(db, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    assert cli_main(["export", "--db", str(db), "--out", str(repo / "tracked"), "--n", "3"]) == 2
    assert not (repo / "tracked" / "jobs.jsonl").exists()


def _write_annotations(dataset_dir: Path, rows: dict[str, str]) -> Path:
    import csv
    src = dataset_dir / "annotations.csv"
    with open(src, newline="", encoding="utf-8-sig") as f:
        data = list(csv.DictReader(f))
    for r in data:
        if r["job_id"] in rows:
            r["expected"] = rows[r["job_id"]]
    out = dataset_dir / "filled.csv"
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0]))
        w.writeheader()
        w.writerows(data)
    return out


def test_annotation_roundtrip_creates_new_version_with_provenance(db, tmp_path):
    out = tmp_path / "o"
    export_real_dataset(db, out, n=6, seed=1)
    ids = [j["id"] for j in load_dataset(out, require_complete_labels=False).job_dicts]
    filled = _write_annotations(out, {ids[0]: "accept", ids[1]: "REJECT", ids[2]: "borderline"})
    labels, unlabeled = parse_annotations(out, filled, labeler="alice", labeled_at="2026-10-01T00:00:00+00:00")
    assert len(labels) == 3 and len(unlabeled) == 3
    assert {lab.expected for lab in labels} == {"accept", "reject", "borderline"}
    assert all(lab.label_source == "human" and lab.labeler == "alice" for lab in labels)
    old_hash = load_dataset(out, require_complete_labels=False).manifest.labels_sha256
    info = import_annotations(out, filled, labeler="alice")
    ds = load_dataset(out, require_complete_labels=False)
    assert info["version"] == "2" and info["labelled"] == 3 and info["unlabelled"] == 3 and ds.manifest.version == "2"
    assert ds.manifest.labels_sha256 != old_hash and len(ds.labels) == 3


@pytest.mark.parametrize("bad", ["maybe", "yes"])
def test_annotation_validation_is_strict(db, tmp_path, bad):
    out = tmp_path / "o"
    export_real_dataset(db, out, n=4, seed=1)
    jid = load_dataset(out, require_complete_labels=False).job_dicts[0]["id"]
    with pytest.raises(DatasetError, match="invalid label"):
        parse_annotations(out, _write_annotations(out, {jid: bad}), labeler="a")
    with pytest.raises(DatasetError, match="labeler"):
        parse_annotations(out, out / "annotations.csv", labeler=" ")


def test_duplicate_detection_is_order_independent(db, tmp_path):
    out = tmp_path / "o"
    export_real_dataset(db, out, n=4, seed=1)
    jid = load_dataset(out, require_complete_labels=False).job_dicts[0]["id"]
    for rows in ([("", ), ("accept",)], [("accept",), ("",)]):
        f = out / "dup.jsonl"
        f.write_text("\n".join(json.dumps({"job_id": jid, "expected": r[0]}) for r in rows) + "\n")
        with pytest.raises(DatasetError, match="duplicate job_id"):
            parse_annotations(out, f, labeler="a")


def test_reimport_is_idempotent_merges_and_archives_previous_version(db, tmp_path):
    out = tmp_path / "o"
    export_real_dataset(db, out, n=6, seed=1)
    ids = [j["id"] for j in load_dataset(out, require_complete_labels=False).job_dicts]
    f1 = _write_annotations(out, {ids[0]: "accept", ids[1]: "reject"})
    r1 = import_annotations(out, f1, labeler="alice", labeled_at="2026-10-01T00:00:00+00:00")
    h1 = load_dataset(out, require_complete_labels=False).manifest.labels_sha256
    # same file again, later: nothing changes (no new timestamps, no version bump)
    r_same = import_annotations(out, f1, labeler="alice", labeled_at="2027-01-01T00:00:00+00:00")
    assert r_same["changed"] == 0 and load_dataset(out, require_complete_labels=False).manifest.labels_sha256 == h1
    # a partial file labelling a different job keeps the earlier labels
    only_third = out / "third.jsonl"
    only_third.write_text(json.dumps({"job_id": ids[2], "expected": "borderline"}) + "\n")
    r2 = import_annotations(out, only_third, labeler="bob")
    ds = load_dataset(out, require_complete_labels=False)
    assert r1["version"] == "2" and r2["version"] == "3" and set(ds.labels) == set(ids[:3])
    assert ds.labels[ids[0]].labeler == "alice" and ds.labels[ids[2]].labeler == "bob"
    # empty `expected` never deletes; the archived v1/v2 remain loadable and hash-valid
    assert load_dataset(out / "versions" / "1", require_complete_labels=False).manifest.version == "1"
    assert len(load_dataset(out / "versions" / "2", require_complete_labels=False).labels) == 2


def test_csv_with_bom_and_non_utf8(db, tmp_path):
    out = tmp_path / "o"
    export_real_dataset(db, out, n=3, seed=1)
    assert (out / "annotations.csv").read_bytes().startswith(b"\xef\xbb\xbf")      # Excel-friendly BOM
    parse_annotations(out, out / "annotations.csv", labeler="a")                     # BOM header accepted
    cp = out / "cp1252.csv"
    cp.write_bytes("job_id,expected\nx,accept\né,accept\n".encode("cp1252"))
    with pytest.raises(DatasetError, match="UTF-8"):
        parse_annotations(out, cp, labeler="a")


def test_annotation_unknown_and_duplicate_ids(db, tmp_path):
    out = tmp_path / "o"
    export_real_dataset(db, out, n=4, seed=1)
    bad = out / "bad.jsonl"
    bad.write_text(json.dumps({"job_id": "nope", "expected": "accept"}) + "\n")
    with pytest.raises(DatasetError, match="unknown job_id"):
        parse_annotations(out, bad, labeler="a")
    jid = load_dataset(out, require_complete_labels=False).job_dicts[0]["id"]
    dup = out / "dup.jsonl"
    dup.write_text("\n".join(json.dumps({"job_id": jid, "expected": "accept"}) for _ in range(2)) + "\n")
    with pytest.raises(DatasetError, match="duplicate job_id"):
        parse_annotations(out, dup, labeler="a")


def test_cli_export_validate_import_flow(db, tmp_path, capsys):
    out = tmp_path / "o"
    assert cli_main(["export", "--db", str(db), "--out", str(out), "--n", "6", "--seed", "2"]) == 0
    ids = [j["id"] for j in load_dataset(out, require_complete_labels=False).job_dicts]
    filled = _write_annotations(out, {i: "accept" for i in ids})
    assert cli_main(["annotate-validate", str(out), str(filled)]) == 0
    assert cli_main(["annotate-import", str(out), str(filled), "--labeler", "bob"]) == 0
    assert cli_main(["validate", str(out)]) == 0                     # complete labels now
    assert cli_main(["export", "--db", str(tmp_path / "missing.db"), "--out", str(tmp_path / "x")]) == 2
