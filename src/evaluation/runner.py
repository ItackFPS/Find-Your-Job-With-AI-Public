"""Decision-level runner: replays a dataset through the REAL production code.

Stages mirrored from ``src/pipeline/orchestrator.py`` (same functions, same
wiring): ``filter_jobs`` → ``RequirementExtractor`` + ``HardConstraintGate``
(``gate_jobs``). The ground-truth labels are joined only after every decision
has been produced (blind evaluation).

Production code is never modified. Time-dependent checks are made
reproducible by freezing ``datetime.now`` inside ``src.pipeline.filter`` to the
dataset's ``as_of`` for the duration of the run.
"""

from __future__ import annotations

import contextlib
import platform
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from src.evaluation.dataset import Dataset
from src.evaluation.fingerprint import strategy_fingerprint
from src.evaluation.metrics import bootstrap_ci, evaluate, percentile
from src.evaluation.models import Decision, RunResult, StrategyRef
from src.models.job import Job


class EvaluationError(RuntimeError):
    pass


@contextlib.contextmanager
def frozen_clock(as_of: datetime) -> Iterator[None]:
    """Freeze ``datetime.now()`` seen by ``src.pipeline.filter`` (only user of now())."""
    import src.pipeline.filter as filter_mod

    original = filter_mod.datetime

    class _Frozen(original):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return as_of if tz is None else as_of.astimezone(tz)

    filter_mod.datetime = _Frozen  # type: ignore[misc]
    try:
        yield
    finally:
        filter_mod.datetime = original  # type: ignore[misc]


@dataclass
class _CountingProvider:
    """Wraps an LLM provider to count calls/failures; delegates everything else."""

    inner: Any
    calls: int = 0
    failures: int = 0
    latencies: list[float] = field(default_factory=list)

    def complete(self, prompt: str, max_tokens: int | None = None) -> str:
        self.calls += 1
        t0 = time.perf_counter()
        try:
            return str(self.inner.complete(prompt, max_tokens=max_tokens))
        except Exception:
            self.failures += 1
            raise
        finally:
            self.latencies.append(time.perf_counter() - t0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _build_llm(strategy: StrategyRef) -> _CountingProvider | None:
    if strategy.llm is None:
        return None
    import httpx

    spec = strategy.llm
    try:
        httpx.get(f"{spec.endpoint}/v1/models", timeout=3.0).raise_for_status()
    except (httpx.HTTPError, ValueError) as exc:
        raise EvaluationError(f"LLM endpoint {spec.endpoint} unreachable: {exc}") from exc
    from src.ai.interfaces import LocalLLMProvider

    return _CountingProvider(LocalLLMProvider(
        endpoint=spec.endpoint, model_name=spec.model_name, temperature=0.0,
        max_tokens=spec.max_tokens, timeout_seconds=spec.timeout_seconds,
        thinking_enabled=False,
    ))


def _git_info() -> dict[str, Any]:
    try:
        root = Path(__file__).resolve().parents[2]
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=5, check=True).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                                    text=True, timeout=5, check=True).stdout.strip())
        return {"git_sha": sha, "dirty": dirty}
    except Exception:  # noqa: BLE001 — metadata only
        return {"git_sha": None, "dirty": None}


def _parse_as_of(value: str) -> datetime:
    dt: datetime = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _esco_map(strategy: StrategyRef, profile: Any) -> dict[str, list[str]]:
    """ESCO synonyms for scoring. Default: disabled (``{}``) so results never depend on the
    local ``esco.db``; ``strategy.esco_db`` points at a database to enable the production lookup."""
    if strategy.esco_db is None:
        return {}
    if not Path(strategy.esco_db).is_file():
        # production silently degrades to "no enrichment"; an explicit request must not
        raise EvaluationError(f"--esco-db {strategy.esco_db!r} does not exist")
    from src.pipeline.scoring import compute_esco_synonym_map

    return compute_esco_synonym_map(list(profile.target_skills), db_path=strategy.esco_db)


def default_profile_path(config_dir: str | Path) -> str:
    """Profile for a dataset that pins none: the one production uses — the personal
    ``profile.local.yaml`` when present, else the tracked example (src/config_files.py)."""
    from src.config_files import resolve_config_path

    return str(resolve_config_path(config_dir, "profile.yaml"))


