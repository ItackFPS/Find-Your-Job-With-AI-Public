"""Scoring of offer-analysis outputs and the plain verdict (plan F8.3). Pure functions.

An output is *valid* when it parses as JSON with a ``requirements`` list whose items carry a
``quote`` string. A requirement is *grounded* when its quote, after whitespace normalisation,
is an exact substring of the offer (plan §C4: items failing the check are dropped and
counted). French sanity: the model's own words (labels, summary — not the quotes) must read
as French, by a function-word count. Recall (reported, not part of the verdict): the share of
each fixture's key requirement phrases that appear inside some grounded quote.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

MIN_QUOTE_CHARS = 4

# Verdict thresholds. A model below either quality bar is "invalid" whatever its speed.
MIN_JSON_VALID_RATE = 0.8
MIN_GROUNDING_RATE = 0.7
MIN_FRENCH_RATE = 0.7
# Above this, analysing one offer while the user waits is too slow.
MAX_ON_DEMAND_SECONDS = 60.0
DEFAULT_OFFERS_PER_DAY = 30

VERDICT_OK = "ok_on_demand"
VERDICT_TOO_SLOW_SMALLER = "too_slow_try_smaller"
VERDICT_TOO_SLOW_NO_AI = "too_slow_continue_without_ai"
VERDICT_INVALID = "invalid_output_try_other"

_WS = re.compile(r"\s+")
_WORD = re.compile(r"[a-zà-öø-ÿœ']+")
_FR_WORDS = frozenset(
    "le la les de des du un une et en pour avec est sur dans au aux à vous votre ce cette qui "
    "que sont ou par pas son sa ses plus être expérience connaissance maîtrise compétence "
    "compétences niveau poste profil formation ans exigé requis souhaité souhaitée minimum "
    "obligatoire appréciée apprécié".split())
_EN_WORDS = frozenset(
    "the an and of to in for with is on are be your you this that experience knowledge "
    "skills skill strong ability years required preferred degree must nice have".split())


def normalise(text: str) -> str:
    """NFC + collapsed whitespace, for the exact-substring check."""
    return _WS.sub(" ", unicodedata.normalize("NFC", text)).strip()


def is_grounded(quote: Any, offer_text: str) -> bool:
    if not isinstance(quote, str):
        return False
    q = normalise(quote)
    return len(q) >= MIN_QUOTE_CHARS and q in normalise(offer_text)


def looks_french(text: str) -> bool:
    words = _WORD.findall(text.lower().replace("’", "'"))
    fr = sum(1 for w in words if w.strip("'") in _FR_WORDS)
    en = sum(1 for w in words if w.strip("'") in _EN_WORDS)
    return fr >= 2 and fr > en


def parse_analysis(raw: str) -> dict[str, Any] | None:
    """The analysis dict, or ``None`` when the output is not valid for scoring."""
    text = raw.strip()
    if text.startswith("```"):  # tolerate a fenced block, as the eligibility parser does
        text = text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("requirements"), list):
        return None
    reqs = data["requirements"]
    if not reqs or not all(isinstance(r, dict) and isinstance(r.get("quote"), str) for r in reqs):
        return None
    return data


@dataclass
class OfferScore:
    offer_id: str
    json_valid: bool
    requirements: int = 0
    grounded: int = 0
    profile_refs: int = 0
    profile_refs_valid: int = 0
    french: bool = False
    expected: int = 0
    found: int = 0
    seconds: float = 0.0
    eval_tokens: int = 0
    eval_seconds: float = 0.0
    error: str | None = None


def score_offer(offer_id: str, offer_text: str, raw: str, profile_ids: set[str],
                seconds: float = 0.0, eval_tokens: int = 0, eval_seconds: float = 0.0,
                expected: list[str] | tuple[str, ...] = ()) -> OfferScore:
    data = parse_analysis(raw)
    score = OfferScore(offer_id=offer_id, json_valid=data is not None, seconds=seconds,
                       eval_tokens=eval_tokens, eval_seconds=eval_seconds,
                       expected=len(expected))
    if data is None:
        return score
    own_words: list[str] = []
    grounded_quotes: list[str] = []
    for req in data["requirements"]:
        score.requirements += 1
        if is_grounded(req.get("quote"), offer_text):
            score.grounded += 1
            grounded_quotes.append(normalise(req["quote"]).lower())
        refs = req.get("profile_entry_ids")
        if isinstance(refs, list):
            score.profile_refs += len(refs)
            score.profile_refs_valid += sum(1 for r in refs if r in profile_ids)
        if isinstance(req.get("label"), str):
            own_words.append(req["label"])
    if isinstance(data.get("summary"), str):
        own_words.append(data["summary"])
    score.french = looks_french(" ".join(own_words))
    score.found = sum(1 for phrase in expected if any(_contains_phrase(q, phrase)
                                                      for q in grounded_quotes))
    return score


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word, case-insensitive: "ATS" is not found in "candidats"."""
    pattern = r"(?<!\w)" + re.escape(normalise(phrase).lower()) + r"(?!\w)"
    return re.search(pattern, text) is not None


