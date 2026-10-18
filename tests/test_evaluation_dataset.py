"""Strict validation and fixture-sync checks for src/evaluation/dataset.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.evaluation.dataset import DatasetError, load_dataset, write_dataset
from src.evaluation.models import LabelRecord

FIX = Path(__file__).parent / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"


def _label(jid: str, expected: str = "accept") -> LabelRecord:
    return LabelRecord(job_id=jid, expected=expected, label_source="human", labeler="t", labeled_at="2026-01-01")


def _make(tmp_path: Path, jobs=None, labels=None, profile: bytes | None = None) -> Path:
    jobs = jobs if jobs is not None else [{"id": "j1", "title": "Dev"}, {"id": "j2", "title": "Ops"}]
    labels = labels if labels is not None else [_label("j1"), _label("j2", "reject")]
    write_dataset(tmp_path, dataset_id="t", version="1", kind="synthetic", created_at="2026-01-01",
                  as_of="2026-01-01T00:00:00+00:00", jobs=jobs, labels=labels, profile_yaml=profile)
    return tmp_path


def test_roundtrip_is_deterministic(tmp_path):
    a = _make(tmp_path / "a")
    b = _make(tmp_path / "b")
    assert (a / "jobs.jsonl").read_bytes() == (b / "jobs.jsonl").read_bytes()
    ds = load_dataset(a)
    assert [j["id"] for j in ds.job_dicts] == ["j1", "j2"] and set(ds.labels) == {"j1", "j2"}
    assert [j.id for j in ds.build_jobs()] == ["j1", "j2"]


@pytest.mark.parametrize("filename", ["jobs.jsonl", "labels.jsonl"])
def test_tampering_is_detected_by_hash(tmp_path, filename):
    root = _make(tmp_path)
    p = root / filename
    p.write_bytes(p.read_bytes() + b"\n")
    with pytest.raises(DatasetError, match="sha256 mismatch"):
        load_dataset(root)


def test_profile_tampering_detected(tmp_path):
    root = _make(tmp_path, profile=b"a: 1\n")
    (root / "profile.yaml").write_bytes(b"a: 2\n")
    with pytest.raises(DatasetError, match="profile.yaml"):
        load_dataset(root)


def test_unknown_job_field_is_an_error_not_silently_dropped(tmp_path):
    root = _make(tmp_path, jobs=[{"id": "j1", "titel": "typo"}], labels=[_label("j1")])
    with pytest.raises(DatasetError, match="unknown field"):
        load_dataset(root)


def test_missing_or_duplicate_job_id(tmp_path):
    with pytest.raises(DatasetError, match="explicit string 'id'"):
        load_dataset(_make(tmp_path / "a", jobs=[{"title": "x"}], labels=[]))
    with pytest.raises(DatasetError, match="duplicate job id"):
        load_dataset(_make(tmp_path / "b", jobs=[{"id": "j"}, {"id": "j"}], labels=[_label("j")]))


def test_orphan_duplicate_and_missing_labels(tmp_path):
    with pytest.raises(DatasetError, match="orphan label"):
        load_dataset(_make(tmp_path / "a", labels=[_label("j1"), _label("j2"), _label("zzz")]))
    with pytest.raises(DatasetError, match="duplicate label"):
        load_dataset(_make(tmp_path / "b", labels=[_label("j1"), _label("j1"), _label("j2")]))
    root = _make(tmp_path / "c", labels=[_label("j1")])
    with pytest.raises(DatasetError, match="without label"):
        load_dataset(root)
    assert load_dataset(root, require_complete_labels=False).unlabeled_ids == ("j2",)


def test_invalid_label_value_rejected(tmp_path):
    root = _make(tmp_path)
    lines = (root / "labels.jsonl").read_text().splitlines()
    row = json.loads(lines[0])
    row["expected"] = "maybe"
    # rewrite with a consistent manifest hash so only the schema check can fail
    from src.evaluation.fingerprint import sha256_bytes
    data = (json.dumps(row) + "\n" + lines[1] + "\n").encode()
    (root / "labels.jsonl").write_bytes(data)
    m = json.loads((root / "manifest.json").read_text())
    m["labels_sha256"] = sha256_bytes(data)
    (root / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(DatasetError, match="invalid label"):
        load_dataset(root)


def test_committed_synthetic_dataset_matches_python_fixtures(tmp_path):
    """The committed JSONL must stay in sync with tests/fixtures/ai_filter_benchmark/*.py."""
    from scripts.materialize_evaluation_fixtures import build_ai_filter_50

    build_ai_filter_50(tmp_path)
    for name in ("jobs.jsonl", "labels.jsonl", "profile.yaml", "manifest.json"):
        assert (tmp_path / name).read_bytes() == (FIX / name).read_bytes(), (
            f"{name} drifted: run `python scripts/materialize_evaluation_fixtures.py`"
        )


def test_line_separator_characters_inside_text_survive_roundtrip(tmp_path):
    """U+2028/U+2029/NEL appear in scraped HTML; str.splitlines() would break the JSONL."""
    desc = "a\u2028b\u2029c\u0085d"
    root = _make(tmp_path, jobs=[{"id": "j1", "description": desc}], labels=[_label("j1")])
    assert load_dataset(root).job_dicts[0]["description"] == desc


def test_undeclared_profile_is_refused_and_stale_profile_removed(tmp_path):
    root = _make(tmp_path)
    (root / "profile.yaml").write_text("a: 1\n")
    with pytest.raises(DatasetError, match="not declared"):
        load_dataset(root)
    _make(tmp_path, profile=None)        # rewriting without a profile drops the stale file
    assert not (root / "profile.yaml").exists()
    assert load_dataset(root).profile_path is None


def test_build_jobs_has_deterministic_discovered_at(tmp_path):
    root = _make(tmp_path)
    ds = load_dataset(root)
    assert {j.discovered_at.isoformat() for j in ds.build_jobs()} == {"2026-01-01T00:00:00+00:00"}
