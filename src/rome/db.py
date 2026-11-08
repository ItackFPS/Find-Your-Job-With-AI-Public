"""Local ROME SQLite FTS index.

ROME 4.0 is France Travail's directory of jobs and professional domains, published
as open data under the Licence Ouverte ROME. ``scripts/setup_rome.py`` fills this
index once; the Profile page's domain field then searches it locally, so there is
no network request at runtime. Reads on a missing database return nothing, so the
rest of the application works before the setup has run.

Reuse requires naming France Travail and the date of the last update: both are kept
in ``meta()`` (``version``, ``published``) and shown by the API.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_DB_PATH = "data/rome.db"


@dataclass(frozen=True)
class RomeEntry:
    """A ROME professional domain or job sheet."""

    code: str  # "M1203" (job sheet), "M12" (professional domain), "M" (broad domain)
    label: str
    kind: str  # "domain" | "occupation"
    group: str = ""  # the professional domain a job sheet belongs to
    aliases: list[str] = field(default_factory=list)  # other job titles (ROME appellations)

    @property
    def keyword(self) -> str:
        """The label without its feminine form ("Chef / Cheffe de cuisine" -> "Chef de cuisine"):
        what the profile stores, since the title filter reads a domain as a literal title word.
        The feminine form repeats the masculine words one for one; words beyond them are shared."""
        masculine, sep, feminine = self.label.partition(" / ")
        if not sep or "/" in feminine:
            return self.label
        m_words, f_words = masculine.split(), feminine.split()
        return " ".join(m_words + f_words[len(m_words):])


class RomeDb:
    def __init__(self, db_path: str = _DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)

    def replace_all(self, entries: list[RomeEntry], meta: dict[str, str]) -> None:
        """Replace the whole index (one transaction: readers never see a half-filled one)."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                DROP TABLE IF EXISTS rome_entries;
                DROP TABLE IF EXISTS rome_fts;
                DROP TABLE IF EXISTS rome_meta;
                CREATE TABLE rome_entries (
                    code TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL,
                    grp TEXT NOT NULL DEFAULT '', aliases TEXT NOT NULL DEFAULT ''
                );
                CREATE VIRTUAL TABLE rome_fts USING fts5(
                    label, grp, aliases, tokenize='unicode61 remove_diacritics 2'
                );
                CREATE TABLE rome_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            for e in entries:
                cur = conn.execute(
                    "INSERT INTO rome_entries (code, label, kind, grp, aliases) VALUES (?, ?, ?, ?, ?)",
                    (e.code, e.label, e.kind, e.group, "|".join(e.aliases)))
                conn.execute("INSERT INTO rome_fts (rowid, label, grp, aliases) VALUES (?, ?, ?, ?)",
                             (cur.lastrowid, e.label, e.group, " ".join(e.aliases)))
            conn.executemany("INSERT INTO rome_meta (key, value) VALUES (?, ?)", list(meta.items()))

    def search(self, query: str, limit: int = 10, kind: str | None = None) -> list[RomeEntry]:
        """Entries whose label, group or aliases start with every word typed; label matches first."""
        words = re.findall(r"\w+", query, flags=re.UNICODE)
        if not words or not self._db_path.exists():
            return []
        fts = " ".join(f'"{w}"*' for w in words)
        sql = ("SELECT e.code, e.label, e.kind, e.grp, e.aliases FROM rome_fts "
               "JOIN rome_entries e ON e.rowid = rome_fts.rowid WHERE rome_fts MATCH ?")
        args: list[object] = [fts]
        if kind:
            sql += " AND e.kind = ?"
            args.append(kind)
        sql += " ORDER BY bm25(rome_fts, 10.0, 2.0, 1.0), e.code LIMIT ?"
        args.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:  # index not built yet
            return []
        return [RomeEntry(r["code"], r["label"], r["kind"], r["grp"], [a for a in r["aliases"].split("|") if a])
                for r in rows]

    def count(self) -> int:
        return self._scalar("SELECT COUNT(*) FROM rome_entries") or 0

    def is_ready(self) -> bool:
        return self.count() > 0

    def meta(self) -> dict[str, str]:
        if not self._db_path.exists():
            return {}
        try:
            with self._connect() as conn:
                return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM rome_meta")}
        except sqlite3.OperationalError:
            return {}

    def _scalar(self, sql: str) -> int | None:
        if not self._db_path.exists():
            return None
        try:
            with self._connect() as conn:
                row = conn.execute(sql).fetchone()
                return int(row[0]) if row else None
        except sqlite3.OperationalError:
            return None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
