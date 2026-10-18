"""Discovery query expansion: derive broad search terms from a user profile.

Converts profile domains/skills into source-specific discovery queries using:
  1. Static baseline — a small, carefully chosen set that always runs
  2. Profile-aware generation — domain/contract terms derived from the profile
  3. Local LLM (additive) — language-appropriate extra queries; never replaces baseline

The final query set is: union(baseline + profile_queries, llm_queries), deduplicated
and capped at max_queries.  The LLM cannot remove baseline coverage.

The output is intentionally broad (high recall).
Filtering and ranking are handled downstream.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import requests

from src.discovery.adaptive import QueryYieldStats, prioritize_queries

if TYPE_CHECKING:
    from src.models.profile import JobSearchProfile

logger = logging.getLogger(__name__)

# Maximum discovery queries generated per source.
_DEFAULT_MAX_QUERIES = 12

# Minimum query count to accept from LLM (reject suspiciously small outputs).
_MIN_LLM_QUERIES = 1

# Static baseline per source — always used regardless of profile or LLM availability.
# Chosen for broad IT recall; keep short and non-noisy.
_STATIC_BASELINE: dict[str, list[str]] = {
    "france_travail": [
        "informatique",
        "développement logiciel",
        "data",
        "cybersécurité",
        "intelligence artificielle",
        "test logiciel",
        "alternance informatique",
    ],
    # Jobicy industry slugs (verified 2026-08-28 via /api/v2/remote-jobs?get=industries).
    # Used as ?industry= filter in multi-query collection.
    "jobicy": [
        "engineering",       # Software Engineering
        "cybersecurity",     # Cybersecurity
        "admin",             # DevOps & Infrastructure
    ],
    # APEC motsCles French keywords (verified 2026-08-28 via rechercheOffre POST).
    # Each keyword drives one paginated search cycle.
    "apec": [
        "informatique",
        "développeur",
        "data",
        "devops",
    ],
    # RemoteOK tag filter (verified 2026-08-28 via /api?tags=).
    # Each tag drives one API call returning tag-filtered jobs.
    "remoteok": [
        "python",
        "javascript",
        "devops",
        "react",
    ],
}

# Domain → extra French IT search queries (additive to baseline).
# Only add where the baseline does not already provide recall.
_DOMAIN_TO_FR_QUERIES: dict[str, list[str]] = {
    "software_development": [],         # covered by "développement logiciel"
    "web_development": ["développement web"],
    "qa_testing": [],                   # covered by "test logiciel"
    "automation": [],                   # covered by "informatique"
    "data": [],                         # covered by "data"
    "it_support": ["support informatique"],
    "systems": ["administrateur systèmes"],
    "networking": ["réseau informatique"],
    "cybersecurity": [],                # covered by "cybersécurité"
    "devops": ["devops"],
    "cloud": ["cloud computing"],
}

# Contract type → extra French IT discovery queries.
_CONTRACT_TO_FR_QUERIES: dict[str, list[str]] = {
    "internship": ["stage informatique"],
    "alternance": [],                   # covered by "alternance informatique"
}

# Domain → extra Jobicy industry slugs (verified 2026-08-28 via ?get=industries).
# Slugs not already in the static baseline.
_DOMAIN_TO_JOBICY_INDUSTRIES: dict[str, list[str]] = {
    "software_development": [],         # covered by "engineering"
    "web_development": ["web-app-design"],
    "qa_testing": ["qa-testing"],
    "automation": [],                   # covered by "admin" (DevOps/Infra)
    "data": [],                         # covered by "data-science"
    "it_support": [],                   # no direct slug
    "systems": [],                      # covered by "admin"
    "networking": [],                   # covered by "admin"
    "cybersecurity": [],                # covered by "cybersecurity"
    "devops": [],                       # covered by "admin"
    "cloud": [],                        # covered by "admin"
}

# ---------------------------------------------------------------------------
# Saturated-query partition vocabulary (France Travail, French)
# ---------------------------------------------------------------------------

# Role/domain terms used to build compound queries when a broad query
# (e.g. "informatique") hits the France Travail API's 3000-result offset
# limit.  Compound: "informatique" + "développeur" → "informatique développeur".
# Keep this list concise — broad role/domain concepts only.
_FT_PARTITION_VOCABULARY: list[str] = [
    "développeur",
    "logiciel",
    "ingénieur",
    "technicien",
    "data",
    "cybersécurité",
    "devops",
    "web",
    "test",
    "cloud",
    "réseaux",
    "systèmes",
    "support",
]

# Domain → partition terms ordered by profile relevance.
# Used to put profile-aligned terms first in the vocabulary.
_FT_DOMAIN_PARTITION_AFFINITY: dict[str, list[str]] = {
    "software_development": ["développeur", "logiciel", "ingénieur"],
    "web_development": ["web", "développeur"],
    "qa_testing": ["test", "logiciel"],
    "automation": ["devops", "logiciel"],
    "data": ["data", "logiciel"],
    "it_support": ["support", "technicien"],
    "systems": ["systèmes", "ingénieur"],
    "networking": ["réseaux"],
    "cybersecurity": ["cybersécurité"],
    "devops": ["devops", "cloud"],
    "cloud": ["cloud", "devops"],
}

# Noisy queries known to pollute France Travail results with non-IT jobs.
# None → reject entirely; str → replace with the safer variant.
_NOISY_QUERY_REPLACEMENTS: dict[str, str | None] = {
    "réseau": "réseau informatique",    # bare "réseau" returns roads, civil engineering
    "reseau": "réseau informatique",
    "numérique": None,                  # "numérique" returns CNC machine operators
    "numerique": None,
    "système": None,                    # too ambiguous alone
    "systeme": None,
}

_MAX_QUERY_LENGTH = 50

# Prompt template for LLM-based expansion.
_EXPANSION_PROMPT = """\
You are a job board search assistant. Generate broad search terms in {language} \
to maximise recall of relevant jobs for the user.

