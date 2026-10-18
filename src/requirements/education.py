"""Education level normalization and extraction from job descriptions.

French education system (Bac+N) and English equivalents are both handled.
The extractor returns the education requirement with its importance level.
"""

from __future__ import annotations

import re
import unicodedata

from src.requirements.models import (
    EducationKind,
    EducationRequirement,
    EvidenceSpan,
    Importance,
)

# ---------------------------------------------------------------------------
# Normalized Bac+N hierarchy (integer comparison: higher = more educated)
# ---------------------------------------------------------------------------

# Map of canonical key → Bac+N integer level.
# Used for comparing user max_education vs job requirement.
EDUCATION_LEVEL_MAP: dict[str, int] = {
    # French levels
    "bac": 0,
    "bac+1": 1,
    "bac+2": 2,
    "bts": 2,
    "dut": 2,
    "deust": 2,
    "bac+3": 3,
    "licence": 3,
    "bachelor": 3,
    "bac+4": 4,
    "master 1": 4,
    "m1": 4,
    "maitrise": 4,
    "bac+5": 5,
    "master": 5,
    "master 2": 5,
    "m2": 5,
    "msc": 5,
    "dea": 5,
    "dess": 5,
    "ingenieur": 5,
    "grande ecole": 5,
    "bac+8": 8,
    "doctorat": 8,
    "phd": 8,
    # English levels
    "high school": 0,
    "hnd": 2,
    "hnc": 2,
    "associate": 2,
    "bs": 3,
    "ba": 3,
    "bsc": 3,
    "ms": 5,
    "mba": 5,
    "doctorate": 8,
}


def parse_education_level(text: str) -> int | None:
    """Return the Bac+N integer for a normalized education string, or None."""
    key = _norm(text.strip())
    # Direct lookup
    if key in EDUCATION_LEVEL_MAP:
        return EDUCATION_LEVEL_MAP[key]
    # Handle "bac+N" variants written without exact spaces
    m = re.match(r"bac\s*\+?\s*(\d)", key)
    if m:
        return int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Regex patterns: (pattern, Bac+N level, display label)
# Order matters — more specific patterns before generic ones.
# ---------------------------------------------------------------------------

# ASCII ', typographic ’ (U+2019) and ‘ (U+2018): postings copied from websites or
# word processors use all three (issue #33).
_APOS = "['’‘]"


