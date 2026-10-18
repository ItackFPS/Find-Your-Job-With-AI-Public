"""Disagreement-engine assessment of an offline evaluation run (rules-only signals).

Builds the same evidence the production journal builds (filter / gate / score evidence via
``src.decisions.evidence``), then runs ``src.decisions.disagreement.assess``. Offline runs have no
semantic similarity, Phase 2 score or LLM verdict, so the numeric spread is unavailable and the
engine relies on UNKNOWN density, completeness and contradictions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.decisions import evidence as ev
from src.decisions.disagreement import Signals, assess, evidence_stats, load_disagreement_config
from src.evaluation.models import Decision
from src.models.job import Job


def assess_decisions(decisions: list[Decision], jobs: list[Job], extras: dict[str, Any], config_dir: Path) -> None:
    cfg = load_disagreement_config(config_dir)
    if cfg.mode == "off":
        return
    by_id = {j.id: j for j in jobs}
    fcfg = extras["filters_config"]
    expected = ev.expected_cache(fcfg)
    rejections: dict[str, str] = extras["filter_rejections"]
    gate_results = extras["gate_results"]
    scoring_config = extras["scoring_config"]
    for d in decisions:
        job = by_id[d.job_id]
        if d.stage == "filter":
            evidence = ev.filter_failure_items(job, fcfg, rejections[d.job_id], all_failing=False, expected=expected)
        else:
            gate = gate_results[d.job_id]
            evidence = ev.gate_items(gate)
            if d.verdict == "accept":
                evidence += ev.filter_pass_items(job, fcfg, expected)
                if scoring_config:
                    from src.pipeline.scoring import explain_score

                    evidence += ev.score_items(explain_score(job, scoring_config, gate, factors=d.factors))
        density, completeness, unknown_dims = evidence_stats(evidence)
        conflict = ev.work_mode_conflict(job, keep_quote=False) if d.verdict == "accept" else None
        result = assess(Signals(
            outcome="accepted" if d.verdict == "accept" else "rejected", rules_score=d.score,
            unknown_density=density, completeness=completeness,
            conflicts=[conflict.description] if conflict else [],
            reject_sources=ev.hard_reject_sources(evidence, d.stage if d.verdict == "reject" else ""),
            unknown_dimensions=tuple(unknown_dims)), cfg)
        d.assessment = {**result.to_dict(), "config_hash": cfg.config_hash()}
