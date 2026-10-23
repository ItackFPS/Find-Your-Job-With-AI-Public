"""Issue #69: temp files written during atomic saves must never linger or be
committable — .env.tmp holds every secret in .env.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "name",
    [".env.tmp", "config/notifications.local.yaml.tmp", "config/profile.local.yaml.tmp"],
)
def test_temp_files_are_gitignored(name: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", name], cwd=_REPO_ROOT, check=False)
    assert result.returncode == 0, f"{name} must be gitignored"