_EDUCATION_PATTERNS: list[tuple[re.Pattern[str], int, str]] = [
    # ── Bac+N explicit notation ──────────────────────────────────────────
    (re.compile(r"bac\s*\+\s*8", re.I), 8, "Bac+8"),
    (re.compile(r"bac\s*\+\s*5", re.I), 5, "Bac+5"),
    (re.compile(r"bac\s*\+\s*4", re.I), 4, "Bac+4"),
    (re.compile(r"bac\s*\+\s*3", re.I), 3, "Bac+3"),
    (re.compile(r"bac\s*\+\s*2", re.I), 2, "Bac+2"),
    (re.compile(r"bac\s*\+\s*1", re.I), 1, "Bac+1"),
    (re.compile(r"\bbac\b(?!\s*\+)", re.I), 0, "Bac"),

    # ── French qualifications ────────────────────────────────────────────
    # PhD / Doctorat (before Master to avoid matching "Docteur en Master")
    (re.compile(r"\bdoctorat\b", re.I), 8, "Doctorat"),
    (re.compile(r"\bph\.?d\b", re.I), 8, "PhD"),

    # Diplôme d'ingénieur / grandes écoles → Bac+5
    (re.compile(r"dipl[oô]me\s+d" + _APOS + r"ing[eé]nieur", re.I), 5, "Diplôme d'ingénieur"),
    (re.compile(r"\bgrande[s]?\s+[eé]cole[s]?\b", re.I), 5, "Grande école"),
    (re.compile(r"\b[eé]cole\s+d" + _APOS + r"ing[eé]nieur[s]?\b", re.I), 5, "École d'ingénieur"),

    # Master 2 / M2 (before plain Master)
    (re.compile(r"\bmaster\s*2\b", re.I), 5, "Master 2"),
    (re.compile(r"\bm\s*2\b", re.I), 5, "M2"),
    (re.compile(r"\bdea\b", re.I), 5, "DEA"),
    (re.compile(r"\bdess\b", re.I), 5, "DESS"),

    # Master / MSc (generic)
    # Not role/data jargon: "Scrum Master", "Master Data Management (MDM)" (#51).
    (re.compile(r"(?<!scrum\s)\bmaster\b(?!\s+data\s+management\b|\s*\(\s*mdm\b)", re.I), 5, "Master"),
    (re.compile(r"\bmsc\b", re.I), 5, "MSc"),
    (re.compile(r"\bmba\b", re.I), 5, "MBA"),

    # Master 1 / Maîtrise (before Bac+3 Licence)
    (re.compile(r"\bmaster\s*1\b", re.I), 4, "Master 1"),
    (re.compile(r"\bm\s*1\b", re.I), 4, "M1"),
    # "maîtrise de/d'/du/des X" = mastery of a skill, NOT the education degree
    # ("Maîtrise d'Excel requise" must not become a Bac+4 requirement, issue
    # #31). Negative lookahead excludes that usage; "Maîtrise en informatique"
    # and "titulaire d'une Maîtrise" still match.
    (re.compile(r"\bma[îi]trise\b(?!\s+(?:de|du|des)\b|\s+d['’‘])", re.I), 4, "Maîtrise"),

    # Licence / Bachelor (Bac+3)
    (re.compile(r"\blicence\b", re.I), 3, "Licence"),
    (re.compile(r"\bbachelor\b", re.I), 3, "Bachelor"),
    (re.compile(r"\bb\.?sc\b", re.I), 3, "BSc"),
    (re.compile(r"\bb\.?[as]\b(?!\s*\+)", re.I), 3, "BA/BS"),

    # BTS / DUT / DEUST (Bac+2)
    (re.compile(r"\bbts\b", re.I), 2, "BTS"),
    (re.compile(r"\bdut\b", re.I), 2, "DUT"),
    (re.compile(r"\bdeust\b", re.I), 2, "DEUST"),
    (re.compile(r"\bhnd\b", re.I), 2, "HND"),
    (re.compile(r"\bhnc\b", re.I), 2, "HNC"),
]

# ---------------------------------------------------------------------------
# Importance modifiers — context scanned in a window around the education term
# ---------------------------------------------------------------------------

_MANDATORY_PATTERNS = [
    re.compile(r"\bminimum\b", re.I),
    re.compile(r"\bau\s+moins\b", re.I),
    re.compile(r"\bau\s+minimum\b", re.I),
    re.compile(r"\brequ[ie]se?\b", re.I),
    re.compile(r"\bobligatoire\b", re.I),
    re.compile(r"\bimpératif\b", re.I),
    re.compile(r"\bimperatif\b", re.I),
    re.compile(r"\brequired\b", re.I),
    re.compile(r"\bmandatory\b", re.I),
    re.compile(r"\bessential\b", re.I),
    re.compile(r"\bexig[eé]\b", re.I),
    re.compile(r"\bnécessaire\b", re.I),
    re.compile(r"\bnecessaire\b", re.I),
    re.compile(r"\bminimum\s+requis\b", re.I),
    re.compile(r"\bniveau\s+minimum\b", re.I),
    # Degree-holding: candidate must hold this diploma
    re.compile(r"\btitulaire\s+d" + _APOS, re.I),
]

# NOTE — "vous préparez un Bac+N" / "en cours de préparation d'un Bac+N"
# ("student-targeting" language) is deliberately NOT in _MANDATORY_PATTERNS
# (nor _PREFERRED_PATTERNS). These phrases describe a candidate who is
# CURRENTLY PURSUING the diploma — typically an apprenticeship/alternance
# posting — not one who already holds it, so it is not equivalent to a
# mandatory-completed requirement. A bare mention with no other qualifier
# therefore falls through _extract_importance's UNKNOWN default (soft
# penalty only, never a hard reject), while an explicit accompanying
# "minimum"/"requis" elsewhere in the same context still independently
# triggers MANDATORY as before. Previously these phrases WERE classified as
# MANDATORY (same bucket as "requis"/"titulaire d'"), which hard-rejected
# real jobs solely because the posting targets students actively enrolled in
# a program — see the 2026-09-05 real-job benchmark's Job 39 finding.

