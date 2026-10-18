"""50-REAL-JOB benchmark: deterministic filter vs. current AI (fully enabled).

READ-ONLY EVALUATION. Never writes to data/jobs.db, never modifies profile.yaml/
filters.yaml/prompts, never changes any production source file. Drives the
real production functions against 50 real jobs sampled from the existing
database.

Two configurations compared, mirroring exactly what orchestrator.py does for
each toggle combination:

  FILTER (current default: matching.llm_extraction_enabled=false,
          matching.llm_eligibility_enabled=false)
    domain/keyword filter (src.pipeline.filter.filter_jobs)
      -> RequirementExtractor(llm_provider=None)  [heuristic-only]
      -> HardConstraintGate.check

  AI (both current AI toggles turned on)
    domain/keyword filter (SAME result as above -- the AI never touches this
    stage, see extractor.py's build_requirements_llm_provider docstring)
      -> RequirementExtractor(llm_provider=LocalLLMProvider)  [heuristic+LLM,
         same wiring as build_requirements_llm_provider: endpoint/model from
         REQGATE_LLM_ENDPOINT/REQGATE_LLM_MODEL_NAME env vars, temp=0.0,
         max_tokens=512, timeout=20s]
      -> HardConstraintGate.check using the SAME (combined) requirements_map
      -> EligibilityGate.run() on the gate's survivors, using the SAME combined
         requirements_map (exactly how orchestrator.py wires it when both
         flags are on: requirements_map is computed once and reused). Calling
         .run() — not evaluate_batch() plus a hand-rolled survivor rule — is
         what actually exercises the production decision path, including the
         deterministic-modality precedence check in
         _deterministic_downgrade_reason() (src/ai/eligibility.py): a REJECT
         is only honored when it isn't contradicted by a PREFERRED/OPTIONAL/
         UNKNOWN/OR-alternative classification the deterministic extractor
         already assigned. A throwaway, temp-file-backed AIAnalysisStorage
         satisfies .run()'s caching parameter — never data/jobs.db.

Sampling: reproducible via a fixed random seed over all active jobs with a
description longer than 500 chars, ordered by id. Job IDs are printed and
saved so the sample is reproducible.

Usage:
    python scripts/benchmark_real_jobs_50.py [--seed 20260905] [--n 50]
        [--endpoint http://localhost:1234] [--model-name qwen/qwen3-8b]
        [--db data/jobs.db] [--config config] [--out scripts/real_jobs_50_raw.json]
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.job import Job
from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
from src.requirements.education import parse_education_level
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate, gate_jobs


def _load_jobs(db_path: str, seed: int, n: int) -> list[Job]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM jobs WHERE status='active' AND length(description) > 500 ORDER BY id"
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    rng = random.Random(seed)
    sample = rng.sample(rows, n)
    jobs = [Job.from_dict(dict(r)) for r in sample]
    return jobs


def _load_jobs_by_ids(db_path: str, job_ids: list[str]) -> list[Job]:
    """Load specific jobs by id, preserving the given order.

    Used for a faithful BEFORE/AFTER re-run: ``_load_jobs``'s seed-based
    ``random.Random(seed).sample()`` reproduces the same 50 jobs only when the
    underlying candidate pool (all active jobs with description > 500 chars)
    is unchanged — if the database has since grown (e.g. later collection
    runs), the same seed samples a different population and yields a
    different 50 jobs. Replaying the exact ids recorded in a prior run's
    ``--out`` JSON (its ``job_ids`` field) keeps "same 50 jobs" — the
    invariant that actually matters for a before/after comparison — intact
    even when "same seed" no longer implies it.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    by_id: dict[str, dict] = {}
    placeholders = ",".join(["?"] * len(job_ids))
    cur.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids)
    for row in cur.fetchall():
        by_id[row["id"]] = dict(row)
    conn.close()

    missing = [jid for jid in job_ids if jid not in by_id]
    if missing:
        print(f"WARNING: {len(missing)} job id(s) from the replay list no longer exist in the db: {missing}")

    return [Job.from_dict(by_id[jid]) for jid in job_ids if jid in by_id]


def _gate_result_to_dict(gr: Any) -> dict[str, Any]:
    return {
        "passes": gr.passes,
        "uncertainty": gr.uncertainty,
        "reasons": [
            {"type": r.type, "requirement": r.requirement, "message": r.message, "severity": r.severity}
            for r in gr.reasons
        ],
        "score_penalties": [
            {"type": r.type, "requirement": r.requirement, "message": r.message}
            for r in gr.score_penalties
        ],
        "flags": list(gr.flags),
    }


