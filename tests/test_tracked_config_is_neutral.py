"""The tracked config defaults carry no one's personal setup.

Personal values live in the git-ignored config/<name>.local.yaml files (see
src/config_files.py); the Profile page writes there. These
checks catch a personal file committed over a default by mistake.
"""

from __future__ import annotations

from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config"


def test_profile_default_is_the_example():
    text = (CONFIG / "profile.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Example candidate profile"), "personal profiles belong in profile.local.yaml"


def test_unpinned_evaluation_uses_the_profile_in_effect(tmp_path):
    """A dataset that pins no profile (real exports pin none) is replayed with the
    profile production uses, so the neutral tracked example never silently replaces
    the personal profile.local.yaml."""
    from src.evaluation.runner import default_profile_path

    (tmp_path / "profile.yaml").write_text("example: true\n", encoding="utf-8")
    assert default_profile_path(tmp_path) == str(tmp_path / "profile.yaml")
    (tmp_path / "profile.local.yaml").write_text("mine: true\n", encoding="utf-8")
    assert default_profile_path(tmp_path) == str(tmp_path / "profile.local.yaml")


def test_feedback_dataset_pins_the_profile_in_effect(tmp_path, monkeypatch):
    import src.evaluation.feedback_dataset as fd
    from src.evaluation.__main__ import main

    (tmp_path / "profile.yaml").write_text("example: true\n", encoding="utf-8")
    (tmp_path / "profile.local.yaml").write_text("mine: true\n", encoding="utf-8")
    seen: dict = {}

    def fake_build(db, out, **kwargs):
        seen.update(kwargs)
        return {"status": "ok", "n_jobs": 0, "labels": 0, "skipped": 0, "thresholds": {}}

    monkeypatch.setattr(fd, "build_feedback_dataset", fake_build)
    assert main(["feedback-dataset", "--out", str(tmp_path / "out"), "--config-dir", str(tmp_path),
                 "--db", str(tmp_path / "jobs.db")]) == 0
    assert seen["profile_path"] == str(tmp_path / "profile.local.yaml")
