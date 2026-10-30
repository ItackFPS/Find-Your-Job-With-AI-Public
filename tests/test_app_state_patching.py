"""The test suite patches app.state so that teardown restores it (see ``isolated_app``).

pytest 9.2 changed ``monkeypatch.setattr`` to take the old value from the instance ``__dict__``.
Starlette's State keeps its values in ``_state`` instead, so a ``setattr`` patch was undone by
deleting the attribute: ``app.state.db_path`` disappeared after the first test that used
``isolated_app`` and every later one failed.
"""

from __future__ import annotations

import pytest


def test_isolated_app_patches_are_restored(isolated_app):
    """Run in a nested MonkeyPatch so its teardown can be observed inside this test."""
    state = isolated_app.state
    before = state.db_path
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(state._state, "db_path", "elsewhere.db")
        assert state.db_path == "elsewhere.db"
    assert state.db_path == before


def test_app_state_keeps_its_paths_after_an_isolated_app_test(isolated_app):
    """A second user of the fixture still finds the attributes the first one patched."""
    from src.api.app import app

    for name in ("db_path", "config_dir", "esco_db_path", "runner"):
        assert hasattr(app.state, name), name