def _requirements_to_dict(req: Any) -> dict[str, Any]:
    edu = req.education
    return {
        "extraction_method": req.extraction_method,
        "education": (
            {
                "level_bac_plus": edu.level_bac_plus,
                "level_label": edu.level_label,
                "importance": edu.importance.value,
                "raw_text": edu.raw_text,
                "source": edu.source.value if hasattr(edu.source, "value") else str(edu.source),
                "is_or_alternative": edu.is_or_alternative,
            }
            if edu is not None
            else None
        ),
        "experience": (
            {
                "min_years": req.experience.min_years,
                "importance": req.experience.importance.value,
                "raw_text": req.experience.raw_text,
            }
            if req.experience is not None
            else None
        ),
        "principal_skill_mismatches": list(req.principal_skill_mismatches),
        "principal_skill_uncorroborated": list(getattr(req, "principal_skill_uncorroborated", [])),
        "title_or_alternatives": list(req.title_or_alternatives),
        "skills": [
            {
                "skill": s.skill,
                "importance": s.importance.value,
                "is_principal": s.is_principal,
                "source": s.source.value if hasattr(s.source, "value") else str(s.source),
                "raw_text": s.raw_text,
            }
            for s in req.skills
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=20260905)
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--endpoint", default="http://localhost:1234")
    parser.add_argument("--model-name", default="qwen/qwen3-8b")
    parser.add_argument("--db", default="data/jobs.db")
    parser.add_argument("--config", default="config")
    parser.add_argument(
        "--eligibility-max-workers",
        type=int,
        default=4,
        help=(
            "Concurrency for the eligibility gate's LLM calls (default 4, matching "
            "production). Some LM Studio deployments divide the loaded model's "
            "context window across parallel request slots, so N concurrent calls "
            "each get only total_context/N tokens — pass 1 to run eligibility "
            "calls sequentially (each gets the full context) if you observe "
            "'exceeds the available context size' errors under concurrency that "
            "do not occur when the same prompts run one at a time."
        ),
    )
    parser.add_argument("--out", default="scripts/real_jobs_50_raw.json")
    parser.add_argument(
        "--replay-from",
        default=None,
        help=(
            "Path to a prior run's --out JSON. Loads the SAME job ids it recorded "
            "(by id, not by re-sampling) instead of --seed/--n — use this for a "
            "faithful before/after re-run when the database may have grown since "
            "the original run (a grown candidate pool makes the same --seed sample "
            "a different 50 jobs)."
        ),
    )
    args = parser.parse_args()

    t0 = time.perf_counter()

    if args.replay_from:
        prior = json.loads(Path(args.replay_from).read_text(encoding="utf-8"))
        replay_ids = prior["job_ids"]
        print(f"Replaying {len(replay_ids)} job IDs recorded in {args.replay_from} ...")
        jobs = _load_jobs_by_ids(args.db, replay_ids)
        job_ids = [j.id for j in jobs]
        print(f"Loaded {len(jobs)}/{len(replay_ids)} jobs by id")
    else:
        print(f"Sampling {args.n} jobs (seed={args.seed}) from {args.db} ...")
        jobs = _load_jobs(args.db, args.seed, args.n)
        job_ids = [j.id for j in jobs]
        print(f"Sampled job IDs: {job_ids}")

    profile = load_profile(str(Path(args.config) / "profile.yaml"))
    engine_cfg = load_filters(str(Path(args.config) / "filters.yaml"))
    filters_config = build_filter_config(profile, engine_cfg)

    print("Running domain/keyword filter (same for both paths) ...")
    passing_jobs, filter_rejections = filter_jobs(jobs, filters_config)
    print(f"Domain filter: {len(passing_jobs)}/{len(jobs)} passed")

    user_max_edu = parse_education_level(profile.max_education) if profile.max_education else None
    user_max_years = getattr(profile.experience, "max_years", None)
    edu_penalty = profile.matching.education_preferred_penalty

    # --- FILTER path: heuristic-only extraction + gate ---------------------
    print("Running FILTER path (heuristic-only requirements gate) ...")
    det_extractor = RequirementExtractor(
        user_skills=list(profile.target_skills), llm_provider=None,
        target_domains=list(profile.target_domains),
    )
    det_gate = HardConstraintGate(
        user_max_education_level=user_max_edu,
        user_max_years_experience=user_max_years,
        education_hard_constraint=profile.matching.education_hard_constraint,
        principal_skill_gate=profile.matching.principal_skill_gate,
        mandatory_skill_gate=profile.matching.mandatory_central_skill_gate,
        profile_version_hash="benchmark-real-50",
    )
    det_req_map = {job.id: det_extractor.extract(job) for job in passing_jobs}
    det_passing, det_gate_results = gate_jobs(passing_jobs, det_req_map, det_gate, education_preferred_penalty=edu_penalty)
    print(f"FILTER: requirements gate {len(det_passing)}/{len(passing_jobs)} passed")

    # --- AI path: heuristic+LLM extraction + gate + eligibility gate -------
    ai_available = False
    try:
        import httpx
        httpx.get(f"{args.endpoint}/v1/models", timeout=3.0)
        ai_available = True
    except Exception as exc:
        print(f"WARNING: local LLM not reachable at {args.endpoint}: {exc}")

    ai_req_map: dict[str, Any] = {}
    ai_gate_results: dict[str, Any] = {}
    ai_passing: list[Job] = []
    eligibility_results: dict[str, Any] = {}
    eligibility_survivors: list[Job] = []

    if ai_available:
        from src.ai.interfaces import LocalLLMProvider
        from src.ai.eligibility import EligibilityGate
        from src.ai.storage import AIAnalysisStorage

        print("Running AI path (heuristic+LLM requirements gate) ...")
        llm_provider = LocalLLMProvider(
            endpoint=args.endpoint, model_name=args.model_name,
            temperature=0.0, max_tokens=512, timeout_seconds=20, thinking_enabled=False,
        )
        ai_extractor = RequirementExtractor(
            user_skills=list(profile.target_skills), llm_provider=llm_provider,
            target_domains=list(profile.target_domains),
        )
        ai_gate = HardConstraintGate(
            user_max_education_level=user_max_edu,
            user_max_years_experience=user_max_years,
            education_hard_constraint=profile.matching.education_hard_constraint,
            principal_skill_gate=profile.matching.principal_skill_gate,
            mandatory_skill_gate=profile.matching.mandatory_central_skill_gate,
            profile_version_hash="benchmark-real-50",
        )
        n_llm_calls = 0
        for i, job in enumerate(passing_jobs):
            req = ai_extractor.extract(job)
            ai_req_map[job.id] = req
            if req.extraction_method == "combined":
                n_llm_calls += 1
            print(f"  [{i+1}/{len(passing_jobs)}] extracted (method={req.extraction_method})")
        print(f"AI requirements extraction: {n_llm_calls}/{len(passing_jobs)} jobs triggered an LLM call")

        ai_passing, ai_gate_results = gate_jobs(passing_jobs, ai_req_map, ai_gate, education_preferred_penalty=edu_penalty)
        print(f"AI: requirements gate {len(ai_passing)}/{len(passing_jobs)} passed")

        print(f"Running eligibility gate on {len(ai_passing)} AI-gate survivors via EligibilityGate.run() "
              f"(the production decision path, eligibility_max_workers={args.eligibility_max_workers}) ...")
        elig_llm_provider = LocalLLMProvider(
            endpoint=args.endpoint, model_name=args.model_name,
            temperature=0.0, max_tokens=100, timeout_seconds=60, thinking_enabled=False,
        )
        eligibility_gate = EligibilityGate(llm_provider=elig_llm_provider, max_workers=args.eligibility_max_workers)
        profile_dict = profile.model_dump()
        reject_confidence = profile.matching.llm_eligibility_reject_confidence

        # EligibilityGate.run() — not evaluate_batch() + a hand-rolled survivor
        # rule — is the actual production consumption path (see the module
        # docstring above): it applies the zero-evidence corroboration check
        # AND _deterministic_downgrade_reason's precedence check before
        # honoring a REJECT. .run() requires an AIAnalysisStorage for its
        # caching layer; a throwaway temp-file-backed one is used here so this
        # read-only benchmark still never touches data/jobs.db.
        with tempfile.TemporaryDirectory() as tmp_dir:
            elig_storage = AIAnalysisStorage(db_path=str(Path(tmp_dir) / "eligibility_cache.db"))
            elig_storage.init_db()
            eligibility_survivors, eligibility_results, elig_stats = eligibility_gate.run(
                ai_passing, ai_req_map, profile_dict, elig_storage,
                profile_version="benchmark-real-50", reject_confidence=reject_confidence,
            )
            elig_storage.close()
        print(
            f"Eligibility gate (via EligibilityGate.run()): {len(eligibility_survivors)}/{len(ai_passing)} passed "
            f"(rejected={elig_stats.rejected}, unsupported_reject={elig_stats.unsupported_reject}, "
            f"uncertain={elig_stats.uncertain}, failed={elig_stats.failed})"
        )
    else:
        print("Skipping AI path entirely (no LLM reachable) -- do not fabricate results.")

    elapsed = time.perf_counter() - t0

    # --- Assemble raw output -------------------------------------------------
    out: dict[str, Any] = {
        "seed": args.seed,
        "n": args.n,
        "job_ids": job_ids,
        "ai_available": ai_available,
        "elapsed_seconds": elapsed,
        # Which EligibilityGate method produced eligibility_survivors/results
        # below — "EligibilityGate.run()" means the actual production decision
        # path (zero-evidence corroboration + deterministic-modality
        # precedence) was exercised, not a hand-rolled confidence-only rule.
        "eligibility_consumption": "EligibilityGate.run()" if ai_available else None,
        "eligibility_max_workers": args.eligibility_max_workers,
        "jobs": [],
    }

    det_passing_ids = {j.id for j in det_passing}
    ai_passing_ids = {j.id for j in ai_passing}
    elig_survivor_ids = {j.id for j in eligibility_survivors}

    for job in jobs:
        entry: dict[str, Any] = {
            "id": job.id,
            "source": job.source,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "countries": job.countries,
            "remote": job.remote,
            "work_mode": job.work_mode,
            "employment_type": job.employment_type,
            "published_at": job.published_at.isoformat() if job.published_at else None,
            "discovered_at": job.discovered_at.isoformat() if job.discovered_at else None,
            "skills": job.skills,
            "canonical_url": job.canonical_url,
            "description": (job.description or "")[:6000],
            "description_length": len(job.description or ""),
        }

        if job.id in filter_rejections:
            entry["domain_filter"] = {"passed": False, "reason": filter_rejections[job.id]}
            entry["filter_verdict"] = "REJECT"
            entry["filter_stage"] = "domain_filter"
            entry["ai_verdict"] = "REJECT"
            entry["ai_stage"] = "domain_filter"
        else:
            entry["domain_filter"] = {"passed": True}
            det_gr = det_gate_results.get(job.id)
            entry["det_requirements"] = _requirements_to_dict(det_req_map[job.id])
            entry["det_gate"] = _gate_result_to_dict(det_gr)
            entry["filter_verdict"] = "ACCEPT" if job.id in det_passing_ids else "REJECT"
            entry["filter_stage"] = "passed" if job.id in det_passing_ids else "requirements_gate"

            if ai_available:
                ai_gr = ai_gate_results.get(job.id)
                entry["ai_requirements"] = _requirements_to_dict(ai_req_map[job.id])
                entry["ai_gate"] = _gate_result_to_dict(ai_gr)
                if job.id not in ai_passing_ids:
                    entry["ai_verdict"] = "REJECT"
                    entry["ai_stage"] = "requirements_gate"
                else:
                    elig_r = eligibility_results.get(job.id)
                    if elig_r is not None:
                        was_high_conf_reject = (
                            elig_r.status == "success"
                            and elig_r.decision.decision == "REJECT"
                            and elig_r.decision.confidence >= reject_confidence
                        )
                        entry["eligibility"] = {
                            "status": elig_r.status,
                            "decision": elig_r.decision.decision,
                            "confidence": elig_r.decision.confidence,
                            "reason": elig_r.decision.reason,
                            # True when EligibilityGate.run() reached a
                            # high-confidence REJECT but the job survived
                            # anyway — i.e. the zero-evidence or
                            # deterministic-modality-precedence check
                            # downgraded it. Directly observable from
                            # already-returned values, not a second
                            # implementation of the gate's logic.
                            "downgraded": was_high_conf_reject and job.id in elig_survivor_ids,
                        }
                    entry["ai_verdict"] = "ACCEPT" if job.id in elig_survivor_ids else "REJECT"
                    entry["ai_stage"] = "passed" if job.id in elig_survivor_ids else "eligibility_gate"
            else:
                entry["ai_verdict"] = None
                entry["ai_stage"] = None

        out["jobs"].append(entry)

    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote raw results to {args.out}")
    print(f"Total wall time: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
