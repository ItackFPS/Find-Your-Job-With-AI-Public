"""ESCO (European Skills, Competences, Qualifications and Occupations) integration.

Provides a local SQLite FTS index of ESCO concepts used for profile autocomplete.

Usage:
    # One-time setup (downloads ESCO data from the public API):
    python scripts/setup_esco.py

    # Runtime autocomplete (requires the index to exist):
    from src.esco.db import EscoDb
    db = EscoDb()
    results = db.search("python", lang="en", limit=10)
"""
