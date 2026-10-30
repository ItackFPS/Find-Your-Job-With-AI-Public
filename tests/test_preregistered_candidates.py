"""Pre-registered lab candidates (completion plan P2.2) stay frozen and keep applying to config/."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from src.lab.bundle import create_bundle

ROOT = Path(__file__).resolve().parents[1]
PRE = ROOT / "docs" / "lab" / "preregistered"
FROZEN = {
    "2026-10-30-h1-distance-first-in-order.json": "1a283eec7b944ffffd41949371380c7c18bcda8519e6620dbdca3c1892cf081f",
    "2026-10-30-h2-distance-up-seniority-down.json": "3c7100911ccccd4115215dbcd27d4ae01f7a010a006d747e58a6b2698ec8d2b2",
    "history-2026-10-17/2026-10-h1-location-first-in-order.json":
        "ee9f6171464440e371e555ef9c2de3aeb45be3db1b44fdb8b500cabd2bb72b33",
    "history-2026-10-17/2026-10-h2-location-up-seniority-down.json":
        "70f055efd640398d5e1d559467dcc78e959d2b0446b727170f4fa3398f8c2a12",
}
CURRENT = [name for name in FROZEN if not name.startswith("history")]
WEIGHTS = ("location_fit", "domain_relevance", "skills_match", "seniority_match", "experience_fit")


@pytest.mark.parametrize("name", sorted(FROZEN))
def test_files_are_byte_identical_to_what_was_frozen(name):
    assert hashlib.sha256((PRE / name).read_bytes()).hexdigest() == FROZEN[name]


def test_the_readme_states_the_hashes_of_the_current_candidates():
    readme = (PRE / "README.md").read_text(encoding="utf-8")
    for name in CURRENT:
        assert re.search(rf"`{re.escape(name)}` \| `{FROZEN[name]}`", readme), name


@pytest.mark.parametrize("name", CURRENT)
def test_a_current_candidate_changes_only_existing_weights_and_still_sums_to_one(name, tmp_path):
    overrides = json.loads((PRE / name).read_text(encoding="utf-8"))
    assert list(overrides) == ["scoring.yaml"]
    changed = overrides["scoring.yaml"]["scoring"]
    base = yaml.safe_load((ROOT / "config" / "scoring.yaml").read_text(encoding="utf-8"))["scoring"]
    assert set(changed) <= set(WEIGHTS) and all(set(v) == {"weight"} for v in changed.values())
    assert all("weight" in base[k] for k in changed)
    total = sum(changed.get(k, base[k])["weight"] for k in WEIGHTS)
    assert total == pytest.approx(1.0)
    create_bundle(ROOT / "config", overrides, tmp_path)      # applies to the configuration as it is now
