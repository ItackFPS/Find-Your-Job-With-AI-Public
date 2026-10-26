"""Career-stage check (update plan §C3, F5.2): an offer's level from several weighted signals.

Job side, four kinds of signal, each giving a career rank (``src.models.levels.CAREER_RANK``):

* ``title`` — a seniority word in the title (weight 1, weak evidence). The vocabulary is
  ``config/filters.yaml`` ``seniority_exclusions`` (the v1 keyword sets) plus a few
  entry-level words;
* ``years`` — the stated minimum years of experience (weight 3 when mandatory, else 2);
* ``responsibilities`` — the description says the role manages people (weight 2);
* ``contract`` — an internship or an apprenticeship (weight 3).

Against the candidate's target levels (the highest target, one more with ``stretch``):

* SATISFIED — no signal above the targets;
* VIOLATED (reject) — managing people conflicts, the conflicting weight reaches 3 (so at
  least one more signal conflicts), and no signal other than a title word fits the targets;
* UNKNOWN (review) — anything else with a conflict. A title word alone, or a title that
  disagrees with the stated years, is weak evidence: it never rejects (extends ADR 0004).

Years above the candidate's maximum stay with the experience check (``max_years``); here
they only weigh in. A level *below* the targets is not a conflict.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

from src.models.levels import CAREER_LEVELS, CAREER_RANK, V1_EXCLUDED_TO_LEVEL
from src.requirements.models import ConstraintState, ExperienceRequirement, Importance

CAREER_RULE_VERSION = "career-level.v1"
# Conflicting weight needed (with people management among it) to reject.
REJECT_WEIGHT = 3

SignalKind = Literal["title", "years", "responsibilities", "contract"]

# Entry-level title words (accent-stripped); higher levels come from seniority_exclusions.
_LOW_TITLE_WORDS: dict[str, str] = {
    "junior": "junior", "jr": "junior", "debutant": "junior", "debutante": "junior",
    "graduate": "graduate", "jeune diplome": "graduate", "jeune diplomee": "graduate", "entry level": "graduate",
    "stage": "internship", "stagiaire": "internship", "intern": "internship", "internship": "internship",
    "alternance": "alternance", "alternant": "alternance", "alternante": "alternance", "apprenti": "alternance",
    "apprentie": "alternance", "apprentissage": "alternance", "apprentice": "alternance",
    # "confirmé" is the French mid level; the v1 "senior" keyword set lists it too, and the
    # lower rank wins (see title_vocabulary).
    "confirme": "mid", "confirmee": "mid",
}

# The description says the role manages people (accent-stripped).
_PEOPLE_MANAGEMENT = re.compile(
    r"(?<![a-z])(?:encadr(?:er|ement|erez)\s+(?:une|d'une|l'|de\s+l')?\s*equipe"
    r"|manag(?:er|ement|erez)\s+(?:une|d'une|l'|de\s+l'|d')?\s*equipe"
    r"|management\s+d'(?:une\s+)?equipe"
    r"|people\s+management|line\s+management|direct\s+reports"
    r"|manag(?:e|ing)\s+(?:a|the)\s+team|lead(?:ing)?\s+a\s+team\s+of)",
)

_CONTRACT_WORDS: tuple[tuple[str, str], ...] = (
    ("internship", "internship"), ("stage", "internship"), ("intern", "internship"),
    ("alternance", "alternance"), ("apprentissage", "alternance"), ("apprenticeship", "alternance"),
)


def _norm(text: str | None) -> str:
    nfd = unicodedata.normalize("NFD", (text or "").replace("\u2019", "'"))
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


@lru_cache(maxsize=1024)
def _word_re(word: str) -> re.Pattern[str]:
    # A hyphen joins a compound ("multi-stage"), so it is not a word boundary here.
    return re.compile(r"(?<![a-z0-9-])" + re.escape(word) + r"(?![a-z0-9-])")


def _has_word(text: str, word: str) -> bool:
    return _word_re(word).search(text) is not None


def years_to_rank(years: int) -> int:
    """0 → graduate, 1–2 → junior, 3–4 → mid, 5–7 → senior, 8+ → lead."""
    if years <= 0:
        return CAREER_RANK["graduate"]
    if years <= 2:
        return CAREER_RANK["junior"]
    if years <= 4:
        return CAREER_RANK["mid"]
    if years <= 7:
        return CAREER_RANK["senior"]
    return CAREER_RANK["lead"]


def title_vocabulary(seniority_exclusions: dict[str, list[str]]) -> dict[str, int]:
    """``{accent-stripped keyword: rank}`` from the v1 keyword sets plus entry-level words,
    longest keyword first. A keyword listed under two levels keeps the lower rank (the
    weaker claim)."""
    vocab: dict[str, int] = {word: CAREER_RANK[level] for word, level in _LOW_TITLE_WORDS.items()}
    for label, words in (seniority_exclusions or {}).items():
        level = V1_EXCLUDED_TO_LEVEL.get(label)
        if level is None:
            continue
        for w in words or []:
            key = _norm(str(w))
            vocab[key] = min(vocab.get(key, CAREER_RANK[level]), CAREER_RANK[level])
    return dict(sorted(vocab.items(), key=lambda kv: len(kv[0]), reverse=True))


def allowed_max_rank(levels: list[str], stretch: bool) -> int | None:
    """Highest acceptable rank for the targets; None when no level is targeted."""
    ranks = [CAREER_RANK[lvl] for lvl in levels if lvl in CAREER_RANK]
    if not ranks:
        return None
    return max(ranks) + (1 if stretch else 0)


@dataclass(frozen=True)
class LevelSignal:
    kind: SignalKind
    rank: int
    weight: int
    evidence: str


@dataclass
class CareerAssessment:
    state: ConstraintState
    signals: list[LevelSignal] = field(default_factory=list)
    conflicts: list[LevelSignal] = field(default_factory=list)
    message: str = ""


def job_level_signals(
    title: str | None,
    description: str | None,
    employment_type: str | None,
    experience: ExperienceRequirement | None,
    vocabulary: dict[str, int],
) -> list[LevelSignal]:
    """Every level signal the offer carries (job side only, no profile)."""
    signals: list[LevelSignal] = []
    t = _norm(title)
    # The vocabulary is longest first, so "tech lead" is reported rather than "lead"; one per rank.
    seen_ranks: set[int] = set()
    for word in vocabulary:
        rank = vocabulary[word]
        if rank not in seen_ranks and _has_word(t, word):
            seen_ranks.add(rank)
            signals.append(LevelSignal("title", rank, 1, f"title says \"{word}\""))
    if experience is not None:
        weight = 3 if experience.importance == Importance.MANDATORY else 2
        signals.append(LevelSignal("years", years_to_rank(experience.min_years), weight,
                                   f"{experience.min_years}+ years ({experience.importance.value})"))
    m = _PEOPLE_MANAGEMENT.search(_norm(description))
    if m:
        signals.append(LevelSignal("responsibilities", CAREER_RANK["manager"], 2,
                                   f"manages people: \"{m.group(0).strip()}\""))
    et = _norm(employment_type)
    for word, level in _CONTRACT_WORDS:
        if _has_word(et, word):
            signals.append(LevelSignal("contract", CAREER_RANK[level], 3, f"contract: {employment_type}"))
            break
    return signals


def assess_career_level(signals: list[LevelSignal], max_rank: int | None) -> CareerAssessment:
    """Compare the offer's signals with the candidate's highest acceptable rank."""
    if max_rank is None:   # no target level: every level is shown (the gate skips the check)
        return CareerAssessment(ConstraintState.SATISFIED, signals, [], "no target level set")
    conflicts = [s for s in signals if s.rank > max_rank]
    if not conflicts:
        return CareerAssessment(ConstraintState.SATISFIED, signals, [], "level fits the targets")
    fits_beyond_title = [s for s in signals if s.rank <= max_rank and s.kind != "title"]
    manages_people = any(s.kind == "responsibilities" for s in conflicts)
    evidence = "; ".join(s.evidence for s in conflicts)
    if manages_people and sum(s.weight for s in conflicts) >= REJECT_WEIGHT and not fits_beyond_title:
        return CareerAssessment(ConstraintState.VIOLATED, signals, conflicts,
                                f"level above your targets: {evidence}")
    return CareerAssessment(ConstraintState.UNKNOWN, signals, conflicts,
                            f"level may be above your targets (to review): {evidence}")


def uses_career_targets(profile: Any) -> bool:
    """True for a profile v2: its target levels replace the v1 seniority keyword lists
    (no target level then means every level is shown)."""
    try:
        return int(getattr(profile, "profile_version", 1) or 1) >= 2
    except (TypeError, ValueError):
        return False


def preferred_levels(profile: Any) -> list[str]:
    """The levels scoring prefers for a v2 profile: the targets, plus the next level up with
    ``stretch`` (the gate accepts it, so scoring must not treat it as wrong seniority)."""
    targets = getattr(profile, "targets", None)
    levels = [lvl for lvl in (getattr(targets, "levels", None) or []) if lvl in CAREER_RANK]
    max_rank = allowed_max_rank(levels, bool(getattr(targets, "stretch", False)))
    if max_rank is None:
        return []
    top = max(CAREER_RANK[lvl] for lvl in levels)
    extra = [lvl for lvl in CAREER_LEVELS if top < CAREER_RANK[lvl] <= max_rank]
    return levels + [lvl for lvl in extra if lvl not in levels]


def career_config(profile: Any, seniority_exclusions: dict[str, list[str]]) -> dict[str, Any] | None:
    """The gate's career-stage settings for a v2 profile, else None (v1 behaviour: the hard
    filter's seniority keywords decide). ``max_rank`` None: no target, nothing to check."""
    if not uses_career_targets(profile):
        return None
    targets = getattr(profile, "targets", None)
    levels = list(getattr(targets, "levels", []) or [])
    return {"max_rank": allowed_max_rank(levels, bool(getattr(targets, "stretch", False))),
            "levels": levels, "vocabulary": title_vocabulary(seniority_exclusions)}
