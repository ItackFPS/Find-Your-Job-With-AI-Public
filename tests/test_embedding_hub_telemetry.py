"""Importing the embedding service turns Hugging Face Hub telemetry off.

Runs in a fresh interpreter: the flag is read once, when huggingface_hub is first
imported, so it must be set before sentence-transformers loads it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ("import src.embeddings.embedding, huggingface_hub.constants as c; "
         "print(c.HF_HUB_DISABLE_TELEMETRY)")


def _probe(env_value: str | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "HF_HUB_DISABLE_TELEMETRY"}
    if env_value is not None:
        env["HF_HUB_DISABLE_TELEMETRY"] = env_value
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def test_telemetry_off_by_default():
    assert _probe(None) == "True"


def test_user_choice_is_kept():
    assert _probe("0") == "False"
