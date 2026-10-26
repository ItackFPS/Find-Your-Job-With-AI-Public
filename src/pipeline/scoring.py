"""Deterministic scoring layer.

Scores jobs based on 5 user-fit components, weighted by explicit priority
order: location, domain relevance, skills, seniority, and experience.
No machine learning in MVP.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable

from src.models.job import Job
from src.pipeline.departments import PACA_NEIGHBOR_DEPARTMENTS, extract_department_code
from src.pipeline.location_distance import assess as assess_location
from src.pipeline.location_distance import describe as describe_location
from src.pipeline.location_distance import distance_config
from src.pipeline.location_match import LocationMatch, resolve_job_location
from src.requirements.models import GateResult

if TYPE_CHECKING:
    from src.models.profile import JobSearchProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Score -> tier classification
# ---------------------------------------------------------------------------
# Single canonical implementation of the 0-100 score -> tier mapping. Every
# Python consumer imports score_to_tier from here:
#   - src/api/routes/jobs.py       (score-tier facet counts)
# One consumer runs in a DIFFERENT runtime and cannot import this module, so
# it carries a deliberate, documented mirror instead — see the "SOURCE OF
# TRUTH" comment there:
#   - frontend/src/utils/scoreToTier.ts (display-only UI label)
# Phase 4 (2026-09-10): fixed-reference calibration, NOT live per-run
# percentiles. The previous thresholds (90/80/75/60) were arbitrary round
# numbers chosen without reference to any real score distribution — after
# the Phase 1-3 redesign, "exceptional" (90+) was mathematically unreachable
# in 30,271 real jobs sampled across 4 separate historical pipeline runs
# (2026-09-08 to 2026-09-10) and "excellent" (80+) held only ~0.2% of jobs,
# leaving most of the distribution crammed into "relevant"/"low_priority"
# with no meaningful separation between a merely-OK job and a genuinely
# strong one.
#
# These 4 thresholds were instead derived ONCE from that same fixed
# 4-run/30,271-job reference sample (score = 0.94*deterministic +
# 0.06*normalized_semantic, i.e. the score AFTER Phases 1-3 — this
# calibration does not touch or re-derive the score itself) and are then
# HARDCODED as constants — they do not move with any individual run's job
# pool, weak or strong. A thin/mediocre collection day does not make its
# best job "exceptional"; a single genuinely exceptional job on an otherwise
# weak day still crosses 80 and is labeled exceptional. Per-run stability
# was explicitly verified: the % of jobs crossing each of these cutoffs
# varied by at most ~2 percentage points across the 4 reference runs.
#
# Reference percentiles at calibration time (pooled 4-run sample):
#   80  ~= p99.8  (~0.2-0.3% of real eligible jobs — genuinely rare)
#   72  ~= p97    (~3% of real eligible jobs)
#   65  ~= p90    (~14-18% of real eligible jobs)
#   60  ~= p75    (~36-47% of real eligible jobs — unchanged from before;
#                  this is the one threshold that happened to already sit
#                  near a meaningful percentile boundary, confirmed rather
#                  than assumed)
#
# These are intentionally NOT recomputed automatically — if the real job
# pool's composition drifts meaningfully over time (e.g. new sources, a
# different market), recalibrating means re-running this same fixed-sample
# analysis and deliberately updating these constants, not switching to a
# live per-run calculation (that would reintroduce exactly the "today's
# best job = excellent" instability this calibration exists to prevent).
#
# STALE as of 2026-09-11 — recalibration recommended, constants NOT yet
# moved: two scoring-formula bugs were fixed on this date (seniority_match
# was letting a source-supplied `job_level` facet, e.g. LinkedIn's generic
# "Mid-Senior level" tag, override an explicit junior/senior signal already
# present in the job title; the education soft-penalty was firing on
# UNKNOWN-importance mentions that are not actual evidence of a requirement)
# — see _score_seniority_match and requirements/gate.py's gate_jobs for the
# fixes. Both artificially depressed real scores. Re-running the same
# 2026-09-10 production sample (7,194 jobs) through the fixed formula shows
# the 60/65/72/80 cutoffs now sit at p25.7/p67.4/p87.8/p98.5 respectively —
# noticeably looser than this comment's documented p75/p90/p97/p99.8
# targets (60 moved the most: p48.4 pre-fix -> p25.7 post-fix, vs. the
# documented p75). This is exactly the kind of drift this section's own
# methodology says should trigger a deliberate recalibration — but a single
# retroactively-simulated run is the same "thin sample" problem this
# calibration philosophy explicitly warns against (see comment above), not
# the multi-run breadth the original 4-run/30,271-job analysis used. The
# constants below are therefore left UNCHANGED pending a fresh multi-run
# calibration pass against several live pipeline runs made AFTER both fixes
# are deployed — do not hand-adjust these from this single simulated data
# point.
_TIER_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (80, "exceptional"),
    (72, "excellent"),
    (65, "strong"),
    (60, "relevant"),
)
_DEFAULT_TIER = "low_priority"

# Public alias for the rare external mirror that needs the raw
# (threshold, tier) pairs themselves — not just score_to_tier()'s result.
# Never mutate this;
# it's the exact same tuple object as _TIER_THRESHOLDS above.
TIER_THRESHOLDS = _TIER_THRESHOLDS


def score_to_tier(score_100: int) -> str:
    """Map a 0-100 integer score to a tier label via FIXED, pre-calibrated
    thresholds — never computed from the current run's own score
    distribution (see the "Score -> tier classification" comment above for
    the calibration methodology and reference data).

    Tiers (score_100 lower bound, inclusive):
        exceptional  >= 80
        excellent    >= 72
        strong       >= 65
        relevant     >= 60
        low_priority  < 60

    Pure function of a single score: this is a presentation/category label
    only — it never modifies the numeric score, and the numeric score is
    computed entirely upstream of this function (Phases 1-3). Calling this
    with the same score_100 always returns the same tier, regardless of
    what other jobs exist in the same run or any other run.

    This is the single canonical implementation — see the module-level
    "Score -> tier classification" comment above for every Python consumer,
    and for the two other-runtime mirrors that cannot import this module.
    """
    for threshold, tier in _TIER_THRESHOLDS:
        if score_100 >= threshold:
            return tier
    return _DEFAULT_TIER


# Phase 5 (2026-09-10): canonical tier ordering, derived from _TIER_THRESHOLDS
# itself (never a second hand-typed list) so a "show tier X and above"
# presentation filter (dashboard visibility floor, etc.) never drifts out of
# sync with the tiers score_to_tier() actually produces. Rank is ordinal only
# (0 = lowest) — never used as, or fed back into, a numeric score.
TIER_RANK: dict[str, int] = {
    _DEFAULT_TIER: 0,
    **{tier: i + 1 for i, (_, tier) in enumerate(reversed(_TIER_THRESHOLDS))},
}


def tier_meets_minimum(tier: str, min_tier: str) -> bool:
    """True when ``tier`` is at least as good as ``min_tier`` (by TIER_RANK).

    Pure presentation-filter helper — never changes a job's score or tier,
    only whether it is *shown* under a given visibility floor. An unknown
    ``min_tier`` (not one of the 5 known tier names) never hides anything —
    callers should validate the value themselves if that matters for their
    use case (e.g. reject an invalid query param before calling this).
    """
    if min_tier not in TIER_RANK:
        return True
    return TIER_RANK.get(tier, 0) >= TIER_RANK[min_tier]


# Skill abbreviation → expanded canonical forms for description matching.
# Mirrors filter.py's _SKILL_ALIASES to prevent false-positive scoring
# (e.g. "ai" in "j'ai" would otherwise count as an AI skill match).
_SCORING_SKILL_ALIASES: dict[str, list[str]] = {
    "ai": ["artificial intelligence", "machine learning"],
    "ml": ["machine learning"],
    "dl": ["deep learning"],
    "nlp": ["natural language processing"],
    "cv": ["computer vision"],
    "js": ["javascript"],
    "ts": ["typescript"],
    "ui": ["user interface"],
    "ux": ["user experience"],
    "db": ["database"],
}

# ── F3: Language detection ────────────────────────────────────────────────────
# Non-profile language name patterns (canonical name → compiled regex).
# Only languages NOT in the user's profile are checked; if the job description
# or requirements mention one of these with a "required" signal nearby, a
# moderate score penalty is applied to the final score.
_NON_PROFILE_LANG_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:german|deutsch(?:e?s?|kenntnisse)?|allemand[e]?s?)\b", re.I), "german"),
    (re.compile(r"\b(?:spanish|español|espagnol|castellano|espaniol)\b", re.I), "spanish"),
    (re.compile(r"\b(?:dutch|néerlandais|nederlandais|hollandais|vlaams)\b", re.I), "dutch"),
    (re.compile(r"\b(?:italian|italiano|italien(?:ne)?)\b", re.I), "italian"),
    (re.compile(r"\b(?:portuguese?|português|portugais)\b", re.I), "portuguese"),
    (re.compile(r"\b(?:chinese|mandarin|chinois|cantonais)\b", re.I), "chinese"),
    (re.compile(r"\b(?:japanese|japonais)\b", re.I), "japanese"),
    (re.compile(r"\b(?:arabic|arabe)\b", re.I), "arabic"),
    (re.compile(r"\b(?:russian|russe)\b", re.I), "russian"),
    (re.compile(r"\b(?:korean|coréen|coreeen)\b", re.I), "korean"),
    (re.compile(r"\b(?:turkish|turc|türkisch)\b", re.I), "turkish"),
    (re.compile(r"\b(?:polish|polonais|polnisch)\b", re.I), "polish"),
    (re.compile(r"\b(?:swedish|suédois|schwedisch)\b", re.I), "swedish"),
]

# Signals that a language is REQUIRED from the candidate (within context window).
_LANG_REQUIRED_RE = re.compile(
    r"\b(?:"
    r"required?|mandatory|obligatoire|essential|essentiell|"
    r"native|fluent|couramment|courant|"
    r"exig[eé]e?s?|requis|bilingue|bilingual|"
    r"proficient|maîtrise|maitrise|"
    r"erforderlich|zwingend|vereist|"
    r"c[12]|b2"  # language level codes often imply "required at this level"
    r")\b",
    re.I,
)

# Signals that a language is merely PREFERRED (skip penalty when found near language).
_LANG_PREFERRED_RE = re.compile(
    r"\b(?:"
    r"preferred?|optionn?el|plus|bonus|appreciated?|"
    r"atout|souhait[eé]e?|apprécié|appréciée?|"
    r"idéalement|notions?|basic|elementary|"
    r"souhaitable|wünschenswert|"
    r"serait un plus|est un plus|sera un plus|would be|is a plus"
    r")\b",
    re.I,
)

# Window (characters) to scan around a language name for requirement signals.
_LANG_WINDOW = 140

# Common seniority indicator words (both positive and negative levels).
# Used by _score_seniority_match to distinguish "title has no seniority signal"
# (→ neutral 0.5) from "title has a wrong/non-preferred seniority signal" (→ 0.0).
# All values must be diacritic-free (matched after _norm_text).
_SENIORITY_INDICATORS: frozenset[str] = frozenset([
    # Over-senior (positive signals for senior-seekers, negative for junior-seekers)
    "senior", "sr",  # "sr" catches "Sr. DevOps Engineer" abbreviation
    "lead", "principal", "staff",
    "director", "vp", "head", "chief", "manager",
    "confirme", "confirmee", "experimente", "experimentee",
    # Junior-level (positive for junior-seekers)
    "junior", "jr", "entry", "graduate", "intern", "internship",
    "alternance", "stagiaire", "debutant",
])

# French synonyms for the profile's English experience-level names. Public
# (no leading underscore) because it's shared outside this module: the
# orchestrator's SCORE stage uses it to build preferred_level_keywords for
# _score_seniority_match (so "Développeur Stagiaire" scores as a preferred
# internship level rather than tripping _SENIORITY_INDICATORS' "stagiaire"
# as a wrong-seniority signal), and the LLM eligibility gate's context
# builder (src/ai/eligibility.py) reuses the same mapping to surface
# internship/alternance/junior context to the model — one source of truth
# for "what French words mean this English experience level", not two.
SENIORITY_FR_SYNONYMS: dict[str, list[str]] = {
    "internship": ["stagiaire", "stage"],
    "junior": ["debutant"],
    "alternance": ["apprenti", "apprentissage"],
    "entry_level": [],
    "graduate": [],
}

# Broad technical role indicators used as a mid tier in domain_relevance scoring.
# A title containing one of these terms (after diacritic/case normalization) is
# treated as a technical role even if it does not directly match a user-selected
# domain keyword.  These are specific enough to exclude non-IT usage but broad
# enough to capture data science, architecture, and similar adjacent roles.
_BROAD_TECH_TITLE_INDICATORS: tuple[str, ...] = (
    # English role terms
    "engineer", "developer", "programmer", "scientist", "analyst",
    "administrator", "architect", "devops",
    # French equivalents (accent-stripped by _norm_text)
    "ingenieur", "developpeur", "programmeur", "analyste",
    "administrateur", "architecte", "technicien",
)

# ---------------------------------------------------------------------------
# Domain-relevance keyword matching precision (2026-09-11 audit fix)
#
# `_score_domain_relevance` (below) matches ``domain_title_keywords``,
# ``domain_description_keywords`` and ``_BROAD_TECH_TITLE_INDICATORS`` via
# plain substring containment (`kw in text`), unlike the rest of this module
# (seniority indicators, off_domain_keywords, skills_match) which are all
# word-boundary guarded. A live audit (2026-09-11, 7,194 real accepted jobs)
# found this let "developer" — a real, wanted keyword for genuine software
# roles — bare-match inside "Business Developer" / "Sales Developer", a
# standard (and common: 167 real postings) French/English SALES job title
# that has nothing to do with software development. Those postings were
# scoring domain_relevance=1.0 (confirmed domain match) purely from this.
#
# A plain word-boundary guard does NOT fix this case: "developer" already
# appears as a complete, space-separated word in "Business Developer" — the
# problem is the PRECEDING word changing what the word means, not a
# substring-inside-a-longer-word situation. So this needs a narrow,
# evidence-driven exclusion (same technique already used for seniority's
# `_THIRD_PARTY_COORDINATION`: a short lookbehind window for a specific
# contextual cue), not a boundary regex.
#
# A blanket word-boundary rewrite of the REST of the keyword list was
# deliberately NOT applied: replaying the full real-job corpus through a
# strict-both-sides boundary guard for every keyword breaks 20+ confirmed
# legitimate matches this vocabulary depends on — French gender-inflected
# forms ("ingénieure" vs "ingenieur"), plurals ("developers",
# "informatiques"), and portmanteau compounds with no separator
# ("NetDevOps", "SecDevOps", "Pentester") would all stop matching. The
# existing bare-substring behavior is intentionally left unchanged for
# every keyword except the two below, where a fix was both confirmed
# necessary (real false positive in production data) and confirmed safe
# (verified against the same real-job corpus to cause zero lost matches).
_GENERIC_DEVELOPER_TERMS: frozenset[str] = frozenset({"developer", "developpeur"})

# "sales"/"business" immediately before "developer"/"développeur" means the
# standard sales-role title ("Business Developer", "Sales Developer"), not a
# software role. A short lookbehind window, same pattern as
# _THIRD_PARTY_COORDINATION above.
_BUSINESS_CONTEXT_QUALIFIER = re.compile(r"\b(?:business|sales)\s*-?\s*$", re.I)
_BUSINESS_CONTEXT_WINDOW_CHARS = 20

# "rust" (the programming language) bare-matched inside "Trust"/"Trusted"
# ("SRE Trusted Platform", "Backend Engineer — Trust & Tools") in the same
# audit — unlike the rest of the list, "rust" has no confirmed legitimate
# compound/inflected form in the real corpus that a strict boundary guard
# would break, so a plain word-boundary guard is the safest fix here.
_STRICT_BOUNDARY_DOMAIN_TERMS: frozenset[str] = frozenset({"rust"})


def _domain_keyword_matches(kw_norm: str, text_norm: str) -> bool:
    """Return True if the (already-normalized) keyword ``kw_norm`` is present
    in ``text_norm`` as a genuine domain signal.

    Default behavior is the original bare substring containment check
    (unchanged for the vast majority of the vocabulary — see the module
    comment above for why). Two confirmed, narrow exceptions:
      - ``_GENERIC_DEVELOPER_TERMS``: still a bare substring search (so
        plurals/compounds keep matching exactly as before), but excluded
        when immediately preceded by a business/sales qualifier (see
        ``_BUSINESS_CONTEXT_QUALIFIER``).
      - ``_STRICT_BOUNDARY_DOMAIN_TERMS``: plain word-boundary guarded.
    """
    if not kw_norm or not text_norm:
        return False

    if kw_norm in _GENERIC_DEVELOPER_TERMS:
        # Bare substring search (unchanged, no boundary requirement) so
        # legitimate continuations — plural "developers", compounds — keep
        # matching exactly as before; only the preceding-context check is
        # new. (A word-boundary guard here would itself regress "Software
        # Python Developers" etc. — confirmed against the real corpus.)
        start = 0
        while True:
            idx = text_norm.find(kw_norm, start)
            if idx == -1:
                return False
            window_before = text_norm[
                max(0, idx - _BUSINESS_CONTEXT_WINDOW_CHARS): idx
            ]
            if not _BUSINESS_CONTEXT_QUALIFIER.search(window_before):
                return True
            start = idx + 1

    if kw_norm in _STRICT_BOUNDARY_DOMAIN_TERMS:
        pattern = r"(?<![a-z0-9])" + re.escape(kw_norm) + r"(?![a-z0-9])"
        return re.search(pattern, text_norm) is not None

    return kw_norm in text_norm


def _norm_text(text: str) -> str:
    """Lowercase and strip diacritical marks."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


