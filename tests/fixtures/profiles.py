"""Paths to frozen test configuration (issue #9).

Tests must not read the developer's personal ``config/profile.yaml``; use
``TEST_PROFILE_PATH`` instead. Non-personal shared vocabularies
(``config/filters.yaml``, ``config/scoring.yaml``, ...) are still read from
``config/``.
"""

from __future__ import annotations

from pathlib import Path

TEST_PROFILE_PATH = str(Path(__file__).resolve().parent / "config" / "profile.yaml")

# Phase B multi-profile fixtures (evaluation strategy 09 §4). A matching change must not make any of
# them worse beyond its interval; P2 and P4 have no labels and give diagnostics only.
_MULTI = Path(__file__).resolve().parent
MULTI_PROFILES: dict[str, str] = {
    "P1": str(_MULTI / "multi_profile" / "p1_it_junior_sisr.yaml"),     # IT junior, BTS SIO SISR-style
    "P2": str(_MULTI / "multi_profile" / "p2_software_dev_mid.yaml"),   # software developer, 3-5 years
    "P3": str(_MULTI / "evaluation" / "datasets" / "g-mo-t-nurse" / "profile.yaml"),  # non-IT (nurse)
    "P4": str(_MULTI / "multi_profile" / "p4_generalist.yaml"),        # generalist, several lines of work
}
