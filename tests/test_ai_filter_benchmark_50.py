"""Dedicated evaluation module for the 50-job AI-filter benchmark.

This is a REGRESSION HARNESS, not a benchmark that has to pass a target
score. It exists so the deterministic-vs-AI comparison in
``scripts/benchmark_ai_filter_50.py`` can be re-run automatically after
future prompt/model changes and its STRUCTURE stays verified (every job
gets a verdict, the AI path never overrides a heuristic hard-reject with an
uncorroborated claim, the harness degrades gracefully when LM Studio is
unavailable).

Per the project's evaluation rules, these tests deliberately do NOT assert a
minimum accuracy/precision/recall score: a synthetic benchmark that must
clear a bar invites tuning the production filter to the benchmark instead of
measuring it honestly. The one substantive accuracy assertion kept here is
a floor derived from the current documented safety invariant (the LLM can
never independently cause a hard rejection — see
``src/requirements/extractor.py``'s module docstring), not from a quality
target.

Run standalone for the full human-readable report:
    python scripts/benchmark_ai_filter_50.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.benchmark_ai_filter_50 import _check_llm_reachable, run_benchmark
from tests.fixtures.ai_filter_benchmark.ground_truth import GROUND_TRUTH
from tests.fixtures.ai_filter_benchmark.jobs import JOBS

_DEFAULT_ENDPOINT = "http://localhost:1234"
# Pinned so results never depend on the developer's personal config/profile.yaml
# (see that file's header and issue #18).
_BENCHMARK_PROFILE = str(
    Path(__file__).parent / "fixtures" / "ai_filter_benchmark" / "profile.yaml"
)


def test_fixture_ids_are_consistent():
    """jobs.py and ground_truth.py must describe exactly the same 50 jobs —
    a silent ID drift between the two files would corrupt every metric."""
    job_ids = {j["id"] for j in JOBS}
    assert len(JOBS) == 50
    assert job_ids == set(GROUND_TRUTH.keys())
    assert all(
        gt["expected"] in ("SHOULD_ACCEPT", "SHOULD_REJECT", "BORDERLINE")
        for gt in GROUND_TRUTH.values()
    )


def test_no_ground_truth_leakage_into_job_fields():
    """The raw job dicts fed to the pipeline must never carry a ground-truth
    hint (category name, expected verdict, etc.) — this is the no-leakage
    invariant the benchmark's design depends on."""
    forbidden_substrings = (
        "SHOULD_ACCEPT", "SHOULD_REJECT", "BORDERLINE",
        "perfect_match", "partial_match", "keyword_trap", "hard_constraint_trap",
        "ground_truth", "ground truth",
    )
    for job in JOBS:
        assert set(job.keys()) <= {
            "id", "title", "company", "location", "countries", "remote",
            "work_mode", "employment_type", "description", "skills",
        }, f"job {job.get('id')} has an unexpected field that could carry benchmark metadata"
        haystack = " ".join(str(v) for v in job.values())
        for forbidden in forbidden_substrings:
            assert forbidden not in haystack, (
                f"job {job['id']} description leaks benchmark metadata: {forbidden!r}"
            )


def test_deterministic_baseline_runs_end_to_end():
    """The deterministic-only path (Path A) must run against the real
    production filter/gate for all 50 jobs and produce a verdict for each,
    with no exceptions. This is the always-on regression check — it needs no
    LLM and must never be skipped."""
    result = run_benchmark(run_ai=False, run_phase2=False, profile_path=_BENCHMARK_PROFILE)
    assert result["ai_available"] is False
    assert result["ai_verdicts"] is None
    assert set(result["det_verdicts"].keys()) == set(GROUND_TRUTH.keys())
    for jid, verdict in result["det_verdicts"].items():
        assert verdict.verdict in ("ACCEPT", "REJECT")
        assert verdict.stage in ("filter", "requirements_gate", "passed")