# Word-boundary-guarded pattern per indicator, precompiled once. A bare
# substring check (the previous implementation) let "lead" match inside
# "leader"/"leadership" and "intern" match inside "interne"/"internes"/
# "international" — common French corporate-boilerplate words ("Thales est
# un leader mondial...", "communautés internes") were misread as candidate
# seniority/internship evidence. See the 2026-09-05 live AI benchmark
# validation (Jobs: Thales Développeur BackEnd, Thales Evaluateur Pentest,
# TELESPAZIO Ingénieur Cybersécurité — all three false "lead"/"intern" hits
# traced to exactly this).
_SENIORITY_INDICATOR_PATTERNS: dict[str, "re.Pattern[str]"] = {
    ind: re.compile(r"(?<![a-z0-9])" + re.escape(ind) + r"(?![a-z0-9])")
    for ind in _SENIORITY_INDICATORS
}

# A seniority word introduced by "et le/la/les/du/des ..." (optionally with
# up to two qualifier words in between, e.g. "et le Squad Lead") or its
# English equivalent "and the ..." is naming ANOTHER, already-identified
# person/role in an enumeration — "le Product Owner et le Squad Lead" reads
# as "the Product Owner and the Squad Lead" (two team members the candidate
# will work alongside/support), not a statement that the candidate must be a
# lead. This is a structural coordination cue, not a lexical one: it doesn't
# matter which role noun precedes it, so it generalizes rather than listing
# specific job titles to exclude. Genuine candidate-facing phrasing ("vous
# êtes Lead Developer", "poste de Tech Lead", "profil Senior") is never
# introduced this way and is unaffected.
_THIRD_PARTY_COORDINATION = re.compile(
    r"\b(?:et|and)\s+(?:le|la|les|du|des|the)\s+(?:\w+\s+){0,2}$", re.I,
)

