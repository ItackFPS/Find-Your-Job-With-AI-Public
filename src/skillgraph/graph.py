"""Build the career Skill Graph from the jobs the pipeline kept and the user's profile.

* **Nodes** — technologies tagged on stored jobs (``jobs.skills``), restricted to the vocabulary the
  requirements gate and the Stats page already use (``KNOWN_TECHNOLOGIES``) so source noise such as
  task descriptions never becomes a "skill". Each node counts its demand among all jobs in the window
  and among **relevant** jobs: a latest final score at or above ``relevant_score``, or a job the user
  marked ``interested`` / acted on (application status other than ``withdrawn``).
* **Edges** — co-occurrence: two skills tagged on the same job; weight = number of such jobs (only pairs
  seen on at least ``min_support`` jobs).
* **Gaps** — skills the profile does not list, ranked by relevant demand, each with its evidence (how
  many relevant jobs, example titles) and the owned skills it co-occurs with (adjacent → easier to add).

The profile lists *target* skills, not proven proficiency, and postings are tagged imperfectly; both
limits are returned with every graph. Nothing is written anywhere.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any

from src.requirements.principal_skill import KNOWN_TECHNOLOGIES

ACTED_STATUSES = ("to_apply", "applied", "waiting", "interview", "accepted")   # legacy values; not "withdrawn"
SMALL_SAMPLE = 20


def _connect_ro(db_path: str | Path) -> sqlite3.Connection | None:
    p = Path(db_path)
    if not p.is_file():
        return None
    conn = sqlite3.connect(f"file:{p.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc) or "no such column" in str(exc):
            return []
        raise


def _skills(raw: Any) -> set[str]:
    try:
        values = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except json.JSONDecodeError:
        return set()
    return {str(v).strip().lower() for v in values if str(v).strip().lower() in KNOWN_TECHNOLOGIES}


def build_skill_graph(
    db_path: str | Path, target_skills: list[str], *, days: int = 90, relevant_score: float = 0.65,
    min_support: int = 2, max_edges_per_skill: int = 5, max_nodes: int = 60, now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).isoformat()
    owned = {s.strip().lower() for s in target_skills if s.strip()}
    graph: dict[str, Any] = {
        "window_days": days, "relevant_definition": (
            f"latest final score >= {relevant_score:.2f}, or marked interested / acted on (not withdrawn)"),
        "n_jobs": 0, "n_relevant": 0, "nodes": [], "edges": [], "gaps": [], "owned": sorted(owned),
        "limitations": [
            "skills come from the technology tags of stored postings; requirements written only in free text are missed",
            "the profile lists target skills, not proven proficiency",
            "only jobs the pipeline kept are stored: demand among rejected postings is not counted",
        ],
    }
    conn = _connect_ro(db_path)
    if conn is None:
        graph["limitations"].append("no database yet")
        return graph
    try:
        jobs = _rows(conn, """SELECT id, title, skills, application_status FROM jobs
                              WHERE status = 'active' AND COALESCE(first_seen_at, discovered_at) >= ?""", (cutoff,))
        scores = {r["job_id"]: r["s"] for r in _rows(conn, """
            SELECT js.job_id AS job_id, js.final_score AS s FROM job_scores js
            JOIN pipeline_runs pr ON js.run_id = pr.id
            WHERE js.final_score IS NOT NULL ORDER BY pr.started_at, pr.id, js.rowid""")}
        interested = {r["job_id"] for r in _rows(conn, """
            SELECT job_id FROM feedback_events WHERE signal_class = 'explicit' AND type = 'interested'""")}
    finally:
        conn.close()

    tagged = [(j["id"], j["title"] or "", _skills(j["skills"])) for j in jobs]
    tagged = [t for t in tagged if t[2]]
    acted = {j["id"] for j in jobs if (j["application_status"] or "") in ACTED_STATUSES}
    relevant = {jid for jid, _, _ in tagged
                if (scores.get(jid) or 0.0) >= relevant_score or jid in interested or jid in acted}
    demand: Counter[str] = Counter()
    rel_demand: Counter[str] = Counter()
    pairs: Counter[tuple[str, str]] = Counter()
    titles: dict[str, list[tuple[float, str]]] = {}
    for jid, title, skills in tagged:
        demand.update(skills)
        if jid in relevant:
            rel_demand.update(skills)
            for s in skills:
                titles.setdefault(s, []).append((scores.get(jid) or 0.0, title))
        pairs.update(combinations(sorted(skills), 2))
    graph["n_jobs"], graph["n_relevant"] = len(tagged), len(relevant)
    if len(relevant) < SMALL_SAMPLE:
        graph["limitations"].append(f"only {len(relevant)} relevant job(s) in the window: treat rankings as indicative")

    n_rel = max(1, len(relevant))
    ranked = sorted(demand, key=lambda s: (-rel_demand[s], -demand[s], s))[:max_nodes]
    keep = set(ranked) | (owned & set(demand))
    graph["nodes"] = [{"skill": s, "demand": demand[s], "relevant_demand": rel_demand[s],
                       "relevant_share": round(rel_demand[s] / n_rel, 4), "owned": s in owned}
                      for s in sorted(keep, key=lambda s: (-rel_demand[s], -demand[s], s))]
    per_skill: dict[str, int] = Counter()
    for (a, b), w in sorted(pairs.items(), key=lambda kv: (-kv[1], kv[0])):
        if w < min_support or a not in keep or b not in keep:
            continue
        if per_skill[a] >= max_edges_per_skill and per_skill[b] >= max_edges_per_skill:
            continue
        per_skill[a] += 1
        per_skill[b] += 1
        graph["edges"].append({"source": a, "target": b, "weight": w})

    def pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def adjacent(skill: str) -> list[dict[str, Any]]:
        together = sorted(((pairs[pair(skill, o)], o) for o in owned if pairs[pair(skill, o)] >= 1),
                          key=lambda x: (-x[0], x[1]))
        return [{"skill": o, "jobs": n} for n, o in together[:3]]

    for s in sorted((s for s in rel_demand if s not in owned and rel_demand[s] >= min_support),
                    key=lambda s: (-rel_demand[s], s))[:15]:
        adj = adjacent(s)
        examples = [t for _, t in sorted(titles.get(s, []), key=lambda x: -x[0])[:3]]
        why = (f"asked for by {rel_demand[s]} of {len(relevant)} relevant job(s) ({rel_demand[s] / n_rel:.0%})"
               + (f"; often alongside your {', '.join(a['skill'] for a in adj)}" if adj else ""))
        graph["gaps"].append({"skill": s, "relevant_demand": rel_demand[s], "demand": demand[s],
                              "relevant_share": round(rel_demand[s] / n_rel, 4), "adjacent_owned": adj,
                              "example_titles": examples, "why": why})
    return graph
