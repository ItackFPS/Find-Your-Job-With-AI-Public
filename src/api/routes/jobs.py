"""Jobs API routes.

GET /api/jobs                   — list stored jobs with optional filters
GET /api/jobs/{id}              — single job detail
GET /api/jobs/{id}/explanation  — why the job matched / was rejected
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from pydantic import BaseModel

from src.api.schemas import ExplanationReason, ExplanationSignal, JobDetailOut, JobExplanationOut, ProvenanceOut
from src.models.job import Job
from src.outcomes.models import OutcomeEventType
from src.outcomes.storage import OutcomeEventStorage
from src.requirements.principal_skill import KNOWN_TECHNOLOGIES
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

if TYPE_CHECKING:
    from src.models.profile import JobSearchProfile


class CountryFacet(BaseModel):
    name: str
    count: int


class CountriesOut(BaseModel):
    total: int
    facets: list[CountryFacet]


class SkillFacet(BaseModel):
    name: str
    count: int


class ScoreTierFacet(BaseModel):
    tier: str
    count: int


class RejectionFacet(BaseModel):
    stage: str
    detail: str
    count: int


class TitleCategoryFacet(BaseModel):
    name: str
    count: int


class JobStatsOut(BaseModel):
    total_jobs: int
    skills: list[SkillFacet] = []
    tools: list[SkillFacet] = []
    score_tiers: list[ScoreTierFacet] = []
    rejections: list[RejectionFacet] = []
    title_categories: list[TitleCategoryFacet] = []


# A display-only grouping of the frequency facet into "skills" (languages,
# frameworks) vs. "tools" (platforms/infra) for the Stats page — does not
# affect matching or scoring, just how the same tag data is labeled.
_TOOL_KEYWORDS = frozenset({
    "docker", "git", "aws", "kubernetes", "n8n", "jira", "linux", "bash",
    "terraform", "jenkins", "azure", "gcp", "ansible", "nginx", "grafana",
    "prometheus", "ci/cd", "github", "gitlab",
})

# Title-category buckets mirror config/sources.yaml's Indeed `queries` groups
# (the pipeline's own configured search terms) — used only to label real,
# already-collected job titles, never to invent new data.
_TITLE_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Python / Data", ["python", "data engineer", "data analyst", "data scientist"]),
    ("Web / Backend (JS, PHP, Node)", ["javascript", " php", "node.js", "développeur web", "developpeur web"]),
    ("Frontend / Backend / Fullstack", [
        "frontend", "front-end", "backend", "back-end", "fullstack", "full stack",
        "software engineer", "ingénieur logiciel", "ingenieur logiciel",
    ]),
    ("QA / Test / Automation", ["qa engineer", "ingénieur test", "ingenieur test", "automation engineer"]),
    ("DevOps / Cloud", ["devops", "cloud engineer"]),
    ("Cybersecurity", ["cybersecurity", "cybersécurité", "cybersecurite", "ingénieur sécurité", "ingenieur securite"]),
    ("IT Support / Network", [
        "support informatique", "administrateur système", "administrateur systeme", "ingénieur réseau", "ingenieur reseau",
    ]),
]


def _aggregate_tech_facets(
    skill_rows: list,
) -> tuple[list[SkillFacet], list[SkillFacet]]:
    """Roll up raw ``jobs.skills`` values into skill/tool facets for the Stats page.

    ``jobs.skills`` mixes real technology names with source-specific noise —
    most notably France Travail's ROME ``competences[].libelle`` values, which
    are French task/activity descriptions ("Configurer le poste de travail
    aux besoins de l'utilisateur"), not skills. Only values that normalize to
    a known technology token — the same vocabulary the requirements gate uses
    to detect principal skills (``src/requirements/principal_skill.py``) —
    are kept; everything else (task descriptions, generic phrases like
    "customer support") is dropped rather than presented as a "skill".

    Case variants of the same technology from different sources (e.g.
    "Docker" vs "docker") are merged into one facet, using whichever raw
    casing occurred most often in the source data as the display label —
    no manually maintained capitalization table.
    """
    counts: dict[str, int] = {}
    display_votes: dict[str, dict[str, int]] = {}
    for r in skill_rows:
        raw_name = r["name"]
        count = r["count"]
        norm = raw_name.strip().lower()
        if norm not in KNOWN_TECHNOLOGIES:
            continue
        counts[norm] = counts.get(norm, 0) + count
        votes = display_votes.setdefault(norm, {})
        votes[raw_name] = votes.get(raw_name, 0) + count

    skills: list[SkillFacet] = []
    tools: list[SkillFacet] = []
    for norm, count in counts.items():
        display_name = max(display_votes[norm].items(), key=lambda kv: kv[1])[0]
        facet = SkillFacet(name=display_name, count=count)
        (tools if norm in _TOOL_KEYWORDS else skills).append(facet)

    skills.sort(key=lambda f: f.count, reverse=True)
    tools.sort(key=lambda f: f.count, reverse=True)
    return skills, tools


def _normalize_rejection_detail(stage: str, detail: str) -> str:
    """The ``filter`` stage stores short reason codes directly (e.g.
    "seniority", "title_keywords"). The ``requirements_gate`` stage instead
    stores a JSON-encoded list of structured reasons (see
    ``rejection_reasons_to_detail`` in src/requirements/gate.py) — use the
    first reason's ``type`` so aggregation groups on a readable label instead
    of a raw JSON blob.
    """
    if stage != "requirements_gate":
        return detail
    try:
        reasons = json.loads(detail)
        if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict):
            return str(reasons[0].get("type", detail))
    except (json.JSONDecodeError, TypeError):
        pass
    return detail


class ApplicationStatusIn(BaseModel):
    application_status: str | None


_VALID_APPLICATION_STATUSES: frozenset[str | None] = frozenset({
    None, "to_apply", "applied", "waiting", "interview", "accepted", "rejected", "denied", "withdrawn",
})


# I18 outcome tracking: only event types a caller (the frontend) may report
# directly. application_status_changed is recorded server-side, from the
# existing PATCH /application-status handler below — never accepted here,
# since its from/to values must come from storage, not from client input.
_CLIENT_REPORTABLE_EVENT_TYPES: frozenset[str] = frozenset({
    OutcomeEventType.VIEWED.value, OutcomeEventType.APPLY_LINK_OPENED.value,
})


class OutcomeEventIn(BaseModel):
    event_type: str


class OutcomeEventOut(BaseModel):
    id: str
    job_id: str
    event_type: str
    occurred_at: datetime
    metadata: dict

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _get_storages(request: Request) -> tuple[Storage, RunStorage]:
    s = Storage(db_path=request.app.state.db_path)
    s.init_db()
    rs = RunStorage(db_path=request.app.state.db_path)
    rs.init_db()
    return s, rs


def _journal_feedback(request: Request, method: str, *args: object, **kwargs: object) -> None:
    """Best-effort mirror of an outcome/behaviour event into the append-only feedback journal.

    Never blocks or alters the request it observes (same contract as the outcome-event recording)."""
    try:
        from src.feedback.service import FeedbackService

        service = FeedbackService(
            request.app.state.db_path, str(Path(getattr(request.app.state, "config_dir", "config"))),
        )
        getattr(service, method)(*args, **kwargs)
    except Exception:  # noqa: BLE001
        logger.warning("Feedback journal recording failed (non-fatal): %s", method, exc_info=True)


def _decision_of_latest_run(request: Request, job_id: str, run_id: str | None) -> Any:
    """The journaled decision for ``job_id`` iff it belongs to ``run_id`` (the run the rest of the
    explanation reflects); ``None`` otherwise or on any journal problem (legacy path then applies)."""
    if run_id is None:
        return None
    try:
        from src.decisions.storage import DecisionStorage

        store = DecisionStorage(request.app.state.db_path)
        try:
            store.init_db()
            rec = store.get_decision_for_run(job_id, run_id)
        finally:
            store.close()
        return rec
    except Exception:  # noqa: BLE001
        logger.warning("Decision lookup for explanation failed (using heuristics)", exc_info=True)
        return None


def _rejection_code(decision: Any) -> str | None:
    """Raw reason code for a rejected decision: the first violated rule (``filter.seniority`` →
    ``seniority``; gate rules keep their id), else the deciding stage; ``None`` when not rejected."""
    if decision.outcome != "rejected":
        return None
    for e in decision.evidence:
        if e.state == "violated":
            rule = str(e.source.rule_id)
            return rule.split(".", 1)[1] if rule.startswith("filter.") else rule
    return str(decision.deciding_stage)


def _get_outcome_storage(request: Request) -> OutcomeEventStorage:
    os_ = OutcomeEventStorage(db_path=request.app.state.db_path)
    os_.init_db()
    return os_


@router.get("", response_model=list[JobDetailOut])
def list_jobs(
    request: Request,
    source: str | None = Query(default=None),
    country: str | None = Query(default=None),
    department: str | None = Query(default=None, description="French department code (e.g. '83' for Var). Only applies when country=France."),
    remote: bool | None = Query(default=None),
    q: str | None = Query(default=None, description="Full-text search"),
    min_tier: str | None = Query(
        default=None,
        description=(
            "Presentation-only visibility floor: only return jobs whose tier "
            "(from the canonical score_to_tier()) is at least this tier — "
            "'exceptional' > 'excellent' > 'strong' > 'relevant' > "
            "'low_priority'. Never filters/deletes anything from storage; "
            "omit (or pass 'low_priority') to see every scored job. Only "
            "applies when a completed pipeline run exists (no-op otherwise, "
            "since there is no run-scoped score to filter by)."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[JobDetailOut]:
    s, rs = _get_storages(request)
    try:
        # Scope to the latest completed run so the Jobs page reflects current
        # results rather than all historical accumulation.  If no run exists yet
        # (fresh install or CLI-only use), fall back to showing all stored jobs.
        latest_run = rs.get_latest_completed_run()
        latest_run_id = latest_run["id"] if latest_run else None

        run_job_ids: list[str] | None = None
        if latest_run_id:
            run_job_ids = rs.get_job_ids_for_run(latest_run_id)

        # Reduce the run's job-id scope by tier BEFORE any pagination happens
        # below, so every downstream branch (FTS intersect, Python-paginated
        # run-scoped listing) sees an already tier-filtered id set and a page
        # of `limit` results is actually full of matching jobs, not thinned
        # out after the fact. Purely a visibility filter — never touches the
        # jobs table or job_scores, and never changes any score/tier value.
        if min_tier and run_job_ids:
            from src.pipeline.scoring import score_to_tier, tier_meets_minimum

            run_scores = rs.get_latest_scores_for_jobs(run_job_ids)
            run_job_ids = [
                jid for jid in run_job_ids
                if tier_meets_minimum(
                    score_to_tier(round(float(run_scores.get(jid, {}).get("final_score") or 0.0) * 100)),
                    min_tier,
                )
            ]

        if q:
            # FTS prefix search: fetch up to 2000 matches then apply all
            # filters in Python so that source/country/remote/department
            # combine correctly with the text search.
            fts_matches = s.search_jobs(q, limit=2000)
            # Intersect with latest-run scope.
            if run_job_ids is not None:
                run_id_set = set(run_job_ids)
                fts_matches = [j for j in fts_matches if j.id in run_id_set]
            # Apply structural filters in Python (mirrors _get_jobs_filtered).
            if source is not None:
                fts_matches = [j for j in fts_matches if j.source == source]
            if country is not None:
                if country == "_worldwide":
                    fts_matches = [j for j in fts_matches if not j.countries]
                else:
                    c_lower = country.lower()
                    fts_matches = [
                        j for j in fts_matches
                        if any(c.lower() == c_lower for c in j.countries)
                    ]
            if remote is not None:
                fts_matches = [j for j in fts_matches if j.remote == remote]
            # Department filter (Python extraction — no SQL column).
            if department is not None:
                from src.pipeline.departments import extract_department_code
                fts_matches = [
                    j for j in fts_matches
                    if extract_department_code(j.location or "", j.countries) == department
                ]
            # Honour pagination on the fully-filtered set.
            jobs = fts_matches[offset: offset + limit]
        else:
            # When a run is available, _get_jobs_filtered returns ALL matching
            # jobs in score order (no SQL pagination), so we can apply
            # structural filters and then paginate in Python while preserving
            # the global score ordering.  Without a run we fall back to
            # discovered_at ordering with SQL pagination.
            if run_job_ids is not None:
                # Fetch ALL jobs for this run in score order; apply structural
                # filters + department extraction; paginate in Python.
                raw = _get_jobs_filtered(
                    s,
                    source=source,
                    country=country,
                    remote=remote,
                    limit=None,   # no SQL pagination — score order preserved
                    offset=0,
                    job_ids=run_job_ids,
                )
                if department is not None:
                    from src.pipeline.departments import extract_department_code
                    raw = [
                        j for j in raw
                        if extract_department_code(j.location or "", j.countries) == department
                    ]
                jobs = raw[offset: offset + limit]
            else:
                # No completed run — use SQL pagination with discovered_at order.
                if department is not None:
                    raw = _get_jobs_filtered(
                        s, source=source, country=country, remote=remote,
                        limit=5000, offset=0,
                    )
                    from src.pipeline.departments import extract_department_code
                    filtered = [
                        j for j in raw
                        if extract_department_code(j.location or "", j.countries) == department
                    ]
                    jobs = filtered[offset: offset + limit]
                else:
                    jobs = _get_jobs_filtered(
                        s, source=source, country=country, remote=remote,
                        limit=limit, offset=offset,
                    )

        # Batch-fetch scores in one query instead of N individual lookups.
        job_id_list = [job.id for job in jobs]
        scores_by_id = rs.get_latest_scores_for_jobs(job_id_list) if latest_run_id and job_id_list else {}

        result: list[JobDetailOut] = []
        for job in jobs:
            job_out = _job_to_job_detail_out(job, scores_by_id.get(job.id))
            result.append(job_out)

        # When a text search is active, preserve FTS BM25 relevance order.
        # Without a search query and with run data, the result is already in
        # global score order from the score-sorted job_ids list; sort only as
        # a fallback when no run data was available.
        if not q and run_job_ids is None:
            result.sort(
                key=lambda j: (j.score.final_score if j.score and j.score.final_score is not None else -1),
                reverse=True,
            )
        return result
    finally:
        s.close()
        rs.close()


@router.get("/countries", response_model=CountriesOut)
def get_job_countries(request: Request) -> CountriesOut:
    """Return job counts grouped by country, scoped to the latest completed run.

    The response includes:
    - ``total``: true count of all scored jobs in the latest run (may exceed
      the sum of facets because jobs with no country are not bucketed).
    - ``facets``: per-country counts, plus a synthetic ``_worldwide`` entry for
      jobs that carry no country (remote-only listings from global job boards).
    """
    s, rs = _get_storages(request)
    try:
        latest_run = rs.get_latest_completed_run()
        latest_run_id = latest_run["id"] if latest_run else None
        run_job_ids: list[str] | None = None
        if latest_run_id:
            run_job_ids = rs.get_job_ids_for_run(latest_run_id)

        conn = s._get_conn()  # noqa: SLF001
        if run_job_ids is not None:
            if not run_job_ids:
                return CountriesOut(total=0, facets=[])
            placeholders = ",".join("?" * len(run_job_ids))
            rows = conn.execute(
                f"""SELECT value AS name, COUNT(*) AS count
                    FROM jobs, json_each(jobs.countries)
                    WHERE jobs.id IN ({placeholders})
                    GROUP BY value ORDER BY count DESC""",
                run_job_ids,
            ).fetchall()
            no_country = conn.execute(
                f"""SELECT COUNT(*) FROM jobs
                    WHERE id IN ({placeholders})
                      AND (countries = '[]' OR countries IS NULL)""",
                run_job_ids,
            ).fetchone()[0]
            total = len(run_job_ids)
        else:
            rows = conn.execute(
                """SELECT value AS name, COUNT(*) AS count
                   FROM jobs, json_each(jobs.countries)
                   GROUP BY value ORDER BY count DESC"""
            ).fetchall()
            no_country = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE countries = '[]' OR countries IS NULL"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        facets = [CountryFacet(name=r["name"], count=r["count"]) for r in rows]
        if no_country > 0:
            facets.append(CountryFacet(name="_worldwide", count=no_country))
        return CountriesOut(total=total, facets=facets)
    finally:
        s.close()
        rs.close()


@router.get("/stats", response_model=JobStatsOut)
def get_job_stats(request: Request) -> JobStatsOut:
    """Real, DB-derived stats for the Stats page. See compute_job_stats()."""
    s, rs = _get_storages(request)
    try:
        return compute_job_stats(s, rs)
    finally:
        s.close()
        rs.close()


def compute_job_stats(s: Storage, rs: RunStorage) -> JobStatsOut:
    """Real, DB-derived stats for GET /api/jobs/stats, computed in exactly
    one place.

    Skills/tools frequency, score-tier distribution and rejection reasons are
    scoped to the latest completed run (same scoping as GET /countries).
    Title categories bucket real job titles from that run against the
    pipeline's own configured Indeed query groups (config/sources.yaml) —
    no invented numbers. Skill/tool frequency is filtered through
    ``_aggregate_tech_facets`` so source-specific noise in ``jobs.skills``
    (e.g. France Travail's ROME task/activity descriptions) never shows up
    as a "skill".
    """
    latest_run = rs.get_latest_completed_run()
    latest_run_id = latest_run["id"] if latest_run else None
    run_job_ids: list[str] | None = None
    if latest_run_id:
        run_job_ids = rs.get_job_ids_for_run(latest_run_id)

    if run_job_ids is not None and not run_job_ids:
        return JobStatsOut(total_jobs=0)

    conn = s._get_conn()  # noqa: SLF001

    if run_job_ids is not None:
        placeholders = ",".join("?" * len(run_job_ids))
        skill_rows = conn.execute(
            f"""SELECT value AS name, COUNT(*) AS count
                FROM jobs, json_each(jobs.skills)
                WHERE jobs.id IN ({placeholders})
                GROUP BY value ORDER BY count DESC""",
            run_job_ids,
        ).fetchall()
        title_rows = conn.execute(
            f"SELECT title FROM jobs WHERE id IN ({placeholders})", run_job_ids
        ).fetchall()
        total_jobs = len(run_job_ids)
    else:
        skill_rows = conn.execute(
            """SELECT value AS name, COUNT(*) AS count
               FROM jobs, json_each(jobs.skills)
               GROUP BY value ORDER BY count DESC"""
        ).fetchall()
        title_rows = conn.execute("SELECT title FROM jobs").fetchall()
        total_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    skills, tools = _aggregate_tech_facets(skill_rows)

    score_tiers: list[ScoreTierFacet] = []
    rejections: list[RejectionFacet] = []
    if latest_run_id:
        # Deferred: src.pipeline's package __init__ eagerly imports
        # sentence-transformers/torch.
        from src.pipeline.scoring import score_to_tier

        score_rows = conn.execute(
            "SELECT final_score FROM job_scores WHERE run_id = ? AND final_score IS NOT NULL",
            (latest_run_id,),
        ).fetchall()
        tier_counts: dict[str, int] = {}
        for r in score_rows:
            score_100 = round((r["final_score"] or 0) * 100)
            tier = score_to_tier(score_100)
            tier_counts[tier] = tier_counts.get(tier, 0) + 1
        score_tiers = [ScoreTierFacet(tier=t, count=c) for t, c in tier_counts.items()]

        rej_rows = conn.execute(
            """SELECT rejection_stage, COALESCE(rejection_detail, 'unknown') AS detail
               FROM job_rejections WHERE run_id = ?""",
            (latest_run_id,),
        ).fetchall()
        rejection_counts: dict[tuple[str, str], int] = {}
        for r in rej_rows:
            key = (r["rejection_stage"], _normalize_rejection_detail(r["rejection_stage"], r["detail"]))
            rejection_counts[key] = rejection_counts.get(key, 0) + 1
        rejections = sorted(
            (RejectionFacet(stage=s, detail=d, count=c) for (s, d), c in rejection_counts.items()),
            key=lambda f: f.count,
            reverse=True,
        )

    category_counts: dict[str, int] = {name: 0 for name, _ in _TITLE_CATEGORIES}
    for row in title_rows:
        title = f" {(row['title'] or '').lower()} "
        for name, keywords in _TITLE_CATEGORIES:
            if any(kw in title for kw in keywords):
                category_counts[name] += 1
                break
    title_categories = sorted(
        (TitleCategoryFacet(name=name, count=count) for name, count in category_counts.items() if count > 0),
        key=lambda f: f.count,
        reverse=True,
    )

    return JobStatsOut(
        total_jobs=total_jobs,
        skills=skills[:20],
        tools=tools[:20],
        score_tiers=score_tiers,
        rejections=rejections[:10],
        title_categories=title_categories,
    )


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(job_id: str, request: Request) -> JobDetailOut:
    s, rs = _get_storages(request)
    try:
        job = s.get_job_by_id(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        score_row = rs.get_job_latest_score(job_id)
        out = _job_to_job_detail_out(job, score_row)
        try:
            out.provenance = [ProvenanceOut(**p) for p in s.get_provenance([job_id]).get(job_id, [])]
        except Exception as exc:  # noqa: BLE001 — the offer is shown even without its sightings
            logger.warning("Provenance unavailable for %s: %s", job_id, exc)
        return out
    finally:
        s.close()
        rs.close()


@router.get("/{job_id}/explanation", response_model=JobExplanationOut)
def get_job_explanation(job_id: str, request: Request) -> JobExplanationOut:
    """Return an explainability summary for a job.

    For accepted jobs: human-readable positive reasons and concerns.
    For rejected jobs: the specific reason(s) the job was rejected.

    The backend is the authority for all explanations; no filtering or
    scoring logic is duplicated in the frontend.
    """
    s, rs = _get_storages(request)
    try:
        job = s.get_job_by_id(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")

        # Determine filter result from the latest run's rejection records.
        latest_run = rs.get_latest_completed_run()
        rejection_detail: str | None = None
        filter_result = "passed"

        if latest_run:
            run_id = latest_run["id"]
            rejection = rs.get_rejection_for_job(run_id, job_id)
            if rejection:
                filter_result = "rejected"
                rejection_detail = rejection.get("rejection_detail")

        from src.api.routes.pipeline import _build_score_breakdown_out

        score_row = rs.get_job_latest_score(job_id)
        score_out = _build_score_breakdown_out(score_row)
        overall_score: float | None = score_out.score_100 if score_out else None

        # Load profile + domain keywords to generate human-readable reasons.
        config_dir = Path(getattr(request.app.state, "config_dir", "config"))
        profile = _load_profile_safe(config_dir)
        domain_keywords = _load_domain_keywords_safe(config_dir, latest_run)
        score_factors = score_out.factors if score_out else None

        from src.pipeline.explain import ExplanationReason as _ER
        from src.pipeline.explain import explain_from_decision, generate_explanation

        # One canonical decision, several representations: when the journal holds the decision of the
        # SAME run the page reflects, render the explanation from it; otherwise (job predates the
        # journal, journal off, older run) fall back to the legacy heuristics.
        decision = _decision_of_latest_run(request, job_id, latest_run["id"] if latest_run else None)
        if decision is not None:
            expl = explain_from_decision(decision)
            filter_result = "rejected" if decision.outcome == "rejected" else "passed"
            # keep the raw reason code consistent with the decision (not with the legacy rejection row)
            rejection_detail = _rejection_code(decision)
        else:
            expl = generate_explanation(
                job=job,
                profile=profile,
                filter_result=filter_result,
                rejection_detail=rejection_detail,
                domain_keywords=domain_keywords,
                score_factors=score_factors,
            )

        def _to_schema(r: _ER) -> ExplanationReason:
            return ExplanationReason(emoji=r.emoji, text=r.text, kind=r.kind)

        personalization_signals, personalization_adjustment = _load_personalization_explanation_safe(
            job, request, config_dir
        )

        return JobExplanationOut(
            job_id=job_id,
            overall_score=overall_score,
            score=score_out,
            filter_result=filter_result,
            rejection_reason=rejection_detail,
            positive_reasons=[_to_schema(r) for r in expl.positive_reasons],
            concerns=[_to_schema(r) for r in expl.concerns],
            rejection_reasons=[_to_schema(r) for r in expl.rejection_reasons],
            personalization_signals=personalization_signals,
            personalization_adjustment=personalization_adjustment,
            explanation_source="decision" if decision is not None else "heuristic",
            decision_id=decision.decision_id if decision is not None else None,
        )
    finally:
        s.close()
        rs.close()


@router.patch("/{job_id}/application-status", response_model=JobDetailOut)
def update_application_status(
    job_id: str,
    body: ApplicationStatusIn,
    request: Request,
) -> JobDetailOut:
    """Persist the user's application status for a job.

    This field is independent of the pipeline — reruns and deduplication
    never touch it.  Valid values: null (clear), to_apply, applied,
    waiting, interview, accepted, rejected, denied, withdrawn.
    """
    if body.application_status not in _VALID_APPLICATION_STATUSES:
        raise HTTPException(status_code=422, detail=f"Invalid application status: {body.application_status!r}")
    s, rs = _get_storages(request)
    try:
        job = s.get_job_by_id(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        previous_status = job.application_status
        s.update_application_status(job_id, body.application_status)
        job = s.get_job_by_id(job_id)
        score_row = rs.get_job_latest_score(job_id)

        # I18: record an outcome event only when the status actually changed
        # (idempotency — re-sending the same status is not a new outcome).
        # Best-effort/non-fatal: outcome tracking must never block the
        # application-status update it observes.
        if previous_status != body.application_status:
            outcome_event_id: str | None = None
            try:
                outcome_storage = _get_outcome_storage(request)
                try:
                    outcome_event = outcome_storage.record_event(
                        job_id,
                        OutcomeEventType.APPLICATION_STATUS_CHANGED,
                        metadata={"from": previous_status, "to": body.application_status},
                    )
                    outcome_event_id = outcome_event.id
                finally:
                    outcome_storage.close()
            except Exception:  # noqa: BLE001
                logger.warning("Outcome event recording failed (non-fatal) for job %s", job_id)
            _journal_feedback(
                request, "record_status_change", job_id, previous_status, body.application_status,
                outcome_event_id=outcome_event_id,
            )

        return _job_to_job_detail_out(job, score_row)
    finally:
        s.close()
        rs.close()


@router.post("/{job_id}/events", response_model=OutcomeEventOut, status_code=201)
def record_outcome_event(job_id: str, body: OutcomeEventIn, request: Request) -> OutcomeEventOut:
    """I18: record an explicit, client-reported outcome event for a job.

    Only "viewed" and "apply_link_opened" may be reported this way —
    "application_status_changed" is derived server-side from the
    application-status endpoint above, never accepted as client input.
    """
    if body.event_type not in _CLIENT_REPORTABLE_EVENT_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid event type: {body.event_type!r}")
    s, rs = _get_storages(request)
    try:
        job = s.get_job_by_id(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
    finally:
        s.close()
        rs.close()

    outcome_storage = _get_outcome_storage(request)
    try:
        event = outcome_storage.record_event(job_id, OutcomeEventType(body.event_type))
    finally:
        outcome_storage.close()
    _journal_feedback(request, "record_behavior", job_id, body.event_type)
    return OutcomeEventOut(
        id=event.id,
        job_id=event.job_id,
        event_type=event.event_type.value,
        occurred_at=event.occurred_at,
        metadata=event.metadata,
    )


@router.get("/{job_id}/events", response_model=list[OutcomeEventOut])
def list_outcome_events(job_id: str, request: Request) -> list[OutcomeEventOut]:
    """I18: retrieve recorded outcome events for a job, newest first."""
    s, rs = _get_storages(request)
    try:
        job = s.get_job_by_id(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
    finally:
        s.close()
        rs.close()

    outcome_storage = _get_outcome_storage(request)
    try:
        events = outcome_storage.get_events_for_job(job_id)
    finally:
        outcome_storage.close()
    return [
        OutcomeEventOut(
            id=e.id, job_id=e.job_id, event_type=e.event_type.value,
            occurred_at=e.occurred_at, metadata=e.metadata,
        )
        for e in events
    ]


def _load_profile_safe(config_dir: Path) -> "JobSearchProfile":
    """Load user profile, returning an empty default profile on any error."""
    from src.models.profile import JobSearchProfile
    try:
        from src.config_files import resolve_config_path
        from src.pipeline.filter import load_profile
        return load_profile(str(resolve_config_path(config_dir, "profile.yaml")))
    except Exception:
        return JobSearchProfile()


def _load_domain_keywords_safe(config_dir: Path, run: dict | None = None) -> dict:
    """Load the domain keyword vocabulary of the strategy ``run`` used, returning empty dict on any error.

    The vocabulary comes from the ``filters.yaml`` that run filtered with — the approved bundle when it ran
    one (``stats.active_strategy``), otherwise ``config/`` — so the explanation describes what decided.
    """
    try:
        from src.decisions.active_strategy import strategy_of_run
        from src.pipeline.filter import load_filters
        recorded = ((run or {}).get("stats") or {}).get("active_strategy")
        engine = load_filters(str(strategy_of_run(recorded, config_dir).path("filters.yaml")))
        domain_keywords: dict = engine.get("domain_keywords", {})
        return domain_keywords
    except Exception:
        return {}


def _load_personalization_explanation_safe(
    job: "Job", request: Request, config_dir: Path
) -> tuple[list["ExplanationSignal"], float]:
    """Compute this job's real personalization adjustment/signals, if any.

    Reuses the same deterministic scoring function the pipeline itself uses
    (``score_job_personalization``) so the explanation can never diverge
    from — or fabricate — what personalization actually did. Returns
    ``([], 0.0)`` on any error (e.g. no feedback recorded yet, DB not
    initialized), matching the "never invent a signal" rule.
    """
    try:
        from src.personalization.config import load_personalization_config
        from src.personalization.engine import build_preference_profile, score_job_personalization
        from src.personalization.storage import PersonalizationStorage

        pers_storage = PersonalizationStorage(db_path=request.app.state.db_path)
        try:
            pers_storage.init_db()
            pers_cfg = load_personalization_config(str(config_dir / "personalization.yaml"))
            feedback_records = pers_storage.get_all_feedback()
            version = pers_storage.get_feedback_version()
            profile = build_preference_profile(feedback_records, pers_cfg, version)
            _, pers_explanation = score_job_personalization(job, profile, pers_cfg)
        finally:
            pers_storage.close()
    except Exception:
        return [], 0.0

    signals = [
        ExplanationSignal(
            signal_type=m.signal_type,
            value=m.value,
            contribution=m.contribution,
            feedback_count=m.feedback_count,
        )
        for m in pers_explanation.matches
    ]
    return signals, pers_explanation.clamped_adjustment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_jobs_filtered(
    storage: Storage,
    source: str | None = None,
    country: str | None = None,
    remote: bool | None = None,
    limit: int | None = 50,
    offset: int = 0,
    job_ids: list[str] | None = None,
) -> list:
    """Filtered job query via SQLite. Builds a WHERE clause from the params.

    ``job_ids``: when provided, restricts results to jobs whose id is in this
    list (used to scope the listing to the latest pipeline run).  When
    job_ids is supplied, ``limit`` is ignored and SQL pagination is NOT
    applied — the caller must paginate in Python AFTER structural filtering
    so that global score ordering is preserved across pages.

    ``limit``: only used when ``job_ids`` is None (fallback path without a
    completed pipeline run).  Set to None to fetch without a SQL limit.
    """
    if job_ids is not None and not job_ids:
        return []

    conn = storage._get_conn()  # noqa: SLF001  # accessing internal for direct SQL
    conditions: list[str] = []
    params: list = []

    if job_ids is not None:
        placeholders = ",".join("?" * len(job_ids))
        conditions.append(f"id IN ({placeholders})")
        params.extend(job_ids)
    if source is not None:
        conditions.append("source = ?")
        params.append(source)
    if country is not None:
        if country == "_worldwide":
            conditions.append("(countries = '[]' OR countries IS NULL)")
        else:
            conditions.append("LOWER(countries) LIKE ?")
            params.append(f'%"{country.lower()}"%')
    if remote is not None:
        conditions.append("remote = ?")
        params.append(1 if remote else 0)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    from src.models.job import Job

    if job_ids is not None:
        # Fetch ALL matching rows without pagination; the caller applies
        # offset/limit in Python after re-ordering to score order.
        sql = f"SELECT * FROM jobs {where}"
        rows = conn.execute(sql, params).fetchall()
        jobs = [Job.from_dict(dict(r)) for r in rows]
        # Restore the score order from job_ids (which is final_score DESC).
        order = {jid: i for i, jid in enumerate(job_ids)}
        jobs.sort(key=lambda j: order.get(j.id, len(job_ids)))
        return jobs
    else:
        # No run scope — fall back to discovered_at ordering with SQL pagination.
        sql = f"SELECT * FROM jobs {where} ORDER BY discovered_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
        return [Job.from_dict(dict(r)) for r in rows]


def _job_to_job_detail_out(job: object, score_row: dict | None) -> JobDetailOut:
    from src.api.routes.pipeline import _build_score_breakdown_out, _resolve_department
    from src.models.job import Job as JobModel
    j: JobModel = job  # type: ignore[assignment]

    score_out = _build_score_breakdown_out(score_row)

    return JobDetailOut(
        id=j.id,
        source=j.source,
        title=j.title,
        company=j.company,
        location=j.location,
        department=_resolve_department(j),
        countries=j.countries,
        remote=j.remote,
        work_mode=j.work_mode,
        employment_type=j.employment_type,
        salary_min=j.salary_min,
        salary_max=j.salary_max,
        salary_currency=j.salary_currency,
        published_at=j.published_at,
        discovered_at=j.discovered_at,
        skills=j.skills,
        canonical_url=j.canonical_url,
        apply_url=j.apply_url,
        status=j.status,
        application_status=j.application_status,
        score=score_out,
        description=j.description,
    )