# Characters scanned immediately before a match to look for the coordination
# cue above. Short and local by design — this is about the immediate
# grammatical construction around the match, not a broad context search.
_COORDINATION_WINDOW_CHARS = 40


def find_seniority_indicators(text: str) -> list[str]:
    """Return which words from ``_SENIORITY_INDICATORS`` appear in *text*.

    Public wrapper around the private indicator set so callers outside this
    module (the LLM eligibility gate's compact-context builder,
    ``src/ai/eligibility.py``) can surface the same deterministic seniority
    signal `_score_seniority_match` uses for scoring — as *context* for the
    model to reason over, never as a rejection rule on its own.

    Matching is word-boundary-guarded (not a bare substring check) and skips
    an occurrence that is grammatically introduced as a third-party
    enumeration ("le Product Owner et le Squad Lead") rather than a
    candidate/job requirement — see ``_THIRD_PARTY_COORDINATION`` above. An
    indicator counts as present if ANY of its occurrences in the text is a
    genuine, non-excluded match.
    """
    if not text:
        return []
    norm = _norm_text(text)
    found: list[str] = []
    for ind, pattern in _SENIORITY_INDICATOR_PATTERNS.items():
        for m in pattern.finditer(norm):
            window_before = norm[max(0, m.start() - _COORDINATION_WINDOW_CHARS): m.start()]
            if _THIRD_PARTY_COORDINATION.search(window_before):
                continue
            found.append(ind)
            break
    return found


_ESCO_DB_PATH_DEFAULT = "data/esco.db"


@lru_cache(maxsize=256)
def _esco_synonyms_for_skill(skill_norm: str, db_path: str = _ESCO_DB_PATH_DEFAULT) -> frozenset[str]:
    """Best-effort ESCO synonym lookup for one already-normalized skill.

    Phase NEXT / I10: additive-only enrichment of ``_expand_skill`` below —
    this NEVER replaces or gates anything; it only widens the set of textual
    forms ``_score_skills_match`` (a soft ranking factor, not a hard filter)
    accepts as evidence for a target skill the candidate already listed.
    Matching against ESCO's own preferred/alt labels (English AND French)
    covers real synonyms and multilingual equivalents (e.g. "Golang"/"Go",
    "JS"/"JavaScript", "développement web"/"web development") without
    growing another hand-maintained list like ``_SCORING_SKILL_ALIASES``.

    Cached per (skill, db_path) with ``lru_cache`` — ``target_skills`` is a
    short, fixed list per pipeline run, so this keeps ESCO lookups to O(one
    query per distinct skill), not O(jobs x skills). Returns an empty
    frozenset on ANY failure (db missing/empty/corrupted) — the caller then
    falls back to exactly the pre-ESCO behavior, so a broken or absent ESCO
    database can only ever reduce enrichment, never break scoring.
    """
    try:
        from src.esco.db import EscoDb

        db = EscoDb(db_path=db_path)
        if not db.is_ready():
            return frozenset()

        # Strip a disambiguating parenthetical the same way EscoConcept.keyword
        # does (e.g. "Go (programming language)" -> "go"), on both sides, so a
        # caller-supplied skill matches regardless of whether it carries
        # ESCO's own disambiguation suffix.
        skill_key = skill_norm.split("(")[0].strip()

        forms: set[str] = set()
        for concept in db.search(skill_norm, limit=5):
            candidate_labels = [
                concept.keyword,
                concept.preferred_label_fr,
                *concept.alt_labels_en,
                *concept.alt_labels_fr,
            ]
            # Only pull synonyms from a concept that IS this skill (exact
            # match on its own keyword/labels) — the FTS query above is a
            # prefix search and can surface loosely-related concepts; without
            # this guard, e.g. searching "java" could pull in "JavaScript"'s
            # synonyms too.
            is_this_skill = any(
                _norm_text(label).split("(")[0].strip() == skill_key
                for label in candidate_labels
                if label
            )
            if not is_this_skill:
                continue
            for label in candidate_labels:
                if label:
                    forms.add(_norm_text(label))
        return frozenset(forms)
    except Exception:  # noqa: BLE001 - best-effort enrichment, never break scoring
        return frozenset()


def compute_esco_synonym_map(
    target_skills: list[str], db_path: str = _ESCO_DB_PATH_DEFAULT
) -> dict[str, list[str]]:
    """Precompute ESCO synonym forms for every target skill, ONCE per
    pipeline run.

    Phase NEXT / I10: the orchestrator calls this once (mirroring how it
    already injects ``skills_match.domain_vocabulary``) and stores the
    result under ``config["scoring"]["skills_match"]["esco_synonyms"]`` —
    ``_expand_skill`` below then only ever reads that already-computed dict,
    it never queries the ESCO database itself. This keeps
    ``_score_skills_match`` a pure function of ``(job, config)`` with no
    hidden global I/O, so every existing caller/test that builds its own
    config dict (with no ``esco_synonyms`` key) keeps behaving exactly as it
    did before ESCO was wired in, and the real ESCO database is only ever
    touched a handful of times per pipeline run, never once per job.
    """
    result: dict[str, list[str]] = {}
    for skill in target_skills:
        key = _norm_text(skill.strip())
        result[key] = sorted(_esco_synonyms_for_skill(key, db_path))
    return result


def _expand_skill(skill: str, config: dict[str, Any] | None = None) -> list[str]:
    """Return canonical forms for a skill: hand-maintained abbreviation
    expansions plus any ESCO-known synonyms/alt-labels already precomputed
    into ``config`` by ``compute_esco_synonym_map`` (best-effort — absent or
    malformed config data simply means no ESCO enrichment, never an error).
    """
    key = _norm_text(skill.strip())
    forms = set(_SCORING_SKILL_ALIASES.get(key, [key]))
    if config is not None:
        esco_map = config.get("scoring", {}).get("skills_match", {}).get("esco_synonyms", {})
        if isinstance(esco_map, dict):
            esco_forms = esco_map.get(key)
            if isinstance(esco_forms, list):
                forms.update(f for f in esco_forms if isinstance(f, str))
    return sorted(forms)


def load_scoring_config(config_path: str) -> dict[str, Any]:
    """Load scoring configuration from YAML file."""
    import yaml

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise TypeError(f"Expected a mapping in {config_path}")

    return data


