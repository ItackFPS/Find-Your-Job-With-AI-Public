"""Jobs API routes.

GET /api/jobs                   — list stored jobs with optional filters
GET /api/jobs/{id}              — single job detail
GET /api/jobs/{id}/explanation  — why the job matched / was rejected
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request

from pydantic import BaseModel, StrictBool

from src.api.schemas import ExplanationReason, ExplanationSignal, JobDetailOut, JobExplanationOut, ProvenanceOut
from src.models.job import Job
from src.outcomes.models import OutcomeEventType
from src.outcomes.storage import OutcomeEventStorage
from src.requirements.principal_skill import MAX_SKILL_CHARS, MAX_SKILL_WORDS, is_skill_label
from src.storage.runs import RunStorage
from src.storage.sqlite import Storage

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.discovery.presets import DiscoveryPreset
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


# Stats page groupings come from the line-of-work presets that apply to the profile
# (config/presets/*.yaml, ``stats`` section): the "tools" split of the skills facet, the
# title buckets, and whether skills are limited to known technology names. A profile
# outside every preset gets neutral stats: skills as collected (minus sentence-like
# source noise) and no title buckets. Display only: nothing here affects matching or scoring.
_MAX_SKILL_WORDS = MAX_SKILL_WORDS
_MAX_SKILL_CHARS = MAX_SKILL_CHARS


def _aggregate_tech_facets(
    skill_rows: list,
    presets: "Sequence[DiscoveryPreset]" = (),
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

    With no applicable preset (a profile outside IT) there is no technology list: values
    are kept as collected, except sentence-like ones (more than ``_MAX_SKILL_WORDS``
    words or ``_MAX_SKILL_CHARS`` characters), which are the source noise described above.
    """
    technologies_only = any(p.technologies_only for p in presets)
    tool_keywords = frozenset().union(*(p.tool_keywords for p in presets)) if presets else frozenset()
    counts: dict[str, int] = {}
    display_votes: dict[str, dict[str, int]] = {}
    for r in skill_rows:
        raw_name = r["name"]
        count = r["count"]
        norm = raw_name.strip().lower()
        if not is_skill_label(norm, technologies_only=technologies_only):
            continue
        counts[norm] = counts.get(norm, 0) + count
        votes = display_votes.setdefault(norm, {})
        votes[raw_name] = votes.get(raw_name, 0) + count

    skills: list[SkillFacet] = []
    tools: list[SkillFacet] = []
    for norm, count in counts.items():
        display_name = max(display_votes[norm].items(), key=lambda kv: kv[1])[0]
        facet = SkillFacet(name=display_name, count=count)
        (tools if norm in tool_keywords else skills).append(facet)

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
    include_expired: bool = Query(
        default=False, description="Also list offers marked expired (issue #86); hidden by default.",
    ),
    include_hidden: bool = Query(
        default=False, description="Also list offers the user hides (ADR 0017); left out by default.",
    ),
    saved_only: bool = Query(default=False, description="Only the offers the user saved, whatever the run."),
    tracked_only: bool = Query(
        default=False, description="Only the offers in the application tracker, whatever the run (issue #253).",
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[JobDetailOut]:
    # Saved and tracked offers stay reachable whatever happened to them (issue #253): expired ones
    # are listed in those views, with their status.
    personal_view = saved_only or tracked_only
    include_expired = include_expired or personal_view
    off_sources = _disabled_sources(request)
    s, rs = _get_storages(request)
    try:
        # Scope to the latest completed run so the Jobs page reflects current
        # results rather than all historical accumulation.  If no run exists yet
        # (fresh install or CLI-only use), fall back to showing all stored jobs.
        latest_run = rs.get_latest_visible_run()
        latest_run_id = latest_run["id"] if latest_run else None

        run_job_ids: list[str] | None = None
        if latest_run_id:
            run_job_ids = rs.get_job_ids_for_run(latest_run_id)
            if not q and not personal_view:
                # Découverte (S4) offers have their own section; the main list never duplicates them.
                from src.learning.exploration import drawn_for_run

                s4_ids = {d["job_id"] for d in drawn_for_run(request.app.state.db_path, latest_run_id)}
                run_job_ids = [j for j in run_job_ids if j not in s4_ids]
                # Weak-evidence offers (ADR 0019 v5 AD-2b) never enter the main list: the best of
                # them have their own collapsed lane (/outside-criteria), the rest stays unshown.
                from src.pipeline.lanes import has_weak_flags

                weak_ids = {r["job_id"] for r in rs.get_jobs_for_run(latest_run_id, limit=-1)
                            if has_weak_flags(r.get("score_breakdown"))}
                if weak_ids:
                    run_job_ids = [j for j in run_job_ids if j not in weak_ids]
            # A source turned back on since the visible run shows its offers at once (#253): the
            # run did not evaluate them because the source was off, so they are restored from
            # their last completed score until the next run evaluates them again.
            restored = _restored_ids(s, rs, latest_run, run_job_ids, off_sources)
            if restored:
                run_job_ids = _merge_by_score(rs, latest_run_id, run_job_ids, restored)

        # Reduce the run's job-id scope by tier BEFORE any pagination happens
        # below, so every downstream branch (FTS intersect, Python-paginated
        # run-scoped listing) sees an already tier-filtered id set and a page
        # of `limit` results is actually full of matching jobs, not thinned
        # out after the fact. Purely a visibility filter — never touches the
        # jobs table or job_scores, and never changes any score/tier value.
        if min_tier and run_job_ids:
            from src.pipeline.scoring import score_to_tier, tier_meets_minimum

            run_scores = rs.get_scores_for_list(run_job_ids, latest_run_id)
            run_job_ids = [
                jid for jid in run_job_ids
                if tier_meets_minimum(
                    score_to_tier(round(float(run_scores.get(jid, {}).get("final_score") or 0.0) * 100)),
                    min_tier,
                )
            ]


        # Save / Hide (ADR 0016, ADR 0017). A hidden offer leaves every list with its exact
        # duplicates (same content hash); the saved view lists saved offers from any run.
        list_state = _list_state(request)
        hidden_ids = {j for j, st in list_state.items() if st["hidden"]}
        if personal_view:
            wanted: set[str] | None = None
            if saved_only:
                wanted = {j for j, st in list_state.items() if st["saved"]}
            if tracked_only:
                tracked = _tracked_ids(s)
                wanted = tracked if wanted is None else wanted & tracked
            wanted = wanted or set()
            in_run = [j for j in (run_job_ids or []) if j in wanted]
            run_job_ids = in_run + sorted(wanted - set(in_run))
        exclude_hashes = set() if include_hidden else s.get_content_hashes(sorted(hidden_ids))
        exclude_ids = set() if include_hidden else set(hidden_ids)
        exclude_ids |= _switched_off_ids(request, s, list_state, off_sources)

        if q:
            # FTS prefix search: fetch up to 2000 matches then apply all
            # filters in Python so that source/country/remote/department
            # combine correctly with the text search.
            fts_matches = s.search_jobs(q, limit=2000)
            fts_matches = [j for j in fts_matches
                           if j.id not in exclude_ids and j.content_hash not in exclude_hashes]
            # Intersect with latest-run scope.
            if run_job_ids is not None:
                run_id_set = set(run_job_ids)
                fts_matches = [j for j in fts_matches if j.id in run_id_set]
            # Apply structural filters in Python (mirrors _get_jobs_filtered).
            if not include_expired:
                fts_matches = [j for j in fts_matches if j.status != "expired"]
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
                    include_expired=include_expired,
                    exclude_ids=exclude_ids,
                    exclude_hashes=exclude_hashes,
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
                        limit=5000, offset=0, include_expired=include_expired,
                        exclude_ids=exclude_ids, exclude_hashes=exclude_hashes,
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
                        limit=limit, offset=offset, include_expired=include_expired,
                        exclude_ids=exclude_ids, exclude_hashes=exclude_hashes,
                    )

        # Batch-fetch scores in one query instead of N individual lookups.
        job_id_list = [job.id for job in jobs]
        scores_by_id = rs.get_scores_for_list(job_id_list, latest_run_id) if latest_run_id and job_id_list else {}

        result: list[JobDetailOut] = []
        for job in jobs:
            job_out = _job_to_job_detail_out(job, scores_by_id.get(job.id))
            st = list_state.get(job.id)
            if st:
                job_out.saved, job_out.hidden = st["saved"], st["hidden"]
            job_out.source_off = job.source in off_sources
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