def test_deterministic_baseline_has_no_false_negatives_on_hard_constraint_traps():
    """The hard-constraint-trap category (Bac+5 mandatory, senior-only,
    5+ years, non-EU on-site, US-only remote) is exactly what the
    requirements gate and domain filter were purpose-built to catch. A
    regression here (a job that SHOULD be rejected slipping through) is a
    genuine safety-architecture bug, not a benchmark-tuning question, so it
    is the one category this suite holds to a hard floor."""
    result = run_benchmark(run_ai=False, run_phase2=False, profile_path=_BENCHMARK_PROFILE)
    for jid, gt in GROUND_TRUTH.items():
        if gt["category"] != "hard_constraint_trap":
            continue
        if gt["expected"] != "SHOULD_REJECT":
            continue
        verdict = result["det_verdicts"][jid]
        assert verdict.verdict == "REJECT", (
            f"{jid} should have been hard-rejected ({gt['reason']}) but was ACCEPTed "
            f"(stage={verdict.stage})"
        )


def test_benchmark_profile_is_pinned_with_default_requirement_gates():
    """Regression for issue #18: the hard-constraint floor assumes the
    requirements gates run at their model defaults. If the pinned profile is
    ever re-snapshotted from a personal profile that disables one, bm50 (a
    Rust-centred role behind a generic "Ingénieur Logiciel" title) silently
    passes again."""
    from src.pipeline.filter import load_profile

    matching = load_profile(_BENCHMARK_PROFILE).matching
    assert matching.principal_skill_gate is True
    assert matching.mandatory_central_skill_gate is True
    assert matching.education_hard_constraint is True


def test_bm50_rust_centred_generic_title_is_rejected_by_mandatory_skill_gate():
    """Issue #18: the description-based gate, not the title, must catch it."""
    result = run_benchmark(run_ai=False, run_phase2=False, profile_path=_BENCHMARK_PROFILE)
    verdict = result["det_verdicts"]["bm50"]
    assert verdict.verdict == "REJECT"
    assert verdict.stage == "requirements_gate"
    assert any("rust" in reason.lower() and "principal" in reason for reason in verdict.reasons)


@pytest.mark.skipif(
    not _check_llm_reachable(_DEFAULT_ENDPOINT),
    reason=f"No local LLM reachable at {_DEFAULT_ENDPOINT} — AI-enhanced path cannot be exercised",
)
def test_ai_enhanced_path_runs_end_to_end():
    """When LM Studio is reachable, the AI-enhanced path (Path B) must also
    produce a verdict for every job, using the real LocalLLMProvider."""
    result = run_benchmark(run_ai=True, run_phase2=False, profile_path=_BENCHMARK_PROFILE)
    assert result["ai_available"] is True
    assert result["ai_verdicts"] is not None
    assert set(result["ai_verdicts"].keys()) == set(GROUND_TRUTH.keys())
    for jid, verdict in result["ai_verdicts"].items():
        assert verdict.verdict in ("ACCEPT", "REJECT")


@pytest.mark.skipif(
    not _check_llm_reachable(_DEFAULT_ENDPOINT),
    reason=f"No local LLM reachable at {_DEFAULT_ENDPOINT} — AI-enhanced path cannot be exercised",
)
def test_ai_path_never_rejects_a_job_the_deterministic_path_accepted_via_uncorroborated_claim():
    """Safety invariant, not a quality target: the requirements gate's
    evidence-corroboration rule (see src/requirements/extractor.py) says an
    LLM claim with no textual anchor in the job posting can never
    independently cause a hard rejection. This test does not assert the AI
    path improves accuracy — only that when it disagrees with the
    deterministic path by rejecting a job the deterministic path accepted,
    every such rejection is attributable to REAL evidence the AI found and
    reported reasons for (never a silent, unexplained flip)."""
    result = run_benchmark(run_ai=True, run_phase2=False, profile_path=_BENCHMARK_PROFILE)
    det = result["det_verdicts"]
    ai = result["ai_verdicts"]
    for jid in GROUND_TRUTH:
        if det[jid].verdict == "ACCEPT" and ai[jid].verdict == "REJECT":
            assert ai[jid].reasons, (
                f"{jid}: AI path rejected a job the deterministic path accepted, "
                f"but recorded no reasons — every AI-driven rejection must carry "
                f"an attributable reason (see HardConstraintGate's evidence rule)."
            )
