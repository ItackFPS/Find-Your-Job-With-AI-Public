"""Deterministic filtering layer.

Filters jobs based on configurable criteria: locations, remote status, seniority,
title keywords, publication age, and salary requirements.

PRINCIPLE: High recall at the hard-filter stage.
Only clearly incompatible jobs are rejected. Precision is handled downstream
by scoring, AI analysis, semantic matching, and personalization.
Missing data is UNKNOWN — not a rejection reason.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from src.models.job import Job
from src.pipeline.location_distance import (
    assess,
    describe,
    distance_config,
    remote_policy_exclusion,
    strict_distance_exclusion,
)

if TYPE_CHECKING:
    from src.models.profile import JobSearchProfile

# Sources whose raw_payload["occupation"] (the source's own occupation for an offer)
# counts as role evidence in the title check: the source name set in config/sources.yaml
# for src/collectors/france_travail_web.py. Other sources' payloads are not read for it.
_OCCUPATION_EVIDENCE_SOURCES = frozenset({"france-travail-web"})

# Skill abbreviation → expanded canonical forms used for DESCRIPTION matching.
# The raw abbreviation stays in TITLE matching (short tokens are fine in titles
# where context is tighter); only the description fallback uses the expanded
# form to prevent substring false-positives (e.g. "ai" ↔ "j'ai" in French).
_SKILL_ALIASES: dict[str, list[str]] = {
    "ai": ["artificial intelligence", "machine learning"],
    # French "IA" (intelligence artificielle) — without this alias the bare
    # 2-letter token "ia" is used directly in description matching and can
    # match incidental substrings in unrelated postings (e.g. a passing
    # "...projets liés à l'IA..." mention in an admin/PM job description),
    # the same false-positive class already documented above for "ai"/"j'ai".
    "ia": ["intelligence artificielle", "machine learning"],
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


# Apostrophe variants used in French text before "expérience":
#   U+0027  APOSTROPHE (standard ASCII)
#   U+2018  LEFT SINGLE QUOTATION MARK
#   U+2019  RIGHT SINGLE QUOTATION MARK
_APOS: str = "[\u0027\u2018\u2019]"  # U+0027 ASCII apos, U+2018 left, U+2019 right

# French year-unit variants: an, ans, année, années (with/without accent).
_FR_YR_PAT: str = r"ann[ée]es?|ans?"

# Pre-compiled patterns for experience extraction.
# Each pattern captures the numeric lower bound of an explicit requirement.
# Ordering: most-specific (qualified) patterns first so the first match is meaningful.
# \*{0,2} handles markdown bold e.g. "4\n  **ans**" in formatted job descriptions.
_EXP_PATTERNS: list[re.Pattern[str]] = [
    # English — "5+ years experience"
    re.compile(r"(\d+)\s*\+\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp\b)", re.I),
    # English — range lower bound: "3-5 years experience"
    re.compile(r"(\d+)\s*[-–]\s*\d+\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp\b)", re.I),
    # English — "minimum N years" / "at least N years"
    re.compile(r"(?:minimum|at\s*least|min\.?)\s*(\d+)\s*(?:years?|yrs?)", re.I),
    # English — plain "N years of experience"
    re.compile(r"(\d+)\s*(?:years?|yrs?)\s*(?:of\s*)?(?:professional\s*)?experience", re.I),
    # French qualified — "au moins/au minimum N ans" (no d’expérience suffix required)
    re.compile(r"au\s+(?:moins|minimum)\s+(\d+)\s*\*{0,2}\s*(?:" + _FR_YR_PAT + ")", re.I),
    # French qualified — "minimum N ans" (no d’expérience suffix required)
    re.compile(r"minimum\s+(\d+)\s*\*{0,2}\s*(?:" + _FR_YR_PAT + ")", re.I),
    # French — "N ans minimum" (trailing qualifier)
    re.compile(r"(\d+)\s*\*{0,2}\s*(?:" + _FR_YR_PAT + r")\s+minimum\b", re.I),
    # French — "N+ ans" (explicit plus is unambiguous)
    re.compile(r"(\d+)\s*\+\s*(?:" + _FR_YR_PAT + ")", re.I),
    # French — "N ans ou plus"
    re.compile(r"(\d+)\s*\*{0,2}\s*(?:" + _FR_YR_PAT + r")\s+ou\s+plus\b", re.I),
    # French — range "3 à 5 ans d’expérience" (suffix required to avoid age/duration false-positives)
    re.compile(r"(\d+)\s*[àa]\s*\d+\s*(?:" + _FR_YR_PAT + r")\s+d" + _APOS + r"exp[éeè]rience", re.I),
    # French — plain "N ans d’expérience" (suffix required for unqualified form)
    re.compile(r"(\d+)\s*\*{0,2}\s*(?:" + _FR_YR_PAT + r")\s+d" + _APOS + r"exp[éeè]rience", re.I),
]

logger = logging.getLogger(__name__)


def load_filters(config_path: str) -> dict[str, Any]:
    """Load the engine vocabulary from a YAML file (config/filters.yaml).

    The returned dict is the engine's keyword vocabulary, not the filter
    config consumed by ``filter_jobs``.  Pass it to ``build_filter_config``
    together with a ``JobSearchProfile`` to obtain the filter config.
    """
    import yaml
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise TypeError(f"Expected a mapping in {config_path}")

        return data


def load_profile(config_path: str) -> "JobSearchProfile":
    """Load a :class:`JobSearchProfile` from a YAML file (config/profile.yaml)."""
    import yaml
    from src.models.profile import JobSearchProfile

    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise TypeError(f"Expected a mapping in {config_path}")

    return JobSearchProfile(**data)


def build_filter_config(profile: "JobSearchProfile", engine_cfg: dict[str, Any]) -> dict[str, Any]:
    """Translate a :class:`JobSearchProfile` + engine vocabulary → filter config.

    The returned dict is consumed directly by :func:`filter_jobs`.

    Separation of concerns
    ----------------------
    - ``profile``     — WHAT the user wants (domains, geography, experience …)
    - ``engine_cfg``  — HOW to match it (keyword lists, country names, …)
    - returned dict   — the concrete filter rules applied to each job

    This function is the bridge between user preferences and the filter engine.
    Changing ``config/profile.yaml`` changes the output of this function
    without modifying any Python code.
    """
    domain_kws: dict[str, Any] = engine_cfg.get("domain_keywords", {})
    seniority_excl: dict[str, list[str]] = engine_cfg.get("seniority_exclusions", {})

    # Domain keywords — primary source of domain-relevance signals.
    # A domain not found in the curated vocabulary (a custom entry, or one
    # picked from the ESCO occupation search) has no pre-built keyword set —
    # fall back to using the domain text itself as a literal title keyword,
    # the same treatment target_skills get below.
    domain_title_include: list[str] = []
    domain_description_include: list[str] = []
    for domain in profile.target_domains:
        kws = domain_kws.get(domain, {})
        if kws:
            domain_title_include.extend(kws.get("title", []))
            domain_description_include.extend(kws.get("description", []))
        else:
            domain_title_include.append(domain)

    # Skill keywords — added to title matching verbatim; for description
    # matching, known abbreviations (AI, ML, …) are expanded to their
    # canonical long forms to prevent substring false-positives.
    skill_title_include: list[str] = list(profile.target_skills)
    skill_description_include: list[str] = []
    for skill in profile.target_skills:
        skill_description_include.extend(_expand_skill_for_description(skill))

    # A domain, when set AND focus_domain is on (the default — see the
    # Profile page's "Focus on domain" switch), is the sole gate for
    # hard-filter acceptance: a skill mention alone must not admit a job
    # outside the chosen domain (e.g. domain=software_development +
    # skill="python" must reject a "Data Analyst (Python)" posting just
    # because it names the skill). Skills still shape ranking downstream
    # (scoring/semantic matching) either way.
    # Turning focus_domain off (or leaving target_domains empty) restores
    # the wider net: a job matching either a domain OR a skill passes.
    if profile.target_domains and profile.focus_domain:
        title_include = domain_title_include
        description_include = domain_description_include
    elif profile.target_domains:
        title_include = domain_title_include + skill_title_include
        description_include = domain_description_include + skill_description_include
    else:
        title_include = skill_title_include
        description_include = skill_description_include

    # Non-IT exclusion groups (excluded_title_keywords / non_it_title_functions)
    # are grouped by the domain that opts back into them — a group's keywords
    # only apply when the user hasn't selected that domain. See the comments
    # above those two config sections in filters.yaml.
    def _flatten_ungrouped(groups: dict[str, list[str]] | list[str]) -> list[str]:
        # Backward-compatible: a flat list (the pre-grouping shape) is always
        # applied, regardless of domain — only a dict gets the opt-out check.
        if isinstance(groups, list):
            return groups
        flat: list[str] = []
        for group_key, keywords in groups.items():
            if group_key not in profile.target_domains:
                flat.extend(keywords)
        return flat

    # Contract type keywords (e.g. "alternance", "apprenti", "stagiaire") are
    # intentionally NOT added to title_include here. A contract type describes
    # HOW a job is offered, never WHAT domain it is in — "Réceptionniste ...
    # alternance" and "Apprenti Employé Libre Service" are not IT jobs just
    # because they mention an apprenticeship. matches_title_keywords() accepts
    # on ANY include-list hit, so merging contract words into the same list
    # let a contract-type keyword alone establish IT relevance, bypassing the
    # domain check entirely (see the 2026-09-05 real-job benchmark). A
    # legitimate "Alternance Développeur Python" still passes because its
    # genuine domain/skill keywords ("développeur", "python") are already in
    # title_include independent of the contract type.

    # Seniority exclusion keywords from the profile's exclusion label list.
    seniority_exclude: list[str] = []
    for level in profile.experience.excluded_seniority:
        seniority_exclude.extend(seniority_excl.get(level, []))

    # Location include list: preferred countries + allowed regions.
    locations_include = list(profile.geography.preferred_countries)
    locations_include.extend(profile.geography.allowed_regions)

    def _dedup(lst: list[str]) -> list[str]:
        return list(dict.fromkeys(lst))

    # Freelance exclusion: title-only, deliberately NOT based on the
    # structured employment_type field. Job boards conflate CDD and
    # freelance under the same generic "contract" employment_type bucket
    # (see src/pipeline/explain.py's _EMP_MAP), so rejecting on that field
    # would also silently drop legitimate CDD postings the profile accepts.
    # A title mention of "Freelance"/"Indépendant" is unambiguous. Only
    # activated when contract_types is non-empty (empty means "accept any
    # contract type" — see JobSearchProfile.contract_types) and doesn't
    # already include freelance.
    contract_types_lower = {ct.lower() for ct in profile.contract_types}
    freelance_exclude_keywords: list[str] = []
    if profile.contract_types and "freelance" not in contract_types_lower:
        freelance_exclude_keywords = list(
            engine_cfg.get("contract_keywords", {}).get("freelance", {}).get("title", [])
        )

    return {
        "locations": {"include": locations_include},
        "remote_scope": engine_cfg.get("remote_scope", {}),
        "remote": {"allowed": []},  # kept for backward compat; logic moved to work_modes
        "work_modes": {"allowed": list(profile.work_modes)},
        "seniority": {"exclude": seniority_exclude},
        "contract_type": {"exclude_title_keywords": freelance_exclude_keywords},
        "title_keywords": {
            "include": _dedup(title_include),
            "description_include": _dedup(description_include),
            "exclude": _flatten_ungrouped(engine_cfg.get("excluded_title_keywords", {})),
            "non_it_functions": _flatten_ungrouped(engine_cfg.get("non_it_title_functions", {})),
        },
        "posted_within_days": profile.freshness_days,
        "salary_requirements": {},
        "experience_years": {"max_years": profile.experience.max_years},
        # Read by the title check: a source's own occupation in one of these domains
        # is role evidence (see matches_title_keywords, step 3b).
        "target_domains": list(profile.target_domains),
        # Distance + remote-policy model (F4.3): None for a v1 profile without a home,
        # which keeps the "distance" and "remote_policy" checks inert.
        "location_distance": distance_config(profile),
    }


def load_filter_config(
    profile_path: str | None = None,
    engine_path: str = "config/filters.yaml",
) -> dict[str, Any]:
    """Convenience wrapper: load profile + vocabulary and return a filter config.

    Equivalent to::

        build_filter_config(load_profile(profile_path), load_filters(engine_path))

    Use this in callers that do not need the intermediate objects.
    ``profile_path=None`` resolves ``config/profile.yaml`` or its personal
    ``config/profile.local.yaml`` override (see src/config_files.py).
    """
    if profile_path is None:
        from src.config_files import resolve_config_path

        profile_path = str(resolve_config_path("config", "profile.yaml"))
    return build_filter_config(load_profile(profile_path), load_filters(engine_path))


def _normalize(text: str) -> str:
    """Lowercase and strip diacritical marks for accent-insensitive matching.

    "Développeur" and "developpeur" both become "developpeur".
    """
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _build_description_pattern(keywords: list[str]) -> "re.Pattern[str] | None":
    """Build a single word-boundary regex for description/skills fallback matching.

    Uses negative lookbehind/lookahead on alphanumeric characters so that
    short skills like "git" do NOT match embedded substrings like "agit" or
    "digit", and "ai" does NOT match "aide" or "j'ai" (the apostrophe in
    French contractions is a non-alphanumeric separator that would otherwise
    create a false word-boundary hit).

    The pattern normalises the apostrophe issue by requiring that the character
    before AND after the match is not [a-z0-9]. Apostrophes/hyphens directly
    adjacent to the keyword are therefore NOT considered word separators.
    """
    if not keywords:
        return None
    parts = [r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])" for kw in keywords]
    try:
        return re.compile("|".join(parts))
    except re.error:
        return None


def _expand_skill_for_description(skill: str) -> list[str]:
    """Return the expanded canonical forms for a skill to use in description matching.

    Short abbreviations like "AI" are expanded to their full canonical phrase
    ("artificial intelligence", "machine learning") so that description
    matching does not accidentally match common French substrings ("j'ai").
    When no alias is defined the original skill (lowercased) is returned.
    """
    key = _normalize(skill.strip())
    aliases = _SKILL_ALIASES.get(key)
    if aliases:
        return [_normalize(a) for a in aliases]
    return [key]


def matches_location(job: Job, config: dict[str, Any]) -> bool:
    """France-first location and remote-eligibility filter.

    Decision tree
    -------------
    1. Job is in a preferred country (France by default)
       → ACCEPT unconditionally (any work mode).

    2. Non-remote job **not** in a preferred country but **known** to be foreign
       (job.countries is non-empty and didn't match)
       → REJECT: physical presence abroad is required; the user cannot commute
         from France.

    3. Non-remote job with completely unknown country (job.countries empty)
       → ACCEPT with benefit of the doubt.

    4. Remote job outside (or with unknown location relative to) a preferred country:
       - Only accepted when the job explicitly signals EU-wide or worldwide remote
         eligibility (via EU/worldwide keywords in job.location).
       - A foreign remote job whose scope is not stated is REJECTED: we cannot
         confirm the user can work from France.
       - A remote job with no country data at all is accepted (genuinely unknown;
         cannot establish it is foreign).

    Backward-compatible path (no ``remote_scope`` in config):
       ``preferred_lower`` is built from the full ``locations.include`` list
       (no regional keywords to exclude) and the standard location-string check
       is applied for both remote and non-remote jobs.

    No geographic restriction (``locations.include`` is empty):
       All jobs pass immediately.
    """
    job_location_lower = (job.location or "").lower().strip()
    job_countries_lower = [c.lower() for c in (job.countries or [])]

    remote_scope_cfg = config.get("remote_scope", {})
    locations_cfg = config.get("locations", {})
    include = locations_cfg.get("include", [])

    # No geographic restriction configured → accept everything.
    if not include:
        return True

    # Regional keywords (e.g. "Europe", "EU") are scope indicators, not country
    # names.  Separate them from concrete preferred countries (e.g. "France").
    eu_region_kws: set[str] = {
        kw.lower() for kw in remote_scope_cfg.get("european_keywords", [])
    } if remote_scope_cfg else set()

    preferred_lower: set[str] = {
        loc.lower() for loc in include
        if loc.lower() not in eu_region_kws
    }

    # ── Step 1: Confirmed preferred country → ACCEPT ──────────────────────
    for pref in preferred_lower:
        if pref in job_location_lower or pref in job_countries_lower:
            return True

    # The job is not in a preferred country (or we have no location data).

    # ── Step 2/3: Non-remote jobs ─────────────────────────────────────────
    # On-site/hybrid work requires physical presence.
    if not job.remote:
        if job_countries_lower:
            # We know the country and it is not preferred → REJECT.
            return False
        # Unknown country, non-remote → cannot determine if foreign → ACCEPT.
        return True

    # ── Step 4: Remote jobs outside (or unconfirmed as) a preferred country ─
    # Accept only when the remote scope explicitly covers France:
    #   - EU/worldwide scope keywords in the location string, OR
    #   - The preferred country appears in the location string (caught in step 1).
    # A foreign remote job with no scope indicator is REJECTED.
    uses_european_scope: bool = bool(
        eu_region_kws and ({loc.lower() for loc in include} & eu_region_kws)
    )

    if uses_european_scope:
        eu_keywords = [k.lower() for k in remote_scope_cfg.get("european_keywords", [])]
        worldwide_keywords = [
            kw.lower() for kw in remote_scope_cfg.get("worldwide_keywords", [
                "worldwide", "global", "anywhere", "international",
            ])
        ]
        scope_indicators = eu_keywords + worldwide_keywords

        if any(kw in job_location_lower for kw in scope_indicators):
            return True  # Explicit EU/worldwide scope → ACCEPT

        if job_countries_lower:
            # If every listed country is a known EU member, the job is
            # EU-restricted remote → the user can work from France → ACCEPT.
            # A single non-EU country in the list (e.g. "United States")
            # makes the whole set ineligible → REJECT.
            eu_countries_set = {
                c.lower() for c in remote_scope_cfg.get("european_countries", [])
            }
            if eu_countries_set and all(c in eu_countries_set for c in job_countries_lower):
                return True
            return False  # Contains non-EU country or no EU config → REJECT

        # Unknown country, remote, no explicit scope → benefit of the doubt.
        return True

    # No European scope in profile (e.g., France-only).
    # Accept only if the country is unknown; reject if confirmed foreign.
    return not bool(job_countries_lower)


def matches_remote(job: Job, config: dict[str, Any]) -> bool:
    """Check if remote status is allowed.

    When ``remote.allowed`` is empty (the recommended setting) every job
    passes regardless of remote status — remote is a scoring preference,
    not a hard exclusion criterion.
    """
    allowed = config.get("remote", {}).get("allowed", [])

    if not allowed:
        return True

    job_remote_str = "remote" if job.remote else ""
    return job_remote_str in allowed


def matches_seniority(job: Job, config: dict[str, Any]) -> bool:
    """Check if seniority level is acceptable.

    Exclusion-based: reject only when the title contains an explicitly very-senior
    keyword (director, VP, C-suite, etc.).  Missing seniority → allow through.

    Matching is diacritic-insensitive so that French accented forms such as
    "sénior", "confirmé", "expérimenté" are caught by the accent-stripped
    keywords in the exclusion list (e.g. "senior", "confirme", "experimente").

    Matching uses word-boundary guards (``(?<![a-z0-9])kw(?![a-z0-9])``) so that
    short standalone keywords like ``lead`` do not false-positive on ``leads`` or
    ``manager`` on ``management``.

    If an ``include`` list is also present (legacy / backward-compatible config),
    it is applied as an additional inclusion gate after exclusions pass.
    """
    seniority_cfg = config.get("seniority", {})
    exclude = seniority_cfg.get("exclude", [])
    include = seniority_cfg.get("include", [])

    # Normalize the title: lowercase + strip diacritics so that "sénior" matches
    # the keyword "senior" and "confirmé" matches "confirme".
    job_title_norm = _normalize(job.title or "")

    # Exclusion check with word-boundary guards.
    for keyword in exclude:
        kw_norm = _normalize(keyword)
        pattern = r"(?<![a-z0-9])" + re.escape(kw_norm) + r"(?![a-z0-9])"
        if re.search(pattern, job_title_norm):
            return False

    # Inclusion check (backward-compatible: only applied when list is non-empty).
    if include:
        return any(_normalize(kw) in job_title_norm for kw in include)

    # No inclusion restriction + passed exclusions → allow.
    return True


# Freshness policy (ADR 0012, issue #105). Bump on any change to freshness_reference.
FRESHNESS_RULE_VERSION = "freshness-v2"
# A source date before this is an artefact (epoch zero, placeholder), not a publication date.
_EARLIEST_PLAUSIBLE_PUBLISHED_AT = datetime(2000, 1, 1, tzinfo=UTC)
# Timezones and clock skew can put a genuine date slightly ahead; beyond this it is unreliable.
_PUBLISHED_AT_FUTURE_TOLERANCE = timedelta(days=1)


def freshness_reference(job: Job) -> tuple[datetime | None, str]:
    """``(reference date, basis)`` used to judge a listing's age.

    basis ``published_at``: the source's date, when plausible. ``first_seen_at``: no usable source
    date, but this pipeline stored the listing before — the listing has been online at least
    since then. ``unknown``: neither. ``discovered_at`` is never used: it is re-stamped on every
    collection, so a long-running undated listing would always look new.
    """
    published = job.published_at
    if published is not None and (
        _EARLIEST_PLAUSIBLE_PUBLISHED_AT <= published <= datetime.now(UTC) + _PUBLISHED_AT_FUTURE_TOLERANCE
    ):
        return published, "published_at"
    first_seen = getattr(job, "first_seen_at", None)
    if first_seen is not None:
        return first_seen, "first_seen_at"
    return None, "unknown"


def matches_published_age(job: Job, config: dict[str, Any]) -> bool:
    """Check if job was posted within the allowed timeframe (see ``freshness_reference``).

    Unknown age → allowed (UNKNOWN is not a rejection reason).
    """
    days = config.get("posted_within_days", 30)
    cutoff = datetime.now(UTC) - timedelta(days=days)

    dt, _basis = freshness_reference(job)
    if dt is None:
        return True  # Unknown date → allow through
    return dt >= cutoff


def matches_salary(job: Job, config: dict[str, Any]) -> bool:
    """Return True if the job salary does not violate configured bounds."""
    req = config.get("salary_requirements", {})
    min_sal = req.get("min_salary")
    max_sal = req.get("max_salary")

    if min_sal is not None and job.salary_min is not None and job.salary_min < min_sal:
        return False

    return not (max_sal is not None and job.salary_max is not None and job.salary_max > max_sal)


def matches_work_mode(job: Job, config: dict[str, Any]) -> bool:
    """Check work-mode compatibility against the user's profile selection.

    Principle: UNKNOWN is not rejected.  Only jobs with a clear on-site signal
    are rejected when the user wants remote-only.

    Decision tree
    -------------
    0. ``job.work_mode`` is a confident, structured signal (populated by a
       subset of collectors — see src/models/job.py) → decide directly from
       it instead of the heuristics below.
    1. No work-mode selection in profile → ACCEPT (no restriction).
    2. User accepts on-site or hybrid → ACCEPT everything (when work_mode is
       unknown, we cannot distinguish on-site from hybrid; broad acceptance
       avoids false rejections).
    3. User wants ONLY remote:
       - ``job.remote = True``  → ACCEPT (confirmed remote).
       - ``job.remote = False`` AND ``job.countries`` is non-empty
         → REJECT (known physical location with no remote flag → on-site/hybrid).
       - ``job.remote = False`` AND no country data
         → ACCEPT (unknown work mode; benefit of the doubt).
    """
    allowed_raw = config.get("work_modes", {}).get("allowed", [])
    if not allowed_raw:
        return True

    # Normalise: "on-site" == "on_site", case-insensitive.
    allowed = {m.lower().replace("-", "_").replace(" ", "_") for m in allowed_raw}

    # If user accepts on-site or hybrid, we cannot apply stricter filtering —
    # a hybrid signal is acceptable to someone who accepts on-site presence.
    # This check must run BEFORE the structured-signal branch so that "hybrid"
    # from Lever/Ashby is not silently rejected when on_site is in allowed.
    if "on_site" in allowed or "hybrid" in allowed:
        return True

    # Trust a confident, structured work_mode signal directly when present.
    # (Only reached when the user wants ONLY remote or has an unusual selection.)
    if job.work_mode in ("remote", "hybrid", "on_site"):
        return job.work_mode in allowed

    # Only "remote" (or equivalent) is in the allowed set.
    if "remote" not in allowed:
        return True  # Unrecognised selection — do not filter.

    if job.remote:
        return True  # Confirmed remote → always accept.

    # Non-remote job: reject only when we have positive location evidence.
    return not bool(job.countries)


def _extract_experience_years_min(text: str) -> int | None:
    """Return the minimum years of experience explicitly stated in *text*.

    Returns ``None`` when no numeric experience requirement is found —
    the caller must treat an absent value as UNKNOWN (not a rejection reason).
    Returns the lower bound of a range when one is given.
    """
    for pattern in _EXP_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                val = int(m.group(1))
                if 0 < val <= 40:  # sanity: ignore "40 years ago"-style matches
                    return val
            except (IndexError, ValueError):
                continue
    return None


def matches_experience_years(job: Job, config: dict[str, Any]) -> bool:
    """Check that the job's stated experience requirement does not exceed the
    candidate's configured maximum years.

    Principle: UNKNOWN is not rejected.  Only jobs that *explicitly state*
    a minimum experience requirement above ``max_years`` are rejected.

    If ``max_years`` is not set in the profile → ACCEPT all.
    If the job description contains no recognisable experience phrase → ACCEPT.
    If the extracted minimum > ``max_years`` → REJECT.
    """
    max_years = config.get("experience_years", {}).get("max_years")
    if max_years is None:
        return True

    # Search title + first 2 000 chars of description (fast, avoids huge blobs).
    text = f"{job.title or ''}\n{(job.description or '')[:2000]}"
    min_required = _extract_experience_years_min(text)

    if min_required is None:
        return True  # Not stated → pass.

    within_limit: bool = min_required <= max_years
    return within_limit


def matches_title_keywords(job: Job, config: dict[str, Any]) -> bool:
    """Check if job is IT-relevant based on title, description, and skills.

    The description/skills fallback uses ``description_include`` (a conservative
    subset of clearly unambiguous IT terms) rather than the full ``include``
    list to avoid false positives from substring matching.  For example,
    "intern" in ``include`` matches "Software Intern" in a title, but would
    also match "interne" (French for "internal") in a description.

    All matching is accent-insensitive.

    Steps:
    1. Reject if the title contains an excluded keyword (always non-IT).
    2. Accept immediately if no inclusion list is configured.
    3. Reject if the title contains a non-IT function word (vendeur, commercial,
       recruteur …) — checked BEFORE the title keyword match.  When the title
       names a non-IT function, IT keywords present alongside it describe the
       product/industry, not the role ("Vendeur informatique" = retail sales of
       IT products).  Uses word-boundary matching so "commercial" does not
       false-positive on "commerciales" or "commercialisation".
    4. Accept immediately if the title contains an included IT keyword.
    5. Description fallback (first 2000 chars) for generic titles whose
       descriptions clearly identify an IT role.
    6. Skills fallback (same stricter list).
    """
    title_cfg = config.get("title_keywords", {})
    excluded = title_cfg.get("exclude", [])
    include = title_cfg.get("include", [])
    # Stricter list for description/skills fallback; falls back to include
    # when not configured (backward-compatible).
    description_include = title_cfg.get("description_include", include)
    # Function words that identify clearly non-IT roles (vendeur, recruteur, commercial …).
    # Checked before title keyword matching — see step 3 above.
    non_it_functions = title_cfg.get("non_it_functions", [])

    title_norm = _normalize(job.title or "")

    # 1. Exclusion check (title only) — always rejected.
    for kw in excluded:
        if _normalize(kw) in title_norm:
            return False

    # 2. No inclusion restriction → accept.
    if not include:
        return True

    include_norm = [_normalize(kw) for kw in include]

    # 3. Non-IT function word in title → reject immediately (before title keyword
    #    match).  Uses word-boundary regex so "commercial" does not false-positive
    #    on "commerciales", and "vendeur" does not false-positive on "revendeur".
    if non_it_functions:
        non_it_norm = [_normalize(kw) for kw in non_it_functions]
        non_it_pattern = _build_description_pattern(non_it_norm)
        if non_it_pattern and non_it_pattern.search(title_norm):
            return False

    # 3b. The source's own occupation for this offer is one of the profile's
    #     target domains (France Travail web: the occupation page it was listed
    #     on). Role evidence when the description is only a short extract that
    #     cannot name the role; exclusions and non-IT functions above still apply.
    #     Only trusted from the source that sets it, and only in the shape it sets
    #     (a dict whose "target_domains" is a list of strings); anything else is ignored.
    if job.source in _OCCUPATION_EVIDENCE_SOURCES:
        occupation = (job.raw_payload or {}).get("occupation")
        domains = occupation.get("target_domains") if isinstance(occupation, dict) else None
        profile_domains = config.get("target_domains")
        if (isinstance(domains, list) and isinstance(profile_domains, list)
                and {d for d in domains if isinstance(d, str)}
                & {d for d in profile_domains if isinstance(d, str)}):
            return True

    # 4. Title match → accept immediately (IT keyword found in title).
    # Word-boundary guards prevent short abbreviations like "ia" (the "IA" skill)
    # from matching substrings in unrelated words.
    # Leading/trailing spaces in keywords (e.g. "intern ") are stripped so that
    # "intern" matches at word boundaries without matching "internship".
    title_pattern = _build_description_pattern([kw.strip() for kw in include_norm])
    if title_pattern and title_pattern.search(title_norm):
        return True

    # 5. Description fallback (first 2000 chars, uses stricter list).
    # Uses word-boundary matching to prevent short skills like "git" from
    # matching embedded substrings in French text ("agit", "digit", "logit").
    desc_kws = [_normalize(kw) for kw in description_include]
    desc_pattern = _build_description_pattern(desc_kws)
    if desc_pattern:
        desc_norm = _normalize((job.description or "")[:2000])
        if desc_pattern.search(desc_norm):
            return True

        # 6. Skills fallback (word-boundary, same stricter list).
        skills_norm = _normalize(" ".join(job.skills or []))
        if desc_pattern.search(skills_norm):
            return True

    return False


def matches_contract_type(job: Job, config: dict[str, Any]) -> bool:
    """Reject a job whose TITLE explicitly signals an unwanted contract
    arrangement (currently: freelance) that the profile's contract_types has
    excluded.

    Title-only by design — see build_filter_config()'s contract_type section
    for why the structured employment_type field is not used here.
    """
    excluded = config.get("contract_type", {}).get("exclude_title_keywords", [])
    if not excluded:
        return True
    title_norm = _normalize(job.title or "")
    for kw in excluded:
        if _normalize(kw) in title_norm:
            return False
    return True


def matches_distance(job: Job, config: dict[str, Any]) -> bool:
    """Distance model (F4.3): reject only on-site work beyond the user's maximum distance
    when the user ticked "strict". Unknown places never fail. Inert without a home."""
    cfg = config.get("location_distance")
    if not cfg:
        return True
    return not strict_distance_exclusion(assess(job, cfg), cfg)


def matches_remote_policy(job: Job, config: dict[str, Any]) -> bool:
    """Distance model (F4.3): reject a remote offer only when it states explicitly that it is
    restricted to residents of another area, and is not itself placed in France."""
    cfg = config.get("location_distance")
    if not cfg:
        return True
    return not remote_policy_exclusion(assess(job, cfg))


_FILTER_CHECKS: list[tuple[str, Callable[[Job, dict[str, Any]], bool]]] = [
    ("location", matches_location),
    ("distance", matches_distance),
    ("remote_policy", matches_remote_policy),
    ("remote", matches_remote),
    ("work_mode", matches_work_mode),
    ("seniority", matches_seniority),
    ("published_age", matches_published_age),
    ("salary", matches_salary),
    ("title_keywords", matches_title_keywords),
    ("contract_type", matches_contract_type),
]


@dataclass(frozen=True)
class FilterCheckOutcome:
    """One hard-filter check evaluated on one job (observability: what was looked at)."""

    code: str
    passed: bool
    observed: str | None
    expected: str | None


def _summarize(value: Any, max_items: int = 12) -> Any:
    """Shorten long lists structurally (``[..12 items.., '+53 more']``) so the text stays valid and readable."""
    if isinstance(value, list):
        head = [_summarize(v, max_items) for v in value[:max_items]]
        return head + [f"+{len(value) - max_items} more"] if len(value) > max_items else head
    if isinstance(value, dict):
        return {k: _summarize(v, max_items) for k, v in value.items()}
    return value


def filter_expected(code: str, config: dict[str, Any]) -> str | None:
    """The config slice a check compares the job against (constant per run: cache it)."""
    import json as _json

    def cfg(*keys: str) -> str | None:
        part: Any = config
        for k in keys:
            part = part.get(k) if isinstance(part, dict) else None
        return None if part in (None, {}, []) else _json.dumps(_summarize(part), ensure_ascii=False, default=str)

    return {
        "location": lambda: cfg("locations"),
        "distance": lambda: _distance_expected(config, "distance"),
        "remote_policy": lambda: _distance_expected(config, "remote_policy"),
        "remote": lambda: cfg("remote", "allowed"),
        "work_mode": lambda: cfg("work_modes", "allowed"),
        "seniority": lambda: cfg("seniority"),
        "published_age": lambda: cfg("posted_within_days"),
        "salary": lambda: cfg("salary_requirements"),
        "title_keywords": lambda: cfg("title_keywords"),
        "contract_type": lambda: cfg("contract_type", "exclude_title_keywords"),
    }.get(code, lambda: None)()


def _distance_expected(config: dict[str, Any], code: str) -> str | None:
    cfg = config.get("location_distance")
    if not cfg:
        return None
    if code == "distance":
        strict = "strict: on-site beyond it is excluded" if cfg.get("strict_distance") else "not strict"
        return f"home {cfg['home']['name']}, max {cfg['max_distance_km']} km ({strict})"
    accepted = [k for k, v in (cfg.get("remote_policy") or {}).items() if v]
    return f"remote accepted: {', '.join(accepted) or 'none'}; explicit restriction to another area is excluded"


def filter_observed(code: str, job: Job, config: dict[str, Any] | None = None) -> str | None:
    """The job fields a check reads (descriptive only). ``config`` is needed only by the
    distance-model checks (their observation is the resolved place and distance)."""
    if code in ("distance", "remote_policy"):
        cfg = (config or {}).get("location_distance")
        if not cfg:
            return None
        d = describe(assess(job, cfg), cfg)
        place = d["place"]
        where = "unknown place" if place is None else f"{place['name']} ({place['precision']})"
        km = "distance unknown" if d["road_km"] is None else f"~{d['road_km']} km by road"
        if code == "distance":
            return f"{where}, {km}, work mode {d['remote_kind']}"
        area = f" {d['remote_area']}" if d["remote_area"] else ""
        return f"remote {d['remote_kind']}{area} ({d['remote_strength']}: {d['remote_evidence']}); {where}"
    if code == "location":
        return f"location={job.location!r} department={job.department!r} countries={job.countries} remote={job.remote}"
    if code == "remote":
        return f"remote={job.remote}"
    if code == "work_mode":
        return f"work_mode={job.work_mode!r} remote={job.remote}"
    if code in ("seniority", "title_keywords"):
        return f"title={job.title!r}"
    if code == "published_age":
        ref, basis = freshness_reference(job)
        return (f"published_at={job.published_at} first_seen_at={getattr(job, 'first_seen_at', None)} "
                f"discovered_at={job.discovered_at} reference={ref} basis={basis}")
    if code == "salary":
        return f"salary_min={job.salary_min} salary_max={job.salary_max} {job.salary_currency}"
    if code == "contract_type":
        return f"title={job.title!r} employment_type={job.employment_type!r}"
    return None


def filter_facts(code: str, job: Job, config: dict[str, Any]) -> tuple[str | None, str | None]:
    """(observed, expected) for a check. Descriptive only — the verdict always comes from the real
    ``matches_*`` functions."""
    return filter_observed(code, job, config), filter_expected(code, config)


def explain_filter_checks(
    job: Job, config: dict[str, Any], *, stop_at_first_failure: bool = False
) -> list[FilterCheckOutcome]:
    """Evaluate the hard-filter checks of ``filter_jobs`` (same functions, same order) on one job.

    With ``stop_at_first_failure`` the result mirrors ``filter_jobs`` exactly (which also stops at
    the first failure); without it every check runs so ALL failing checks are visible. Pure and
    observational: ``filter_jobs`` itself is unchanged.
    """
    out: list[FilterCheckOutcome] = []
    for code, check in _FILTER_CHECKS:
        passed = bool(check(job, config))
        observed, expected = filter_facts(code, job, config)
        out.append(FilterCheckOutcome(code, passed, observed, expected))
        if stop_at_first_failure and not passed:
            break
    return out


def filter_jobs(
    jobs: list[Job],
    config: dict[str, Any],
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[Job], dict[str, str]]:
    """Returns (valid_jobs, rejection_reasons) — rejection_reasons maps
    each rejected job's id to the first check it failed.

    ``progress_callback``, if given, is called ``(processed, total)`` after
    each job — purely observational, never affects which jobs pass."""
    rejection_reasons: dict[str, str] = {}
    valid: list[Job] = []
    total = len(jobs)

    for i, job in enumerate(jobs, start=1):
        reason = None
        for reason_code, check in _FILTER_CHECKS:
            if not check(job, config):
                reason = reason_code
                break
        if reason is not None:
            rejection_reasons[job.id] = reason
        else:
            valid.append(job)
        if progress_callback is not None:
            progress_callback(i, total)

    return valid, rejection_reasons