@router.get("/decouverte", response_model=list[JobDetailOut])
def list_decouverte(request: Request) -> list[JobDetailOut]:
    """The Découverte (S4) slots of the latest completed run, in slot order (ADR 0019).

    A uniform random draw independent of every ranking: no score floor, so some offers are poor
    matches by design. Hidden offers are left out."""
    from src.learning.exploration import drawn_for_run

    s, rs = _get_storages(request)
    try:
        latest_run = rs.get_latest_visible_run()
        if not latest_run:
            return []
        ids = [d["job_id"] for d in drawn_for_run(request.app.state.db_path, latest_run["id"])]
        list_state = _list_state(request)
        off = _switched_off_ids(request, s, list_state)
        ids = [j for j in ids if not list_state.get(j, {}).get("hidden") and j not in off]
        scores_by_id = rs.get_scores_for_list(ids, latest_run["id"]) if ids else {}
        jobs_by_id = s.get_jobs_by_ids(ids) if ids else {}
        result: list[JobDetailOut] = []
        for job_id in ids:
            job = jobs_by_id.get(job_id)
            if job is None:
                continue
            job_out = _job_to_job_detail_out(job, scores_by_id.get(job_id))
            st = list_state.get(job_id)
            if st:
                job_out.saved, job_out.hidden = st["saved"], st["hidden"]
            result.append(job_out)
        return result
    finally:
        s.close()
        rs.close()


