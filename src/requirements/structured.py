"""Structured evidence read from a source's own fields (ADR 0023 step 1, v5 stage L2).

France Travail offers carry typed fields the text extractor (ADR 0004) never sees: the ROME
occupation (``romeCode``, 100 % of a 1,161-offer sample), the experience requirement
(``experienceExige`` D/E and ``experienceLibelle``), the education requirement (``formations[]``)
and the salary (``salaire.libelle``). This module turns them into typed, frozen values.

Pure functions over the stored ``raw_payload``: no database, no network, no decision. Nothing here
changes a gate or a score; consumers (the extractor-agreement report today, the occupation model and
the requirement strength later) read these values and decide for themselves. Other sources have no
such fields and give ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

FRANCE_TRAVAIL = "france_travail"
_FT_RAW_KEY = "_france_travail_raw"     # the collector keeps the whole API offer under this key

Exigence = Literal["required", "preferred"]
Period = Literal["annual", "monthly", "hourly", "daily"]


@dataclass(frozen=True)
class StructuredOccupation:
    scheme: str                 # "rome"
    code: str                   # e.g. "M1805"
    label: str = ""             # romeLibelle
    appellation: str = ""       # appellationlibelle


@dataclass(frozen=True)
class StructuredExperience:
    required: bool | None       # E → True, D ("débutant accepté") → False, absent → None
    min_years: float | None     # parsed from the label; 0.0 for beginners; None when no duration
    label: str = ""


@dataclass(frozen=True)
class StructuredEducation:
    level_min: int | None       # bac+N lower bound; 0 = bac; -1 = below bac (CAP, BEP); None unparsed
    level_max: int | None       # upper bound ("Bac+3, Bac+4" → 3..4; "Bac+5 et plus" → 5..None)
    exigence: Exigence | None   # E → required, S → preferred
    label: str = ""


@dataclass(frozen=True)
class StructuredSalary:
    period: Period | None
    minimum: float | None
    maximum: float | None
    months: float | None        # "sur 12 mois"
    currency: str | None
    label: str = ""


@dataclass(frozen=True)
class StructuredEvidence:
    source: str
    occupation: StructuredOccupation | None
    experience: StructuredExperience | None
    education: tuple[StructuredEducation, ...]
    salary: StructuredSalary | None


def structured_evidence(source: str, raw_payload: dict[str, Any] | None) -> StructuredEvidence | None:
    """The source's structured fields, or ``None`` for a source that has none."""
    if source != FRANCE_TRAVAIL or not isinstance(raw_payload, dict):
        return None
    offer = raw_payload.get(_FT_RAW_KEY, raw_payload)
    return from_france_travail(offer if isinstance(offer, dict) else {})


def from_france_travail(offer: dict[str, Any]) -> StructuredEvidence:
    return StructuredEvidence(
        source=FRANCE_TRAVAIL,
        occupation=_occupation(offer),
        experience=_experience(offer),
        education=tuple(e for e in (_education(f) for f in _list(offer.get("formations"))) if e is not None),
        salary=_salary(offer.get("salaire")),
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _occupation(offer: dict[str, Any]) -> StructuredOccupation | None:
    code = _text(offer.get("romeCode")).upper()
    if not re.fullmatch(r"[A-N]\d{4}", code):
        return None
    return StructuredOccupation("rome", code, _text(offer.get("romeLibelle")),
                                _text(offer.get("appellationlibelle")))


_YEARS = re.compile(r"(\d+(?:[.,]\d+)?)\s*an", re.IGNORECASE)
_MONTHS = re.compile(r"(\d+(?:[.,]\d+)?)\s*mois", re.IGNORECASE)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_experience_years(label: str) -> float | None:
    """Years stated in an FT experience label: "2 An(s)" → 2.0, "6 Mois" → 0.5,
    "Débutant accepté" → 0.0, "Expérience exigée" (no duration) → None."""
    low = label.lower()
    if "débutant" in low or "debutant" in low:
        return 0.0
    if m := _YEARS.search(label):
        return _num(m.group(1))
    if m := _MONTHS.search(label):
        return round(_num(m.group(1)) / 12, 2)
    return None


def _experience(offer: dict[str, Any]) -> StructuredExperience | None:
    code = _text(offer.get("experienceExige")).upper()
    label = _text(offer.get("experienceLibelle"))
    if not code and not label:
        return None
    required = True if code == "E" else False if code == "D" else None
    years = parse_experience_years(label)
    if years is None and required is False:
        years = 0.0
    return StructuredExperience(required, years, label)


_BAC_PLUS = re.compile(r"bac\s*\+\s*(\d)", re.IGNORECASE)


def parse_education_level(label: str) -> tuple[int | None, int | None]:
    """``(min, max)`` bac+N levels of an FT ``niveauLibelle``."""
    low = label.lower()
    levels = [int(n) for n in _BAC_PLUS.findall(label)]
    if levels:
        top = None if "et plus" in low else max(levels)
        return min(levels), top
    if re.search(r"\b(cap|bep)\b", low):
        return -1, -1
    if re.search(r"\bbac\b", low):
        return 0, 0
    return None, None


def _education(formation: dict[str, Any]) -> StructuredEducation | None:
    label = _text(formation.get("niveauLibelle"))
    if not label:
        return None
    lo, hi = parse_education_level(label)
    ex = _text(formation.get("exigence")).upper()
    exigence: Exigence | None = "required" if ex == "E" else "preferred" if ex == "S" else None
    return StructuredEducation(lo, hi, exigence, label)


_PERIODS: dict[str, Period] = {"annuel": "annual", "mensuel": "monthly", "horaire": "hourly",
                               "journalier": "daily"}
_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(euros?|€)", re.IGNORECASE)
_ON_MONTHS = re.compile(r"sur\s*(\d+(?:[.,]\d+)?)\s*mois", re.IGNORECASE)


def parse_salary_label(label: str) -> StructuredSalary | None:
    """"Annuel de 40000.0 Euros à 50000.0 Euros sur 12.0 mois" → annual 40000..50000 over 12 months.
    ``None`` when the label states no amount (a comment or benefits only)."""
    amounts = [_num(m.group(1)) for m in _AMOUNT.finditer(label)]
    if not amounts:
        return None
    first = label.split(maxsplit=1)[0].lower() if label.split() else ""
    months = _ON_MONTHS.search(label)
    return StructuredSalary(_PERIODS.get(first), min(amounts), max(amounts),
                            _num(months.group(1)) if months else None, "EUR", label)


def _salary(salaire: Any) -> StructuredSalary | None:
    if not isinstance(salaire, dict):
        return None
    return parse_salary_label(_text(salaire.get("libelle")))