def run_decisions(
    dataset: Dataset, strategy: StrategyRef, jobs: list[Job]
) -> tuple[list[Decision], dict[str, float], _CountingProvider | None, dict[str, Any]]:
    """Run filter + requirements gate; returns decisions in job-id order.

    Also returns stage timings, the LLM wrapper (if any) and intermediates
    (``gate_results``/``filtered``) used by later slices.
    """
    from src.pipeline.filter import build_filter_config, filter_jobs, load_filters, load_profile
    from src.requirements.education import candidate_education
    from src.requirements.extractor import RequirementExtractor
    from src.requirements.gate import HardConstraintGate, gate_jobs

    config_dir = Path(strategy.config_dir)
    profile_path = strategy.profile_path or (
        str(dataset.profile_path) if dataset.profile_path else default_profile_path(config_dir)
    )
    profile = load_profile(profile_path)
    engine_cfg = load_filters(str(config_dir / "filters.yaml"))
    filters_config = build_filter_config(profile, engine_cfg)

    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    passing, filter_rejections = filter_jobs(jobs, filters_config)
    timings["filter"] = time.perf_counter() - t0

    llm = _build_llm(strategy)
    user_max_edu, user_edu_label, user_held_edu = candidate_education(profile)
    user_max_years = None
    if profile.experience is not None:
        user_max_years = getattr(profile.experience, "max_years", None)
    extractor = RequirementExtractor(
        user_skills=list(profile.target_skills),
        llm_provider=llm,  # type: ignore[arg-type]  # duck-typed counting wrapper
        target_domains=list(profile.target_domains),
    )
    gate = HardConstraintGate(
        user_max_education_level=user_max_edu,
        user_max_years_experience=user_max_years,
        education_hard_constraint=getattr(profile.matching, "education_hard_constraint", True),
        principal_skill_gate=getattr(profile.matching, "principal_skill_gate", True),
        mandatory_skill_gate=getattr(profile.matching, "mandatory_central_skill_gate", True),
        profile_version_hash="evaluation",
        career_level=filters_config.get("career_level"),
        user_education_label=user_edu_label,
        user_held_education_level=user_held_edu,
    )
    edu_penalty = getattr(profile.matching, "education_preferred_penalty", 0.20)

    t1 = time.perf_counter()
    per_job_ms: list[float] = []
    requirements_map = {}
    for job in passing:
        tj = time.perf_counter()
        requirements_map[job.id] = extractor.extract(job)
        per_job_ms.append((time.perf_counter() - tj) * 1000)
    _passed, gate_results = gate_jobs(passing, requirements_map, gate, education_preferred_penalty=edu_penalty)
    timings["requirements_gate"] = time.perf_counter() - t1

    # -- scoring (accepted jobs only, like the orchestrator) ----------------------------------
    from src.pipeline.scoring import build_scoring_config, load_scoring_config, score_jobs_with_breakdown

    accepted = [j for j in passing if gate_results[j.id].passes]
    scoring_path = config_dir / "scoring.yaml"
    t2 = time.perf_counter()
    scored: dict[str, tuple[float, dict[str, float]]] = {}
    if scoring_path.exists():
        esco = _esco_map(strategy, profile)
        scoring_config = build_scoring_config(load_scoring_config(str(scoring_path)), profile, engine_cfg,
                                              esco_synonyms=esco)
        for job, score, factors in score_jobs_with_breakdown(accepted, scoring_config, gate_results=gate_results):
            scored[job.id] = (score, factors)
    else:                                   # same fallback as the orchestrator
        scored = {j.id: (0.5, {}) for j in accepted}
    timings["scoring"] = time.perf_counter() - t2

    decisions: list[Decision] = []
    for job in jobs:
        if job.id in filter_rejections:
            decisions.append(Decision(
                job_id=job.id, verdict="reject", stage="filter",
                reasons=[f"filter:{filter_rejections[job.id]}"],
            ))
            continue
        res = gate_results[job.id]
        req = requirements_map[job.id]
        decisions.append(Decision(
            job_id=job.id,
            verdict="accept" if res.passes else "reject",
            stage="passed" if res.passes else "requirements_gate",
            reasons=[r.message for r in res.reasons],
            flags=list(res.flags),
            extraction_method=req.extraction_method,
            score=scored[job.id][0] if job.id in scored else None,
            factors=scored[job.id][1] if job.id in scored else None,
        ))
    decisions.sort(key=lambda d: d.job_id)
    extras = {
        "filters_config": filters_config,
        "filter_rejections": filter_rejections,
        "scoring_config": scoring_config if scoring_path.exists() else None,
        "per_job_extraction_ms": per_job_ms,
        "gate_results": gate_results,
        "requirements_map": requirements_map,
        "passing_jobs": passing,
        "profile": profile,
        "engine_cfg": engine_cfg,
        "profile_path": profile_path,
    }
    return decisions, timings, llm, extras


def _dataset_warnings(dataset: Dataset) -> list[str]:
    """Sampling limits of a real dataset, from its manifest (the private export sidecar is never read)."""
    from src.evaluation.export import PIPELINE_REJECTED_NOTE

    if dataset.manifest.kind != "real":
        return []
    if PIPELINE_REJECTED_NOTE in dataset.manifest.notes:
        return ["real dataset with a pipeline-rejected sample: false negatives are measurable on that subsample "
                "only, which excludes rejections older than the decision journal (see manifest notes)"]
    return ["real dataset: only jobs accepted at export time (survival bias); "
            "false negatives are NOT measurable until rejected-job snapshots exist "
            "(`export --rejected N` adds a sample from the decision journal)"]


