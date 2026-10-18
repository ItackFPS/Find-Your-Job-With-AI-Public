"""Regression: tls-client must be loaded before torch / scikit-learn.

tls-client (python-jobspy's HTTP backend, used by the LinkedIn scanner) bundles a
Go DLL that aborts the whole backend on Windows when it is first loaded after
torch + scikit-learn (pulled in by sentence-transformers). The backend loads
those at startup via src.embeddings.embedding, so that module preloads tls_client.

Checked in a fresh interpreter: modules imported earlier in this pytest process
would otherwise make the load order meaningless.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("tls_client")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import json, sys
import src.embeddings.embedding
order = list(sys.modules)
print(json.dumps({name: order.index(name) if name in order else None
                  for name in ("tls_client", "torch", "sklearn")}))
"""


def test_embedding_module_loads_tls_client_before_torch_and_sklearn():
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr

    positions = json.loads(result.stdout.strip().splitlines()[-1])
    assert positions["tls_client"] is not None, f"tls_client was not loaded: {positions}"
    assert positions["torch"] is not None, f"torch was not loaded: {positions}"
    assert positions["tls_client"] < positions["torch"], positions
    if positions["sklearn"] is not None:
        assert positions["tls_client"] < positions["sklearn"], positions