@router.get("/outside-criteria", response_model=list[JobDetailOut])
def list_outside_criteria(request: Request) -> list[JobDetailOut]:
    """The "outside your criteria" lane of the latest completed run (ADR 0019 v5 AD-2b; L5.3).

    Offers that reached scoring with a WEAK hard-filter flag, final score and domain relevance
    above the lane floors, best first, at most ``outside_lane_size``. Each carries its
    ``score.weak_flags`` (the criterion it misses). Hidden offers are left out. Empty while every
    check is in ``reject`` mode."""
    from src.pipeline.lanes import OUTSIDE, assign_lanes, load_lane_params

    s, rs = _get_storages(request)
    try:
        latest_run = rs.get_latest_visible_run()
        if not latest_run:
            return []
        rows = rs.get_jobs_for_run(latest_run["id"], limit=-1)        # -1: every row of the run
        config_dir = Path(getattr(request.app.state, "config_dir", "config"))
        lanes = assign_lanes(((r["job_id"], float(r.get("final_score") or 0.0), r.get("score_breakdown"))
                              for r in rows), load_lane_params(config_dir / "scoring.yaml"))
        list_state = _list_state(request)
        off = _switched_off_ids(request, s, list_state)
        score = {r["job_id"]: float(r.get("final_score") or 0.0) for r in rows}
        ids = sorted((j for j, lane in lanes.items() if lane == OUTSIDE
                      and not list_state.get(j, {}).get("hidden") and j not in off),
                     key=lambda j: (-score[j], j))
        scores_by_id = rs.get_scores_for_list(ids, latest_run["id"]) if ids else {}
        jobs_by_id = s.get_jobs_by_ids(ids) if ids else {}
        result: list[JobDetailOut] = []
        for job_id in ids:
            job = jobs_by_id.get(job_id)
            if job is None:
                continue
            job_out = _job_to_job_detail_out(job, scores_by_id.get(job_id))
            st = list_state.get(job_id)
            if st:
                job_out.saved, job_out.hidden = st["saved"], st["hidden"]
            result.append(job_out)
        return result
    finally:
        s.close()
        rs.close()


class JobCountOut(BaseModel):
    count: int


