"""Which employers publish their offers through a recruiting tool with a public API.

Read from the offers already stored: a link to ``jobs.lever.co/acme`` names Acme's board. Nothing is
guessed and no directory is needed, so this works for any line of work the other sources reach.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_PATTERNS: dict[str, re.Pattern[str]] = {
    "lever": re.compile(r"jobs\.lever\.co/([\w-]+)", re.I),
    "ashby": re.compile(r"jobs\.ashbyhq\.com/([\w-]+)", re.I),
    "smartrecruiters": re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I),
    "recruitee": re.compile(r"\b([\w-]+)\.recruitee\.com", re.I),
    "greenhouse": re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)", re.I),
}
_NOT_EMPLOYERS = frozenset({"api", "embed", "job_board", "jobs", "oneclick-ui", "v1", "www"})
_HOSTS = ("%lever.co%", "%ashbyhq.com%", "%smartrecruiters.com%", "%recruitee.com%", "%greenhouse.io%")
DEFAULT_LIMIT = 300


def boards_in_text(text: str) -> set[tuple[str, str]]:
    """The ``(tool, employer)`` pairs named by the links in ``text``."""
    return {(ats, slug.lower()) for ats, pattern in _PATTERNS.items() for slug in pattern.findall(text or "")
            if slug.lower() not in _NOT_EMPLOYERS}


def boards_from_offers(db_path: str | Path, limit: int = DEFAULT_LIMIT) -> list[dict[str, str]]:
    """Boards named by stored offers, most often seen first. [] when the database cannot be read."""
    where = " OR ".join("(canonical_url LIKE ? OR apply_url LIKE ? OR description LIKE ? OR raw_payload LIKE ?)"
                        for _ in _HOSTS)
    params = [h for host in _HOSTS for h in (host, host, host, host)]
    seen: dict[tuple[str, str], int] = {}
    try:
        conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            rows = conn.execute(
                f"SELECT canonical_url, apply_url, description, raw_payload FROM jobs WHERE {where}",  # noqa: S608
                params).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning("Could not read offers for ATS boards: %s", exc)
        return []
    for row in rows:
        for board in boards_in_text(" ".join(str(v or "") for v in row)):
            seen[board] = seen.get(board, 0) + 1
    ranked = sorted(seen.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [{"ats": ats, "slug": slug} for (ats, slug), _ in ranked]