_PREFERRED_PATTERNS = [
    re.compile(r"\bsouhait[eé][e]?\b", re.I),
    re.compile(r"\bid[eé]alement\b", re.I),
    re.compile(r"\bde\s+pr[eé]f[eé]rence\b", re.I),
    re.compile(r"\bpr[eé]f[eé]r[eé][e]?\b", re.I),
    re.compile(r"\bappr[eé]ci[eé][e]?\b", re.I),
    re.compile(r"\bun\s+plus\b", re.I),
    re.compile(r"\ba\s+plus\b", re.I),
    re.compile(r"\bnice\s+to\s+have\b", re.I),
    re.compile(r"\bpreferred\b", re.I),
    re.compile(r"\bideally\b", re.I),
    re.compile(r"\bdesirable\b", re.I),
    re.compile(r"\bou\s+[eé]quivalente?\b", re.I),   # "ou équivalent/équivalente" softens
    re.compile(r"\bou\s+[eé]quivalence\b", re.I),
    re.compile(r"\bou\s+exp[eé]rience\s+[eé]quivalente?\b", re.I),  # "ou expérience équivalente"
    re.compile(r"\bsinon\s+exp[eé]rience\s+[eé]quivalente?\b", re.I),  # "sinon expérience équivalente"
    re.compile(r"\bserait\s+un\s+atout\b", re.I),
    re.compile(r"\bun\s+atout\b", re.I),
    re.compile(r"\bbienvenu[e]?\b", re.I),
]

# OR-alternative phrases — subset of _PREFERRED_PATTERNS.
# When present, the candidate may satisfy either the education branch OR the
# alternative (e.g. "ou expérience équivalente"), so no score penalty is due.
# NOTE: this does NOT exempt the requirement from the hard education ceiling
# (see EducationRequirement.is_or_alternative's docstring) — a generic "ou
# équivalent" still names an explicit level and is rejected like any other
# excess. Only the narrower _EXPERIENCE_ALTERNATIVE_PATTERNS subset below
# (professional experience explicitly substituting for the degree) exempts
# the hard ceiling, via is_experience_alternative.
_OR_ALTERNATIVE_PATTERNS = [
    re.compile(r"\bou\s+[eé]quivalente?\b", re.I),
    re.compile(r"\bou\s+[eé]quivalence\b", re.I),
    re.compile(r"\bou\s+exp[eé]rience\s+[eé]quivalente?\b", re.I),
    re.compile(r"\bsinon\s+exp[eé]rience\s+[eé]quivalente?\b", re.I),
]

# Narrower subset of _OR_ALTERNATIVE_PATTERNS: specifically professional
# experience substituting for the degree, never a generic "ou équivalent".
# This is a genuinely different, well-defined substitute path — not just
# softer wording of the same degree requirement — so it DOES exempt the
# requirement from the hard education ceiling.
_EXPERIENCE_ALTERNATIVE_PATTERNS = [
    re.compile(r"\bou\s+exp[eé]rience\s+[eé]quivalente?\b", re.I),
    re.compile(r"\bsinon\s+exp[eé]rience\s+[eé]quivalente?\b", re.I),
]


