"""Principal skill detection from job titles.

Detects when a job title is centered on a technology the candidate does not know.

A "principal skill" is one that defines the core competency of the role —
e.g. the language or framework whose name appears in the job title as the
primary identifier (not just a passing mention in the description).

Decision: a technology is treated as principal when it appears in the job
title AND is not in the candidate's profile (or covered by a known language-
family relationship).  The title is the strongest available signal.

Examples:
    "Développeur Rust"        → Rust is principal (if user doesn't know Rust → reject)
    "Rust Backend Engineer"   → Rust is principal
    "Java Developer"          → Java is principal (if user doesn't have Java → reject)
    "React Developer"         → React is in the JS family (user has JavaScript → OK)
    "PHP/Laravel Developer"   → PHP in profile + Symfony is PHP family → OK
    "Backend Developer"       → no technology token extracted → no principal skill
    "Python/Rust Developer"   → Python OK, Rust missing → reject
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Compiled-pattern cache
#
# 2026-09-05 profiling (scripts/profile_requirements_gate.py) found that
# re.compile() alone accounted for 82% of the entire requirements_gate
# stage's wall time (98.7s of 121s over 4,000 real jobs) — NOT matching
# work. The cause: _extract_tech_tokens_from_title and
# _is_direct_ou_alternative build a fresh pattern STRING per (variant,
# known-skill) combination and call the module-level re.search(pattern,
# text), which compiles-and-caches internally but with a small, bounded
# cache — the combinatorial variety of dynamically-built pattern strings
# here (variants x the user's expanded known-skill set) evicts that cache
# constantly, so the same pattern gets recompiled over and over across jobs
# even though the full set of possible patterns is fixed for the lifetime
# of a single RequirementExtractor (it depends only on _CORE_LANGUAGES,
# a static table, and the user's profile skills, which don't change
# per-job). An unbounded cache here is safe: entry count is bounded by
# vocabulary size, not by the number of jobs processed.
#
# This is a pure memoization layer — same pattern string + same flags always
# returns the same compiled Pattern, so match results are byte-for-byte
# identical to calling re.search/re.compile directly. It changes nothing
# about *what* matches, only how often the same regex gets compiled.
_COMPILED_PATTERN_CACHE: dict[tuple[str, int], "re.Pattern[str]"] = {}


def _cached_search(pattern: str, text: str, flags: int = 0) -> "re.Match[str] | None":
    """re.search(pattern, text, flags), but reusing a compiled Pattern
    across calls instead of relying on re's own bounded internal cache."""
    key = (pattern, flags)
    compiled = _COMPILED_PATTERN_CACHE.get(key)
    if compiled is None:
        compiled = re.compile(pattern, flags)
        _COMPILED_PATTERN_CACHE[key] = compiled
    return compiled.search(text)

# ---------------------------------------------------------------------------
# Core programming languages — require explicit knowledge
# These can appear in job titles and define the role's principal skill.
# ---------------------------------------------------------------------------

# Canonical lower-case name → set of spelling variants (all lower-case)
_CORE_LANGUAGES: dict[str, set[str]] = {
    "rust":       {"rust"},
    "go":         {"go", "golang"},
    "kotlin":     {"kotlin"},
    "swift":      {"swift"},
    "java":       {"java"},
    "scala":      {"scala"},
    "elixir":     {"elixir"},
    "erlang":     {"erlang"},
    "haskell":    {"haskell"},
    "ocaml":      {"ocaml"},
    "clojure":    {"clojure"},
    "c++":        {"c++", "cpp", "c/c++"},
    "c#":         {"c#", "csharp", "c sharp"},
    ".net":       {".net", "dotnet", "dot net", "asp.net", ".net core", ".net framework"},
    "ruby":       {"ruby"},
    "dart":       {"dart"},
    "cobol":      {"cobol"},
    "perl":       {"perl"},
    "lua":        {"lua"},
    "groovy":     {"groovy"},
    "assembly":   {"assembly", "asm"},
    "vba":        {"vba"},
    "fortran":    {"fortran"},
    "abap":       {"abap"},
    "sap":        {"sap"},    # SAP requires domain expertise
    "r":          {"r"},      # R language — handled carefully via structural patterns
}

# ---------------------------------------------------------------------------
# Language-family relationships
# When user knows a parent language, related frameworks/stacks are acceptable.
# Key = canonical language in user profile (lower-case)
# Value = set of tech tokens (lower-case) covered by that language knowledge
# ---------------------------------------------------------------------------