@dataclass
class BenchmarkMetrics:
    offers: int
    json_valid_rate: float
    grounding_rate: float
    french_rate: float
    profile_ref_valid_rate: float | None
    recall: float | None
    requirements_per_offer: float
    seconds_per_offer: float
    tokens_per_second: float | None
    per_offer: list[OfferScore] = field(default_factory=list)


def aggregate(scores: list[OfferScore]) -> BenchmarkMetrics:
    n = len(scores)
    valid = [s for s in scores if s.json_valid]
    reqs = sum(s.requirements for s in valid)
    refs = sum(s.profile_refs for s in valid)
    eval_s = sum(s.eval_seconds for s in scores)
    expected = sum(s.expected for s in scores)
    return BenchmarkMetrics(
        offers=n,
        json_valid_rate=len(valid) / n if n else 0.0,
        grounding_rate=sum(s.grounded for s in valid) / reqs if reqs else 0.0,
        french_rate=sum(s.french for s in valid) / len(valid) if valid else 0.0,
        profile_ref_valid_rate=sum(s.profile_refs_valid for s in valid) / refs if refs else None,
        recall=sum(s.found for s in scores) / expected if expected else None,
        requirements_per_offer=reqs / len(valid) if valid else 0.0,
        seconds_per_offer=sum(s.seconds for s in scores) / n if n else 0.0,
        tokens_per_second=sum(s.eval_tokens for s in scores) / eval_s if eval_s > 0 else None,
        per_offer=scores,
    )


def verdict(metrics: BenchmarkMetrics, *, offers_per_day: int = DEFAULT_OFFERS_PER_DAY,
            smaller_tier_available: bool = True) -> tuple[str, dict[str, Any]]:
    """A plain verdict code and its parameters, for the UI to put in words."""
    spo = round(metrics.seconds_per_offer, 1)
    for rate, floor, reason in ((metrics.json_valid_rate, MIN_JSON_VALID_RATE, "json"),
                                (metrics.grounding_rate, MIN_GROUNDING_RATE, "grounding"),
                                (metrics.french_rate, MIN_FRENCH_RATE, "french")):
        if rate < floor:
            return VERDICT_INVALID, {"reason": reason, "rate": round(rate, 3), "minimum": floor}
    if metrics.seconds_per_offer > MAX_ON_DEMAND_SECONDS:
        code = VERDICT_TOO_SLOW_SMALLER if smaller_tier_available else VERDICT_TOO_SLOW_NO_AI
        return code, {"seconds_per_offer": spo, "maximum": MAX_ON_DEMAND_SECONDS}
    return VERDICT_OK, {
        "seconds_per_offer": spo,
        "offers_per_day": offers_per_day,
        "minutes_per_day": round(metrics.seconds_per_offer * offers_per_day / 60.0, 1),
    }