@router.get("/count", response_model=JobCountOut)
def count_jobs(
    request: Request,
    source: str | None = Query(default=None),
    country: str | None = Query(default=None),
    department: str | None = Query(default=None),
    remote: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    min_tier: str | None = Query(default=None),
    include_expired: bool = Query(default=False),
    include_hidden: bool = Query(default=False),
    saved_only: bool = Query(default=False),
    tracked_only: bool = Query(default=False),
) -> JobCountOut:
    """How many offers ``GET /api/jobs`` lists for the same filters, all pages together (Phase B
    B7-C, P-017): the number a headline may announce is the number the list shows."""
    return JobCountOut(count=len(_all_listed(
        request, source=source, country=country, department=department, remote=remote, q=q, min_tier=min_tier,
        include_expired=include_expired, include_hidden=include_hidden, saved_only=saved_only,
        tracked_only=tracked_only)))


def _all_listed(request: Request, **filters: Any) -> list[JobDetailOut]:
    """Every offer ``GET /api/jobs`` lists for ``filters``, all pages together, in list order: the
    one set that counts, facets and status are computed from (issue #253)."""
    params: dict[str, Any] = {"source": None, "country": None, "department": None, "remote": None, "q": None,
                              "min_tier": None, "include_expired": False, "include_hidden": False,
                              "saved_only": False, "tracked_only": False, **filters}
    out: list[JobDetailOut] = []
    offset, page = 0, 500
    while True:
        rows = list_jobs(request, **params, limit=page, offset=offset)
        out.extend(rows)
        if len(rows) < page:
            return out
        offset += page


