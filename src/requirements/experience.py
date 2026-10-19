"""Minimum experience requirement extraction from job descriptions.

Extracts explicit mandatory/preferred year-of-experience requirements.
Only patterns with a clear mandatory or preferred qualifier are returned —
bare "N ans d'expérience" without a qualifier is intentionally ignored to
avoid penalising jobs that merely mention years as context, not constraints.
"""

from __future__ import annotations

import re

from src.requirements.models import EvidenceSpan, ExperienceRequirement, Importance

# Shorthand: matches "ans", "an", "année", "années", "annee", "annees"
# Used inline in every French pattern.
_FR_YEAR_UNIT = r"(?:ann?[eé][eé]?es?|ans?)"

# French/English year patterns with explicit mandatory/preferred context.
# Each tuple: (compiled pattern, importance).
# Group 1 captures the integer year count.
_EXPERIENCE_PATTERNS: list[tuple[re.Pattern, Importance]] = [
    # ── French mandatory ──────────────────────────────────────────────────────
    # "au moins N ans" / "au moins 4\n  **ans**" (handles markdown bold + newlines)
    (re.compile(
        r"\bau\s+moins\s+([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\b", re.I
    ), Importance.MANDATORY),
    # "au minimum N ans"
    (re.compile(
        r"\bau\s+minimum\s+([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\b", re.I
    ), Importance.MANDATORY),
    # "minimum N ans" / "minimum de N ans"
    (re.compile(
        r"\bminimum\s+(?:de?\s+)?([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\b", re.I
    ), Importance.MANDATORY),
    # "N ans minimum"
    (re.compile(
        r"\b([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\s+minimum\b", re.I
    ), Importance.MANDATORY),
    # "N+ ans"
    (re.compile(
        r"\b([1-9][0-9]?)\s*\+\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\b", re.I
    ), Importance.MANDATORY),
    # "N ans ou plus"
    (re.compile(
        r"\b([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\s+ou\s+plus\b", re.I
    ), Importance.MANDATORY),

    # ── English mandatory ─────────────────────────────────────────────────────
    # "at least N years"
    (re.compile(r"\bat\s+least\s+([1-9][0-9]?)\s+years?\b", re.I), Importance.MANDATORY),
    # "minimum N years"
    (re.compile(r"\bminimum\s+([1-9][0-9]?)\s+years?\b", re.I), Importance.MANDATORY),
    # "N+ years"
    (re.compile(r"\b([1-9][0-9]?)\+\s*years?\b", re.I), Importance.MANDATORY),
    # "N years or more" / "N years minimum"
    (re.compile(r"\b([1-9][0-9]?)\s+years?\s+(?:or\s+more|minimum)\b", re.I), Importance.MANDATORY),

    # ── French preferred ──────────────────────────────────────────────────────
    # "idéalement N ans"
    (re.compile(
        r"\bid[eé]alement\s+([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\b", re.I
    ), Importance.PREFERRED),
    # "N ans souhaité/e"
    (re.compile(
        r"\b([1-9][0-9]?)\s*\*{0,2}\s*" + _FR_YEAR_UNIT + r"\s+souhait[eé][e]?\b", re.I
    ), Importance.PREFERRED),

    # ── English preferred ─────────────────────────────────────────────────────
    # "ideally N years"
    (re.compile(r"\bideally\s+([1-9][0-9]?)\s+years?\b", re.I), Importance.PREFERRED),
    # "N years preferred"
    (re.compile(r"\b([1-9][0-9]?)\s+years?\s+preferred\b", re.I), Importance.PREFERRED),
]


# The lower bound of a year range ending right where a pattern's year count starts:
# "3 à 8", "2-4", "3 – 5", "entre 2 et 6", "3 to 5", "3 ans à 5". Anchored at the end ($ with
# search(pos, endpos)), so only a number joined to the count by a range connector qualifies.
_RANGE_LOWER = re.compile(
    r"\b([0-9]|1[0-9])\s*(?:" + _FR_YEAR_UNIT + r"\s*)?(?:à|a|au|-|–|—|to|et|and)\s*$", re.I
)


def extract_experience(text: str) -> ExperienceRequirement | None:
    """Extract the most significant explicit minimum experience requirement.

    Returns None when no qualifying pattern is found.

    Resolution: highest-year MANDATORY wins; if none, highest-year PREFERRED.
    Year must be in [1, 20] to reject page numbers, version strings, etc.
    """
    if not text:
        return None

    # (years, raw_text, match_start, match_end)
    best_mandatory: tuple[int, str, int, int] | None = None
    best_preferred: tuple[int, str, int, int] | None = None

    for pattern, importance in _EXPERIENCE_PATTERNS:
        for match in pattern.finditer(text):
            years = int(match.group(1))
            if years < 1 or years > 20:
                continue
            m_start = match.start()
            # "3 à 8 ans minimum": the qualifier follows the range's upper bound, but the
            # requirement is its lower bound (a 3-year candidate is in range).
            lower = _RANGE_LOWER.search(text, max(0, match.start(1) - 16), match.start(1))
            if lower is not None and int(lower.group(1)) < years:
                years, m_start = int(lower.group(1)), lower.start(1)
                if years == 0:              # "0 à 2 ans": no minimum at all
                    continue
            raw_text = text[max(0, m_start - 20) : match.end() + 20].strip()
            if importance == Importance.MANDATORY:
                if best_mandatory is None or years > best_mandatory[0]:
                    best_mandatory = (years, raw_text, m_start, match.end())
            else:
                if best_preferred is None or years > best_preferred[0]:
                    best_preferred = (years, raw_text, m_start, match.end())

    if best_mandatory is not None:
        years, raw_text, start, end = best_mandatory
        return ExperienceRequirement(
            min_years=years,
            importance=Importance.MANDATORY,
            raw_text=raw_text,
            span=EvidenceSpan(
                text=raw_text,
                start=start,
                end=end,
                field="description",
                extractor_id="experience.min_years.v2",
            ),
        )
    if best_preferred is not None:
        years, raw_text, start, end = best_preferred
        return ExperienceRequirement(
            min_years=years,
            importance=Importance.PREFERRED,
            raw_text=raw_text,
            span=EvidenceSpan(
                text=raw_text,
                start=start,
                end=end,
                field="description",
                extractor_id="experience.preferred_years.v2",
            ),
        )
    return None