def _norm(text: str) -> str:
    """Lowercase + strip diacritics."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


_CONTEXT_CHARS = 120

# Punctuation that ends a clause. Only used to split text BETWEEN two
# education mentions (see _clause_bounds) — never to shorten the context of a
# mention that has no neighbour, so "Bac+5. Ce diplôme est obligatoire."
# keeps its existing reading.
_CLAUSE_SEPARATOR = re.compile(r"[,;.!?\n•|]")


# Numeric experience durations ("3 ans", "2 années", "4 years"). Used only as
# clause boundaries for education context (issue #41): a qualifier such as
# "au moins" in "…, vous justifiez d'au moins 3 ans d'expérience" belongs to
# the experience clause, not to a degree in the previous clause. Wordings
# without a number ("expérience significative") are deliberately not
# boundaries — whether a nearby qualifier refers to the degree is ambiguous.
_EXPERIENCE_DURATION = re.compile(
    r"\b[1-9][0-9]?\s*\+?\s*(?:ann?[eé][eé]?es?|ans?|years?)\b", re.I
)


def _clause_bounds(
    text: str,
    match_start: int,
    match_end: int,
    other_spans: list[tuple[int, int]],
    experience_spans: list[tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """Limits of the context window for one education mention.

    A qualifier belongs to the mention in its own clause: when another
    education mention lies within the window and a clause separator sits
    between the two, the window stops at that separator. "Bac+5 souhaité,
    Bac+2 minimum requis." gives Bac+5 " souhaité" and Bac+2 " minimum
    requis." instead of both seeing both qualifiers (issue #19). Mentions
    with no separator between them ("BTS ou DUT requis", "Master (Bac+5)
    souhaité") keep sharing qualifiers, as before. Punctuation inside another
    mention (the dot in "B.Sc") is not a separator.

    ``experience_spans`` (numeric durations such as "3 ans", issue #41) bound
    the window at the separator CLOSEST to the duration: only the clause that
    contains it is cut off, so the degree keeps its own list items and
    softeners ("Bac+5 en électronique, systèmes embarqués, ou équivalent.
    Expérience de 3 ans").
    """
    left = max(0, match_start - _CONTEXT_CHARS)
    right = match_end + _CONTEXT_CHARS

    def separators(lo: int, hi: int) -> list[re.Match[str]]:
        return [
            sep for sep in _CLAUSE_SEPARATOR.finditer(text, lo, hi)
            if not any(start <= sep.start() < end for start, end in other_spans)
        ]

    before = [end for start, end in other_spans if left <= end <= match_start]
    if before:
        found = separators(min(before), match_start)
        if found:
            left = found[-1].end()

    after = [start for start, end in other_spans if match_end <= start <= right]
    if after:
        found = separators(match_end, max(after))
        if found:
            right = found[0].start()

    for exp_start, exp_end in experience_spans or []:
        if match_end <= exp_start <= right:
            found = separators(match_end, exp_start)
            if found:
                right = min(right, found[-1].start())
        elif left <= exp_end <= match_start:
            found = separators(exp_end, match_start)
            if found:
                left = max(left, found[0].end())

    return left, right


# ---------------------------------------------------------------------------
# Held / enrolled / preparing (issue #40)
# ---------------------------------------------------------------------------

# A cue governs the education mentions that follow it in the same sentence,
# until another cue appears. Held cues are listed so that "Étudiant(e) en
# Bac+3, titulaire d'un Bac+2" reads the Bac+2 as held. Only the participle
# "diplômé(e)" is a held cue: the noun in "vous préparez un diplôme Bac+5" is
# not (issue #63).
_HELD_CUE = re.compile(
    r"\b(?:titulaire|dipl[oô]m(?:é(?:\(e\)|e)?|e\(e\)|ee)s?|issue?(?:\(e\))?\s+d)",
    re.I,
)
# "actuellement en" only when a study level/programme follows — not
# "actuellement en recherche d'un stage".
_STUDY_WORD = (
    r"(?:bac|master|m\s*[12]\b|licence|l\s*[123]\b|bts|but|dut|[eé]cole|cycle|pr[eé]pa"
    r"|derni|premi|deuxi|troisi|quatri|cinqui|\d|formation|alternance|[eé]tudes?\b)"
)
_ENROLLED_CUE = re.compile(
    r"\b(?:[eé]tudiant(?:\(e\)|e|\.e)?s?\s+en|inscrit(?:\(e\)|e)?s?\s+en)\b"
    r"|\bactuellement\s+en\s+(?=" + _STUDY_WORD + r")",
    re.I,
)
# Separate pattern: "Étudiant(e) en dernière année d'…" shares its "en" with
# the cue above, and regex matches never overlap.
_ENROLLED_YEAR_CUE = re.compile(
    r"\b(?:en\s+)?(?:derni[eè]re|premi[eè]re|deuxi[eè]me|troisi[eè]me|quatri[eè]me|cinqui[eè]me|\d\s*(?:e|[eè]me))"
    r"\s+ann[eé]e\s+(?:de\b|du\b|des\b|d['’‘])",
    re.I,
)
_FINAL_YEAR_CUE = re.compile(r"\bderni[eè]re\s+ann[eé]e\b", re.I)
_PREPARING_CUE = re.compile(
    r"\b(?:vous\s+pr[eé]parez|tu\s+pr[eé]pares|en\s+cours\s+de\s+pr[eé]paration|pr[eé]parant)\b",
    re.I,
)
_CUE_LOOKBACK_CHARS = 100
_SENTENCE_SEPARATOR = re.compile(r"[.;!?\n]")

# Lowest Bac+N year a student can be in when enrolled in a programme named
# without a year. Programmes whose first year is not implied by their name
# (Licence, BTS, écoles d'ingénieur…) are absent: their stage is unknown.
_ENROLMENT_ENTRY_LEVEL = {
    "Master": 4, "MSc": 4, "MBA": 4, "Master 1": 4, "M1": 4,
    "Master 2": 5, "M2": 5, "Doctorat": 6, "PhD": 6,
}


def _governing_cue(text: str, match_start: int) -> tuple[EducationKind, re.Match[str] | None]:
    """The kind of the last held/enrolled/preparing cue before a mention,
    within its sentence. No cue → HELD (today's reading)."""
    lo = max(0, match_start - _CUE_LOOKBACK_CHARS)
    segment_start = lo
    for sep in _SENTENCE_SEPARATOR.finditer(text, lo, match_start):
        segment_start = sep.end()
    best: tuple[int, EducationKind, re.Match[str]] | None = None
    for kind, pattern in (
        (EducationKind.HELD, _HELD_CUE),
        (EducationKind.ENROLLED, _ENROLLED_CUE),
        (EducationKind.ENROLLED, _ENROLLED_YEAR_CUE),
        (EducationKind.PREPARING, _PREPARING_CUE),
    ):
        # No endpos: a cue's lookahead may need to see the mention itself.
        for cue in pattern.finditer(text, segment_start):
            if cue.start() >= match_start:
                break
            if best is None or cue.start() > best[0]:
                best = (cue.start(), kind, cue)
    if best is None:
        return EducationKind.HELD, None
    return best[1], best[2]


# "en cursus / cycle / formation Bac+5": the level names the programme, not
# the year the student is in.
_PROGRAMME_LEVEL_PREFIX = re.compile(
    r"\b(?:cursus|cycles?|formations?|fili[eè]res?|programmes?)\s+(?:de\s+(?:niveau\s+)?|niveau\s+)?$",
    re.I,
)


def _enrolment_level(
    cue: re.Match[str] | None, level: int, label: str, text: str = "", match_start: int = 0
) -> int | None:
    """Lowest Bac+N year implied by an enrolment mention, or None if unknown."""
    if cue is not None and _FINAL_YEAR_CUE.search(cue.group(0)):
        return level  # final year of the named programme
    if _PROGRAMME_LEVEL_PREFIX.search(text[max(0, match_start - 30):match_start]):
        return None
    if cue is not None and re.search(r"\d|premi|deuxi|troisi|quatri|cinqui", cue.group(0), re.I):
        return None  # "Ne année de …": year numbering is programme-specific
    if label.startswith("Bac+"):
        return level
    return _ENROLMENT_ENTRY_LEVEL.get(label)


# Programmes whose current year is not implied by their name. Named as an
# alternative in an enrolment requirement ("Étudiant(e) en école d'ingénieur
# ou en Master"), they make the required stage unknown — checked on the text
# itself because some spellings are not education mentions yet.
_UNKNOWN_STAGE_PROGRAMME = (
    r"(?:[eé]coles?|licence|bachelor|but\b|dut\b|bts\b|cycles?|pr[eé]pa)"
)
_UNKNOWN_STAGE_ALTERNATIVE = re.compile(
    r"^\s*(?:une?\s+|l['’‘]\s*)?" + _UNKNOWN_STAGE_PROGRAMME
    + r"|(?:\bou|,)\s+(?:en\s+|une?\s+|d['’‘]\s*une?\s+)?" + _UNKNOWN_STAGE_PROGRAMME,
    re.I,
)
# After a final-year cue only a NEW enrolment ("… ou en école d'ingénieur")
# is an alternative; "… ou de Master 2" stays inside the final-year scope.
_UNKNOWN_STAGE_AFTER_FINAL_YEAR = re.compile(
    r"(?:\bou|,)\s+en\s+" + _UNKNOWN_STAGE_PROGRAMME, re.I
)


def _enrolment_group_level(
    text: str, cue: re.Match[str] | None, group: list[EducationRequirement]
) -> int | None:
    """Lowest required year for mentions governed by one enrolment cue."""
    spans = [(r.span.start, r.span.end) for r in group if r.span is not None]
    members = [
        r for r in group
        if r.span is None or not any(
            (s, e) != (r.span.start, r.span.end) and s <= r.span.start and r.span.end <= e
            for s, e in spans
        )
    ]  # "Master 2" also matches the generic "Master" pattern: keep the longer one
    if cue is not None:
        end = len(text)
        separator = _SENTENCE_SEPARATOR.search(text, cue.end())
        if separator:
            end = separator.start()
        after_cue = text[cue.end():end]
        alternative = (
            _UNKNOWN_STAGE_AFTER_FINAL_YEAR if _FINAL_YEAR_CUE.search(cue.group(0))
            else _UNKNOWN_STAGE_ALTERNATIVE
        )
        if alternative.search(after_cue):
            return None
    # Unknown-stage programmes that are real alternatives were handled above;
    # otherwise they only qualify an explicit stage ("Bac+5 en école
    # d'ingénieur"), so the explicit stages decide.
    levels = [r.enrolment_level_min for r in members if r.enrolment_level_min is not None]
    return min(levels) if levels else None


# After a mention, a qualifier belongs to the degree only until a connector
# starts another requirement: "Bac+5 en informatique, avec une spécialité IA
# ou équivalent" — the "ou équivalent" qualifies the specialty (issue #40).
_REQUIREMENT_CONNECTOR = re.compile(
    r"\b(?:avec|ainsi\s+que|notamment|dont|et\s+(?:une?\b|des\b|de\s+la\b|du\b|d['’‘]))",
    re.I,
)


def _extract_importance(
    text: str,
    match_start: int,
    match_end: int,
    other_spans: list[tuple[int, int]] | None = None,
    experience_spans: list[tuple[int, int]] | None = None,
) -> tuple[Importance, bool, bool]:
    """Determine importance and OR-alternative flags from context around an education match.

    Returns (importance, is_or_alternative, is_experience_alternative).

    Scans 120 characters before the match and 120 after for modifier keywords,
    clipped to the mention's own clause when ``other_spans`` (the other
    education mentions in *text*) places a neighbour across a clause separator
    — see _clause_bounds. Preferred modifiers take precedence over mandatory
    ones when both appear in that context.

    is_or_alternative is True when any OR-alternative phrase ("ou équivalent",
    "ou expérience équivalente", etc.) is detected — the candidate may satisfy
    either branch, so no score penalty should be applied for education mismatch
    (this does not affect hard-rejection — see EducationRequirement's docstring).

    is_experience_alternative is True only for the narrower "ou/sinon
    expérience équivalente" phrasing — an explicit, well-defined alternative
    (professional experience) distinct from a generic "ou équivalent" — and
    DOES exempt the requirement from the hard education ceiling.
    """
    left, right = _clause_bounds(
        text, match_start, match_end, other_spans or [], experience_spans
    )
    window_before = text[left:match_start]
    window_after = text[match_end:right]
    connector = _REQUIREMENT_CONNECTOR.search(window_after)
    if connector:
        window_after = window_after[:connector.start()]
    context = window_before + " " + window_after

    has_mandatory = any(p.search(context) for p in _MANDATORY_PATTERNS)
    has_preferred = any(p.search(context) for p in _PREFERRED_PATTERNS)
    is_or_alt = any(p.search(context) for p in _OR_ALTERNATIVE_PATTERNS)
    is_experience_alt = any(p.search(context) for p in _EXPERIENCE_ALTERNATIVE_PATTERNS)

    if has_preferred:
        return Importance.PREFERRED, is_or_alt, is_experience_alt
    if has_mandatory:
        return Importance.MANDATORY, False, False  # OR-alt doesn't soften mandatory
    # No qualifier found → UNKNOWN (ambiguous); applies soft 20% penalty, never hard-rejects.
    # Only hard-reject when an explicit mandatory word ("minimum", "requis", etc.) is present.
    return Importance.UNKNOWN, False, False


def find_level_evidence(text: str, level_bac_plus: int) -> EvidenceSpan | None:
    """Return an EvidenceSpan if *level_bac_plus* literally appears in *text*.

    Ignores importance context entirely — this is a pure presence-of-level
    check, deliberately decoupled from ``extract_education``'s mandatory/
    preferred classification. It exists to corroborate an LLM-claimed
    education level against the actual job text: if the level token isn't
    even present, the claim has no textual anchor and must not be treated
    as attributable evidence (see ``RequirementExtractor._parse_llm_response``).
    """
    if not text:
        return None
    for pattern, level, label in _EDUCATION_PATTERNS:
        if level != level_bac_plus:
            continue
        m = pattern.search(text)
        if m:
            raw_snippet = text[max(0, m.start() - 30): m.end() + 30].strip()
            return EvidenceSpan(
                text=raw_snippet,
                start=m.start(),
                end=m.end(),
                field="description",
                extractor_id=f"education.{label.lower().replace('+', 'p').replace(' ', '_')}.corroboration.v1",
            )
    return None


# In current postings "maîtrise" is overwhelmingly skill proficiency ("Maîtrise
# avancée de l'anglais", "Maîtrise Cloud", "C-Maîtrise professionnelle"); the
# Maîtrise degree was discontinued with the LMD reform. It therefore counts as
# an education mention only when a degree cue precedes it in the same sentence:
# "titulaire d'une maîtrise", "diplômé d'un bac et d'une maîtrise" (#51).
_DEGREE_CUE_REQUIRED = frozenset({"Maîtrise"})
_DEGREE_CUE = re.compile(r"\b(?:titulaire|dipl[oô]m[eé]e?s?|dipl[oô]me)\b", re.I)
_DEGREE_CUE_LOOKBACK_CHARS = 60


def _has_degree_cue(text: str, match_start: int) -> bool:
    segment = text[max(0, match_start - _DEGREE_CUE_LOOKBACK_CHARS):match_start]
    separators = list(re.finditer(r"[.;!?\n]", segment))
    if separators:
        segment = segment[separators[-1].end():]
    return bool(_DEGREE_CUE.search(segment))


# Held education ranges (issue #58, owner decision 2026-09-18): two held
# mentions joined only by a range connector — "Bac+3 à Bac+5",
# "Bac+2/Bac+3", "Bac+3 ou Bac+4", "entre Bac+3 et Bac+5" — are ONE
# requirement whose minimum is the lower level, like enrolment ranges (#40).
# Mentions separated by anything else (a qualifier, a condition, a clause)
# stay separate requirements and keep the "highest explicit level" rule.
_RANGE_CONNECTOR = re.compile(r"\s*(?:à|a|au|/|-|–|—|ou)\s*", re.I)
_RANGE_ENTRE = re.compile(r"\bentre\s+(?:le\s+|un\s+)?$", re.I)
_IMPORTANCE_STRENGTH = {
    Importance.MANDATORY: 4, Importance.PREFERRED: 3, Importance.CORE: 2,
    Importance.OPTIONAL: 1, Importance.UNKNOWN: 0,
}


def _is_range_gap(text: str, lower: EducationRequirement, upper: EducationRequirement) -> bool:
    assert lower.span is not None and upper.span is not None
    between = text[lower.span.end:upper.span.start]
    if _RANGE_CONNECTOR.fullmatch(between):
        return True
    return (
        between.strip().lower() == "et"
        and bool(_RANGE_ENTRE.search(text[max(0, lower.span.start - 12):lower.span.start]))
    )


def _merge_held_ranges(
    text: str, found: list[EducationRequirement], cues: list[re.Match[str] | None]
) -> None:
    """Collapse each adjacent held range into its lower mention, in place.

    The lower mention keeps its level, label and evidence span, and takes the
    strongest importance and alternative flags of the range, so "Bac+3 à
    Bac+5 requis" is a MANDATORY Bac+3 requirement. Chains ("Bac+2, Bac+3
    ou Bac+5") are not ranges: only a pair with nothing but a connector
    between them is merged.
    """
    held = [
        i for _start, i in sorted(
            (r.span.start, i) for i, r in enumerate(found)
            if r.kind == EducationKind.HELD and r.span is not None
        )
    ]
    dropped: set[int] = set()
    for a, b in zip(held, held[1:]):
        lower, upper = found[a], found[b]
        if a in dropped or lower.level_bac_plus >= upper.level_bac_plus:
            continue
        if not _is_range_gap(text, lower, upper):
            continue
        if _IMPORTANCE_STRENGTH[upper.importance] > _IMPORTANCE_STRENGTH[lower.importance]:
            lower.importance = upper.importance
        lower.is_or_alternative = lower.is_or_alternative or upper.is_or_alternative
        lower.is_experience_alternative = (
            lower.is_experience_alternative or upper.is_experience_alternative
        )
        dropped.add(b)
    for i in sorted(dropped, reverse=True):
        del found[i]
        del cues[i]


def extract_education(text: str) -> EducationRequirement | None:
    """Extract the highest explicitly stated education requirement from *text*.

    Returns None when no education requirement is found.

    When multiple education levels are mentioned:
    - an adjacent held range ("Bac+3 à Bac+5", "BTS ou Licence") is one
      requirement at its LOWEST level (#58);
    - otherwise held requirements win over enrolment/preparing ones, and among
      them the HIGHEST mandatory level, then highest preferred, then highest
      core ("Bac+2 souhaité, Bac+5 exigé" → Bac+5);
    - enrolment mentions governed by one cue form a range starting at its
      lowest level (#40).
    """
    if not text:
        return None

    found: list[EducationRequirement] = []
    cues: list[re.Match[str] | None] = []

    matches = [
        (m, level, label)
        for pattern, level, label in _EDUCATION_PATTERNS
        for m in pattern.finditer(text)
    ]
    spans = [(m.start(), m.end()) for m, _level, _label in matches]
    # Experience durations bound the context like neighbouring education
    # mentions do, but are not education mentions themselves (issue #41).
    experience_spans = [
        (e.start(), e.end()) for e in _EXPERIENCE_DURATION.finditer(text)
        if not any(s < e.end() and e.start() < t for s, t in spans)
    ]

    for m, level, label in matches:
        if label in _DEGREE_CUE_REQUIRED and not _has_degree_cue(text, m.start()):
            continue
        # Mentions overlapping this one (e.g. two patterns matching the same
        # words) are the same mention, not a neighbour.
        others = [
            (start, end) for start, end in spans if end <= m.start() or start >= m.end()
        ]
        importance, is_or_alt, is_experience_alt = _extract_importance(
            text, m.start(), m.end(), others, experience_spans
        )
        raw_snippet = text[max(0, m.start() - 30): m.end() + 30].strip()
        span = EvidenceSpan(
            text=raw_snippet,
            start=m.start(),
            end=m.end(),
            field="description",
            extractor_id=f"education.{label.lower().replace('+', 'p').replace(' ', '_')}.v1",
        )
        kind, cue = _governing_cue(text, m.start())
        found.append(EducationRequirement(
            level_bac_plus=level,
            level_label=label,
            importance=importance,
            is_or_alternative=is_or_alt,
            is_experience_alternative=is_experience_alt,
            raw_text=raw_snippet,
            span=span,
            kind=kind,
            enrolment_level_min=(
                _enrolment_level(cue, level, label, text, m.start())
                if kind == EducationKind.ENROLLED else None
            ),
        ))
        cues.append(cue)

    if not found:
        return None

    _merge_held_ranges(text, found, cues)

    # Held requirements with an explicit qualifier win, with today's rules:
    # 1. the HIGHEST mandatory level, 2. highest preferred, 3. highest core.
    held = [r for r in found if r.kind == EducationKind.HELD]
    for importance in (Importance.MANDATORY, Importance.PREFERRED, Importance.CORE):
        candidates = [r for r in held if r.importance == importance]
        if candidates:
            return max(candidates, key=lambda r: r.level_bac_plus)

    # Preparing a diploma that is explicitly made mandatory is judged like a
    # held requirement by the gate ("Vous préparez un Bac+5 minimum").
    preparing_mandatory = [
        r for r in found
        if r.kind == EducationKind.PREPARING and r.importance == Importance.MANDATORY
    ]
    if preparing_mandatory:
        return max(preparing_mandatory, key=lambda r: r.level_bac_plus)

    # Enrolment: mentions governed by the same cue form one range, which
    # starts at its lowest level ("Étudiant(e) en Bac+3 à Bac+4" → Bac+3).
    # A member of unknown stage makes the whole group unknown (conservative).
    enrolled_groups: dict[int, tuple[re.Match[str] | None, list[EducationRequirement]]] = {}
    for requirement, cue in zip(found, cues):
        if requirement.kind == EducationKind.ENROLLED:
            key = cue.start() if cue is not None else -1
            enrolled_groups.setdefault(key, (cue, []))[1].append(requirement)
    if enrolled_groups:
        group_cue, group = next(iter(enrolled_groups.values()))
        representative = max(group, key=lambda r: r.level_bac_plus)
        representative.enrolment_level_min = _enrolment_group_level(text, group_cue, group)
        return representative

    preparing = [r for r in found if r.kind == EducationKind.PREPARING]
    if preparing:
        return max(preparing, key=lambda r: r.level_bac_plus)

    return max(found, key=lambda r: r.level_bac_plus)
