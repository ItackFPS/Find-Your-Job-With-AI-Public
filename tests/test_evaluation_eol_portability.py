"""Windows portability of hash-pinned evaluation inputs (issue #106).

The strategy fingerprint hashes raw file bytes and the committed golden was generated from LF, so those
files must be LF in every working tree; generated dataset files must not depend on the platform newline.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from src.evaluation.dataset import write_dataset
from src.evaluation.models import LabelRecord

ROOT = Path(__file__).resolve().parent.parent

# Every file whose raw bytes enter a strategy fingerprint (src/decisions/strategy.py, src/evaluation/runner.py)
# apart from the user-owned profile, plus the fixture profile the committed golden is checked against.
FINGERPRINT_INPUTS = [
    "config/filters.yaml",
    "config/scoring.yaml",
    "config/semantic_config.yaml",
    "config/personalization.yaml",
    "tests/fixtures/ai_filter_benchmark/profile.yaml",
]


def test_generated_manifest_uses_lf_newlines_on_every_platform(tmp_path):
    label = LabelRecord(job_id="j1", expected="accept", label_source="human", labeler="t", labeled_at="2026-01-01")
    write_dataset(tmp_path, dataset_id="t", version="1", kind="synthetic", created_at="2026-01-01",
                  as_of="2026-01-01T00:00:00+00:00", jobs=[{"id": "j1"}], labels=[label])
    raw = (tmp_path / "manifest.json").read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"}\n")


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
@pytest.mark.parametrize("rel", FINGERPRINT_INPUTS)
def test_fingerprint_inputs_are_pinned_to_lf_by_gitattributes(rel):
    out = subprocess.run(["git", "check-attr", "eol", "--", rel], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    assert out.stdout.strip().endswith("eol: lf"), f"{rel} must be `eol=lf` in .gitattributes: {out.stdout!r}"