def build_scoring_config(
    scoring_config: dict[str, Any],
    profile: JobSearchProfile,
    engine_cfg: dict[str, Any],
    esco_synonyms: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Enrich ``scoring_config`` IN PLACE with the profile- and vocabulary-derived values the
    scoring factors need, and return that same object (extracted verbatim from
    ``run_pipeline``; behaviour unchanged).

    ``scoring_config`` is the loaded ``scoring.yaml``; ``profile`` is a ``JobSearchProfile``;
    ``engine_cfg`` is the loaded ``filters.yaml`` vocabulary. Call once per run: it consults
    the ESCO database (best-effort) and callers must keep using the returned object.

    ``esco_synonyms``: production passes ``None`` (the map is computed here). Offline evaluation
    passes a precomputed map (``{}`` = no enrichment) so results never depend on the local
    ``esco.db``.
    """
    seniority_cfg = (
        scoring_config
        .setdefault("scoring", {})
        .setdefault("seniority_match", {})
    )
    # Build preferred-level keywords from English profile levels plus
    # French synonyms so that "Développeur Stagiaire" scores as a
    # preferred level (internship) rather than as wrong seniority.
    # Without French synonyms, _SENIORITY_INDICATORS would flag
    # "stagiaire" and score the title 0.0 instead of a bonus.
    # "intern"/"trainee" are also aliased onto "internship": titles
    # with only the bare word "Intern" (not "Internship" — e.g.
    # "DevOps Intern", "Cloud Engineer (Intern)") were failing this
    # preferred-match check while still tripping
    # _SENIORITY_INDICATORS' own "intern" entry as a wrong-seniority
    # signal, penalizing exactly the demographic this profile
    # prioritizes. Confirmed against real production data: 54 jobs
    # were wrongly flagged before this alias was added.
    pref_keywords: list[str] = []
    # Profile v2 with target levels (F5.2): the targets are the preferred levels.
    targets = getattr(getattr(profile, "targets", None), "levels", None) or []
    preferred = (list(targets) if getattr(profile, "profile_version", 1) >= 2 and targets
                 else profile.experience.preferred_levels)
    for lvl in preferred:
        pref_keywords.append(lvl.replace("_", " "))
        pref_keywords.extend(SENIORITY_FR_SYNONYMS.get(lvl, []))
        if lvl == "internship":
            pref_keywords.extend(["intern", "trainee"])
        if lvl == "graduate" and targets:
            pref_keywords.append("entry level")   # v1 "entry_level" is folded into "graduate"
    seniority_cfg["preferred_level_keywords"] = pref_keywords

    # Inject preferred countries so location scoring can
    # differentiate France-preferred jobs from other EU jobs.
    location_cfg = (
        scoring_config
        .setdefault("scoring", {})
        .setdefault("location_fit", {})
    )
    location_cfg["preferred_countries"] = list(profile.geography.preferred_countries)
    # Ordered department/remote priority list — the SOLE source
    # of truth for location_fit's priority tiers (see
    # _score_location_fit / location_match.resolve_job_location).
    # No hardcoded department default: an empty profile list
    # means no location gets special ranking treatment.
    location_cfg["location_preferences"] = [
        p.model_dump() for p in profile.geography.location_preferences
    ]
    # Distance + remote-policy model (F4.3, src/pipeline/location_distance.py): when the
    # profile has a home, it replaces the priority list above for location_fit and the
    # location bonus. None keeps the v1 behaviour exactly.
    location_cfg["distance"] = distance_config(profile)

    # Inject user's target skills so the scorer can compute real skill overlap
    # instead of returning the hardcoded 0.5 constant.
    skills_cfg = (
        scoring_config
        .setdefault("scoring", {})
        .setdefault("skills_match", {})
    )
    skills_cfg["target_skills"] = list(profile.target_skills)

    # ESCO synonym enrichment (Phase NEXT / I10) — computed ONCE
    # per run, here, never per-job inside the scoring loop itself
    # (see scoring.compute_esco_synonym_map's docstring for why).
    # Best-effort: a missing/empty/corrupted ESCO database just
    # means no enrichment for this run, never a crash or a
    # degraded skills_match score relative to before ESCO existed.
    skills_cfg["esco_synonyms"] = (
        esco_synonyms if esco_synonyms is not None
        else compute_esco_synonym_map(list(profile.target_skills))
    )

    # Inject domain-specific SKILL/TOOL vocabulary (2026-09-11,
    # config/filters.yaml's domain_skill_vocabulary) for the
    # skills_match fallback's domain-vocabulary bonus — flattened
    # across the candidate's selected target_domains only, deduped,
    # order-preserving. Distinct from target_skills: see
    # _score_skills_match / _has_domain_vocabulary_hit in scoring.py
    # for why this must never be merged into target_skills itself.
    domain_vocab_terms: list[str] = []
    for domain in profile.target_domains:
        domain_vocab_terms.extend(
            engine_cfg.get("domain_skill_vocabulary", {}).get(domain, [])
        )
    skills_cfg["domain_vocabulary"] = list(dict.fromkeys(domain_vocab_terms))

    # Inject domain title/description keywords, and off-domain
    # keywords, for domain_relevance scoring. Title/description
    # keywords are the per-domain vocabulary from filters.yaml for
    # each domain the user selected; off-domain keywords are the
    # SAME non-IT/off-domain vocabulary the hard filter already
    # uses (excluded_title_keywords + non_it_title_functions) —
    # reused here as defense-in-depth evidence that a job is
    # *confirmed* wrong-domain, not merely undetected.
    domain_rel_cfg = (
        scoring_config
        .setdefault("scoring", {})
        .setdefault("domain_relevance", {})
    )
    domain_title_kws: list[str] = []
    domain_desc_kws: list[str] = []
    for domain in profile.target_domains:
        kws = engine_cfg.get("domain_keywords", {}).get(domain, {})
        domain_title_kws.extend(kws.get("title", []))
        domain_desc_kws.extend(kws.get("description", []))
    domain_rel_cfg["domain_title_keywords"] = list(dict.fromkeys(domain_title_kws))
    domain_rel_cfg["domain_description_keywords"] = list(dict.fromkeys(domain_desc_kws))
    off_domain_kws: list[str] = []
    for group in engine_cfg.get("excluded_title_keywords", {}).values():
        off_domain_kws.extend(group)
    for group in engine_cfg.get("non_it_title_functions", {}).values():
        off_domain_kws.extend(group)
    domain_rel_cfg["off_domain_keywords"] = list(dict.fromkeys(off_domain_kws))

    # Inject profile languages for F3 language penalty detection.
    lang_cfg = (
        scoring_config
        .setdefault("scoring", {})
        .setdefault("language", {})
    )
    lang_cfg["profile_languages"] = list(
        getattr(profile, "languages", ["French", "English"])
    )
    return scoring_config


def _get_float(
    config: dict[str, Any],
    path: str,
    default: float,
) -> float:
    """Safely extract a numeric value from a nested config path."""
    current: Any = config

    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)

    if current is None:
        return default

    if not isinstance(current, (int, float)):
        raise TypeError(f"Expected numeric value for {path!r}")

    return float(current)


def _get_keywords(
    config: dict[str, Any],
    path: str,
) -> list[str]:
    """Safely extract a list of string keywords from configuration."""
    current: Any = config

    for part in path.split("."):
        if not isinstance(current, dict):
            return []
        current = current.get(part)

    if not isinstance(current, list):
        return []

    return [item for item in current if isinstance(item, str)]


def _compute_factors(
    job: Job, config: dict[str, Any], gate_result: GateResult | None = None
) -> dict[str, float]:
    """Compute the 5 user-fit factors for a single job.

    ``domain_relevance`` and ``seniority_match`` are "confirmed dealbreaker"
    factors: hitting their bottom value (0.05 / 0.0 respectively) triggers a
    gate in ``_apply_weights`` that caps/multiplies the whole composite score,
    on top of losing their own weight share. The other three (location,
    skills, experience) are pure "matter of degree" factors with no gate.
    """
    # Structured seniority data (e.g. LinkedIn's job_level field), passed to
    # _score_seniority_match as a fallback only — an explicit signal in the
    # job title always takes precedence. See _score_seniority_match's
    # docstring for why (source-supplied level facets are coarser and less
    # reliable than the posting's own title).
    job_level: str | None = (
        job.raw_payload.get("job_level")
        if isinstance(job.raw_payload, dict)
        else None
    )

    domain_relevance = _score_domain_relevance(job.title, job.description, config)
    distance_cfg = _distance_cfg(config)

    return {
        "location_fit": (
            assess_location(job, distance_cfg).score if distance_cfg else _score_location_fit(
                job.location, job.countries, job.work_mode, job.remote, job.department, config)
        ),
        "domain_relevance": domain_relevance,
        "skills_match": _score_skills_match(job, config, domain_relevance),
        "seniority_match": _score_seniority_match(job.title, config, job_level=job_level),
        "experience_fit": _score_experience_fit(job, config, gate_result),
    }


def _domain_gate_fired(factors: dict[str, float], config: dict[str, Any]) -> bool:
    """True if domain_relevance is at the confirmed-wrong-domain floor."""
    confirmed_wrong_domain = _get_float(
        config, "scoring.domain_relevance.confirmed_wrong_score", 0.05
    )
    return factors.get("domain_relevance", 1.0) <= confirmed_wrong_domain


def _seniority_gate_fired(factors: dict[str, float]) -> bool:
    """True if seniority_match is a confirmed mismatch."""
    return factors.get("seniority_match", 1.0) == 0.0


def _gate_fired(factors: dict[str, float], config: dict[str, Any]) -> bool:
    """True if either "confirmed dealbreaker" gate (domain or seniority)
    fires for this factor set. Shared by ``_apply_weights`` (applies the
    cap/multiplier) and ``cap_personalization_at_gates`` (re-enforces it
    after the later personalization stage).
    """
    return _domain_gate_fired(factors, config) or _seniority_gate_fired(factors)


def _scoring_weights(config: dict[str, Any]) -> dict[str, float]:
    """The five factor weights (config values with their defaults)."""
    return {
        "location_fit": _get_float(config, "scoring.location_fit.weight", 0.30),
        "domain_relevance": _get_float(config, "scoring.domain_relevance.weight", 0.25),
        "skills_match": _get_float(config, "scoring.skills_match.weight", 0.20),
        "seniority_match": _get_float(config, "scoring.seniority_match.weight", 0.15),
        "experience_fit": _get_float(config, "scoring.experience_fit.weight", 0.10),
    }


def _distance_cfg(config: dict[str, Any]) -> dict[str, Any] | None:
    """The distance model's settings (``build_scoring_config``), or None for the v1 model."""
    cfg = config.get("scoring", {}).get("location_fit", {}).get("distance")
    return cfg if isinstance(cfg, dict) and cfg.get("home") else None


def _location_bonus(job: Job, config: dict[str, Any]) -> float:
    """The location bonus added before the gates. v1: ``top_location_bonus`` for the single
    top-preferred location. Distance model: ``top_location_bonus × (1 − d/R)`` for on-site/
    hybrid offers inside the radius (see src/pipeline/location_distance.py)."""
    bonus = _get_float(config, "scoring.gates.top_location_bonus", 0.08)
    distance_cfg = _distance_cfg(config)
    if distance_cfg:
        return bonus * assess_location(job, distance_cfg).proximity
    is_top = _is_top_preferred_location(
        job.location, job.countries, job.work_mode, job.remote, job.department, config)
    return bonus if is_top else 0.0


def _apply_weights(
    factors: dict[str, float],
    config: dict[str, Any],
    is_top_preferred_location: bool = False,
    location_bonus: float | None = None,
) -> float:
    """Combine the 5 factor scores into a single 0-1 score.

    This is a hybrid model: additive weighting for the three "matter of
    degree" factors (location, skills, experience), plus two explicit
    multiplicative gates for the "confirmed dealbreaker" factors (domain,
    seniority) — a confirmed mismatch on either must not be compensable by
    the other factors, which a pure weighted average would otherwise allow.

    Top-preferred-location bonus: adds a small fixed bonus to ``base``
    before the domain/seniority gates, for the single job resolving to the
    profile's highest-priority ``status="preferred"`` location (see
    ``_is_top_preferred_location``). Applied before the gates so it is
    fully absorbed by the domain cap and scaled down by the seniority
    multiplier along with the rest of ``base`` — it can only ever raise the
    score, never rescue a confirmed dealbreaker. See
    ``scoring.gates.top_location_bonus`` in config/scoring.yaml.

    ``location_bonus``, when given, is the bonus itself (``_location_bonus``, which also
    covers the distance model) and replaces the ``is_top_preferred_location`` flag.
    """
    weights = _scoring_weights(config)
    total = sum(weights.values())
    if total == 0:
        return 0.5
    base = sum(factors[k] * weights[k] for k in factors) / total

    if location_bonus is None:
        location_bonus = (
            _get_float(config, "scoring.gates.top_location_bonus", 0.08) if is_top_preferred_location else 0.0)
    if location_bonus:
        base = base + location_bonus

    if _domain_gate_fired(factors, config):
        domain_cap = _get_float(config, "scoring.gates.domain_cap", 0.40)
        base = min(base, domain_cap)

    if _seniority_gate_fired(factors):
        seniority_multiplier = _get_float(config, "scoring.gates.seniority_multiplier", 0.55)
        base = base * seniority_multiplier

    return min(max(base, 0.0), 1.0)


def cap_personalization_at_gates(
    pre_personalization: list[tuple[Job, float]],
    post_personalization: list[tuple[Job, float]],
    factors_by_job_id: dict[str, dict[str, float]],
    config: dict[str, Any],
) -> list[tuple[Job, float]]:
    """Re-enforce the domain/seniority dealbreaker gates AFTER personalization.

    Personalization (Phase 4, ``src.personalization.engine``) operates only
    on the post-gate ``(job, score)`` composite — it has no visibility into
    *why* that score is what it is, so it can add up to
    ``personalization.max_abs_adjustment`` to a job whose ``domain_relevance``
    or ``seniority_match`` already triggered a confirmed-dealbreaker gate in
    ``_apply_weights``, undoing the gate's purpose.

    This function does not change what personalization computed — it only
    refuses the POSITIVE portion of the net adjustment for a gated job
    (via ``_gate_fired``, the same condition ``_apply_weights`` uses): a
    gated job's final score can drop below its pre-personalization value
    (negative personalization still applies in full) but never rise above
    it. Non-gated jobs are returned unchanged.
    """
    pre_by_id = {job.id: score for job, score in pre_personalization}
    capped: list[tuple[Job, float]] = []
    for job, post_score in post_personalization:
        factors = factors_by_job_id.get(job.id)
        if factors is not None and _gate_fired(factors, config):
            pre_score = pre_by_id.get(job.id, post_score)
            post_score = min(post_score, pre_score)
        capped.append((job, post_score))
    capped.sort(key=lambda x: x[1], reverse=True)
    return capped


def _detect_language_penalty(job: Job, config: dict[str, Any]) -> float:
    """Return a score penalty (0.0–penalty_required) when a non-profile language
    is explicitly required from the candidate.

    Rules (F3):
    - French or English mentioned → no penalty (profile languages).
    - Non-profile language found near a "preferred/plus" signal → no penalty.
    - Non-profile language found near a "required/mandatory/fluent" signal → penalty.
    - Language mentioned with no requirement signal (description language only) → no penalty.
    - Ambiguous / undetected → no penalty.

    The penalty is configurable via scoring.language.penalty_required (default 0.15).
    Profile languages are injected from profile.languages by the orchestrator;
    the function always treats French and English as safe (never penalised).
    """
    penalty_required = _get_float(config, "scoring.language.penalty_required", 0.15)
    if penalty_required <= 0.0:
        return 0.0

    # Profile languages (injected by orchestrator, always includes French + English).
    profile_langs: set[str] = set(
        lang.lower()
        for lang in config.get("scoring", {}).get("language", {}).get("profile_languages", [])
    )
    # Always treat French and English as safe regardless of injection.
    profile_langs.update({"french", "english", "français", "anglais", "francais"})

    # Search text: title + first 3000 chars of description.
    text = (job.title or "") + " " + (job.description or "")[:3000]
    text_lower = text.lower()

    for lang_pat, lang_name in _NON_PROFILE_LANG_PATTERNS:
        if lang_name in profile_langs:
            continue
        for m in lang_pat.finditer(text_lower):
            # Extract context window around this language mention.
            start = max(0, m.start() - _LANG_WINDOW)
            end = min(len(text_lower), m.end() + _LANG_WINDOW)
            ctx = text_lower[start:end]

            # Preferred/plus signal overrides any required signal → no penalty.
            if _LANG_PREFERRED_RE.search(ctx):
                continue

            # Required/mandatory/fluent signal → apply penalty.
            if _LANG_REQUIRED_RE.search(ctx):
                return penalty_required

    return 0.0


def _apply_gate_and_language_penalties(
    raw: float, job: Job, config: dict[str, Any], gate_result: GateResult | None
) -> float:
    """Apply the gate's soft penalties plus the language penalty, multiplicatively.

    Penalties (hard constraints already removed these jobs):
    - ``gate_result.education_penalty``  — soft education mismatch
    - ``gate_result.skill_penalty``      — mandatory secondary skill not in profile
    - ``gate_result.experience_penalty`` — experience requirement exceeds profile max
    - language penalty                   — non-profile language explicitly required (F3)

    Phase LATER / I14: the three gate penalties come from the typed
    ``GateResult`` gate_jobs() already returns per job, not from
    ``job.raw_payload`` — see ``GateResult``'s own docstring.
    ``gate_result=None`` (no gate ran) means no gate penalties apply, same as
    when the old raw_payload lookup found nothing.
    """
    if gate_result is not None:
        if gate_result.education_penalty > 0.0:
            raw = raw * (1.0 - gate_result.education_penalty)
        if gate_result.skill_penalty > 0.0:
            raw = raw * (1.0 - gate_result.skill_penalty)
        if gate_result.experience_penalty > 0.0:
            raw = raw * (1.0 - gate_result.experience_penalty)
    lang_penalty = _detect_language_penalty(job, config)
    if lang_penalty > 0.0:
        raw = raw * (1.0 - lang_penalty)
    return min(max(raw, 0.0), 1.0)


def score_job(
    job: Job, config: dict[str, Any], gate_result: GateResult | None = None
) -> float:
    """Compute a deterministic score for a single job (0.0 to 1.0).

    ``gate_result``, if given, is the ``GateResult`` ``gate_jobs()`` computed
    for this job — see ``_apply_gate_and_language_penalties`` for how its
    soft penalties are applied.
    """
    raw = _apply_weights(
        _compute_factors(job, config, gate_result), config,
        location_bonus=_location_bonus(job, config),
    )
    return _apply_gate_and_language_penalties(raw, job, config, gate_result)


@dataclass(frozen=True)
class ScoreExplanation:
    """How a job's deterministic score was assembled (observability; mirrors ``score_job``)."""

    factors: dict[str, float]
    weights: dict[str, float]
    weighted_base: float | None          # None when every weight is 0 (composite is then the neutral 0.5)
    top_location_bonus: float            # 0.0 unless the job resolves to the top preferred location
    pre_gate_score: float | None         # weighted base + top-location bonus, before the gates
    domain_gate_fired: bool
    domain_cap: float | None
    domain_cap_applied: bool             # the cap actually lowered the score (pre-gate above the cap)
    seniority_gate_fired: bool
    seniority_multiplier: float | None
    composite: float                     # after weights + bonus + gates, before penalties
    penalties: dict[str, float]          # education / skill / experience / language (fractions)
    final: float                         # == score_job(...)
    location_match: dict[str, Any] | None  # the resolved location preference, if any
    # Distance model only (location_distance.describe): place, road km, remote eligibility.
    location_detail: dict[str, Any] | None = None


def explain_score(
    job: Job,
    config: dict[str, Any],
    gate_result: GateResult | None = None,
    factors: dict[str, float] | None = None,
) -> ScoreExplanation:
    """Decompose ``score_job`` into its parts. Reuses the very helpers ``score_job`` calls, so
    ``composite``/``final`` equal what production computes (asserted by tests); ``factors`` may be
    passed in when already computed (avoids recomputing them)."""
    if factors is None:
        factors = _compute_factors(job, config, gate_result)
    location_bonus = _location_bonus(job, config)
    weights = _scoring_weights(config)
    total = sum(weights.values())
    base = sum(factors[k] * weights[k] for k in factors) / total if total else None
    domain_fired = _domain_gate_fired(factors, config)
    seniority_fired = _seniority_gate_fired(factors)
    composite = _apply_weights(factors, config, location_bonus=location_bonus)
    bonus = location_bonus if total else 0.0
    pre_gate = None if base is None else base + bonus
    cap = _get_float(config, "scoring.gates.domain_cap", 0.40)
    final = _apply_gate_and_language_penalties(composite, job, config, gate_result)
    prefs = config.get("scoring", {}).get("location_fit", {}).get("location_preferences") or []
    match = None
    distance_cfg = _distance_cfg(config)
    detail = describe_location(assess_location(job, distance_cfg), distance_cfg) if distance_cfg else None
    if prefs and not distance_cfg:
        dept = job.department or extract_department_code(job.location or "", job.countries)
        is_remote = (job.work_mode == "remote") or (job.work_mode is None and job.remote)
        m = resolve_job_location(dept, is_remote, job.countries, prefs)
        match = None if m is None else {
            "type": m.type, "value": m.value, "status": m.status, "priority": m.priority, "label": m.label}
    return ScoreExplanation(
        factors=dict(factors), weights=weights, weighted_base=base,
        top_location_bonus=bonus, pre_gate_score=pre_gate,
        domain_gate_fired=domain_fired and total > 0,
        domain_cap=cap if domain_fired and total else None,
        domain_cap_applied=bool(domain_fired and pre_gate is not None and pre_gate > cap),
        seniority_gate_fired=seniority_fired and total > 0,
        seniority_multiplier=(
            _get_float(config, "scoring.gates.seniority_multiplier", 0.55) if seniority_fired and total else None),
        composite=composite,
        penalties={
            "education": gate_result.education_penalty if gate_result else 0.0,
            "skill": gate_result.skill_penalty if gate_result else 0.0,
            "experience": gate_result.experience_penalty if gate_result else 0.0,
            "language": _detect_language_penalty(job, config),
        },
        final=final, location_match=match, location_detail=detail,
    )


def score_jobs(
    jobs: list[Job],
    config: dict[str, Any],
    gate_results: dict[str, GateResult] | None = None,
) -> list[tuple[Job, float]]:
    """Score all jobs and return sorted by score (descending).

    ``gate_results``, if given, maps job.id -> GateResult (as returned by
    ``src.requirements.gate.gate_jobs()``) so each job's soft gate penalties
    and extracted experience-years are applied — see ``score_job``.
    """
    gate_results = gate_results or {}
    scored: list[tuple[Job, float]] = [
        (job, score_job(job, config, gate_results.get(job.id))) for job in jobs
    ]
    scored.sort(key=lambda x: x[1], reverse=True)
    logger.info("Scoring complete: %d jobs scored", len(scored))
    return scored


def score_jobs_with_breakdown(
    jobs: list[Job],
    config: dict[str, Any],
    gate_results: dict[str, GateResult] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[tuple[Job, float, dict[str, float]]]:
    """Score all jobs, returning (job, score, factor_breakdown) sorted by score (descending).

    The factor_breakdown dict maps each scoring dimension (location_fit,
    domain_relevance, skills_match, seniority_match, experience_fit) to its
    raw factor value before weighting/gating.  This is used by the API layer
    to persist and display explainable score breakdowns.

    ``gate_results``, if given, maps job.id -> GateResult — see ``score_jobs``.

    ``progress_callback``, if given, is called ``(processed, total)`` after
    each job is scored (before the final sort) — purely observational,
    never affects any score.
    """
    gate_results = gate_results or {}
    result: list[tuple[Job, float, dict[str, float]]] = []
    total = len(jobs)
    for i, job in enumerate(jobs, start=1):
        gate_result = gate_results.get(job.id)
        factors = _compute_factors(job, config, gate_result)
        raw = _apply_weights(factors, config, location_bonus=_location_bonus(job, config))
        score = _apply_gate_and_language_penalties(raw, job, config, gate_result)
        result.append((job, score, factors))
        if progress_callback is not None:
            progress_callback(i, total)
    result.sort(key=lambda x: x[1], reverse=True)
    logger.info("Scoring (with breakdown) complete: %d jobs scored", len(result))
    return result


# -- helpers ---------------------------------------------------------------


def _score_domain_relevance(
    title: str | None, description: str | None, config: dict[str, Any]
) -> float:
    """Score how well the job matches the user's selected target domains.

    Evidence hierarchy
    ------------------
    0.05 : "Confirmed wrong" — the title matches a known off-domain/non-IT
           role keyword (config/filters.yaml's excluded_title_keywords +
           non_it_title_functions, reused here). This is checked FIRST,
           before any positive evidence: confirmed off-domain title evidence
           must win over incidental positive signals (a generic role noun
           in the same title, or a technology word in the description) —
           see the "Evidence-hierarchy ordering" audit note below. This is
           the only case that triggers the domain gate in _apply_weights.
    1.0  : Domain keyword (from profile.target_domains × filters.yaml
           vocabulary) found in the title.
    0.7  : Domain keyword found in the description (first 2000 chars) —
           title didn't match, but the job body confirms the domain.
    0.35 : No domain keyword anywhere, but a broad technical role indicator
           is present in the title (engineer, developer, ingénieur, …).
           "developer"/"développeur" specifically do not count when
           immediately preceded by "business"/"sales" — see
           _domain_keyword_matches and its module-level comment.
    0.45 : "Undetermined" — no domain evidence AND no off-domain evidence.
           This is NOT further penalized: not being able to detect the
           domain is not the same as confirming the job is the wrong domain.

    The injected config keys are:
      scoring.domain_relevance.domain_title_keywords       — from selected domains
      scoring.domain_relevance.domain_description_keywords — from selected domains
      scoring.domain_relevance.off_domain_keywords          — from filters.yaml

    Off-domain title evidence is checked first and unconditionally, so a
    generic role noun ("ingénieur", "technicien", …) elsewhere in the title
    can never rescue a confirmed off-domain job before its off-domain
    evidence is consulted.
    """
    title_norm = _norm_text(title or "")

    off_domain_kws = _get_keywords(config, "scoring.domain_relevance.off_domain_keywords")
    if off_domain_kws and title_norm:
        for kw in off_domain_kws:
            kw_norm = _norm_text(kw)
            if re.search(r"(?<![a-z0-9])" + re.escape(kw_norm) + r"(?![a-z0-9])", title_norm):
                return 0.05

    domain_title_kws = _get_keywords(config, "scoring.domain_relevance.domain_title_keywords")
    if domain_title_kws and any(
        _domain_keyword_matches(_norm_text(kw), title_norm) for kw in domain_title_kws
    ):
        return 1.0

    domain_desc_kws = _get_keywords(config, "scoring.domain_relevance.domain_description_keywords")
    if domain_desc_kws:
        desc_norm = _norm_text((description or "")[:2000])
        if any(_domain_keyword_matches(_norm_text(kw), desc_norm) for kw in domain_desc_kws):
            return 0.7

    if any(_domain_keyword_matches(ind, title_norm) for ind in _BROAD_TECH_TITLE_INDICATORS):
        return 0.35

    return 0.45


def _location_preference_score(match: LocationMatch) -> float:
    """Score a resolved location_preferences match: 1.0 for the top-priority
    ``preferred`` entry, decaying 0.03 per lower priority position (floored
    at 0.70); ``acceptable`` entries are capped 0.15 below the equivalent
    ``preferred`` score. Deliberately simple — a bounded linear decay, not a
    second scoring subsystem: the location_fit weight (0.30) already does
    the real work, this only orders preferences among themselves.
    """
    base = 1.0 if match.status == "preferred" else 0.85
    decay = min(0.30, 0.03 * match.priority)
    return max(base - decay, 0.70)


def _top_preferred_location_priority(location_preferences: list[dict[str, Any]]) -> int | None:
    """Index (within the user's configured ``location_preferences`` list) of
    the single highest-priority ``status="preferred"`` entry — the profile's
    OWN definition of "top preferred location", read directly from the
    profile data rather than inferred from any derived score. Returns
    ``None`` when the list has no ``"preferred"`` entry at all (nothing to
    protect — an all-``"acceptable"`` list, for instance).

    List position is priority (see ``_score_location_fit``'s docstring), so
    this is simply the lowest index among ``"preferred"`` entries — it does
    not assume that entry is at index 0: a profile that happened to list an
    ``"acceptable"`` department before its ``"preferred"`` one is handled
    correctly.
    """
    preferred_indices = [
        idx for idx, pref in enumerate(location_preferences or [])
        if pref.get("status") == "preferred"
    ]
    return min(preferred_indices) if preferred_indices else None


def _is_top_preferred_location(
    location: str | None,
    countries: list[str],
    work_mode: str | None,
    remote: bool,
    department: str | None,
    config: dict[str, Any],
) -> bool:
    """True when this job resolves to the profile's single highest-priority
    ``status="preferred"`` location — the ONE entry ``_apply_weights``'s
    top-location bonus protects.

    Reads the resolved ``LocationMatch``'s own ``.status``/``.priority``
    fields rather than inferring "top preferred" from
    ``_score_location_fit`` returning ``1.0``, so it stays correct even if
    that function's decay formula changes. Mirrors ``_score_location_fit``'s
    own branching (location_preferences first, legacy ``target_department``
    fallback only when that list is empty) so the two functions can never
    disagree about which job this is.

    Deliberately narrower than "any ``status="preferred"`` entry": a profile
    can configure multiple preferred entries, and only the single
    lowest-priority-index one gets this bonus — giving every preferred entry
    an equal bonus would not change the relative gap between them.
    """
    location_fit_cfg = config.get("scoring", {}).get("location_fit", {})
    location_preferences = location_fit_cfg.get("location_preferences") or []
    dept = department or extract_department_code(location or "", countries)
    is_remote = (work_mode == "remote") or (work_mode is None and remote)

    if location_preferences:
        top_priority = _top_preferred_location_priority(location_preferences)
        if top_priority is None:
            return False
        match = resolve_job_location(dept, is_remote, countries, location_preferences)
        return (
            match is not None
            and match.status == "preferred"
            and match.priority == top_priority
        )

    # Legacy single-value fallback — mirrors _score_location_fit's own
    # legacy branch: only consulted when location_preferences is empty, and
    # the one configured value is necessarily "the top one" since there is
    # only one.
    legacy_target_dept = location_fit_cfg.get("target_department")
    return bool(legacy_target_dept) and dept is not None and dept == str(legacy_target_dept)


def _score_location_fit(
    location: str | None,
    countries: list[str],
    work_mode: str | None,
    remote: bool,
    department: str | None,
    config: dict[str, Any],
) -> float:
    """Score based on geographic fit: priority-ordered department/remote
    preferences first, then country/remote tiers.

    Location Preferences (2026-09-10): the top tiers are driven entirely by
    ``profile.geography.location_preferences`` — an ORDERED list of
    department/remote entries (e.g. [{"type": "department", "value": "83",
    "status": "preferred"}, ...]) the candidate configures on the Profile
    page, injected here as ``scoring.location_fit.location_preferences`` by
    orchestrator.py, and resolved via
    ``src.pipeline.location_match.resolve_job_location``. There is NO hardcoded department
    default — an empty/unset profile list means no location gets this tier.
    List position is priority: the first (index 0) matching preference wins,
    each following one scoring slightly lower. This is a ranking preference
    only: a job outside the list is never rejected for it and still
    competes normally on every other factor (domain, skills, seniority,
    experience, remote status). ``status="excluded"`` entries are vetoed by
    the resolver (never chosen as the winning match), not scored here at
    all — an excluded job with no other match falls through to the ordinary
    tiers below exactly like any other non-preferred location.

    ``target_department`` (a single legacy string, e.g. {"target_department":
    "83"}) is still honoured for backward compatibility with configs/tests
    that set it directly, but ONLY when ``location_preferences`` is empty —
    a configured priority list always takes precedence and is never
    overridden by the legacy key.

    Scoring tiers
    -------------
    1.0 down to 0.70 : The candidate's highest-priority matching location
           (``status="preferred"``), decaying slightly for each lower
           priority position; ``status="acceptable"`` matches are capped
           below preferred ones. See _location_preference_score() below.
    0.85 : A neighboring department of a Var/PACA preference (on-site or
           hybrid) — see PACA_NEIGHBOR_DEPARTMENTS. This bonus tier is
           independent of location_preferences and unaffected by it; it
           remains tied to the fixed PACA constant regardless of what the
           user selects elsewhere.
    0.75 : Preferred country (e.g. France), remote.
    0.60 : Preferred country, on-site/hybrid/unknown work mode.
    0.55 : Outside the preferred country but remote-eligible (already passed
           the location hard filter's EU/worldwide-remote-scope check).
    0.40 : Unknown/unresolvable location.

    Phase 3 (2026-09-10): the preferred-country on-site/hybrid baseline was
    lowered from 0.65 to 0.60 — not a weight change. Audit found 85.5% of
    all real jobs sitting at this exact tier (the vast majority of French
    IT postings are outside Var/PACA), so raising location_fit's WEIGHT
    would have amplified almost nothing for that 85.5% and only marginally
    widened the Var/PACA differential. Lowering this baseline instead widens
    the gap between Var (1.0) / PACA (0.85) and everywhere else without
    touching either bonus tier, without penalizing remote (0.75, still the
    2nd-highest tier), and without creating any hard rejection — a job here
    still clearly outranks a non-preferred-country job (0.55) or an unknown
    location (0.40).

    When no preferred_countries is configured (e.g. unit tests), every
    location with any data gets the 0.65/0.75 tier — backward-compatible
    behaviour matching the rest of this module's "no config → don't guess"
    convention. This fallback path is intentionally untouched by the Phase 3
    baseline change: it only exists for callers/tests that never set up
    preferred_countries in the first place, not for real preferred-country
    scoring.
    """
    location_fit_cfg = config.get("scoring", {}).get("location_fit", {})
    location_preferences = location_fit_cfg.get("location_preferences") or []

    dept = department or extract_department_code(location or "", countries)
    is_remote = (work_mode == "remote") or (work_mode is None and remote)

    if location_preferences:
        match = resolve_job_location(dept, is_remote, countries, location_preferences)
        if match is not None:
            return _location_preference_score(match)
    else:
        # Legacy single-value fallback — only consulted when no priority
        # list is configured, and never overrides one that is.
        legacy_target_dept = location_fit_cfg.get("target_department")
        if legacy_target_dept and dept is not None and dept == str(legacy_target_dept):
            return 1.0

    preferred: list[str] = [
        c.lower()
        for c in config.get("scoring", {}).get("location_fit", {}).get("preferred_countries", [])
    ]
    location_lower = (location or "").lower()
    countries_lower = [c.lower() for c in (countries or [])]
    is_preferred_country = bool(preferred) and any(
        p in location_lower or p in countries_lower for p in preferred
    )

    if dept in PACA_NEIGHBOR_DEPARTMENTS and work_mode in ("on_site", "hybrid", None):
        return 0.85

    if not location_lower and not countries_lower:
        return 0.40  # Unknown → conservative, not neutral.

    if preferred:
        if is_preferred_country:
            return 0.75 if is_remote else 0.60
        return 0.55  # Not preferred, but passed the hard filter's remote-scope check.

    # No preferred countries configured → any known location gets the base tier.
    return 0.75 if is_remote else 0.65


def _score_seniority_match(
    title: str | None,
    config: dict[str, Any],
    job_level: str | None = None,
) -> float:
    """Score based on seniority level match against profile preferred_levels.

    Source priority (2026-09-11 audit fix): the job TITLE is always checked
    first; ``job_level`` (a generic source-supplied facet, e.g. LinkedIn's
    "Mid-Senior level") is only consulted as a fallback when the title
    itself carries no seniority signal at all — no preferred-level keyword
    match AND no wrong-seniority indicator. This matters because
    source-level metadata is coarser and less reliable than the posting's
    own title: a live audit found 773 accepted LinkedIn jobs tagged
    "mid-senior level" while 98.6% of them stated no years-of-experience
    requirement anywhere in the text, and at least 9 had an explicit
    "Junior"/"Apprentice"/"Alternance" title that the tag was silently
    overriding (e.g. "DBA Junior (H/F)", "IT Apprentice" scored as a
    confirmed wrong-seniority dealbreaker despite the title itself saying
    otherwise). When the title has no signal, behavior is unchanged from
    before this fix — job_level (if present) decides exactly as it did
    previously.

    Scoring tiers
    -------------
    No preferred-level keywords configured  → 0.5 (neutral; no profile preference).
    Preferred keyword found in title        → bonus score (≥ 0.5, capped at 1.0).
    Title has a wrong-seniority indicator
      and no preferred keyword              → 0.0 (wrong level explicitly stated).
    Title has no signal at all              → fall back to job_level using the
                                              same two checks; if job_level also
                                              has no signal (or is absent)  → 0.5.

    Example: "AI Engineer" has no seniority indicator → falls back to
             job_level, or 0.5 if job_level is also absent/uninformative.
             "Senior Software Engineer" with preferred=["junior"] → 0.0 penalty
             (title signal, job_level never consulted).
             "DBA Junior (H/F)" with job_level="Mid-Senior level" → title's
             "Junior" keyword wins → bonus score (job_level never consulted).
    """
    keywords = _get_keywords(config, "scoring.seniority_match.preferred_level_keywords")
    if not keywords:
        return 0.5

    title_score = _seniority_text_score(title, keywords, config)
    if title_score is not None:
        return title_score

    if job_level:
        job_level_score = _seniority_text_score(job_level, keywords, config)
        if job_level_score is not None:
            return job_level_score

    return 0.5


def _seniority_text_score(
    text: str | None, keywords: list[str], config: dict[str, Any]
) -> float | None:
    """Return a seniority score for a single piece of text, or ``None`` when
    the text carries no seniority signal at all (no preferred-level keyword
    match and no wrong-seniority indicator) — signalling the caller should
    fall through to another source rather than defaulting to neutral itself.

    Uses the same word-boundary-guarded, third-party-coordination-aware
    detector (``find_seniority_indicators``) rather than a bare substring
    check — a bare `"lead" in text_norm` check matched inside
    "leader"/"leadership" (see test_seniority_indicator_detection.py).
    """
    if not text:
        return None

    text_norm = _norm_text(text)
    matches = sum(1 for keyword in keywords if _norm_text(keyword) in text_norm)

    if matches > 0:
        bonus = _get_float(config, "scoring.seniority_match.junior_bonus", 1.2)
        return min(matches * bonus, 1.0)

    if find_seniority_indicators(text):
        return 0.0

    return None


def _score_experience_fit(
    job: Job, config: dict[str, Any], gate_result: GateResult | None = None
) -> float:
    """Score based on the job's stated minimum years-of-experience requirement.

    ``years`` comes from ``gate_result.min_experience_years`` (Phase LATER /
    I14 — previously ``job.raw_payload["_extracted_min_experience_years"]``,
    see ``GateResult``'s own docstring for why this moved), set
    unconditionally by ``src.requirements.gate.gate_jobs()`` alongside the
    existing penalty fields (regardless of MANDATORY/PREFERRED importance —
    a MANDATORY excess over the candidate's max is already a hard rejection
    upstream and never reaches scoring; a PREFERRED excess also still applies
    its own separate multiplicative `_experience_penalty`, unrelated to this
    curve, which is about the stated-years value itself, not about exceeding
    the candidate's configured max). ``gate_result=None`` (no gate ran, or a
    caller scoring a job outside the normal pipeline) means "unknown", same
    as before.

    Curve
    -----
    years <= 1  → 1.0
    years >= 2  → max(floor, 1.0 - decay_per_year * (years - 1))
    unknown     → unknown_score (0.50 by default — genuine neutral/uncertain,
                  not a positive fit signal; see below and scoring.yaml's
                  comment).

    Why 0.50, not the old 0.80 (Phase 3, 2026-09-10)
    -------------------------------------------------
    "Unknown" here means the requirements-extraction stage found no
    experience-years statement in the posting at all (``exp is None`` in
    ``src.requirements.gate``) — it is the ABSENCE of evidence, not evidence
    of anything. The previous 0.80 default treated that absence as
    near-maximal positive evidence (only 0.20 below the best possible score,
    reachable by NO stated requirement at all versus an explicit "0-1 years"
    statement) based on a real-data correlation (unknown-experience jobs'
    junior-signal rate was empirically closer to stated-2yr than stated-3yr
    jobs). That correlation is real but is a proxy, not a stated fact about
    THIS job — and every other "no evidence available" case in this module
    (``_score_skills_match``'s 0.5 when no target_skills configured,
    ``_score_seniority_match``'s 0.5 when no seniority signal at all) already
    uses a genuine neutral midpoint, not a proxy-inflated one. Unknown
    experience is now scored the same way: neutral, not favorable — 98.3% of
    real jobs carry no stated experience requirement, so this single value
    was effectively setting the experience contribution for nearly the
    entire job pool, and treating "we don't know" as "almost certainly great"
    for that many jobs was exactly the kind of unearned inflation the rest of
    the score redesign was correcting elsewhere.
    """
    years: int | None = None
    if gate_result is not None and gate_result.min_experience_years is not None:
        years = int(gate_result.min_experience_years)

    if years is None:
        return _get_float(config, "scoring.experience_fit.unknown_score", 0.50)

    if years <= 1:
        return 1.0

    decay = _get_float(config, "scoring.experience_fit.decay_per_year", 0.15)
    floor = _get_float(config, "scoring.experience_fit.floor", 0.15)
    return max(floor, min(1.0, 1.0 - decay * (years - 1)))


def _score_skills_match(
    job: Job, config: dict[str, Any], domain_relevance: float | None = None
) -> float:
    """Score based on actual overlap between job content and user's target skills.

    Returns 0.5 when no target_skills are configured (neutral — unknown).
    Scales up linearly from ~0.4 as the fraction of matching skills increases,
    normalized against min(len(target_skills), denominator_cap) rather than
    the full list length — matching ~5 of a longer profile skill list is
    already a strong match; requiring most of a 10+ item list to approach 1.0
    structurally caps every real job well below its own ceiling (see
    scoring.yaml's comment).

    Role-context fallback (no explicit skill/tool evidence)
    ---------------------------------------------------------
    When no target skill appears anywhere in the job (``matches == 0``), the
    plain "no overlap" floor is 0.05 — appropriate when the job's domain
    itself is also unconfirmed (a real weak/generic match). But 2026-09-10
    audit data showed 58% of ALL real scored jobs hit this exact floor, and
    86% of those were jobs whose title/description already confirms them as
    in-domain IT roles (``domain_relevance >= 0.7``) — e.g. "Technicien
    Réseau Système", "Administrateur Systèmes" — roles that are squarely
    role-based, not tool-based, and simply don't itemize a tech stack in the
    posting. Treating "tools unstated" the same as "tools absent/wrong"
    systematically punished exactly the IT-support/network/systems category
    the profile prioritizes.

    ``domain_relevance`` is optional (default ``None``) so every pre-existing
    caller/test that scores skills_match in isolation, without domain
    context, keeps the original 0.05-floor behavior unchanged — the fallback
    only activates when the caller (``_compute_factors``) actually supplies
    the job's already-computed domain_relevance value.

    When supplied and ``domain_relevance >= fallback_domain_threshold``
    (default 0.7 — matches _score_domain_relevance's own "title match" (1.0)
    and "description match" (0.7) tiers, deliberately excluding its 0.35
    "broad tech title only" and 0.45 "undetermined" tiers, which are not a
    confirmed domain match):
        fallback = fallback_base + fallback_domain_scale * domain_relevance
    With the default base=0.20/scale=0.20, this yields 0.34 (domain=0.7) to
    0.40 (domain=1.0) — continuous in domain_relevance, not a flat jump, and
    deliberately kept BELOW the weakest possible explicit-match score (0.52,
    for 1 match out of a 5-skill denominator) so real skill/tool evidence
    always outranks the fallback. Below the threshold (domain_relevance <
    0.7 — undetermined or broad-tech-title-only), the plain 0.05 floor is
    unchanged: a genuinely weak/generic job is not rescued by this fallback.

    Domain-vocabulary bonus within the fallback (2026-09-11)
    ----------------------------------------------------------
    The fallback above still under-credits confirmed cybersecurity/
    networking/systems/devops/automation jobs: their real vocabulary
    (SIEM, pentest, Active Directory, VLAN, CI/CD, ...) structurally cannot
    overlap with a general software/web-dev ``target_skills`` list, so they
    always land at the flat fallback regardless of how tool-heavy the
    posting actually is. When the fallback applies AND the job's title or
    description mentions a term from ``scoring.skills_match.domain_vocabulary``
    (a small curated list injected per the candidate's selected domains from
    ``config/filters.yaml``'s ``domain_skill_vocabulary`` — see
    ``_has_domain_vocabulary_hit``), a bounded bonus (default 0.12) is added,
    capped at 0.50 — still below the weakest explicit target_skill match
    (0.52), so this can never outrank the candidate's own listed skills. This
    is deliberately NOT the same list as ``target_skills``: it never appears
    as a "matched skill" in the UI, since the candidate never claimed to know
    that specific tool — it only informs the deterministic score.

    Matching strategy
    -----------------
    Title text: raw skill form (word-boundary guarded).
      Job titles are structured and do not contain free-form French prose, so
      matching "AI" directly in a title like "AI Engineer" is safe and correct.

    Description/skills text: expanded canonical forms (word-boundary guarded).
      Free-form French text contains false-positive substrings such as "j'ai"
      (= "I have") for the "AI" skill, or "agit" for "git".  Expanding short
      abbreviations to their canonical long forms ("AI" → "artificial
      intelligence", "machine learning") avoids these false positives.

    A skill counts as matched when it appears in EITHER source (title OR
    description/skills), so jobs with rich descriptions and jobs with only
    a title both produce meaningful scores.
    """
    target_skills: list[str] = (
        config.get("scoring", {}).get("skills_match", {}).get("target_skills", [])
    )
    if not target_skills:
        return 0.5  # No profile skills configured → neutral

    # Separate title text from description/skills text so we can apply
    # different matching strategies to each.
    title_norm = _norm_text(job.title or "")

    desc_parts: list[str] = []
    if job.skills:
        desc_parts.append(_norm_text(" ".join(job.skills)))
    if job.description:
        desc_parts.append(_norm_text(job.description[:2000]))
    desc_norm = " ".join(desc_parts)

    if not title_norm and not desc_norm:
        return 0.5  # No job content to match against → neutral

    matches = 0
    for skill in target_skills:
        skill_norm = _norm_text(skill.strip())
        matched = False

        # 1. Title: match raw form (word-boundary guard prevents substrings).
        if title_norm:
            raw_pattern = r"(?<![a-z0-9])" + re.escape(skill_norm) + r"(?![a-z0-9])"
            if re.search(raw_pattern, title_norm):
                matched = True

        # 2. Description/skills list: match expanded canonical forms to avoid
        #    French prose false-positives ("j'ai" ≠ AI, "agit" ≠ git).
        if not matched and desc_norm:
            for form in _expand_skill(skill, config):
                form_pattern = r"(?<![a-z0-9])" + re.escape(form) + r"(?![a-z0-9])"
                if re.search(form_pattern, desc_norm):
                    matched = True
                    break

        if matched:
            matches += 1

    if matches == 0:
        if domain_relevance is not None:
            fallback_threshold = _get_float(
                config, "scoring.skills_match.fallback_domain_threshold", 0.7
            )
            if domain_relevance >= fallback_threshold:
                fallback_base = _get_float(config, "scoring.skills_match.fallback_base", 0.20)
                fallback_scale = _get_float(
                    config, "scoring.skills_match.fallback_domain_scale", 0.20
                )
                fallback = fallback_base + fallback_scale * domain_relevance
                if _has_domain_vocabulary_hit(title_norm, desc_norm, config):
                    bonus = _get_float(config, "scoring.skills_match.domain_vocab_bonus", 0.12)
                    # Capped below 0.52 (the weakest possible EXPLICIT
                    # target_skill match) — domain-specific vocabulary is
                    # real evidence the job is tool/tech-oriented, but it is
                    # not the candidate's OWN listed skill, so it must never
                    # outrank genuine target_skills evidence. See
                    # _has_domain_vocabulary_hit's docstring.
                    fallback = min(fallback + bonus, 0.50)
                return min(max(fallback, 0.0), 1.0)
        return 0.05  # No skill overlap and no confirmed domain; hard filter already ensured IT relevance

    # Scale: floor at ~0.4 for any match, up to 1.0 once `denominator_cap`
    # skills are matched (not the full list length — see docstring above).
    denominator_cap = int(_get_float(config, "scoring.skills_match.denominator_cap", 5))
    denominator = min(len(target_skills), max(denominator_cap, 1))
    fraction = min(matches / denominator, 1.0)
    return min(0.4 + 0.6 * fraction, 1.0)


def _has_domain_vocabulary_hit(
    title_norm: str, desc_norm: str, config: dict[str, Any]
) -> bool:
    """True if any term from ``scoring.skills_match.domain_vocabulary`` (a
    flat, already-deduplicated list injected by the orchestrator from
    ``config/filters.yaml``'s ``domain_skill_vocabulary``, restricted to the
    candidate's selected ``profile.target_domains``) appears in the job's
    title or description.

    Deliberately a SEPARATE vocabulary from ``target_skills``: these terms
    represent real technology evidence for a domain (e.g. "SIEM", "VLAN",
    "CI/CD") the candidate did not personally list as a skill — used only to
    inform ``_score_skills_match``'s fallback tier, never surfaced as a
    "matched skill". Plain word-boundary matching (no alias expansion): the
    curated terms are specific multi-character tool/technology names, not
    short abbreviations prone to the French-prose false positives
    ``_expand_skill`` exists to avoid (e.g. "ai" inside "j'ai").
    """
    terms = _get_keywords(config, "scoring.skills_match.domain_vocabulary")
    if not terms:
        return False
    for term in terms:
        term_norm = _norm_text(term.strip())
        if not term_norm:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(term_norm) + r"(?![a-z0-9])"
        if title_norm and re.search(pattern, title_norm):
            return True
        if desc_norm and re.search(pattern, desc_norm):
            return True
    return False