def run_evaluation(dataset: Dataset, strategy: StrategyRef, *, assess: bool = False) -> RunResult:
    """Run the dataset under ``strategy`` and return an immutable ``RunResult``.

    ``assess=True`` also runs the disagreement engine on the deterministic evidence (rules-only
    signals: no semantic similarity, Phase 2 or LLM verdict in an offline run) and adds the review
    metrics — a SHADOW evaluation that never changes a verdict."""
    jobs = dataset.build_jobs()
    as_of = _parse_as_of(dataset.manifest.as_of)
    started = time.perf_counter()
    with frozen_clock(as_of):
        decisions, timings, llm, extras = run_decisions(dataset, strategy, jobs)
        if assess:
            from src.evaluation.assessment import assess_decisions

            # timed per outcome: an accepted job's evidence (filter pass + score) costs more than a rejected one's
            for outcome in ("accepted", "rejected"):
                part = [d for d in decisions if (d.verdict == "accept") == (outcome == "accepted")]
                ta = time.perf_counter()
                assess_decisions(part, jobs, extras, Path(strategy.config_dir))
                timings[f"assessment_{outcome}"] = time.perf_counter() - ta
    timings["total"] = time.perf_counter() - started
    n_accepted = sum(1 for d in decisions if d.verdict == "accept")
    stage_jobs = {"filter": len(jobs), "requirements_gate": len(extras["passing_jobs"]), "scoring": n_accepted}
    if assess:
        stage_jobs.update(assessment_accepted=n_accepted, assessment_rejected=len(decisions) - n_accepted)

    config_dir = Path(strategy.config_dir)
    files: dict[str, str | Path] = {
        "profile": extras["profile_path"], "filters": config_dir / "filters.yaml",
        "scoring": config_dir / "scoring.yaml",
    }
    if strategy.esco_db:
        files["esco_db"] = strategy.esco_db
    fp, file_hashes = strategy_fingerprint(
        files, extra=strategy.llm.model_dump(exclude={"model_meta"}) if strategy.llm else None,
    )
    from src.evaluation.annotation_assist import assisted_job_ids

    labels = dataset.labels
    assisted = frozenset(assisted_job_ids(dataset.path))
    metrics = evaluate(decisions, labels, assisted)
    pairs = [(labels[d.job_id].expected, d.verdict) for d in decisions if d.job_id in labels]
    metrics["bootstrap_ci_95"] = bootstrap_ci(pairs)
    if assess:
        from src.evaluation.metrics import review_metrics

        metrics["review"] = review_metrics(decisions, labels)

    ms = extras["per_job_extraction_ms"]
    n = max(1, len(jobs))
    llm_info: dict[str, Any] = {"enabled": llm is not None, "calls": 0, "failures": 0}
    if llm is not None:
        llm_info.update(calls=llm.calls, failures=llm.failures,
                        mean_latency_s=(sum(llm.latencies) / len(llm.latencies)) if llm.latencies else None,
                        p95_latency_s=percentile(llm.latencies, 0.95),
                        model_name=strategy.llm.model_name if strategy.llm else None,
                        model_meta=strategy.llm.model_meta if strategy.llm else {})
    warnings: list[str] = []
    if dataset.unlabeled_ids:
        warnings.append(f"{len(dataset.unlabeled_ids)} unlabelled job(s) excluded from metrics")
    if llm is not None and llm.failures:
        warnings.append(
            f"{llm.failures}/{llm.calls} LLM call(s) failed; those extractions fell back to "
            "heuristics, so this run is not a clean LLM measurement"
        )
    unmarked = sum(1 for jid, lab in dataset.labels.items() if lab.label_source == "human" and jid in assisted)
    if unmarked:
        warnings.append(
            f"{unmarked} label(s) are recorded as independent `human` but an assistant proposal is on record "
            "for them (imported before provenance tracking): treat every metric as assistant-influenced; "
            "re-run annotate-import to record them as human_assisted"
        )
    n_assisted = sum(1 for lab in dataset.labels.values() if lab.label_source == "human_assisted")
    if n_assisted:
        warnings.append(f"{n_assisted}/{len(dataset.labels)} label(s) are assistant-assisted (not independent evidence)")
    warnings += _dataset_warnings(dataset)

    return RunResult(
        run_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        dataset={
            "id": dataset.manifest.dataset_id, "version": dataset.manifest.version,
            "kind": dataset.manifest.kind, "jobs_sha256": dataset.manifest.jobs_sha256,
            "labels_sha256": dataset.manifest.labels_sha256, "as_of": dataset.manifest.as_of,
        },
        strategy={"name": strategy.name, "fingerprint": fp, "files": file_hashes},
        code=_git_info(),
        environment={"python": sys.version.split()[0], "platform": platform.platform()},
        decisions=decisions,
        metrics=metrics,
        timings={
            "stage_seconds": {k: round(v, 6) for k, v in timings.items()},
            "stage_jobs": stage_jobs,           # jobs each timed stage processed (workload, not speed)
            "per_job_ms_mean": round(timings["total"] * 1000 / n, 3),
            "extraction_ms_mean": round(sum(ms) / len(ms), 3) if ms else None,
            "extraction_ms_p95": round(percentile(ms, 0.95) or 0.0, 3) if ms else None,
        },
        llm=llm_info,
        warnings=warnings,
    )