User's target domains: {domains}
User's key skills: {skills}

Rules:
- Use broad category words (not specific job titles)
- Output terms appropriate for a {language}-language job board
- Include 5-8 terms
- Return ONLY a JSON array of strings, no explanation

Example: ["informatique", "développement", "logiciel", "data", "test"]"""


@dataclass
class DiscoveryExpansionResult:
    """Provenance metadata for a single source's query expansion."""

    source: str
    language: str
    queries: list[str] = field(default_factory=list)
    baseline_queries: list[str] = field(default_factory=list)
    profile_queries: list[str] = field(default_factory=list)
    llm_queries: list[str] = field(default_factory=list)
    rejected_queries: list[str] = field(default_factory=list)
    llm_available: bool = False
    # Phase LATER / I15: queries that historical yield data promoted ahead of
    # their original static/profile position (a query only appears here if
    # it actually moved earlier — an empty list means either no history was
    # supplied or history simply confirmed the existing order).
    adaptive_promoted: list[str] = field(default_factory=list)


@dataclass
class DiscoveryQuerySet:
    """Broad discovery queries for a specific source (kept for import compatibility)."""

    source: str
    language: str
    queries: list[str] = field(default_factory=list)


class DiscoveryQueryExpander:
    """Generates broad discovery queries from a JobSearchProfile.

    Strategy per expansion call:
      1. Static baseline  — always included, source-specific
      2. Profile queries  — domain/contract/skill terms derived from the profile
      3. LLM queries      — additive only; union with (1)+(2), never a replacement

    Any failure in the LLM path leaves the baseline intact.
    """

    def __init__(
        self,
        llm_endpoint: str | None = None,
        llm_model: str = "local-model",
        llm_timeout: int = 10,
        max_queries: int = _DEFAULT_MAX_QUERIES,
    ) -> None:
        self._llm_endpoint = llm_endpoint
        self._llm_model = llm_model
        self._llm_timeout = llm_timeout
        self.max_queries = max_queries
        self._last_expansion: dict[str, DiscoveryExpansionResult] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def expand_all(
        self,
        profile: "JobSearchProfile",
        yield_history_by_source: dict[str, dict[str, QueryYieldStats]] | None = None,
    ) -> dict[str, list[str]]:
        """Return discovery queries for all supported source types.

        Returns a dict mapping source_type → list[query_string].
        Expands for france_travail (FR), apec (FR), jobicy (EN slugs), remoteok (EN tags).

        ``yield_history_by_source``, if given, maps source_type -> the
        per-query yield history for that source (see
        ``src.discovery.adaptive.compute_query_yield_history``) — used to
        reorder (never add/remove) queries by measured historical
        performance. Omitting it (the default) reproduces the exact
        pre-I15 query order.
        """
        yield_history_by_source = yield_history_by_source or {}
        return {
            "france_travail": self.expand(
                profile, source="france_travail", language="fr",
                yield_history=yield_history_by_source.get("france_travail"),
            ),
            "apec": self.expand(
                profile, source="apec", language="fr",
                yield_history=yield_history_by_source.get("apec"),
            ),
            "jobicy": self.expand(
                profile, source="jobicy", language="en",
                yield_history=yield_history_by_source.get("jobicy"),
            ),
            "remoteok": self.expand(
                profile, source="remoteok", language="en",
                yield_history=yield_history_by_source.get("remoteok"),
            ),
        }

    def expand(
        self,
        profile: "JobSearchProfile",
        source: str,
        language: str = "fr",
        yield_history: dict[str, QueryYieldStats] | None = None,
    ) -> list[str]:
        """Generate broad discovery queries for a single source.

        Returns a non-empty deduplicated list of query strings that always
        includes the static baseline for the source.

        LLM queries are ADDITIVE: they extend the baseline, never replace it.

        ``yield_history``, if given (query -> QueryYieldStats for THIS
        source), reorders the static+profile queries by measured historical
        yield before the LLM/cap step — see
        ``src.discovery.adaptive.prioritize_queries`` for the bounded,
        deterministic reordering rules. Never changes which queries are
        included, only their order.
        """
        # 1. Static baseline (guaranteed floor).
        baseline = list(_STATIC_BASELINE.get(source, ["informatique"]))

        # 2. Profile-derived additions.
        profile_extra = self._profile_queries(profile, source)
        pre_llm_raw = _deduplicate_preserve_order(baseline + profile_extra)
        pre_llm, rejected_pre = _apply_quality_filter(pre_llm_raw)

        # 2b. Adaptive reordering (Phase LATER / I15) — bounded to a pure
        # permutation of the exact same query list computed above.
        adaptive_promoted: list[str] = []
        if yield_history:
            order_before = pre_llm
            pre_llm = prioritize_queries(pre_llm, yield_history)
            index_before = {q: i for i, q in enumerate(order_before)}
            adaptive_promoted = [
                q for i, q in enumerate(pre_llm) if index_before[q] > i
            ]

        # 3. LLM expansion (additive only — never removes baseline coverage).
        llm_extra: list[str] = []
        rejected_llm: list[str] = []
        llm_available = False

        if self._llm_endpoint:
            domains = list(profile.target_domains)
            skills = list(profile.target_skills)
            try:
                llm_raw = self._try_llm_expansion(domains, skills, language)
                llm_available = True
                pre_lower = {q.lower() for q in pre_llm}
                llm_candidates = [q for q in llm_raw if q.lower() not in pre_lower]
                llm_filtered, rejected_llm = _apply_quality_filter(llm_candidates)
                llm_extra = llm_filtered
            except Exception as exc:
                logger.warning(
                    "LLM query expansion failed for source=%s — using baseline: %s",
                    source,
                    exc,
                )

        merged = _deduplicate_preserve_order(pre_llm + llm_extra)[: self.max_queries]

        # Track profile-only additions (not already in baseline).
        baseline_lower = {b.lower() for b in baseline}
        profile_only = [q for q in profile_extra if q.lower() not in baseline_lower]

        meta = DiscoveryExpansionResult(
            source=source,
            language=language,
            queries=merged,
            baseline_queries=baseline,
            profile_queries=profile_only,
            llm_queries=llm_extra,
            rejected_queries=rejected_pre + rejected_llm,
            llm_available=llm_available,
            adaptive_promoted=[q for q in adaptive_promoted if q in merged],
        )
        self._last_expansion[source] = meta

        logger.info(
            "Discovery expansion: source=%s baseline=%s profile_extra=%s llm=%s "
            "rejected=%s adaptive_promoted=%s → queries=%s",
            source,
            baseline,
            profile_only,
            llm_extra,
            meta.rejected_queries,
            meta.adaptive_promoted,
            merged,
        )

        return merged

    # ------------------------------------------------------------------
    # Profile-aware query generation
    # ------------------------------------------------------------------

    def _profile_queries(
        self,
        profile: "JobSearchProfile",
        source: str,
    ) -> list[str]:
        """Derive extra queries from the profile (domains + contracts + skills)."""
        extra: list[str] = []

        if source == "france_travail":
            for domain in profile.target_domains:
                extra.extend(_DOMAIN_TO_FR_QUERIES.get(domain, []))

            for contract in profile.contract_types:
                extra.extend(_CONTRACT_TO_FR_QUERIES.get(contract, []))

            # Tech skills work as-is on France Travail (FT indexes English tech terms).
            for skill in profile.target_skills:
                skill = skill.strip()
                if skill and 2 <= len(skill) <= 25:
                    extra.append(skill)

        elif source == "apec":
            # APEC motsCles: same French IT vocabulary as France Travail.
            for domain in profile.target_domains:
                extra.extend(_DOMAIN_TO_FR_QUERIES.get(domain, []))

            for contract in profile.contract_types:
                extra.extend(_CONTRACT_TO_FR_QUERIES.get(contract, []))

        elif source == "jobicy":
            # Jobicy industry slugs: map domains to verified industry slugs.
            for domain in profile.target_domains:
                extra.extend(_DOMAIN_TO_JOBICY_INDUSTRIES.get(domain, []))

        elif source == "remoteok":
            # RemoteOK tags: short English tech skills from profile.
            for skill in profile.target_skills:
                skill = skill.strip().lower()
                if skill and 2 <= len(skill) <= 20:
                    extra.append(skill)

        return extra

    # ------------------------------------------------------------------
    # LLM expansion
    # ------------------------------------------------------------------

    def _try_llm_expansion(
        self,
        domains: list[str],
        skills: list[str],
        language: str,
    ) -> list[str]:
        """Call the local LLM and parse the returned query list.

        Raises on any failure so the caller can fall back gracefully.
        Returns an empty list if the output is unusable.
        """
        prompt = _EXPANSION_PROMPT.format(
            language=language,
            domains=", ".join(domains) if domains else "software, technology",
            skills=", ".join(skills) if skills else "programming",
        )

        url = f"{self._llm_endpoint}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": self._llm_model,
            "messages": [
                {"role": "system", "content": "/no_think"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 256,
        }

        response = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self._llm_timeout,
        )
        response.raise_for_status()

        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return []
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        content = message.get("content", "") if isinstance(message, dict) else ""

        return _parse_query_list(str(content))

    # ------------------------------------------------------------------
    # Partition vocabulary
    # ------------------------------------------------------------------

    def generate_partition_vocabulary(
        self,
        profile: "JobSearchProfile",
        source: str,
    ) -> list[str]:
        """Return a profile-prioritised list of partition terms for *source*.

        Used by the France Travail collector to build compound queries when a
        broad query (e.g. "informatique") is saturated.  Profile-relevant
        terms appear first; the remaining vocabulary follows in a fixed order.

        Returns [] for unsupported sources.
        """
        if source != "france_travail":
            return []

        result: list[str] = []
        seen: set[str] = set()

        # Profile-relevant terms first (affinity order per domain)
        for domain in profile.target_domains:
            for term in _FT_DOMAIN_PARTITION_AFFINITY.get(domain, []):
                if term not in seen:
                    seen.add(term)
                    result.append(term)

        # Fill with remaining vocabulary
        for term in _FT_PARTITION_VOCABULARY:
            if term not in seen:
                seen.add(term)
                result.append(term)

        return result

    # ------------------------------------------------------------------
    # Legacy helper (kept for callers that may call it directly)
    # ------------------------------------------------------------------

    def _static_fallback(self, source: str) -> list[str]:
        """Return a minimal safe fallback for the given source."""
        return list(_STATIC_BASELINE.get(source, ["informatique"]))


