"""Turn what the user typed for their line of work into matchable vocabulary.

``profile.target_domains`` holds free text as much as curated keys: the domain picker
stores whatever the user typed ("Administration systèmes", "Soins infirmiers"), and
occupations come from the occupation search. The vocabulary in ``filters.yaml`` is keyed
by a fixed list of ids, so reading it with ``domain_keywords.get(domain)`` found nothing
for a typed label: the hard filter fell back on the literal words and ``domain_relevance``
had no domain evidence at all, capping every offer at its "undetermined" tier.

This module is the single place that resolves a label, for any line of work, using only
word morphology and the vocabulary already in the configuration:

* a label equal to a vocabulary key (ignoring case and accents) takes that whole entry;
* a label of several words that names one of the entry's title keywords ("Administration
  systèmes" and "administrateur systèmes" reduce to the same stems) takes that whole entry;
* otherwise it takes only the keywords whose words are all in the label ("Soins infirmiers"
  takes "infirmier" but not the rest of the healthcare entry);
* the label always stands for itself too: its literal text, and its stems as a set that
  must all occur in a title, in any order and inflection.

Used by the hard filter (``build_filter_config``), the scorer (``build_scoring_config``) and
discovery, so they all read a label the same way.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable

# Function words of the two languages the sources publish in. Grammar, not vocabulary.
_STOPWORDS = frozenset({
    "a", "and", "au", "aux", "d", "de", "des", "du", "en", "et", "for", "in", "l", "la", "le",
    "les", "of", "on", "or", "ou", "par", "pour", "sur", "the", "to", "un", "une",
})

# Inflection and derivation endings, longest first. A stem is only compared with other
# stems, so it only has to be the same for the forms of one word.
_SUFFIXES = (
    "ionnaires", "ionnaire", "abilites", "abilite", "ationnels", "ationnel", "atrices", "ateurs",
    "icienne", "iciens", "ations", "atrice", "ateur", "icien", "iques", "ation", "ables", "ement",
    "ments", "ieres", "iers", "iere", "ique", "able", "ment", "euse", "eurs", "ance", "ence",
    "erie", "ions", "ings", "ion", "ing", "ier", "eur", "ere", "ers", "ure", "ors", "aux", "eau",
    "er", "or", "es", "e", "s", "x",
)
_MIN_STEM = 3


def plain(text: str) -> str:
    """Lowercase text without accents, every non-alphanumeric run as one space."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_text = decomposed.encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9+#]+", ascii_text.lower()))


def stem(word: str) -> str:
    """A light French/English stem: one form for the inflections of a word."""
    if word.endswith("eaux"):
        word = word[:-1]
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)]
    return word


def content_stems(text: str) -> tuple[str, ...]:
    """The stems of the content words of ``text``, in order, without repeats."""
    return tuple(dict.fromkeys(stem(w) for w in plain(text).split() if w not in _STOPWORDS))


@dataclass(frozen=True)
class DomainVocabulary:
    """What a profile's labels resolve to."""

    families: tuple[str, ...] = ()           # the vocabulary keys taken as a whole
    title_terms: tuple[str, ...] = ()        # substring/whole-word terms for titles
    description_terms: tuple[str, ...] = ()  # terms for descriptions
    title_stem_sets: tuple[tuple[str, ...], ...] = ()  # each: every stem must be in the title


def _dedup(items: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(dict.fromkeys(items))


def resolve_domains(labels: Iterable[str], domain_keywords: dict[str, Any]) -> DomainVocabulary:
    """Resolve free-text ``labels`` against the ``domain_keywords`` vocabulary.

    A pure function of its arguments. An unknown label is never an error: it stands for
    itself, which is all that can be said about a line of work the vocabulary has not heard of.
    """
    by_plain_key = {plain(key.replace("_", " ")): key for key in domain_keywords}
    keyword_stems = {
        key: [(kw, set(content_stems(kw))) for kw in (entry.get("title") or [])]
        for key, entry in domain_keywords.items() if isinstance(entry, dict)
    }

    families: list[str] = []
    title_terms: list[str] = []
    description_terms: list[str] = []
    stem_sets: list[tuple[str, ...]] = []

    def take_family(key: str) -> None:
        entry = domain_keywords[key]
        families.append(key)
        title_terms.extend(entry.get("title") or [])
        description_terms.extend(entry.get("description") or [])

    for raw in labels:
        label = (raw or "").strip()
        words = plain(label.replace("_", " "))
        if not words:
            continue
        key = by_plain_key.get(words)
        if key is not None:
            take_family(key)
            continue

        stems = set(content_stems(label))
        title_terms.append(label.replace("_", " "))
        description_terms.append(label.replace("_", " "))
        if stems:
            stem_sets.append(content_stems(label))

        for entry_key, keywords in keyword_stems.items():
            names_entry = len(stems) >= 2 and any(kw_stems == stems for _, kw_stems in keywords)
            if names_entry:
                take_family(entry_key)
                continue
            title_terms.extend(kw for kw, kw_stems in keywords if kw_stems and kw_stems <= stems)

    return DomainVocabulary(
        families=_dedup(families),
        title_terms=_dedup(title_terms),
        description_terms=_dedup(description_terms),
        title_stem_sets=_dedup(stem_sets),
    )


def profile_labels(profile: Any) -> list[str]:
    """Everything the user gave to name their line of work: domains, then occupations."""
    labels = [str(d) for d in getattr(profile, "target_domains", None) or []]
    for occupation in getattr(profile, "target_occupations", None) or []:
        labels.extend(t for t in (occupation.label_fr, occupation.keyword, occupation.label) if t)
    return labels


def effective_domains(profile: Any, domain_keywords: dict[str, Any]) -> list[str]:
    """The profile's ``target_domains`` plus the vocabulary keys they resolve to.

    For code keyed on the vocabulary ids (presets, per-domain queries). The user's own
    labels stay first and unchanged.
    """
    resolved = resolve_domains(profile_labels(profile), domain_keywords)
    return list(dict.fromkeys([*getattr(profile, "target_domains", []), *resolved.families]))


def title_has_stem_set(title: str, stem_sets: Iterable[Iterable[str]]) -> bool:
    """True when every stem of at least one set occurs in ``title``."""
    title_stems = {stem(w) for w in plain(title).split()}
    return any(group and set(group) <= title_stems for group in map(tuple, stem_sets))
