"""The suite-wide guard in ``tests/conftest.py``: no test can open a database under the real ``data/``.

Probes never touch real files even if the guard were broken: read-only URIs and a path in a directory
that does not exist fail to open instead of creating anything.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.mark.parametrize("target", [
    lambda: (f"file:{(DATA / 'jobs.db').as_posix()}?mode=ro", {"uri": True}),
    lambda: (os.fsencode(f"file:{(DATA / 'jobs.db').as_posix()}?mode=ro"), {"uri": True}),
    lambda: (DATA / "__no_such_dir__" / "probe.db", {}),
    lambda: (str(DATA / "__no_such_dir__" / "probe.db"), {}),
])
def test_opening_a_real_database_is_refused(target):
    database, kwargs = target()
    with pytest.raises(PermissionError, match="real database"):
        sqlite3.connect(database, **kwargs)


def test_relative_paths_are_resolved_against_the_working_directory(monkeypatch):
    monkeypatch.chdir(DATA.parent)
    with pytest.raises(PermissionError, match="real database"):
        sqlite3.connect("file:data/jobs.db?mode=ro", uri=True)


def test_temporary_databases_still_work(tmp_path):
    sqlite3.connect(tmp_path / "ok.db").close()
    sqlite3.connect(":memory:").close()