@router.get("/countries", response_model=CountriesOut)
def get_job_countries(
    request: Request,
    source: str | None = Query(default=None),
    remote: bool | None = Query(default=None),
    q: str | None = Query(default=None),
    min_tier: str | None = Query(default=None),
    include_expired: bool = Query(default=False),
    saved_only: bool = Query(default=False),
    tracked_only: bool = Query(default=False),
) -> CountriesOut:
    """Offer counts per country over exactly the offers ``GET /api/jobs`` lists for the same
    filters, the country filter aside (issue #253: before, the total counted every offer of the run,
    Découverte, hidden and switched-off ones included, so it did not match the list).

    - ``total``: how many offers the list holds (may exceed the sum of facets: an offer with
      several countries counts in each, one with none in ``_worldwide``).
    - ``facets``: per-country counts, plus ``_worldwide`` for offers that carry no country.
    """
    rows = _all_listed(request, source=source, remote=remote, q=q, min_tier=min_tier,
                       include_expired=include_expired, saved_only=saved_only, tracked_only=tracked_only)
    counts: dict[str, int] = {}
    no_country = 0
    for job in rows:
        if not job.countries:
            no_country += 1
        for c in dict.fromkeys(job.countries):
            counts[c] = counts.get(c, 0) + 1
    facets = [CountryFacet(name=n, count=c) for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    if no_country > 0:
        facets.append(CountryFacet(name="_worldwide", count=no_country))
    return CountriesOut(total=len(rows), facets=facets)


class ListStatusOut(BaseModel):
    """Why the main list holds what it holds (issue #253). Counts, never offers."""
    visible_run_id: str | None = None
    visible_run_completed_at: str | None = None
    latest_run_id: str | None = None
    latest_run_status: str | None = None
    latest_run_outcome: str | None = None
    # The latest run did not replace the list (it failed, or no source succeeded in it).
    previous_list_kept: bool = False
    # Sources of the latest completed run that are incomplete, with the reason (failed, partial,
    # network_outage, suspect_empty), and the stored offers the visible run carried, per source.
    incomplete_sources: dict[str, str] = {}
    carried_by_source: dict[str, int] = {}
    # Offers of a source turned back on since the visible run, shown from their last score until
    # the next run evaluates them again.
    restored_by_source: dict[str, int] = {}
    listed: int = 0
    hidden_by_user: int = 0
    decouverte: int = 0
    outside_criteria: int = 0
    source_off: dict[str, int] = {}
    expired: dict[str, int] = {}


@router.get("/list-status", response_model=ListStatusOut)
def list_status(request: Request) -> ListStatusOut:
    """What the main list holds and what it leaves out, by reason: the run it shows, whether the
    latest run kept the previous list, incomplete sources, carried offers, and the offers left out
    because the user hid them, they are in Découverte or the outside-criteria lane, their source is
    off, or they expired. Every number is a count over stored data; nothing here filters."""
    from src.learning.exploration import drawn_for_run
    from src.pipeline.lanes import has_weak_flags

    s, rs = _get_storages(request)
    try:
        visible = rs.get_latest_visible_run()
        latest_rows = rs.list_runs(limit=1)
        latest = latest_rows[0] if latest_rows else None
        latest_completed = rs.get_latest_completed_run()
        list_state = _list_state(request)
        off_sources = _disabled_sources(request)
        conn = s._get_conn()  # noqa: SLF001
        out = ListStatusOut(
            visible_run_id=visible["id"] if visible else None,
            visible_run_completed_at=(str(visible.get("completed_at")) if visible and visible.get("completed_at")
                                      else None),
            latest_run_id=latest["id"] if latest else None,
            latest_run_status=latest.get("status") if latest else None,
            latest_run_outcome=((latest.get("stats") or {}).get("outcome") or None) if latest else None,
            previous_list_kept=bool(visible and latest and latest["id"] != visible["id"]
                                    and latest.get("status") in ("completed", "failed")),
            incomplete_sources=dict(((latest_completed or {}).get("stats") or {}).get("incomplete_sources") or {}),
            carried_by_source=dict(((visible or {}).get("stats") or {}).get("carried_by_source") or {}),
            hidden_by_user=sum(1 for st in list_state.values() if st["hidden"]),
        )
        if visible:
            run_ids = rs.get_job_ids_for_run(visible["id"])
            restored = _restored_ids(s, rs, visible, run_ids, off_sources)
            if restored:
                for source, n in conn.execute(
                        f"SELECT source, COUNT(*) FROM jobs WHERE id IN ({','.join('?' * len(restored))}) "
                        "GROUP BY source", restored):
                    out.restored_by_source[source] = int(n)
            out.decouverte = len(drawn_for_run(request.app.state.db_path, visible["id"]))
            out.outside_criteria = sum(1 for r in rs.get_jobs_for_run(visible["id"], limit=-1)
                                       if has_weak_flags(r.get("score_breakdown")))
        if off_sources:
            kept = {j for j, st in list_state.items() if st["saved"]}
            marks = ",".join("?" * len(off_sources))
            for source, job_id in conn.execute(
                    f"SELECT source, id FROM jobs WHERE status = 'active' AND source IN ({marks}) "
                    "AND (application_status IS NULL OR application_status = '')", sorted(off_sources)):
                if job_id not in kept:
                    out.source_off[source] = out.source_off.get(source, 0) + 1
        out.expired = {str(r[0] or "unknown"): int(r[1]) for r in conn.execute(
            "SELECT expiry_reason, COUNT(*) FROM jobs WHERE status = 'expired' GROUP BY expiry_reason")}
    finally:
        s.close()
        rs.close()
    out.listed = len(_all_listed(request))
    return out


@router.get("/stats", response_model=JobStatsOut)
def get_job_stats(request: Request) -> JobStatsOut:
    """Real, DB-derived stats for the Stats page. See compute_job_stats()."""
    s, rs = _get_storages(request)
    try:
        profile = _load_profile_safe(Path(getattr(request.app.state, "config_dir", "config")))
        return compute_job_stats(s, rs, profile)
    finally:
        s.close()
        rs.close()


def compute_job_stats(
    s: Storage, rs: RunStorage, profile: "JobSearchProfile | None" = None
) -> JobStatsOut:
    """Real, DB-derived stats for GET /api/jobs/stats, computed in exactly
    one place.

    Skills/tools frequency, score-tier distribution and rejection reasons are
    scoped to the latest completed run (same scoping as GET /countries).
    Title categories bucket real job titles from that run against the title
    buckets of the line-of-work presets that apply to ``profile`` (none without
    one) — no invented numbers. Skill/tool frequency is filtered through
    ``_aggregate_tech_facets`` so source-specific noise in ``jobs.skills``
    (e.g. France Travail's ROME task/activity descriptions) never shows up
    as a "skill".
    """
    latest_run = rs.get_latest_visible_run()
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

    from src.discovery.presets import load_domain_keywords, load_presets, matching_presets
    from src.pipeline.domain_vocabulary import effective_domains

    presets = matching_presets(load_presets(), effective_domains(profile, load_domain_keywords())
                               if profile is not None else [])
    skills, tools = _aggregate_tech_facets(skill_rows, presets)

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

    categories = [c for p in presets for c in p.title_categories]
    category_counts: dict[str, int] = {name: 0 for name, _ in categories}
    for row in title_rows:
        title = f" {(row['title'] or '').lower()} "
        for name, keywords in categories:
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
        st = _list_state(request, [job_id]).get(job_id)
        if st:
            out.saved, out.hidden = st["saved"], st["hidden"]
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
        latest_run = rs.get_latest_visible_run()
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


class AvailabilityIn(BaseModel):
    available: StrictBool


@router.post("/{job_id}/availability", response_model=JobDetailOut)
def report_availability(job_id: str, body: AvailabilityIn, request: Request) -> JobDetailOut:
    """The user reports an offer as no longer available (``available: false``; it is marked
    expired and stays so even if a source lists it again), or available after all (issue #86)."""
    s, rs = _get_storages(request)
    try:
        if not s.set_reported_expired(job_id, expired=not body.available, now=datetime.now(UTC)):
            raise HTTPException(status_code=404, detail="Job not found")
        job = s.get_job_by_id(job_id)
        return _job_to_job_detail_out(job, rs.get_job_latest_score(job_id))
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

def _list_state(request: Request, job_ids: list[str] | None = None) -> dict[str, dict[str, bool]]:
    """Save / Hide state per offer; empty (never an error) when the journal cannot be read."""
    from src.feedback.storage import FeedbackStorage

    store = FeedbackStorage(request.app.state.db_path)
    try:
        store.init_db()
        return store.list_state(job_ids)
    except Exception as exc:  # noqa: BLE001 — listing offers never fails on the journal
        logger.warning("Save/Hide state unavailable: %s", exc)
        return {}
    finally:
        store.close()


def _disabled_sources(request: Request) -> set[str]:
    """The sources that are off now, for every reason (``src.source_enablement.disabled_sources``,
    issue #253); empty (never an error) when the configuration cannot be read."""
    from src.source_enablement import disabled_sources, settings_path_for

    state = request.app.state
    settings = getattr(state, "settings_path", None) or settings_path_for(state.db_path)
    try:
        return disabled_sources(getattr(state, "config_dir", "config"), settings)
    except Exception as exc:  # noqa: BLE001 — listing offers never fails on the settings
        logger.warning("Source switches unavailable: %s", exc)
        return set()


def _restored_ids(storage: Storage, rs: RunStorage, run: dict[str, Any], run_ids: list[str],
                  off_now: set[str]) -> list[str]:
    """Offers of a source that was off when ``run`` ran (``stats.sources_off``) and is on now:
    active, scored in an earlier completed run, not in ``run_ids``, not an exact duplicate (same
    content hash) of an offer of the run, and with no weak-evidence flag. Best score first."""
    from src.pipeline.lanes import has_weak_flags

    back_on = set((run.get("stats") or {}).get("sources_off") or []) - off_now
    if not back_on:
        return []
    conn = storage._get_conn()  # noqa: SLF001
    in_run = set(run_ids)
    run_hashes = storage.get_content_hashes(run_ids)
    marks = ",".join("?" * len(back_on))
    candidates = [r[0] for r in conn.execute(
        f"SELECT id, content_hash FROM jobs WHERE status = 'active' AND source IN ({marks})", sorted(back_on))
        if r[0] not in in_run and not (r[1] and r[1] in run_hashes)]
    scores = rs.get_latest_scores_for_jobs(candidates, completed_only=True)
    # Only an offer whose latest evaluation accepted it: a later completed run that rejected it
    # (the profile changed since) wins over an older score.
    rejected_at = _latest_rejection_times(rs, [j for j in candidates if j in scores])
    kept = [j for j in candidates if j in scores
            and str(scores[j].get("run_started_at") or "") > rejected_at.get(j, "")
            and not has_weak_flags(scores[j].get("score_breakdown"))]
    return sorted(kept, key=lambda j: (-float(scores[j].get("final_score") or 0.0), j))


def _latest_rejection_times(rs: RunStorage, job_ids: list[str]) -> dict[str, str]:
    """For each job, the start time of the latest completed run that rejected it."""
    out: dict[str, str] = {}
    conn = rs._get_conn()  # noqa: SLF001
    for i in range(0, len(job_ids), 500):
        chunk = job_ids[i:i + 500]
        for job_id, started in conn.execute(
                f"""SELECT r.job_id, MAX(p.started_at) FROM job_rejections r JOIN pipeline_runs p ON p.id = r.run_id
                    WHERE p.status = 'completed' AND r.job_id IN ({','.join('?' * len(chunk))})
                    GROUP BY r.job_id""", chunk):
            out[job_id] = str(started or "")
    return out


def _merge_by_score(rs: RunStorage, run_id: str, run_ids: list[str], restored: list[str]) -> list[str]:
    """``run_ids`` in their order with ``restored`` (best first) merged in by final score: a
    restored offer goes before the first run offer that scores lower."""
    scores = rs.get_scores_for_list(run_ids + restored, run_id)

    def score(j: str) -> float:
        return float((scores.get(j) or {}).get("final_score") or 0.0)

    out: list[str] = []
    i = k = 0
    while i < len(run_ids) and k < len(restored):
        if score(restored[k]) > score(run_ids[i]):
            out.append(restored[k])
            k += 1
        else:
            out.append(run_ids[i])
            i += 1
    return out + run_ids[i:] + restored[k:]


def _tracked_ids(storage: Storage) -> set[str]:
    """Offers in the application tracker (``jobs.application_status`` set)."""
    rows = storage._get_conn().execute(  # noqa: SLF001
        "SELECT id FROM jobs WHERE application_status IS NOT NULL AND application_status != ''").fetchall()
    return {r[0] for r in rows}


def _switched_off_ids(request: Request, storage: Storage, list_state: dict[str, dict[str, bool]],
                      off: set[str] | None = None) -> set[str]:
    """Offers of a source that is off, hidden from every list while it is off (owner decision D1,
    Phase B B4-A; since issue #253 every source that is off, not only the user-switchable ones).
    Never deleted; an offer the user saved or tracks (``application_status``) stays listed."""
    if off is None:
        off = _disabled_sources(request)
    if not off:
        return set()
    rows = storage._get_conn().execute(  # noqa: SLF001
        f"SELECT id FROM jobs WHERE source IN ({','.join('?' * len(off))}) "
        "AND (application_status IS NULL OR application_status = '')", sorted(off)).fetchall()
    return {r[0] for r in rows if not list_state.get(r[0], {}).get("saved")}


def _get_jobs_filtered(
    storage: Storage,
    source: str | None = None,
    country: str | None = None,
    remote: bool | None = None,
    limit: int | None = 50,
    offset: int = 0,
    job_ids: list[str] | None = None,
    include_expired: bool = True,
    exclude_ids: set[str] | None = None,
    exclude_hashes: set[str] | None = None,
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
    if not include_expired:
        conditions.append("status != 'expired'")
    if exclude_ids:
        conditions.append(f"id NOT IN ({','.join('?' * len(exclude_ids))})")
        params.extend(sorted(exclude_ids))
    if exclude_hashes:
        conditions.append(f"(content_hash IS NULL OR content_hash NOT IN ({','.join('?' * len(exclude_hashes))}))")
        params.extend(sorted(exclude_hashes))

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    from src.models.job import Job

    if job_ids is not None:
        # Fetch ALL matching rows without pagination; the caller applies
        # offset/limit in Python after re-ordering to score order.
        sql = f"SELECT * FROM jobs {where}"
        rows = conn.execute(sql, params).fetchall()
        jobs = [Job.from_dict(dict(r)) for r in rows]
        # Restore the run order from job_ids (visible positions, else final_score DESC).
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
        valid_through=j.valid_through,
        expired_at=j.expired_at,
        expiry_reason=j.expiry_reason,
        application_status=j.application_status,
        score=score_out,
        description=j.description,
    )