_LANGUAGE_FAMILIES: dict[str, set[str]] = {
    "python": {
        "django", "flask", "fastapi", "celery", "sqlalchemy",
        "pytorch", "tensorflow", "keras", "sklearn", "scipy",
        "pandas", "numpy", "jupyter", "airflow", "mlflow",
    },
    "javascript": {
        "react", "react.js", "reactjs",
        "angular", "angularjs",
        "vue", "vue.js", "vuejs", "nuxt", "nuxt.js", "nuxtjs",
        "svelte", "jquery",
        "next.js", "nextjs", "next",
        "express", "express.js",
        "electron",
        # TypeScript is a JS superset — acceptable given JS knowledge
        "typescript", "ts",
    },
    "node.js": {
        "express", "fastify", "nestjs", "nest.js",
        "next.js", "nextjs",
    },
    "php": {
        "laravel", "symfony", "wordpress", "drupal",
        "magento", "prestashop", "yii", "codeigniter", "zend",
    },
    "typescript": {
        "react", "angular", "vue", "next.js", "nextjs", "nuxt",
    },
    "java": {
        "spring", "spring boot", "springboot", "hibernate",
        "jakarta", "quarkus", "micronaut", "maven", "gradle",
    },
    "ruby": {
        "rails", "ruby on rails", "sinatra",
    },
    "scala": {
        "spark", "akka", "play",
    },
    "kotlin": {
        "spring", "ktor", "android",
    },
    "swift": {
        "ios", "swiftui", "uikit", "xcode",
    },
    "dart": {
        "flutter",
    },
    "go": {
        "gin", "echo", "fiber",
    },
    "rust": {
        "actix", "tokio", "wasm",
    },
}

# Technologies that are acceptable as principal skills when the user has
# general software engineering skills (not language-specific).
# These are tooling / platform concepts rather than languages.
_PLATFORM_TECH_OK_FOR_IT_GENERALISTS = {
    "docker", "kubernetes", "k8s",
    "terraform", "ansible", "helm",
    "git", "linux", "bash", "shell",
    "aws", "azure", "gcp", "cloud",
    "elasticsearch", "redis", "kafka", "rabbitmq",
    "postgresql", "mysql", "mongodb", "sqlite",
    "nginx", "apache",
    "graphql", "rest", "api",
    "ci/cd", "cicd", "jenkins", "gitlab", "github",
    "n8n", "airflow",
}


def _build_known_technologies() -> frozenset[str]:
    tokens: set[str] = set()
    for canonical, variants in _CORE_LANGUAGES.items():
        tokens.add(canonical)
        tokens.update(variants)
    for canonical, related in _LANGUAGE_FAMILIES.items():
        tokens.add(canonical)
        tokens.update(related)
    tokens.update(_PLATFORM_TECH_OK_FOR_IT_GENERALISTS)
    return frozenset(tokens)


# Lower-case technology tokens this module already recognises for principal-
# skill gating (_CORE_LANGUAGES + _LANGUAGE_FAMILIES + platform tools). Public
# read-only export for non-gating consumers — e.g. the Stats page's skill/tool
# aggregation (src/api/routes/jobs.py) — that need to tell a real technology
# name apart from arbitrary job-posting text without maintaining a second
# vocabulary. Never used to reject jobs; that stays exclusively the job of
# detect_missing_principal_skills[_with_or] below.
KNOWN_TECHNOLOGIES: frozenset[str] = _build_known_technologies()

# Tokens excluded from extract_known_technologies() below: real technologies
# that also double as ordinary English/French words ("go", "swift", "dart",
# the single-letter "r" language), so scanning them against unstructured
# description text produces false positives that title-only gating avoids by
# relying on job-title role-noun context (see _extract_tech_tokens_from_title).
_AMBIGUOUS_FREE_TEXT_TOKENS = frozenset({"go", "r", "swift", "dart"})

_FREE_TEXT_TOKEN_PATTERNS: dict[str, "re.Pattern[str]"] = {
    token: re.compile(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])")
    for token in KNOWN_TECHNOLOGIES
    if token not in _AMBIGUOUS_FREE_TEXT_TOKENS
}


def extract_known_technologies(text: str) -> list[str]:
    """Return known technology tokens mentioned anywhere in free-form text.

    Used as a skills fallback during normalization (src/pipeline/normalize.py)
    for sources whose raw payload has no structured skills field (Indeed,
    APEC, EURES, Hellowork, Greenhouse, Lever, WTTJ, most LinkedIn listings).
    Matches against the same KNOWN_TECHNOLOGIES vocabulary the requirements
    gate and Stats page already use, so a job gets tagged with a technology
    only if the pipeline would recognise it as one elsewhere too.
    """
    norm = _norm(text)
    return sorted(token for token, pattern in _FREE_TEXT_TOKEN_PATTERNS.items() if pattern.search(norm))

