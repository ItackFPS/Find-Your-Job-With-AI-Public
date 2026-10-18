"""Paths to frozen test configuration (issue #9).

Tests must not read the developer's personal ``config/profile.yaml``; use
``TEST_PROFILE_PATH`` instead. Non-personal shared vocabularies
(``config/filters.yaml``, ``config/scoring.yaml``, ...) are still read from
``config/``.
"""

from __future__ import annotations

from pathlib import Path

TEST_PROFILE_PATH = str(Path(__file__).resolve().parent / "config" / "profile.yaml")
