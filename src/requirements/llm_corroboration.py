"""Shared corroboration guard chain for LLM-claimed technologies.

``RequirementExtractor`` (src/requirements/extractor.py) runs the exact same
four-step guard chain in two places before an LLM-claimed technology is
allowed to carry any weight: once for LLM-claimed skills
(``_build_llm_skill``) and once for LLM-claimed principal technologies
(inside ``_parse_llm_response``). This module extracts that chain into one
place so both call sites stay in sync.

The four steps, in order:
  1. Generic role/duty word ("front-end", "database") — not a real technology
     name at all, drop unconditionally (see ``_GENERIC_NON_TECH_TOKENS``).
  2. The candidate's own target domain (or a known synonym of it) — expected
     vocabulary, not evidence of a mismatch (see ``_DOMAIN_FAMILY_SYNONYMS``).
  3. The candidate already knows this technology (directly, or via the same
     family-expansion the heuristic title detector uses) — not missing.
  4. Textual corroboration against the actual job text, with a further check
     for an inclusive/alternative-technology list the candidate already
     satisfies via a different named item.

See the "LLM claim safety guards" note in extractor.py's module docstring
for the concrete false-positive mechanisms these guards were built to close.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.requirements.models import EvidenceSpan
from src.requirements.principal_skill import _norm as _norm_skill
from src.requirements.principal_skill import _user_knows_tech

# ---------------------------------------------------------------------------
# Guard 1 — generic role/duty/technology-CATEGORY words
# ---------------------------------------------------------------------------

# Generic role/duty/technology-CATEGORY words the LLM sometimes names as if
# they were a specific technology (in violation of its own prompt
# instructions) — these must never be treated as a named technology
# regardless of corroboration, since the claim itself is not a real
# technology name. Normalized by stripping spaces/hyphens so "front-end",
# "front end", and "frontend" are all recognized as the same generic token.
_GENERIC_NON_TECH_TOKENS: frozenset[str] = frozenset({
    "frontend", "backend", "fullstack", "database", "databases",
    "basesdedonnees", "basededonnees", "api", "apis", "web", "mobile",
    "cloud", "architecture", "devops", "testing", "tests", "code", "coding",
    "software", "logiciel", "application", "applications",
})


def _is_generic_non_tech_term(term: str) -> bool:
    """Return True when *term* is a generic role/duty word, not a named technology."""
    collapsed = _norm_skill(term).replace("-", "").replace(" ", "").replace("'", "")
    return collapsed in _GENERIC_NON_TECH_TOKENS


# ---------------------------------------------------------------------------
# Guard 2 — candidate's own target domain / synonyms
# ---------------------------------------------------------------------------

# Domain "families": normalized synonym/vocabulary tokens that belong to a
# target domain but aren't necessarily substrings of the domain name itself
# (e.g. "pentest" belongs to "cybersécurité" but shares no characters with
# it). Keyed by the NORMALIZED (accent-stripped, lower-case) domain name as
# it can appear in JobSearchProfile.target_domains / config/profile.yaml.
# Deliberately narrow and evidence-based — populated from the exact terms the
# benchmark found being misclassified, not a speculative technology dump.
_DOMAIN_FAMILY_SYNONYMS: dict[str, frozenset[str]] = {
    "cybersecurite": frozenset({
        "pentest", "penetration testing", "test d'intrusion", "antivirus",
        "siem", "xdr", "edr", "firewall", "pare-feu", "vpn", "ids", "ips",
        "soc", "cert", "malware", "vulnerabilite", "vulnerability",
        "securite reseau", "network security",
    }),
    "cybersecurity": frozenset({
        "pentest", "penetration testing", "antivirus", "siem", "xdr", "edr",
        "firewall", "vpn", "ids", "ips", "soc", "cert", "malware",
        "vulnerability", "network security",
    }),
    "reseaux": frozenset({"reseau", "network", "networking", "protocole reseau"}),
    "systemes": frozenset({"systeme", "system", "systems", "administration systeme"}),
    "informatique": frozenset({"informatique", "it", "systeme d'information"}),
}


def _is_target_domain_term(term: str, target_domains: list[str]) -> bool:
    """Return True when *term* is (or belongs to the family of) one of the
    candidate's own ``target_domains`` — i.e. it is NOT an unknown technology.

    Checked before any LLM-claimed technology is allowed to cause a hard
    rejection: a technology claim that just names the user's own target
    domain, or a close synonym of it, is expected vocabulary, not evidence of
    a mismatch.
    """
    if not target_domains:
        return False
    term_norm = _norm_skill(term)
    if not term_norm:
        return False
    for domain in target_domains:
        domain_norm = _norm_skill(domain)
        if not domain_norm:
            continue
        # Direct containment either way — handles singular/plural variants
        # ("reseau" / "reseaux") and compound domain names.
        if domain_norm in term_norm or term_norm in domain_norm:
            return True
        family = _DOMAIN_FAMILY_SYNONYMS.get(domain_norm)
        if family and term_norm in family:
            return True
    return False


# ---------------------------------------------------------------------------
# Guard 4 (corroboration) — inclusive/alternative technology lists
# ---------------------------------------------------------------------------

# Phrases signalling an INCLUSIVE/alternative technology list ("one or more
# of the following"), as opposed to an exclusive mandatory requirement. When
# a claimed technology's corroborating text sits inside one of these list
# contexts AND the candidate already satisfies at least one OTHER item from
# the same list, the claim is a satisfied OR-alternative, not a missing
# requirement — mirrors principal_skill.py's title-only _is_direct_ou_alternative
# for the description/LLM path.
_INCLUSIVE_LIST_MARKERS = re.compile(
    r"\b(?:"
    r"plusieurs\s+(?:technologies|langages|outils)|"
    r"l['’]?une?\s+des|l['’]?un\s+des|"
    r"au\s+moins\s+(?:un[e]?|l['’]?un[e]?)|"
    r"one\s+or\s+more\s+of|any\s+of\s+the\s+following|some\s+of\s+the\s+following|"
    r"several\s+(?:of\s+the\s+following|technologies|languages)|"
    r"multiple\s+.{0,20}?among|parmi\s+(?:les|des)"
    r")\b",
    re.I,
)


def _is_satisfied_inclusive_alternative(
    text_norm: str, match_start: int, match_end: int, user_skills_norm: set[str]
) -> bool:
    """Return True when the match sits in an inclusive-list context the
    candidate already satisfies via another item in the same list.

    Scans a window around the match for an inclusive-list marker phrase; if
    found, checks whether any of the candidate's own known skills also
    appears in that window — meaning the candidate already covers the "one
    of several" requirement via a different named technology, so this
    specific claim must not be treated as a missing mandatory requirement.
    """
    window = text_norm[max(0, match_start - 200): match_end + 200]
    if not _INCLUSIVE_LIST_MARKERS.search(window):
        return False
    for skill in user_skills_norm:
        if len(skill) <= 1:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(skill) + r"(?![a-z0-9])"
        if re.search(pattern, window):
            return True
    return False


def _find_phrase_evidence(
    original_text: str,
    normalized_text: str,
    phrase: str,
    extractor_id: str,
    field: str = "description",
) -> EvidenceSpan | None:
    """Return an EvidenceSpan if *phrase* literally appears in *normalized_text*.

    Used to corroborate an LLM-claimed skill/technology against the actual
    job text. ``normalized_text`` must be ``_norm_skill(original_text)`` —
    diacritic-stripping is index-preserving for the accented characters this
    codebase deals with (one combining mark per base character), so match
    offsets from the normalized text are used directly against the original
    text for the returned span.
    """
    phrase_norm = _norm_skill(phrase.strip())
    if not phrase_norm:
        return None
    pattern = r"(?<![a-z0-9])" + re.escape(phrase_norm) + r"(?![a-z0-9])"
    m = re.search(pattern, normalized_text)
    if not m:
        return None
    start, end = m.start(), m.end()
    snippet = original_text[max(0, start - 30): end + 30].strip()
    return EvidenceSpan(text=snippet, start=start, end=end, field=field, extractor_id=extractor_id)


# ---------------------------------------------------------------------------
# The shared chain
# ---------------------------------------------------------------------------

@dataclass
class LLMClaimVerdict:
    """Outcome of running the shared guard chain on one LLM-claimed technology.

    ``outcome`` is one of:
      "generic"             — not a real technology name (guard 1).
      "target_domain"       — the candidate's own target domain/synonym (guard 2).
      "known"                — the candidate already knows this tech (guard 3).
      "satisfied_alternative" — corroborated, but sits in an inclusive list the
                                candidate already satisfies via another item.
      "corroborated"         — a literal textual match was found (``span`` set).
      "uncorroborated"       — no literal textual match was found.
    """

    outcome: str
    span: EvidenceSpan | None = None


def evaluate_llm_tech_claim(
    claim: str,
    *,
    target_domains: list[str],
    user_skills_norm: set[str],
    corroboration_text: str,
    corroboration_text_norm: str,
    extractor_id: str,
) -> LLMClaimVerdict:
    """Run the shared four-step guard chain for one LLM-claimed technology.

    ``claim`` is stripped once up front, matching the corroboration text
    against that stripped form for every step — identical semantics to
    ``RequirementExtractor._build_llm_skill``'s existing behavior.
    """
    name = claim.strip()

    if _is_generic_non_tech_term(name):
        return LLMClaimVerdict("generic")
    if _is_target_domain_term(name, target_domains):
        return LLMClaimVerdict("target_domain")

    name_norm = _norm_skill(name)
    if _user_knows_tech(name_norm, {name_norm}, user_skills_norm):
        return LLMClaimVerdict("known")

    span = _find_phrase_evidence(
        corroboration_text, corroboration_text_norm, name, extractor_id=extractor_id,
    )
    if span is not None and _is_satisfied_inclusive_alternative(
        corroboration_text_norm, span.start, span.end, user_skills_norm,
    ):
        return LLMClaimVerdict("satisfied_alternative", span=span)
    if span is not None:
        return LLMClaimVerdict("corroborated", span=span)
    return LLMClaimVerdict("uncorroborated")