# ---------------------------------------------------------------------------
# Module-level helpers (public — imported by tests and callers)
# ---------------------------------------------------------------------------

def _apply_quality_filter(
    queries: list[str],
) -> tuple[list[str], list[str]]:
    """Apply quality rules to a list of queries.

    Returns (accepted, rejected).  Handles noisy term replacement/rejection
    and length limits.  Does NOT deduplicate — call _deduplicate_preserve_order
    before or after as needed.
    """
    accepted: list[str] = []
    rejected: list[str] = []

    for q in queries:
        q = q.strip()
        if not q:
            continue
        if len(q) > _MAX_QUERY_LENGTH:
            rejected.append(q)
            continue
        q_lower = q.lower()
        if q_lower in _NOISY_QUERY_REPLACEMENTS:
            replacement = _NOISY_QUERY_REPLACEMENTS[q_lower]
            if replacement is None:
                rejected.append(q)
            else:
                accepted.append(replacement)
        else:
            accepted.append(q)

    return accepted, rejected


def _parse_query_list(text: str) -> list[str]:
    """Parse a JSON array of strings from LLM output.

    Handles:
    - Clean JSON array
    - JSON array wrapped in markdown fences
    - Partial matches (array embedded in prose)

    Returns an empty list on any parse failure.
    """
    cleaned = text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
        return _validate_string_list(parsed)
    except (json.JSONDecodeError, ValueError):
        pass

    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(cleaned[start : end + 1])
            return _validate_string_list(parsed)
        except (json.JSONDecodeError, ValueError):
            pass

    logger.warning("Could not parse query list from LLM output: %s", text[:200])
    return []


def _validate_string_list(value: Any) -> list[str]:
    """Ensure the parsed value is a non-empty list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]


def _deduplicate_preserve_order(queries: list[str]) -> list[str]:
    """Remove duplicate queries (case-insensitive) while preserving insertion order."""
    seen: set[str] = set()
    result: list[str] = []
    for q in queries:
        key = q.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(q)
    return result
