"""Local ESCO SQLite FTS database.

Schema
------
One SQLite database (``data/esco.db``) with two tables:

``esco_concepts``
    Normalised concept storage.

``esco_fts`` (FTS5 virtual table)
    Full-text index over all label fields in all languages.
    Supports instant prefix / phrase search without network access.

Autocomplete flow
-----------------
1. User types in the Profile UI.
2. Frontend debounces (300 ms) and calls ``GET /api/esco/skills?q=<term>&lang=en``.
3. The API queries this module.
4. Results are returned in < 10 ms (pure local SQLite).

Zero external requests at runtime after setup.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_DEFAULT_DB_PATH = "data/esco.db"


@dataclass
class EscoConcept:
    """A single ESCO concept as returned by the autocomplete query."""

    concept_uri: str
    preferred_label_en: str
    preferred_label_fr: str
    alt_labels_en: list[str] = field(default_factory=list)
    alt_labels_fr: list[str] = field(default_factory=list)
    concept_type: str = "skill"  # 'skill' | 'occupation' | 'skill_group'

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept_uri": self.concept_uri,
            "preferred_label_en": self.preferred_label_en,
            "preferred_label_fr": self.preferred_label_fr,
            "alt_labels_en": self.alt_labels_en,
            "alt_labels_fr": self.alt_labels_fr,
            "concept_type": self.concept_type,
        }

    @property
    def keyword(self) -> str:
        """Simplified pipeline keyword: preferred English label, stripped of
        parenthetical additions.  Used in ``target_skills`` for keyword matching.

        Example: ``"Python (programming language)"`` → ``"Python"``
        """
        label = self.preferred_label_en
        if "(" in label:
            label = label[: label.index("(")].strip()
        return label


class EscoDb:
    """Thin wrapper around the local ESCO SQLite FTS database.

    The database is created lazily on first write.  Read operations on a
    non-existent database return empty results so the rest of the application
    degrades gracefully until ``setup_esco.py`` has been run.
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_db(self) -> None:
        """Create the ESCO tables and FTS index if they do not exist."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS esco_concepts (
                    concept_uri       TEXT PRIMARY KEY,
                    preferred_label_en TEXT NOT NULL DEFAULT '',
                    preferred_label_fr TEXT NOT NULL DEFAULT '',
                    alt_labels_en      TEXT NOT NULL DEFAULT '',
                    alt_labels_fr      TEXT NOT NULL DEFAULT '',
                    concept_type       TEXT NOT NULL DEFAULT 'skill'
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS esco_fts USING fts5(
                    concept_uri UNINDEXED,
                    preferred_label_en,
                    preferred_label_fr,
                    alt_labels_en,
                    alt_labels_fr,
                    content='esco_concepts',
                    content_rowid='rowid'
                );

                CREATE TRIGGER IF NOT EXISTS esco_ai AFTER INSERT ON esco_concepts BEGIN
                    INSERT INTO esco_fts(rowid, concept_uri, preferred_label_en,
                                        preferred_label_fr, alt_labels_en, alt_labels_fr)
                    VALUES (new.rowid, new.concept_uri, new.preferred_label_en,
                            new.preferred_label_fr, new.alt_labels_en, new.alt_labels_fr);
                END;

                CREATE TRIGGER IF NOT EXISTS esco_au AFTER UPDATE ON esco_concepts BEGIN
                    INSERT INTO esco_fts(esco_fts, rowid, concept_uri, preferred_label_en,
                                        preferred_label_fr, alt_labels_en, alt_labels_fr)
                    VALUES ('delete', old.rowid, old.concept_uri, old.preferred_label_en,
                            old.preferred_label_fr, old.alt_labels_en, old.alt_labels_fr);
                    INSERT INTO esco_fts(rowid, concept_uri, preferred_label_en,
                                        preferred_label_fr, alt_labels_en, alt_labels_fr)
                    VALUES (new.rowid, new.concept_uri, new.preferred_label_en,
                            new.preferred_label_fr, new.alt_labels_en, new.alt_labels_fr);
                END;

                CREATE TRIGGER IF NOT EXISTS esco_ad AFTER DELETE ON esco_concepts BEGIN
                    INSERT INTO esco_fts(esco_fts, rowid, concept_uri, preferred_label_en,
                                        preferred_label_fr, alt_labels_en, alt_labels_fr)
                    VALUES ('delete', old.rowid, old.concept_uri, old.preferred_label_en,
                            old.preferred_label_fr, old.alt_labels_en, old.alt_labels_fr);
                END;
            """)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert_concept(self, concept: EscoConcept) -> None:
        """Insert or replace a single concept."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO esco_concepts
                    (concept_uri, preferred_label_en, preferred_label_fr,
                     alt_labels_en, alt_labels_fr, concept_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    concept.concept_uri,
                    concept.preferred_label_en,
                    concept.preferred_label_fr,
                    "|".join(concept.alt_labels_en),
                    "|".join(concept.alt_labels_fr),
                    concept.concept_type,
                ),
            )

    def upsert_many(self, concepts: list[EscoConcept]) -> None:
        """Bulk insert or replace concepts (one transaction for speed)."""
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT OR REPLACE INTO esco_concepts
                    (concept_uri, preferred_label_en, preferred_label_fr,
                     alt_labels_en, alt_labels_fr, concept_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        c.concept_uri,
                        c.preferred_label_en,
                        c.preferred_label_fr,
                        "|".join(c.alt_labels_en),
                        "|".join(c.alt_labels_fr),
                        c.concept_type,
                    )
                    for c in concepts
                ],
            )

    # ------------------------------------------------------------------
    # Read — autocomplete
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        lang: str = "en",
        limit: int = 10,
        concept_type: str | None = None,
    ) -> list[EscoConcept]:
        """Full-text search across all label fields.

        Args:
            query:        User-typed text (may be partial, accented, etc.).
            lang:         Hint for result ordering — ``"en"`` or ``"fr"``.
                          The FTS index covers both languages regardless.
            limit:        Maximum results to return.
            concept_type: Optional filter — ``"skill"``, ``"occupation"``, etc.

        Returns:
            List of matching concepts, most relevant first.
        """
        if not self._db_path.exists():
            return []

        # Strip dangerous FTS5 special characters.  We need at most a prefix
        # wildcard, so stripping everything else is safe.
        clean = _sanitize_fts_query(query)
        if not clean:
            return []

        try:
            with self._connect() as conn:
                # FTS5 query: match against all indexed columns, prefer matches
                # in the language the user is likely typing in.
                fts_query = clean + "*"  # prefix match

                if concept_type:
                    rows = conn.execute(
                        """
                        SELECT c.concept_uri, c.preferred_label_en, c.preferred_label_fr,
                               c.alt_labels_en, c.alt_labels_fr, c.concept_type
                        FROM esco_fts f
                        JOIN esco_concepts c ON c.rowid = f.rowid
                        WHERE esco_fts MATCH ? AND c.concept_type = ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, concept_type, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT c.concept_uri, c.preferred_label_en, c.preferred_label_fr,
                               c.alt_labels_en, c.alt_labels_fr, c.concept_type
                        FROM esco_fts f
                        JOIN esco_concepts c ON c.rowid = f.rowid
                        WHERE esco_fts MATCH ?
                        ORDER BY rank
                        LIMIT ?
                        """,
                        (fts_query, limit),
                    ).fetchall()

                return [_row_to_concept(r) for r in rows]
        except sqlite3.OperationalError:
            # FTS table not initialised yet — return empty gracefully.
            return []

    def count(self) -> int:
        """Return the number of concepts in the database."""
        if not self._db_path.exists():
            return 0
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(*) FROM esco_concepts").fetchone()
                return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            return 0

    def is_ready(self) -> bool:
        """True when the database exists and contains at least one concept."""
        return self.count() > 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        # See src/linkedin/staging.py for why this is needed: several
        # modules open their own connection to the same jobs.db file.
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _sanitize_fts_query(raw: str) -> str:
    """Remove FTS5 special characters that could cause parse errors.

    Allows letters, digits, spaces, hyphens, dots, and underscores.
    Trims the result and collapses extra whitespace.
    """
    import re
    cleaned = re.sub(r'[^\w\s\-.]', ' ', raw, flags=re.UNICODE)
    return " ".join(cleaned.split())


def _row_to_concept(row: sqlite3.Row) -> EscoConcept:
    alt_en_raw: str = row["alt_labels_en"] or ""
    alt_fr_raw: str = row["alt_labels_fr"] or ""
    return EscoConcept(
        concept_uri=row["concept_uri"],
        preferred_label_en=row["preferred_label_en"],
        preferred_label_fr=row["preferred_label_fr"],
        alt_labels_en=[s for s in alt_en_raw.split("|") if s],
        alt_labels_fr=[s for s in alt_fr_raw.split("|") if s],
        concept_type=row["concept_type"],
    )