# Role-noun tokens that identify the "job function" in the title.
# A technology appearing before one of these is in "principal position".
_ROLE_NOUNS_PATTERN = re.compile(
    r"\b(?:"
    r"d[eé]veloppeur|developpeuse?|d[eé]veloppeuse?|"
    r"d[eé]veloppeu?r|developer|"
    r"ing[eé]nieur|ingenieure?|engineer|"
    r"programmeur|programmeuse?|programmer|"
    r"architecte|architect|"
    r"technicien|technicienne|"
    r"expert|sp[eé]cialiste|specialist|"
    r"consultant|"
    r"dev\b"
    r")\b",
    re.I,
)


def _norm(text: str) -> str:
    """Lowercase + strip diacritics."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def _normalize_user_skills(user_skills: list[str]) -> set[str]:
    """Return a set of normalized, lower-case user skill tokens."""
    normalized: set[str] = set()
    for skill in user_skills:
        key = _norm(skill.strip())
        normalized.add(key)
        # Add common aliases
        if key == "javascript":
            normalized.add("js")
        elif key == "typescript":
            normalized.add("ts")
        elif key == "node.js":
            normalized.add("nodejs")
            normalized.add("node")
        elif key == "vue.js":
            normalized.add("vue")
            normalized.add("vuejs")
        elif key == "c#":
            normalized.add("csharp")
        elif key == ".net":
            normalized.add("dotnet")
    return normalized


def _user_knows_tech(
    tech_canonical: str,
    tech_variants: set[str],
    user_skills_norm: set[str],
) -> bool:
    """Return True when the user's profile covers the given technology.

    Checks direct presence AND language-family relationships.
    """
    # Direct match
    if tech_canonical in user_skills_norm:
        return True
    for variant in tech_variants:
        if variant in user_skills_norm:
            return True

    # Language-family lookup: does the user know a parent language?
    for parent_lang, family_members in _LANGUAGE_FAMILIES.items():
        if parent_lang in user_skills_norm:
            # User knows this parent — check if the tech is in its family
            if tech_canonical in family_members:
                return True
            for variant in tech_variants:
                if variant in family_members:
                    return True

    return False


# Matches "role-word.net" in titles where the standard lookbehind fails because a
# letter precedes the dot (e.g. "Développeur.net", "Ingénieur.net").
# Restricted to role-function words so domain names like "planet.net" or "amazon.net"
# do not produce false positives.  ASP.NET, .NET Core, dotnet, etc. are handled via
# the standard variant loop (they are in _CORE_LANGUAGES[".net"] variants).
_NET_COMPOUND_TITLE_PATTERN = re.compile(
    r"(?:d[eé]veloppeur|developer|ing[eé]nieur|ingenieur|engineer|"
    r"expert|sp[eé]cialiste|specialist|lead|architect(?:e)?|"
    r"programmeur|programmer|consultant|technicien)\.net\b(?![a-z0-9.])",
    re.I,
)


def _extract_tech_tokens_from_title(title_norm: str) -> list[tuple[str, set[str]]]:
    """Extract known technology tokens from a normalized title.

    Returns a list of (canonical_name, variants) tuples for each tech found.
    Excludes platform/tooling tech that is acceptable for any IT generalist.
    """
    found: list[tuple[str, set[str]]] = []

    for canonical, variants in _CORE_LANGUAGES.items():
        # Skip "r" here — too short, handled below with structural patterns
        if canonical == "r":
            continue
        for variant in variants:
            # Word-boundary match (handles "C++" with escaped chars)
            escaped = re.escape(variant)
            pattern = r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"
            if _cached_search(pattern, title_norm):
                found.append((canonical, variants))
                break

    # Special handling for "R" language — only accept as tech when immediately
    # adjacent to a role noun or slash-separated from another tech token.
    # Negative lookahead (?!&) prevents matching "R" in "R&D".
    r_patterns = [
        re.compile(r"\br\s*/\s*python\b", re.I),
        re.compile(r"\bpython\s*/\s*r\b(?!&)", re.I),
        re.compile(r"\b(?:developer?|ing[eé]nieur|engineer)\s+r\b(?!&)", re.I),
        re.compile(r"\br\b(?!&)\s+(?:developer?|ing[eé]nieur|engineer)\b", re.I),
        re.compile(r"\bdeveloppeur\s+r\b(?!&)", re.I),
        re.compile(r"\br\b(?!&)\s+developpeur\b", re.I),
    ]
    for p in r_patterns:
        if p.search(title_norm):
            found.append(("r", {"r"}))
            break

    # Also check platform/framework tokens that imply a missing parent language
    _FRAMEWORK_TO_LANGUAGE: dict[str, tuple[str, set[str]]] = {
        "spring":     ("java",   {"java"}),
        "spring boot": ("java",  {"java"}),
        "springboot": ("java",   {"java"}),
        "rails":      ("ruby",   {"ruby"}),
        "flutter":    ("dart",   {"dart"}),
    }
    for fw_token, (lang_canonical, lang_variants) in _FRAMEWORK_TO_LANGUAGE.items():
        escaped = re.escape(fw_token)
        pat = r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])"
        if _cached_search(pat, title_norm):
            # Only flag if the user doesn't have the parent language at all
            # (this is checked later, but we surface the canonical language)
            if (lang_canonical, lang_variants) not in found:
                found.append((lang_canonical, lang_variants))

    # Extra .NET detection: "word.net" like "Développeur.net" where the standard
    # lookbehind (?<![a-z0-9]) fails because a letter immediately precedes the dot.
    # The main loop above already handles ".net", "dotnet", "asp.net", ".net core".
    _net_canonical = ".net"
    _net_variants = _CORE_LANGUAGES[_net_canonical]
    if not any(c == _net_canonical for c, _ in found):
        if _NET_COMPOUND_TITLE_PATTERN.search(title_norm):
            found.append((_net_canonical, _net_variants))

    return found


def _expand_known_tokens(user_skills_norm: set[str]) -> set[str]:
    """Return all tech tokens the user knows (direct + language-family expansion)."""
    known: set[str] = set(user_skills_norm)
    for parent_lang, family in _LANGUAGE_FAMILIES.items():
        if parent_lang in user_skills_norm:
            known |= family
    return known


def _is_direct_ou_alternative(
    variants: set[str],
    title_norm: str,
    user_skills_norm: set[str],
) -> bool:
    """Return True if the tech is directly 'X ou/or Y' or 'Y ou/or X' with a known skill.

    Only matches when "ou" or "or" directly connects the two tokens — "/" is treated
    as AND and does NOT trigger this (most slash-separated titles are AND relationships).
    """
    known_tokens = _expand_known_tokens(user_skills_norm)
    for variant in variants:
        v_esc = re.escape(variant)
        for known in known_tokens:
            if len(known) <= 1:
                continue
            k_esc = re.escape(known)
            p1 = r"(?<![a-z0-9])" + v_esc + r"(?![a-z0-9])\s+(?:ou|or)\s+(?<![a-z0-9])" + k_esc + r"(?![a-z0-9])"
            p2 = r"(?<![a-z0-9])" + k_esc + r"(?![a-z0-9])\s+(?:ou|or)\s+(?<![a-z0-9])" + v_esc + r"(?![a-z0-9])"
            if _cached_search(p1, title_norm, re.I) or _cached_search(p2, title_norm, re.I):
                return True
    return False


def _detect_principal_skill_mismatches(
    title: str,
    user_skills: list[str],
) -> tuple[list[str], list[str]]:
    """Core logic — returns (hard_mismatches, or_alternatives)."""
    if not title:
        return [], []

    title_norm = _norm(title)
    user_skills_norm = _normalize_user_skills(user_skills)
    tech_tokens = _extract_tech_tokens_from_title(title_norm)
    if not tech_tokens:
        return [], []

    hard: list[str] = []
    or_alts: list[str] = []

    for canonical, variants in tech_tokens:
        if _user_knows_tech(canonical, variants, user_skills_norm):
            continue
        if _is_direct_ou_alternative(variants, title_norm, user_skills_norm):
            or_alts.append(canonical)
        else:
            hard.append(canonical)

    return hard, or_alts


def detect_missing_principal_skills(
    title: str,
    user_skills: list[str],
) -> list[str]:
    """Return technologies that are principal in the job title but absent from profile.

    Technologies that appear directly 'X ou/or Y' with a known user skill are
    excluded — they are genuine OR alternatives and should not trigger hard rejection.
    """
    hard, _ = _detect_principal_skill_mismatches(title, user_skills)
    return hard


def detect_missing_principal_skills_with_or(
    title: str,
    user_skills: list[str],
) -> tuple[list[str], list[str]]:
    """Return (hard_mismatches, or_alternatives) for the job title.

    hard_mismatches:  unknown techs that are clearly principal → hard reject
    or_alternatives:  unknown techs directly 'X ou Y' with a known skill → flag only
    """
    return _detect_principal_skill_mismatches(title, user_skills)
