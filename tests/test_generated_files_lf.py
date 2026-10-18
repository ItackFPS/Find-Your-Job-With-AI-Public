"""Hash-pinned or committed files the tooling generates must not depend on the platform newline.

On Windows ``Path.write_text`` turns ``\\n`` into ``\\r\\n``: a lab bundle hashed from its LF text then
fails its own ``verify_bundle``, and ``golden --update`` would commit CRLF into a ``-text`` fixture.
"""

from __future__ import annotations

from pathlib import Path

from src.evaluation.__main__ import main as evaluation_cli
from src.lab.bundle import BUNDLE_FILES, bundle_hash, create_bundle, verify_bundle

ROOT = Path(__file__).resolve().parents[1]
DS = ROOT / "tests" / "fixtures" / "evaluation" / "datasets" / "synthetic-ai-filter-50"
FIXTURE_PROFILE = ROOT / "tests" / "fixtures" / "ai_filter_benchmark" / "profile.yaml"


def test_bundle_files_are_lf_and_verify_against_their_own_hash(tmp_path):
    digest, path = create_bundle(ROOT / "config", {"scoring.yaml": {"scoring": {"weights": {"skills": 0.31}}}},
                                 tmp_path)
    for name in (*BUNDLE_FILES, "overrides.json"):
        assert b"\r" not in (path / name).read_bytes(), name
    assert bundle_hash(path) == digest
    verify_bundle(path, digest)


def test_golden_update_writes_lf(tmp_path):
    out = tmp_path / "golden.json"
    assert evaluation_cli(["golden", str(DS), str(out), "--update", "--profile", str(FIXTURE_PROFILE)]) == 0
    raw = out.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
