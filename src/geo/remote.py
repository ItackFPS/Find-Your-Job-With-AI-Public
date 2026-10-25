"""Who can do this job from where: typed remote eligibility with its evidence.

Plan F4.2 (§C2). The old model treated any remote offer without a country as
"Remote France" — which ranked a Hong Kong office job with one home-working day a
week first. This module answers instead, from the offer's own words:

* ``onsite`` / ``hybrid`` — work happens at a workplace (``hybrid_days`` when stated);
* ``remote_france``, ``remote_eu``, ``remote_worldwide`` — remote, open from that area;
* ``remote_restricted`` — remote but only for residents/authorised workers of
  ``area`` (``"US"``, ``"UK"``, ``"LATAM"``…), i.e. not open to someone in France;
* ``unknown`` — not enough evidence. Unknown is never a match *and* never a rejection.

Every answer carries the quoted ``evidence`` and its ``strength``: ``explicit`` when
the text states it ("must reside in the US"), ``inferred`` when it comes from a
structured field (a location of "USA" on a remote board). Callers decide what to do
with each (F4.3); this module only reads.

Order of precedence, most specific first: an explicit residency/authorisation
restriction, an explicit openness statement, workplace (hybrid) wording, then
structured location fields. A restriction beats openness wording because "remote,
worldwide team" boilerplate is common next to "US residents only".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ONSITE = "onsite"
HYBRID = "hybrid"
REMOTE_FRANCE = "remote_france"
REMOTE_EU = "remote_eu"
REMOTE_WORLDWIDE = "remote_worldwide"
REMOTE_RESTRICTED = "remote_restricted"
UNKNOWN = "unknown"

EXPLICIT = "explicit"
INFERRED = "inferred"


@dataclass(frozen=True)
class RemoteEligibility:
    kind: str
    area: str | None = None          # for remote_restricted: "US", "UK", "LATAM", ...
    evidence: str | None = None      # the words this answer rests on
    strength: str = INFERRED
    hybrid_days: int | None = None   # days at the workplace per week, when stated

    @property
    def open_from_france(self) -> bool | None:
        """True / False when the evidence says so, None when it does not."""
        if self.kind in (REMOTE_FRANCE, REMOTE_EU, REMOTE_WORLDWIDE):
            return True
        if self.kind == REMOTE_RESTRICTED:
            return False
        return None


# Area names -> canonical area. France and the EU are "open from France" areas.
_AREAS: dict[str, str] = {
    "france": "FR",
    "eu": "EU", "europe": "EU", "european union": "EU", "emea": "EU", "european": "EU",
    "cet": "EU", "cest": "EU",
    "us": "US", "u.s.": "US", "usa": "US", "united states": "US", "america": "US",
    "uk": "UK", "united kingdom": "UK", "england": "UK", "great britain": "UK",
    "canada": "CA", "latam": "LATAM", "latin america": "LATAM", "south america": "LATAM",
    "apac": "APAC", "asia": "APAC", "australia": "AU", "new zealand": "NZ", "india": "IN",
    "philippines": "PH", "brazil": "BR", "mexico": "MX", "germany": "DE", "spain": "ES",
    "portugal": "PT", "poland": "PL", "netherlands": "NL", "ireland": "IE", "israel": "IL",
    "south africa": "ZA", "nigeria": "NG", "kenya": "KE", "pakistan": "PK", "hong kong": "HK",
    "singapore": "SG", "japan": "JP", "argentina": "AR", "colombia": "CO", "romania": "RO",
    "americas": "AMERICAS", "north america": "AMERICAS",
    "austria": "AT", "belgium": "BE", "bulgaria": "BG", "croatia": "HR", "cyprus": "CY",
    "czechia": "CZ", "czech republic": "CZ", "denmark": "DK", "estonia": "EE", "finland": "FI",
    "greece": "GR", "hungary": "HU", "italy": "IT", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "malta": "MT", "slovakia": "SK", "slovenia": "SI", "sweden": "SE",
    "switzerland": "CH", "norway": "NO", "ukraine": "UA", "serbia": "RS", "albania": "AL",
    "turkey": "TR", "egypt": "EG", "morocco": "MA", "tunisia": "TN", "malaysia": "MY",
    "indonesia": "ID", "vietnam": "VN", "thailand": "TH", "china": "CN", "south korea": "KR",
    "chile": "CL", "peru": "PE", "uruguay": "UY", "costa rica": "CR", "middle east": "ME",
    "uae": "AE", "united arab emirates": "AE", "saudi arabia": "SA",
}
# "Headquarters: Remote (United States)" (We Work Remotely) describes the company,
# not who may apply: removed before the restriction/openness patterns run.
_HEADQUARTERS_RE = re.compile(r"headquarters\s*:\s*[^\n]{0,60}?(?:\)|(?=\s{2,})|$|(?=\s+URL:))", re.I | re.M)
_AREA_ALT = "|".join(sorted((re.escape(a) for a in _AREAS), key=len, reverse=True))
_US_TZ = r"(?:est|edt|pst|pdt|cst|mst|eastern|pacific|central|mountain)"

_RESTRICTION_RES = [
    # "must be located in the US", "must reside in Canada", "must live in a state where"
    re.compile(rf"\bmust (?:be )?(?:located|based|residing|reside|live|living)\s+(?:with)?in\s+(?:the\s+)?({_AREA_ALT}|a (?:us )?state)\b", re.I),
    # "authorized to work in the USA", "eligible to work in the UK", "right to work in the UK"
    re.compile(rf"\b(?:authori[sz]ed|eligible|right|permitted)\s+to\s+work\s+in\s+(?:the\s+)?({_AREA_ALT})\b", re.I),
    # "US only", "Remote (US only)", "UK-only"
    re.compile(rf"\b({_AREA_ALT})[\s-]+only\b", re.I),
    # "open to candidates (based) in the US", "only hiring in Canada"
    re.compile(rf"\b(?:only\s+(?:hiring|accepting|considering)|open\s+(?:only\s+)?to)\s+(?:candidates|applicants|residents|people)?\s*(?:based|located|living)?\s*(?:in|from)\s+(?:the\s+)?({_AREA_ALT})\b", re.I),
    # "US-based candidates only", "UK based only" ("for US based applicants, the salary
    # range is..." and "our US based employees enjoy..." are not restrictions)
    re.compile(rf"\b({_AREA_ALT})[\s-]based\s+(?:(?:candidates|applicants|contractors)\s+)?only\b", re.I),
    # "Remote - US", "Remote (Canada)", "Remote, UK"
    re.compile(rf"\bremote\s*(?:[-–,:(]\s*)({_AREA_ALT})\b\)?", re.I),
    # "within US time zones", "overlap with EST"
    re.compile(r"\b(?:within|in)\s+(?:the\s+)?(us|north american?)\s+time\s*zones?\b", re.I),
    re.compile(rf"\b(?:work|overlap)\s+(?:\w+\s+){{0,3}}{_US_TZ}\s+(?:business\s+)?hours\b", re.I),
]
_STATE_RE = re.compile(r"\ba (?:us )?state\b", re.I)

_OPEN_RES = [
    re.compile(r"\b(?:work|working)\s+from\s+anywhere\b", re.I),
    re.compile(r"\banywhere\s+in\s+the\s+world\b", re.I),
    re.compile(r"\b(?:fully\s+|100\s*%\s*)?remote\s*(?:[-–,:(]\s*|\s+)(?:worldwide|anywhere|global(?:ly)?)\b", re.I),
    re.compile(r"\b(?:location|based)\s*:\s*(?:worldwide|anywhere|global)\b", re.I),
    re.compile(r"\bhire\s+(?:talent\s+|people\s+)?(?:from\s+)?(?:anywhere|worldwide|all over the world)\b", re.I),
]
_FRENCH_REMOTE_RES = [
    re.compile(r"\bt[ée]l[ée]travail\s+(?:total|complet|int[ée]gral|100\s*%)", re.I),
    re.compile(r"\b100\s*%\s*(?:t[ée]l[ée]travail|remote|à distance)\b", re.I),
    re.compile(r"\b(?:full|fully)[\s-]remote\b", re.I),
]
_HYBRID_RES = [
    # Work-arrangement wording only: "hybrid cloud" or "a hybrid Microsoft environment" is not.
    re.compile(r"\bhybrid\s+(?:work|working|role|position|model|schedule|mode|policy|setup|arrangement|job|contract|remote)\b", re.I),
    re.compile(r"\b(?:travail|mode|poste|rythme|organisation|format|contrat)\s+hybride\b", re.I),
    re.compile(r"\b(?:in|on)\s+an?\s+hybrid\s+(?:way|fashion|setting|capacity|basis|manner|format|model)\b", re.I),
    re.compile(r"\ben\s+hybride\b(?=\s*[.,;:)\n]|\s+(?:avec|entre|sur|au|à|depuis)\b)", re.I),
    re.compile(r"\b(?:remote|t[ée]l[ée]travail|office|bureau)\s*(?:/|ou|or|&|\+)\s*hybride?\b", re.I),
    re.compile(r"\bhybride?\s*(?:\(|:|-\s|–)\s*(?:\d|remote|office|t[ée]l[ée]travail|on-?site)", re.I),
    re.compile(r"\bt[ée]l[ée]travail\s+(?:partiel|possible|occasionnel|ponctuel)\b", re.I),
    re.compile(r"\b(\d|un|deux|trois|quatre)\s+jours?\s+(?:de\s+)?t[ée]l[ée]travail\b", re.I),
    re.compile(r"\b(\d|one|two|three|four)\s+days?\s+(?:a|per)\s+week\s+(?:in|at|from)\s+(?:the|our)\s+office\b", re.I),
    # "Work From Home (WFH) option available one day per week" counts home days.
    re.compile(r"\b(?:wfh|work from home|remote|t[ée]l[ée]travail)\W+(?:\w+\W+){0,4}?(\d|one|two|three)\s+days?\s+(?:a|per)\s+week\b", re.I),
]
_NUM = {"un": 1, "one": 1, "deux": 2, "two": 2, "trois": 3, "three": 3, "quatre": 4, "four": 4}

_ANYWHERE_LOC = {"anywhere", "worldwide", "global", "remote worldwide", "remote anywhere", "world"}
_EU_LOC = {"europe", "eu", "emea", "european union", "remote europe", "remote eu"}


def _area(name: str) -> str | None:
    n = name.lower().strip().rstrip(".")
    if _STATE_RE.fullmatch(n):
        return "US"
    if n.startswith("north american"):
        return "AMERICAS"
    return _AREAS.get(n)


def _snippet(text: str, m: re.Match[str], width: int = 60) -> str:
    start, end = max(0, m.start() - 20), min(len(text), m.end() + width - (m.end() - m.start()))
    return " ".join(text[start:end].split())


def _restriction(text: str) -> RemoteEligibility | None:
    for regex in _RESTRICTION_RES:
        for m in regex.finditer(text):
            group = m.group(1) if m.groups() else None
            area = _area(group) if group else "US"  # the time-zone patterns are US ones
            if area is None:
                continue
            ev = _snippet(text, m)
            if area == "FR":
                return RemoteEligibility(REMOTE_FRANCE, evidence=ev, strength=EXPLICIT)
            if area == "EU":
                return RemoteEligibility(REMOTE_EU, evidence=ev, strength=EXPLICIT)
            return RemoteEligibility(REMOTE_RESTRICTED, area=area, evidence=ev, strength=EXPLICIT)
    return None


# Residency/authorisation wordings strong enough to EXCLUDE an offer (F4.3 remote policy):
# "must reside in", "authorized to work in", "X only", "only hiring in", "X-based only".
# Deliberately not "Remote - US" (often the company's location) nor the time-zone patterns
# ("overlap with EST hours" says when, not where): those never exclude.
_EXPLICIT_RESTRICTION_RES = _RESTRICTION_RES[:5]
_AREA_WORD_RE = re.compile(rf"(?<![\w.])({_AREA_ALT})(?![\w])", re.I)
_SENTENCE_END_RE = re.compile(r"[.;!?\n]")
_ONLY_PATTERNS = (2, 4)  # "X only" and "X-based (candidates) only" in _RESTRICTION_RES
_PERK_OR_PAY_RE = re.compile(
    r"holiday|salary|compensation|\bpay\b|\brange\b|benefit|health|insurance|401k|\bpto\b|bonus|equity"
    r"|employees|\busd\b|\$", re.I)


def _is_perk_or_pay(text: str, m: re.Match[str]) -> bool:
    """An "X only" that qualifies a perk or a pay range, or sits in parentheses, says nothing
    about who may apply."""
    before = text[max(0, m.start() - 1): m.start()]
    window = text[max(0, m.start() - 80): min(len(text), m.end() + 80)]
    return before == "(" or bool(_PERK_OR_PAY_RE.search(window))


def explicit_restriction_areas(
    *, title: str | None = None, description: str | None = None, location: str | None = None,
) -> tuple[set[str], str | None]:
    """Every area named by an explicit residency/authorisation restriction, and the first quote.

    Collects ALL matches, and for each one every area named in the rest of its sentence
    ("must be located in the US or France" -> {"US", "FR"}), so a caller can tell an offer
    restricted to one foreign area from one that also names France or the EU. A named place
    that is not a known area yields "?" (the caller must then not rely on the set).
    """
    text = _HEADQUARTERS_RE.sub(" ", f"{title or ''}\n{location or ''}\n{description or ''}")
    areas: set[str] = set()
    quote: str | None = None
    for i, regex in enumerate(_EXPLICIT_RESTRICTION_RES):
        for m in regex.finditer(text):
            if i in _ONLY_PATTERNS and _is_perk_or_pay(text, m):
                continue  # "Federal Holidays (US only)", "salary range for US residents only"
            group = m.group(1) if m.groups() else None
            first = _area(group) if group else None
            areas.add(first or "?")
            end = _SENTENCE_END_RE.search(text, m.end())
            tail = text[m.end(): end.start() if end else min(len(text), m.end() + 120)]
            for w in _AREA_WORD_RE.finditer(tail):
                if (a := _area(w.group(1))) is not None:
                    areas.add(a)
            quote = quote or _snippet(text, m)
    return areas, quote


def _hybrid(text: str) -> RemoteEligibility | None:
    """Hybrid wording, preferring a match that states the number of days."""
    first: RemoteEligibility | None = None
    for regex in _HYBRID_RES:
        m = regex.search(text)
        if not m:
            continue
        days = None
        if m.groups():
            raw = m.group(1).lower()
            n = int(raw) if raw.isdigit() else _NUM.get(raw)
            if n is not None:
                office = "office" in m.group(0).lower()
                # "2 jours de télétravail" / "WFH one day per week" count home days.
                days = n if office else max(0, 5 - n)
        found = RemoteEligibility(HYBRID, evidence=_snippet(text, m), strength=EXPLICIT, hybrid_days=days)
        if days is not None:
            return found
        first = first or found
    return first


def _location_areas(location: str | None, countries: list[str] | None) -> set[str]:
    """Areas named by the structured fields: "WORLD", "FR", "EU", or country codes.

    "Texas, USA" -> {"US"}; "APAC, Middle East" -> {"APAC", "ME"}; "USA or Canada" ->
    {"US", "CA"}; a French town -> {"FR"}; nothing recognised -> empty set.
    """
    areas: set[str] = set()
    for part in re.split(r"\s*(?:,|/|\||;|\bor\b|\bou\b|&|\band\b|\bet\b|\(|\))\s*", (location or "").lower()):
        part = part.replace("remote", "").replace("only", "").strip(" -–:.")
        if not part:
            continue
        if part in _ANYWHERE_LOC:
            areas.add("WORLD")
        elif part in _EU_LOC:
            areas.add("EU")
        elif (a := _area(part)) is not None:
            areas.add(a)
    for c in countries or []:
        if (a := _area(c)) is not None:
            areas.add(a)
    if not areas and location and _french_context(location, countries):
        areas.add("FR")
    return areas


def _from_areas(areas: set[str], evidence: str) -> RemoteEligibility:
    if "WORLD" in areas:
        return RemoteEligibility(REMOTE_WORLDWIDE, evidence=evidence)
    if "FR" in areas:
        return RemoteEligibility(REMOTE_FRANCE, evidence=evidence)
    if "EU" in areas:
        return RemoteEligibility(REMOTE_EU, evidence=evidence)
    return RemoteEligibility(REMOTE_RESTRICTED, area="/".join(sorted(areas)), evidence=evidence)


def classify_remote(
    *,
    title: str | None = None,
    description: str | None = None,
    location: str | None = None,
    countries: list[str] | None = None,
    remote: bool | None = None,
    work_mode: str | None = None,
) -> RemoteEligibility:
    """Classify an offer's remote eligibility from its fields. Never raises."""
    title = title or ""
    text = _HEADQUARTERS_RE.sub(" ", f"{title}\n{location or ''}\n{description or ''}")
    loc_ev = f"location: {location}" if location else f"countries: {countries}"
    areas = _location_areas(location, countries)

    restricted = _restriction(text)
    if restricted is not None:
        return restricted

    for regex in _OPEN_RES:
        m = regex.search(text)
        if m:
            ev = _snippet(text, m)
            if areas and not areas & {"WORLD", "FR", "EU"}:
                # "work from anywhere" next to a location field naming another
                # country: boilerplate or not, the evidence disagrees.
                return RemoteEligibility(UNKNOWN, evidence=f"{ev} | {loc_ev}")
            if areas & {"FR", "EU"} and "WORLD" not in areas:
                return _from_areas(areas, f"{ev} | {loc_ev}")
            return RemoteEligibility(REMOTE_WORLDWIDE, evidence=ev, strength=EXPLICIT)

    hybrid = _hybrid(text)
    if hybrid is not None or (work_mode or "").lower() == "hybrid":
        return hybrid or RemoteEligibility(HYBRID, evidence=f"work_mode={work_mode}")

    is_remote = (bool(remote) or (work_mode or "").lower() == "remote"
                 or "remote" in (location or "").lower())
    if is_remote:
        for regex in _FRENCH_REMOTE_RES:
            m = regex.search(text)
            if m and "FR" in areas:
                return RemoteEligibility(REMOTE_FRANCE, evidence=_snippet(text, m), strength=EXPLICIT)
        if areas:
            return _from_areas(areas, loc_ev)
        return RemoteEligibility(UNKNOWN, evidence="remote, no stated area")

    if (work_mode or "").lower() in ("on-site", "onsite", "on_site") or (remote is False and location):
        return RemoteEligibility(ONSITE, evidence=f"location: {location}" if location else None)
    return RemoteEligibility(UNKNOWN)


_FRENCH_MARKER_RE = re.compile(
    r"(?:^|[\s,(])(?:france|fr)(?:$|[\s,)])|(?<!\d)\d{5}(?!\d)|\((?:\d{2,3}|2[ab])\)|^\s*(?:\d{2,3}|2[ab])\s*[-–]\s",
    re.I)


def _french_context(location: str | None, countries: list[str] | None) -> bool:
    """The offer itself is placed in France.

    Needs France in the countries, or a French marker in the location (the word
    France/FR, a postcode, "(83)", "83 - "). A bare town name is not enough: remote
    boards list "Regina," or "Allan," (Canada) and "Macau," that are also the names
    of small French communes.
    """
    if countries and any(c.strip().lower() in ("france", "fr") for c in countries):
        return True
    if not location or not _FRENCH_MARKER_RE.search(location):
        return False
    from src.geo.places import resolve_place

    return resolve_place(location, countries) is not None
