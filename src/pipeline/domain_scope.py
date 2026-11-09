"""Which "other line of work" vocabulary applies to a profile.

``excluded_title_keywords`` and ``non_it_title_functions`` in ``config/filters.yaml``
are groups of title words, one group per line of work (accounting, healthcare, trades,
sales …). They serve as off-domain evidence: a title naming another line of work than
the user's is not what they look for. A group must never apply to the user's own line
of work, or the jobs they search for would be rejected for being what they asked.

A group belongs to the profile, and is skipped, when

* its key is one of the profile's ``target_domains`` (a curated domain such as
  ``healthcare``), or
* one of its title words appears, as whole words, in a term the user gave for their
  line of work: a target domain or a target occupation ("infirmier", "aide soignant",
  "électricien" …), however it is spelled (case, accents, hyphens). Skills are not
  read: a skill such as "python" names a tool, not a line of work.

Shared by the hard filter (``build_filter_config``) and the scorer
(``domain_relevance`` off-domain evidence) so both see the same vocabulary.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.profile import JobSearchProfile


def _words(text: str) -> str:
    """Lowercase, accent-free text with every non-alphanumeric run as one space."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", plain.lower()))


def profile_terms(profile: "JobSearchProfile") -> list[str]:
    """The words the user gave to name their line of work, normalized."""
    raw: list[str] = list(profile.target_domains)
    for occupation in profile.target_occupations:
        raw.extend(t for t in (occupation.label_fr, occupation.keyword, occupation.label) if t)
    seen: dict[str, None] = {}
    for text in raw:
        words = _words(text)
        if words:
            seen.setdefault(words)
    return list(seen)


def group_belongs_to_profile(
    group_key: str, keywords: list[str], profile: "JobSearchProfile", terms: list[str] | None = None
) -> bool:
    """Whether the line of work a group describes is the profile's own."""
    if group_key in profile.target_domains:
        return True
    padded = [f" {t} " for t in (profile_terms(profile) if terms is None else terms)]
    for keyword in keywords:
        needle = f" {_words(keyword)} "
        if needle.strip() and any(needle in term for term in padded):
            return True
    return False


def groups_outside_profile(
    groups: dict[str, list[str]], profile: "JobSearchProfile"
) -> list[str]:
    """Keywords of every group that is not the profile's own line of work, in order."""
    terms = profile_terms(profile)
    flat: list[str] = []
    for group_key, keywords in groups.items():
        if not group_belongs_to_profile(group_key, keywords, profile, terms):
            flat.extend(keywords)
    return flat
