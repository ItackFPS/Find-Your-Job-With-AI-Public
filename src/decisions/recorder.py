"""``DecisionRecorder`` — collects what each stage decided about each job, then persists it.

Contract with the pipeline (ADR 0008):

* **Observability only.** Every ``observe_*`` method only READS what the stage already
  produced; nothing it does can change a decision, a score or a ranking.
* **Fail-open.** Any error inside the recorder is logged, remembered in ``summary()`` and
  swallowed — a journal problem must never break a run.
* **Complete.** Accepted AND rejected jobs (and, optionally, jobs dropped by validation /
  deduplication) each get one ``DecisionRecord`` per run.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from src.decisions.active_strategy import ActiveStrategy
from src.decisions.config import DecisionConfig, load_decision_config
from src.decisions.disagreement import DisagreementConfig, Signals, assess, evidence_stats, load_disagreement_config
from src.decisions import evidence as ev
from src.decisions.models import (
    ConflictItem, DecisionRecord, EvidenceItem, Exposure, JobMeta, SourceRef, StageName, StageTraceEntry,
)
from src.decisions.storage import DecisionStorage, snapshot_hash, snapshot_payload
from src.decisions.strategy import build_strategy_manifest

logger = logging.getLogger(__name__)

@dataclass
class _Draft:
    job: Any
    trace: list[StageTraceEntry] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    conflicts: list[ConflictItem] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None            # None = still alive
    deciding_stage: StageName | None = None
    rejection_snapshot: bool = False      # text worth keeping if this job is rejected
    scored: bool = False                  # reached scoring in the real pipeline (ground truth)
    eligibility: str | None = None        # LLM verdict as a categorical signal ("PASS"/"UNCERTAIN"/"REJECT")
    eligibility_overridden: bool = False  # LLM said REJECT but the job survived the gate


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(self: DecisionRecorder, *args: Any, **kwargs: Any) -> Any:
        if not self.enabled or self._error:
            return None
        try:
            return fn(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — journal must never break a run
            self._error = f"{fn.__name__}: {exc}"
            logger.warning("Decision recorder disabled for this run (non-fatal): %s", self._error)
            return None

    return wrapper


class DecisionRecorder:
    def __init__(
        self, config: DecisionConfig, db_path: str, run_id: str, strategy: dict[str, Any] | None,
        disagreement: DisagreementConfig | None = None,
    ) -> None:
        self.config = config
        self.disagreement = disagreement or DisagreementConfig(mode="off")
        self._semantic: dict[str, float] = {}
        self._phase2: dict[str, float] = {}
        self._db_path = db_path
        self.run_id = run_id
        self.strategy = strategy
        self.enabled = config.enabled and strategy is not None
        self._drafts: dict[str, _Draft] = {}
        self._order: list[str] = []
        self._user_stances: dict[str, dict[str, Any]] = {}   # job_id -> stance, loaded at finalize
        self._error: str | None = None
        self._filter_config: dict[str, Any] | None = None
        self._filter_expected: dict[str, str | None] | None = None
        self._scoring_config: dict[str, Any] | None = None
        self._gate_results: dict[str, Any] = {}
        self._skipped = 0                 # per-job observations that failed and were skipped
        self._written = 0
        self._snapshots = 0

    @classmethod
    def create(
        cls, *, config_dir: str, profile_path: str | Path, db_path: str, run_id: str, use_ai: bool,
        active: ActiveStrategy | None = None,
    ) -> DecisionRecorder:
        """Never raises: on any problem returns a disabled recorder carrying the error."""
        cfg = DecisionConfig(enabled=False)
        try:
            cfg = load_decision_config(config_dir)
            strategy = build_strategy_manifest(config_dir, profile_path, use_ai=use_ai, active=active) if cfg.enabled else None
            rec = cls(cfg, db_path, run_id, strategy, load_disagreement_config(config_dir))
        except Exception as exc:  # noqa: BLE001
            rec = cls(cfg, db_path, run_id, None)
            rec._error = f"create: {exc}"
            logger.warning("Decision recorder disabled (non-fatal): %s", exc)
        return rec

    # -- helpers --------------------------------------------------------------------------------

    @contextlib.contextmanager
    def _job_guard(self, job: Any) -> Iterator[None]:
        """One bad job must not cost the whole run's journal: skip it, count it, keep going."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001 — journal must never break a run
            self._skipped += 1
            logger.warning("Decision recorder skipped an observation for job %s (non-fatal): %s",
                           getattr(job, "id", "?"), exc)

    def _draft(self, job: Any) -> _Draft:
        d = self._drafts.get(job.id)
        if d is None:
            d = self._drafts[job.id] = _Draft(job)
            self._order.append(job.id)
        return d

    @staticmethod
    def _reject(d: _Draft, stage: StageName) -> None:
        if d.outcome is None:
            d.outcome, d.deciding_stage = "rejected", stage

    # -- stage observers ------------------------------------------------------------------------

    @_safe
    def observe_validation(self, all_jobs: list[Any], valid_jobs: list[Any]) -> None:
        valid_ids = {j.id for j in valid_jobs}
        for job in all_jobs:
            with self._job_guard(job):
                if job.id in valid_ids:
                    self._draft(job).trace.append(StageTraceEntry(stage="validate", result="pass"))
                elif self.config.record_upstream_rejections:
                    d = self._draft(job)
                    d.trace.append(StageTraceEntry(stage="validate", result="reject"))
                    d.evidence.append(EvidenceItem(
                        dimension="data_quality", polarity="negative", state="violated",
                        source=SourceRef(stage="validate", rule_id="validate.required_fields", evidence_source="metadata"),
                        message="Failed minimum data-quality validation"))
                    self._reject(d, "validate")

    @_safe
    def observe_dedup(self, valid_jobs: list[Any], deduped_jobs: list[Any]) -> None:
        kept = {j.id for j in deduped_jobs}
        for job in valid_jobs:
            with self._job_guard(job):
                if job.id in kept:
                    self._draft(job).trace.append(StageTraceEntry(stage="deduplicate", result="pass"))
                elif self.config.record_upstream_rejections:
                    d = self._draft(job)
                    d.trace.append(StageTraceEntry(stage="deduplicate", result="reject"))
                    d.evidence.append(EvidenceItem(
                        dimension="data_quality", polarity="negative", state="violated",
                        source=SourceRef(stage="deduplicate", rule_id="dedup.identity_hierarchy", evidence_source="metadata"),
                        message="Duplicate of another collected job"))
                    self._reject(d, "deduplicate")

    @_safe
    def observe_filter(
        self, jobs_in: list[Any], rejections: dict[str, str], *, skipped: bool = False,
        filter_config: dict[str, Any] | None = None,
    ) -> None:
        self._filter_config = filter_config
        self._filter_expected = ev.expected_cache(filter_config) if filter_config is not None else None
        for job in jobs_in:
            with self._job_guard(job):
                d = self._draft(job)
                if skipped:
                    d.trace.append(StageTraceEntry(stage="filter", result="skipped", detail="no profile/filters config"))
                    continue
                code = rejections.get(job.id)
                if code is None:
                    d.trace.append(StageTraceEntry(stage="filter", result="pass"))
                    continue
                d.trace.append(StageTraceEntry(stage="filter", result="reject", detail=code))
                if filter_config is not None:
                    d.evidence.extend(ev.filter_failure_items(
                        job, filter_config, code, all_failing=self.config.all_filter_failures,
                        expected=self._filter_expected))
                else:
                    d.evidence.append(EvidenceItem(
                        dimension=ev.FILTER_DIMENSION.get(code, "other"), polarity="negative", state="violated",
                        source=SourceRef(stage="filter", rule_id=f"filter.{code}", evidence_source="rule"),
                        message=f"Rejected by hard filter: {code}"))
                d.rejection_snapshot = True
                self._reject(d, "filter")

    @_safe
    def observe_gate(self, jobs_in: list[Any], gate_results: dict[str, Any]) -> None:
        for job in jobs_in:
            with self._job_guard(job):
                d = self._draft(job)
                res = gate_results.get(job.id)
                if res is None:
                    d.trace.append(StageTraceEntry(stage="requirements_gate", result="error", detail="no gate result"))
                    continue
                # build everything first, apply atomically: a failure leaves the draft untouched
                items = ev.gate_items(res)
                conflicts = [ConflictItem(description=text) for text in res.contradictions]
                signal = {
                    "uncertainty": res.uncertainty, "rule_version": res.rule_version,
                    "penalties": {"education": res.education_penalty, "skill": res.skill_penalty,
                                  "experience": res.experience_penalty},
                    "min_experience_years": res.min_experience_years,
                }
                trace = StageTraceEntry(
                    stage="requirements_gate",
                    result="reject" if not res.passes else ("uncertain" if res.uncertainty else "pass"),
                    detail=",".join(r.type for r in res.reasons) if not res.passes else None)
                d.evidence.extend(items)
                d.conflicts.extend(conflicts)
                d.signals["gate"] = signal
                d.trace.append(trace)
                if not res.passes:
                    d.rejection_snapshot = True
                    self._reject(d, "requirements_gate")

    @_safe
    def observe_gate_error(self, jobs_in: list[Any], error: str) -> None:
        for job in jobs_in:
            with self._job_guard(job):
                self._draft(job).trace.append(StageTraceEntry(stage="requirements_gate", result="error", detail=error[:200]))

    @_safe
    def observe_eligibility(self, jobs_in: list[Any], elig_result: Any) -> None:
        rejected = {r.job_id: r for r in elig_result.rejection_records}
        verdicts = getattr(elig_result, "decisions", None) or {}
        for job in jobs_in:
            with self._job_guard(job):
                d = self._draft(job)
                v = verdicts.get(job.id)
                if v is not None and v.get("status") == "success":
                    d.eligibility = v.get("decision")
                    # the LLM's own confidence stays a signal, never the decision confidence
                    d.signals["eligibility"] = {"decision": v.get("decision"),
                                                "llm_self_reported_confidence": v.get("confidence")}
                rec = rejected.get(job.id)
                if rec is None:
                    d.eligibility_overridden = d.eligibility == "REJECT"
                    d.trace.append(StageTraceEntry(stage="llm_eligibility", result="pass" if elig_result.ran else "skipped"))
                    continue
                try:
                    detail = json.loads(rec.rejection_detail)
                except (TypeError, ValueError):
                    detail = {}
                d.trace.append(StageTraceEntry(stage="llm_eligibility", result="reject", detail=str(detail.get("decision"))))
                d.evidence.append(EvidenceItem(
                    dimension="seniority", polarity="negative", state="violated",
                    source=SourceRef(stage="llm_eligibility", rule_id="llm_eligibility.role_level",
                                     evidence_source="llm"),
                    message=str(detail.get("reason") or "LLM eligibility reject")))
                d.signals["eligibility"] = {"decision": detail.get("decision"),
                                            "llm_self_reported_confidence": detail.get("confidence")}
                d.rejection_snapshot = True
                self._reject(d, "llm_eligibility")

    @_safe
    def observe_semantic(self, scores_by_job: dict[str, float]) -> None:
        """Normalised semantic similarity per job (only jobs actually embedded)."""
        self._semantic = dict(scores_by_job)

    @_safe
    def observe_phase2(self, results_by_job: dict[str, Any]) -> None:
        """Phase 2 ``profile_match_score`` per job (successful analyses that produced one)."""
        for jid, r in results_by_job.items():
            score = getattr(getattr(r, "analysis", None), "profile_match_score", None)
            if getattr(r, "status", None) == "success" and score is not None:
                self._phase2[jid] = float(score)

    @_safe
    def observe_gate_results(self, gate_results: dict[str, Any]) -> None:
        """Keep the gate results (read-only) so score penalties can be explained at finalize."""
        self._gate_results = gate_results

    @_safe
    def observe_scores(
        self, scored_with_breakdown: list[tuple[Any, float, dict[str, float]]],
        scoring_config: dict[str, Any] | None = None,
    ) -> None:
        self._scoring_config = scoring_config or None
        for job, score, factors in scored_with_breakdown:
            with self._job_guard(job):
                d = self._draft(job)
                d.scored = True
                d.trace.append(StageTraceEntry(stage="score", result="pass"))
                d.signals["deterministic_score"] = score
                d.signals["factors"] = dict(factors)

    # -- persistence ----------------------------------------------------------------------------

    @_safe
    def finalize(self, scored_job_records: list[Any]) -> None:
        """Turn drafts into records (exposure from the final ranking) and persist them.

        The pipeline is the source of truth: a job that actually reached scoring is ``accepted``
        even if a stage observer had recorded a rejection the pipeline then did not apply (e.g. a
        gate error fallback); the disagreement is kept as a conflict. Job text (snapshots AND the
        quoted evidence spans) follows ``store_*_text``."""
        from src.pipeline.scoring import score_to_tier

        assert self.strategy is not None
        now = datetime.now(UTC).isoformat(timespec="seconds")
        rank_by_id = {r.job_id: (i, r) for i, r in enumerate(scored_job_records, start=1)}
        self._user_stances = self._load_user_stances() if self.disagreement.mode != "off" else {}
        records: list[DecisionRecord] = []
        snapshots: dict[str, dict[str, Any]] = {}
        for job_id in self._order:
            d = self._drafts[job_id]
            with self._job_guard(d.job):
                record, snap = self._build_record(d, job_id, now, rank_by_id, score_to_tier)
                records.append(record)
                if snap is not None:
                    snapshots.setdefault(snap[0], snap[1])
        store = DecisionStorage(self._db_path)
        try:
            store.init_db()
            store.register_strategy(self.strategy)
            self._written, self._snapshots = store.write_run(records, snapshots, compress=self.config.compress_text)
            store.purge_text(self.config.text_retention_days)
            store.purge_records(self.config.record_retention_days)
        finally:
            store.close()

    def _load_user_stances(self) -> dict[str, dict[str, Any]]:
        """The user's stance per job from the feedback journal (fail-open: none on any error)."""
        try:
            from src.feedback.storage import FeedbackStorage

            fs = FeedbackStorage(self._db_path)
            try:
                return fs.user_stances(list(self._order))
            finally:
                fs.close()
        except Exception as exc:  # noqa: BLE001 — a feedback problem must never block the journal
            logger.warning("User feedback stances unavailable for the disagreement engine: %s", exc)
            return {}

    def _build_record(
        self, d: _Draft, job_id: str, now: str, rank_by_id: dict[str, tuple[int, Any]],
        score_to_tier: Callable[[int], str],
    ) -> tuple[DecisionRecord, tuple[str, dict[str, Any]] | None]:
        assert self.strategy is not None
        job = d.job
        conflicts = list(d.conflicts)
        engine_conflicts = [c.description for c in d.conflicts]      # evidence contradictions only
        trace = list(d.trace)
        if d.scored and d.outcome == "rejected":
            conflicts.append(ConflictItem(
                description=f"journal observed a {d.deciding_stage} rejection that the pipeline did not "
                            "apply (stage error fallback); the job reached scoring"))
            outcome, stage = "accepted", "ranking"
        elif d.outcome is None and not d.scored:
            conflicts.append(ConflictItem(description="job never reached scoring and no stage recorded a decision"))
            trace.append(StageTraceEntry(stage="score", result="error", detail="not scored"))
            outcome, stage = "rejected", "score"
        else:
            outcome, stage = d.outcome or "accepted", d.deciding_stage or "ranking"
        exposure = None
        fit = None
        if outcome == "accepted":
            ranked = rank_by_id.get(job_id)
            if ranked is not None:
                rank, rec = ranked
                exposure = Exposure(
                    rank=rank, final_score=rec.final_score, deterministic_score=rec.deterministic_score,
                    semantic_score=rec.semantic_score, personalization_adjustment=rec.personalization_adjustment,
                    tier=score_to_tier(round(rec.final_score * 100)))
                fit = rec.deterministic_score
                trace.append(StageTraceEntry(stage="ranking", result="pass"))
            else:
                fit = d.signals.get("deterministic_score")
        want_text = (outcome == "rejected" and d.rejection_snapshot and self.config.store_rejected_text) or (
            outcome == "accepted" and self.config.store_accepted_text)
        evidence = list(d.evidence)
        if outcome == "accepted":
            if self._filter_config is not None and self.config.record_passing_filter_evidence:
                evidence.extend(ev.filter_pass_items(job, self._filter_config, self._filter_expected))
            if self._scoring_config:
                try:
                    from src.pipeline.scoring import explain_score

                    expl = explain_score(job, self._scoring_config, self._gate_results.get(job_id),
                                         factors=d.signals.get("factors"))
                    stored = fit if fit is not None else d.signals.get("deterministic_score")
                    if stored is not None and abs(expl.final - stored) > 1e-9:
                        # the explanation must be the stored score; if it is not, say so instead of misleading
                        conflicts.append(ConflictItem(dimensions=[], description=(
                            f"score explanation ({expl.final:.6f}) disagrees with the stored score ({stored:.6f}); "
                            "score evidence withheld")))
                    else:
                        evidence.extend(ev.score_items(expl))
                        d.signals["score_explanation"] = {
                            "weights": expl.weights, "weighted_base": expl.weighted_base,
                            "pre_gate_score": expl.pre_gate_score, "top_location_bonus": expl.top_location_bonus,
                            "domain_cap": expl.domain_cap, "domain_cap_applied": expl.domain_cap_applied,
                            "seniority_multiplier": expl.seniority_multiplier, "composite": expl.composite,
                            "penalties": expl.penalties, "final": expl.final, "location_match": expl.location_match}
                except Exception as exc:  # noqa: BLE001 — the record is still written, without score evidence
                    self._skipped += 1
                    logger.warning("Score explanation skipped for job %s (non-fatal): %s", job_id, exc)
            conflict = ev.work_mode_conflict(job, keep_quote=want_text)
            if conflict is not None:
                conflicts.append(conflict)
                engine_conflicts.append(conflict.description)
        if not want_text:      # no job text may leak through quoted evidence either
            evidence = [e.model_copy(update={"span": e.span.model_copy(update={"text": ""})}) if e.span else e
                        for e in evidence]
        assessment = None
        if self.disagreement.mode != "off":
            density, completeness, unknown_dims = evidence_stats(evidence)
            user = self._user_stances.get(job_id) or {}
            assessment = assess(Signals(
                outcome="rejected" if outcome == "rejected" else "accepted",
                rules_score=fit, semantic=self._semantic.get(job_id), phase2=self._phase2.get(job_id),
                eligibility=d.eligibility, eligibility_reject_overridden=d.eligibility_overridden,
                unknown_density=density, completeness=completeness,
                conflicts=engine_conflicts,
                reject_sources=ev.hard_reject_sources(evidence, stage),
                unknown_dimensions=tuple(unknown_dims),
                user_stance=user.get("stance"), user_stance_basis=user.get("basis")), self.disagreement)
            known = {c.description for c in conflicts}
            conflicts.extend(ConflictItem(dimensions=[], description=c) for c in assessment.conflicts if c not in known)
            d.signals["assessment"] = {**assessment.to_dict(), "mode": self.disagreement.mode,
                                       "config_hash": self.disagreement.config_hash(),
                                       "signals": {"rules": fit, "semantic": self._semantic.get(job_id),
                                                   "phase2": self._phase2.get(job_id), "eligibility": d.eligibility,
                                                   "user": user or None}}
            if self.disagreement.mode == "active" and assessment.state == "review" and outcome == "accepted":
                outcome = "review"          # a label only: the job stays ranked/notified exactly as before
                # (a REJECTED job keeps outcome "rejected"; the review queue is assessment.state == "review")
        meta = JobMeta(title=job.title, company=job.company, source=job.source, location=job.location,
                       content_hash=getattr(job, "content_hash", None) or None)
        snap = None
        h = None
        if want_text:
            payload = snapshot_payload(job)
            h = snapshot_hash(payload)
            snap = (h, {"payload": payload, "job_id": job_id, "meta": meta})
        record = DecisionRecord(
            decision_id=str(uuid.uuid4()), run_id=self.run_id, job_id=job_id, decided_at=now,
            outcome=outcome, deciding_stage=stage,  # type: ignore[arg-type]
            strategy_version=self.strategy["strategy_version"], fit_score=fit,
            decision_confidence=round(assessment.confidence, 4) if assessment else None, confidence_calibrated=False,
            evidence=evidence, conflicts=conflicts, signals=d.signals, stage_trace=trace,
            exposure=exposure, snapshot_hash=h, job=meta)
        return record, snap

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"enabled": self.enabled, "recorded": self._written, "snapshots": self._snapshots}
        if self._skipped:
            out["skipped_observations"] = self._skipped
        if self.strategy:
            out["strategy_version"] = self.strategy["strategy_version"]
        if self._error:
            out["error"] = self._error
        return out
