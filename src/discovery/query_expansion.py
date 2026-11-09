"""Discovery query expansion: derive broad search terms from a user profile.

Converts profile domains/skills into source-specific discovery queries using:
  1. Preset baseline — vocabulary for the profile's line of work, read from
     ``config/presets/*.yaml`` (nothing profession-specific lives in this module)
  2. Profile-aware generation — the profile's own domains, skills and contract terms
  3. Local LLM (additive) — language-appropriate extra queries; never replaces (1)+(2)

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
from src.discovery.presets import (
    DiscoveryPreset,
    load_neutral,
    load_presets,
    matching_presets,
    normalize_domain,
)

if TYPE_CHECKING:
    from pathlib import Path

    from src.models.profile import JobSearchProfile

logger = logging.getLogger(__name__)

# Maximum discovery queries generated per source.
_DEFAULT_MAX_QUERIES = 12

# Minimum query count to accept from LLM (reject suspiciously small outputs).
_MIN_LLM_QUERIES = 1

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
- Stay within the user's own line of work; do not add unrelated fields
- Include 5-8 terms
- Return ONLY a JSON array of strings, no explanation, for example ["term one", "term two"]"""


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
        config_dir: "str | Path | None" = None,
    ) -> None:
        self._config_dir = config_dir
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
        Expands for france_travail, indeed and eures (FR keywords).

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
            "indeed": self.expand(
                profile, source="indeed", language="fr",
                yield_history=yield_history_by_source.get("indeed"),
            ),
            "eures": self.expand(
                profile, source="eures", language="fr",
                yield_history=yield_history_by_source.get("eures"),
            ),
        }

    def collector_config(self, profile: "JobSearchProfile", source: str) -> dict[str, Any]:
        """Collector config keys the applicable presets supply for *source*.

        Lists are concatenated (duplicates dropped, order kept), mappings merged.
        The caller applies them only for keys the user's own source config leaves
        empty, so explicit configuration always wins.
        """
        merged: dict[str, Any] = {}
        for preset in self._presets_for(profile):
            for key, value in preset.collector_config.get(source, {}).items():
                if isinstance(value, list):
                    current = merged.setdefault(key, [])
                    current.extend(v for v in value if v not in current)
                elif isinstance(value, dict):
                    merged.setdefault(key, {}).update(value)
                else:
                    merged.setdefault(key, value)
        return merged

    def expand(
        self,
        profile: "JobSearchProfile",
        source: str,
        language: str = "fr",
        yield_history: dict[str, QueryYieldStats] | None = None,
    ) -> list[str]:
        """Generate broad discovery queries for a single source.

        Returns a deduplicated list of query strings: the baseline of every
        preset that applies to the profile, plus the profile's own terms. The
        list is empty when the profile gives nothing to search for (collectors
        then use the queries from their own source configuration).

        LLM queries are ADDITIVE: they extend the baseline, never replace it.

        ``yield_history``, if given (query -> QueryYieldStats for THIS
        source), reorders the static+profile queries by measured historical
        yield before the LLM/cap step — see
        ``src.discovery.adaptive.prioritize_queries`` for the bounded,
        deterministic reordering rules. Never changes which queries are
        included, only their order.
        """
        # 1. Preset baseline (guaranteed floor while a preset applies).
        presets = self._presets_for(profile)
        baseline = _deduplicate_preserve_order(
            [q for p in presets for q in p.baseline.get(source, [])]
        )
        noisy = _noisy_replacements(presets)

        # 2. Profile-derived additions.
        profile_extra = self._profile_queries(profile, source)
        pre_llm_raw = _deduplicate_preserve_order(baseline + profile_extra)
        pre_llm, rejected_pre = _apply_quality_filter(pre_llm_raw, noisy)

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

        domains = list(profile.target_domains)
        skills = list(profile.target_skills)
        # Nothing to describe means nothing to expand: never invent a field.
        if self._llm_endpoint and (domains or skills):
            try:
                llm_raw = self._try_llm_expansion(domains, skills, language)
                llm_available = True
                pre_lower = {q.lower() for q in pre_llm}
                llm_candidates = [q for q in llm_raw if q.lower() not in pre_lower]
                llm_filtered, rejected_llm = _apply_quality_filter(llm_candidates, noisy)
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

    def _presets_for(self, profile: "JobSearchProfile") -> list[DiscoveryPreset]:
        return matching_presets(load_presets(self._config_dir), list(profile.target_domains))

    def _profile_queries(
        self,
        profile: "JobSearchProfile",
        source: str,
    ) -> list[str]:
        """Derive extra queries from the profile (domains + contracts + skills).

        Order: the profile's own domains first (so the cap never drops them in
        favour of preset extras), then preset extras per selected domain and
        contract, then skills.
        """
        extra: list[str] = []
        presets = self._presets_for(profile)
        # Domains no preset owns (free text, an ESCO occupation label…): the user's
        # own words are the query.
        custom = [
            d.strip().replace("_", " ")
            for d in profile.target_domains
            if d.strip() and not any(p.owns(d) for p in presets)
        ]
        # Occupations the user picked (French label first: these are French sources).
        custom.extend(_occupation_terms(profile))

        if source in ("france_travail", "apec", "indeed", "eures"):
            fr_terms = load_neutral(self._config_dir).contract_terms.get("fr", {})
            extra.extend(custom)
            for contract in profile.contract_types:
                term = fr_terms.get(contract)
                if term:
                    extra.extend(f"{term} {domain}" for domain in custom)
            for preset in presets:
                by_domain = preset.domain_queries.get(source, {})
                for domain in profile.target_domains:
                    extra.extend(by_domain.get(normalize_domain(domain), []))
                for contract in profile.contract_types:
                    extra.extend(preset.contract_queries.get(source, {}).get(contract, []))

            if source == "france_travail":
                # Skills work as-is on France Travail.
                for skill in profile.target_skills:
                    skill = skill.strip()
                    if skill and 2 <= len(skill) <= 25:
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
            domains=", ".join(domains) if domains else "not specified",
            skills=", ".join(skills) if skills else "not specified",
        )

        from src.ai.endpoint import require_local_endpoint

        require_local_endpoint(self._llm_endpoint)
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
        broad query is saturated. With a matching preset, its profile-relevant
        terms come first, then the rest of its vocabulary; without one, the
        profession-neutral vocabulary from ``config/discovery.yaml``.

        Returns [] for unsupported sources.
        """
        if source != "france_travail":
            return []

        presets = self._presets_for(profile)
        if not presets:
            return _deduplicate_preserve_order(
                load_neutral(self._config_dir).partition_vocabulary.get(source, [])
            )

        result: list[str] = []
        seen: set[str] = set()

        def add(term: str) -> None:
            if term not in seen:
                seen.add(term)
                result.append(term)

        # Profile-relevant terms first (affinity order per domain)
        for preset in presets:
            for domain in profile.target_domains:
                for term in preset.partition_affinity.get(normalize_domain(domain), []):
                    add(term)
        for preset in presets:
            for term in preset.partition_vocabulary:
                add(term)

        return result


# ---------------------------------------------------------------------------
# Module-level helpers (public — imported by tests and callers)
# ---------------------------------------------------------------------------

def apply_preset_collector_config(
    cfg: dict[str, Any],
    profile: "JobSearchProfile",
    source: str,
    config_dir: "str | Path | None" = None,
) -> dict[str, Any]:
    """``cfg`` plus the collector config the profile's line-of-work presets supply for
    *source*, only for keys ``cfg`` leaves empty: explicit configuration always wins."""
    merged = dict(cfg)
    expander = DiscoveryQueryExpander(config_dir=config_dir)
    for key, value in expander.collector_config(profile, source).items():
        if not merged.get(key):
            merged[key] = value
    return merged


def _occupation_terms(profile: "JobSearchProfile") -> list[str]:
    """Search words for the profile's target occupations (French label, else keyword)."""
    terms: list[str] = []
    for entry in profile.target_occupations:
        term = (entry.label_fr or entry.keyword or entry.label).strip()
        if "(" in term:
            term = term[: term.index("(")].strip()
        if term:
            terms.append(term)
    return terms


def _noisy_replacements(presets: list[DiscoveryPreset]) -> dict[str, str | None]:
    """Merged noisy-query rules of the presets that apply."""
    merged: dict[str, str | None] = {}
    for preset in presets:
        merged.update(preset.noisy_replacements)
    return merged


def _apply_quality_filter(
    queries: list[str],
    noisy_replacements: dict[str, str | None] | None = None,
) -> tuple[list[str], list[str]]:
    """Apply quality rules to a list of queries.

    Returns (accepted, rejected).  Handles noisy term replacement/rejection
    (``noisy_replacements``: lowercase query -> safer query, ``None`` rejects)
    and length limits.  Does NOT deduplicate — call _deduplicate_preserve_order
    before or after as needed.
    """
    noisy = noisy_replacements or {}
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
        if q_lower in noisy:
            replacement = noisy[q_lower]
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
