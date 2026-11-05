"""Requirement extractor vs France Travail structured fields (v5 stage L2, ADR 0025 "requirement
extraction quality").

France Travail states the experience and, on a minority of offers, the education it requires in
typed fields (``src.requirements.structured``). The text extractor (ADR 0004) reads only the title
and description. On stored France Travail offers the structured value is the reference, and this
report gives, per field:

* ``precision`` — among offers where the extractor found a value and the structured field states
  one, the share that agree;
* ``recall`` — among offers whose structured field states a requirement, the share where the
  extractor found one at all;
* ``disagreement`` — among offers where both state a value, the share that disagree.

Agreement rules (stated, not tuned): experience agrees when the years differ by at most
``YEARS_TOLERANCE``; a "débutant accepté" offer counts as 0 years. Education agrees when the
extracted bac+N level lies inside the structured range of any of the offer's formations.

Also reported: ``rome_coverage``, the share of France Travail offers with a valid ROME code.

Read-only (``mode=ro``, ``query_only``). The result is counts, rates and Wilson intervals, never
offer content. Runs the heuristic extractor only (no LLM), with an empty skill list: experience and
education extraction do not depend on the profile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.evaluation.export import open_readonly
from src.evaluation.metrics import wilson
from src.models.job import Job
from src.requirements.extractor import RequirementExtractor
from src.requirements.structured import FRANCE_TRAVAIL, structured_evidence

YEARS_TOLERANCE = 1.0
DEFAULT_LIMIT = 2000        # most recent offers; enough for a weekly figure, bounded run time


def _rate(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "value": k / n if n else None, "ci95": wilson(k, n)}


def extractor_agreement(db_path: str | Path, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    conn = open_readonly(db_path)
    try:
        rows = conn.execute("SELECT * FROM jobs WHERE source = ? ORDER BY discovered_at DESC LIMIT ?",
                            (FRANCE_TRAVAIL, limit)).fetchall()
    finally:
        conn.close()
    extractor = RequirementExtractor(user_skills=[])
    offers = with_rome = 0
    exp = {"both": 0, "agree": 0, "extracted": 0, "stated": 0, "stated_found": 0, "beginner_conflict": 0}
    edu = {"both": 0, "agree": 0, "stated": 0, "stated_found": 0}
    for row in rows:
        job = Job.from_dict(dict(row))
        ev = structured_evidence(job.source, job.raw_payload)
        if ev is None:
            continue
        offers += 1
        with_rome += ev.occupation is not None
        req = extractor.extract(job)
        x = float(req.experience.min_years) if req.experience is not None else None
        y = ev.experience.min_years if ev.experience is not None else None
        if x is not None:
            exp["extracted"] += 1
        if y is not None and y >= 1:
            exp["stated"] += 1
            exp["stated_found"] += x is not None
        if x is not None and y is not None:
            exp["both"] += 1
            exp["agree"] += abs(x - y) <= YEARS_TOLERANCE
            exp["beginner_conflict"] += bool(ev.experience and ev.experience.required is False and x >= 2)
        ranges = [(e.level_min, e.level_max) for e in ev.education if e.level_min is not None]
        level = req.education.level_bac_plus if req.education is not None else None
        if ranges:
            edu["stated"] += 1
            edu["stated_found"] += level is not None
        if ranges and level is not None:
            edu["both"] += 1
            edu["agree"] += any(lo <= level and (hi is None or level <= hi) for lo, hi in ranges)  # type: ignore[operator]
    status = "no_france_travail_offers" if offers == 0 else "measured"
    return {
        "status": status, "offers": offers, "limit": limit, "years_tolerance": YEARS_TOLERANCE,
        "rome_coverage": _rate(with_rome, offers),
        "experience": {
            "precision": _rate(exp["agree"], exp["both"]),
            "recall": _rate(exp["stated_found"], exp["stated"]),
            "disagreement": _rate(exp["both"] - exp["agree"], exp["both"]),
            "beginner_conflicts": exp["beginner_conflict"], "extracted": exp["extracted"],
        },
        "education": {
            "precision": _rate(edu["agree"], edu["both"]),
            "recall": _rate(edu["stated_found"], edu["stated"]),
            "disagreement": _rate(edu["both"] - edu["agree"], edu["both"]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m src.evaluation.extractor_agreement",
                                 description="Requirement extractor vs France Travail structured fields (read-only)")
    ap.add_argument("--db", default="data/jobs.db")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ns = ap.parse_args(argv)
    print(json.dumps(extractor_agreement(ns.db, limit=ns.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
