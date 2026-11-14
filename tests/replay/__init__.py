"""Replay harness (Phase B, B1-C): run the whole product pipeline without contacting any job source.

Test and development tooling only. It lives under ``tests/`` (never installed: ``pyproject.toml`` packages
``src*`` only) and is never registered in the product's collector registry
(``tests/test_replay_harness.py`` checks both).
"""
