"""Benchmark test suite for the hard filter with safety metrics.

Measures:
    - unsafe_rejection_rate: suitable/unknown jobs incorrectly rejected
    - incompatible_rejection_recall: known incompatible jobs correctly rejected
    - rejection_precision: rejected jobs that are genuinely incompatible
    - unknown_pass_rate: uncertain evidence correctly passed through
    - extraction_f1: claim extraction accuracy on labelled cases
    - contradiction_recall: contradictions correctly detected
    - latency: per-job evaluation time

Primary safety metric: unsafe_rejection_rate must be zero.
False rejection is weighted much more heavily than false pass.

Benchmark corpus:
    Each test case is labelled with:
        "safe" — compatible or uncertain, must pass
        "incompatible" — objectively incompatible, should be rejected
        "uncertain" — evidence is ambiguous/contradictory, must pass with uncertainty

Release gate: any test finding an unsafe rejection is a blocker.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import pytest

from src.models.job import Job
from src.requirements.extractor import RequirementExtractor
from src.requirements.gate import HardConstraintGate
from src.requirements.models import Importance
from src.requirements.work_mode_evidence import (
    WorkModeSignal,
    detect_work_mode_contradiction,
    extract_work_mode_evidence,
)


# ---------------------------------------------------------------------------
# Benchmark corpus
# ---------------------------------------------------------------------------

Label = Literal["safe", "incompatible", "uncertain"]


@dataclass
class BenchmarkCase:
    title: str
    description: str
    label: Label
    reason: str  # human-readable explanation of expected behavior
    remote: bool | None = None  # job.remote metadata when testing contradictions


# Candidate profile
_SKILLS = [
    "python", "javascript", "php", "MySQL", "Node.js",
    "Git", "Linux", "HTML", "CSS", "Bash", "Docker", "n8n",
]
_MAX_EDU = 2     # Bac+2
_MAX_YEARS = 3


BENCHMARK_CORPUS: list[BenchmarkCase] = [
    # ────────────────────────────────────────────────────────────────────────
    # SAFE (compatible or uncertain → MUST PASS)
    # ────────────────────────────────────────────────────────────────────────
    BenchmarkCase(
        "Développeur Python",
        "Rejoignez notre équipe Python. CDI Paris.",
        "safe",
        "Python in profile, no constraints mentioned",
    ),
    BenchmarkCase(
        "Web Developer",
        "Looking for a JavaScript developer. Remote-friendly.",
        "safe",
        "JavaScript in profile",
    ),
    BenchmarkCase(
        "Stage Développeur Web",
        "Bac+2 requis. Stage de 6 mois.",
        "safe",
        "Bac+2 mandatory == user max → satisfied",
    ),
    BenchmarkCase(
        "Développeur PHP",
        "Formation Bac+2 souhaitée.",
        "safe",
        "Bac+2 preferred ≤ max → soft penalty, not rejection",
    ),
    BenchmarkCase(
        "Backend Engineer",
        "Idéalement Bac+5 en informatique.",
        "incompatible",
        "Bac+5 preferred exceeds Bac+2 max → hard reject (behavior change, "
        "2026-09: PREFERRED is no longer exempt, only 'mandatory only' was "
        "the old rule)",
    ),
    BenchmarkCase(
        "Dev",
        "Minimum 2 ans d'expérience.",
        "safe",
        "2 years mandatory ≤ 3yr max → pass",
    ),
    BenchmarkCase(
        "Developer",
        "Exciting opportunity. Competitive salary and benefits.",
        "safe",
        "No constraints mentioned → pass",
    ),
    BenchmarkCase(
        "Ingénieur logiciel",
        "Bac+5 ou expérience équivalente requise.",
        "safe",
        "Equivalency clause softens to PREFERRED → pass",
    ),
    BenchmarkCase(
        "Développeur Node.js",
        "Rejoignez une équipe de 8 personnes. Bonne ambiance.",
        "safe",
        "Team size mention, no education/experience constraint",
    ),
    BenchmarkCase(
        "Développeur JavaScript",
        "Expérience de 3 ans souhaités.",
        "safe",
        "3 years preferred → soft penalty, not rejection",
    ),
    BenchmarkCase(
        "Dev Linux",
        "Poste ouvert aux juniors. Télétravail possible.",
        "safe",
        "Junior-friendly, remote possible (not exclusive)",
    ),
    BenchmarkCase(
        "Docker DevOps",
        "Vous maîtrisez Docker et Kubernetes. Bac+2 requis.",
        "safe",
        "Docker in profile, Bac+2 = user max",
    ),
    BenchmarkCase(
        "Développeur Python ou Go",
        "",
        "safe",
        "Python is OR alternative with known skill → flag, not reject",
    ),
    # Edge: Bac+5 in description without qualifier
    BenchmarkCase(
        "Dev",
        "Formation Bac+5 en informatique.",
        "safe",
        "Bac+5 without qualifier → UNKNOWN importance → soft penalty only",
    ),
    # Edge: company has Bac+5 employees (not a requirement)
    BenchmarkCase(
        "Développeur Python",
        "Notre équipe est composée de profils Bac+5 et Bac+3.",
        "safe",
        "Bac+5 describes team composition, not candidate requirement",
    ),
    # ────────────────────────────────────────────────────────────────────────
    # INCOMPATIBLE (objectively incompatible → SHOULD BE REJECTED)
    # ────────────────────────────────────────────────────────────────────────
    BenchmarkCase(
        "Dev",
        "Bac+5 obligatoire en informatique.",
        "incompatible",
        "Bac+5 mandatory exceeds Bac+2 max → reject",
    ),
    BenchmarkCase(
        "Software Engineer",
        "Master's degree required.",
        "incompatible",
        "Master=Bac+5 mandatory → reject",
    ),
    BenchmarkCase(
        "Ingénieur Logiciel",
        "Diplôme d'ingénieur requis.",
        "incompatible",
        "Engineering degree=Bac+5 mandatory → reject",
    ),
    BenchmarkCase(
        "Dev",
        "Licence professionnelle requise.",
        "incompatible",
        "Licence=Bac+3 mandatory > Bac+2 max → reject",
    ),
    BenchmarkCase(
        "Développeur Rust",
        "",
        "incompatible",
        "Rust in title, not in profile → principal skill reject",
    ),
    BenchmarkCase(
        "Java Developer",
        "",
        "incompatible",
        "Java in title, not in profile → principal skill reject",
    ),
    BenchmarkCase(
        "PhD required position",
        "PhD required. Doctorat obligatoire.",
        "incompatible",
        "PhD=Bac+8 mandatory → reject",
    ),
    BenchmarkCase(
        "Ingénieur .NET",
        "",
        "incompatible",
        ".NET in title, not in profile → principal skill reject",
    ),
    # ────────────────────────────────────────────────────────────────────────
    # UNCERTAIN (ambiguous/contradictory → MUST PASS, uncertainty flagged)
    # ────────────────────────────────────────────────────────────────────────
    # NOTE: "Bac+5 souhaité, Bac+2 minimum requis." was here (mixed-cues case,
    # intended to resolve to Bac+2 mandatory=satisfied). Removed from the
    # aggregate corpus 2026-09: it's not actually ambiguous evidence — it's a
    # pre-existing extraction bug (see TestKnownExtractionWindowBleedBug
    # below) where _extract_importance()'s 120-char context window bleeds
    # between the two adjacent education mentions, misclassifying the Bac+2
    # match as PREFERRED (inheriting "souhaité" from the nearby Bac+5 mention)
    # instead of MANDATORY, so resolution incorrectly returns Bac+5/PREFERRED
    # instead of Bac+2/MANDATORY. Left in the aggregate corpus, this pollutes
    # the safety metrics with a case that isn't genuinely ambiguous evidence.
    BenchmarkCase(
        "Dev",
        "Idéalement Bac+5, sinon expérience équivalente acceptée.",
        "uncertain",
        "Preferred Bac+5 + equivalency → definitely pass",
    ),
    BenchmarkCase(
        "Dev",
        "Formation souhaitée : Bac+2 à Bac+5.",
        "safe",
        "A held range counts at its lowest level (Bac+2, owner decision "
        "2026-09-18, #58) — the candidate's Bac+2 satisfies it. Previously "
        "labelled 'incompatible' under a 'range → highest level' rule that "
        "came from the Bezannes job, which #54 reclassified as enrolment.",
    ),
]


# ---------------------------------------------------------------------------
# Formerly known extraction bug (issue #19, see the removal note above the
# corpus): kept out of the aggregate safety metrics, asserted here as a
# regression test now that it is fixed.
# ---------------------------------------------------------------------------

class TestKnownExtractionWindowBleedBug:
    """'Bac+5 souhaité, Bac+2 minimum requis.' must resolve to Bac+2
    MANDATORY (satisfied, candidate max=2). It used to resolve to Bac+5
    PREFERRED (exceeds max, hard-rejects) because _extract_importance()'s
    120-char context window ignored clause boundaries, so the Bac+2 match
    picked up "souhaité" from the adjacent Bac+5 mention. Fixed by
    clause-aware context bounds in src/requirements/education.py (issue #19);
    kept here as a regression test."""

    def test_mixed_cues_should_resolve_to_lower_mandatory_level(self):
        from src.requirements.education import extract_education

        edu = extract_education("Bac+5 souhaité, Bac+2 minimum requis.")
        assert edu is not None
        assert edu.level_bac_plus == 2, (
            "Regression (#19): expected Bac+2 (MANDATORY), not Bac+5 "
            "(PREFERRED) — context window bleed between adjacent mentions. "
            f"Got: {edu}"
        )
        assert edu.importance == Importance.MANDATORY


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def _run_benchmark(corpus: list[BenchmarkCase]) -> dict:
    extractor = RequirementExtractor(user_skills=_SKILLS)
    gate = HardConstraintGate(
        user_max_education_level=_MAX_EDU,
        user_max_years_experience=_MAX_YEARS,
        principal_skill_gate=True,
        mandatory_skill_gate=True,
    )

    total = len(corpus)
    unsafe_rejections: list[BenchmarkCase] = []
    correctly_rejected: list[BenchmarkCase] = []
    missed_incompatible: list[BenchmarkCase] = []
    correct_passes: list[BenchmarkCase] = []
    uncertain_passed: list[BenchmarkCase] = []
    uncertain_rejected: list[BenchmarkCase] = []

    timings: list[float] = []

    for case in corpus:
        job = Job(id="j1", source="bench", title=case.title, description=case.description)
        if case.remote is not None:
            job.remote = case.remote

        t0 = time.perf_counter()
        reqs = extractor.extract(job)
        result = gate.check(reqs)
        elapsed = time.perf_counter() - t0
        timings.append(elapsed)

        if case.label == "safe":
            if not result.passes:
                unsafe_rejections.append(case)
            else:
                correct_passes.append(case)

        elif case.label == "incompatible":
            if not result.passes:
                correctly_rejected.append(case)
            else:
                missed_incompatible.append(case)

        elif case.label == "uncertain":
            if not result.passes:
                uncertain_rejected.append(case)
            else:
                uncertain_passed.append(case)

    # Compute metrics
    safe_count = sum(1 for c in corpus if c.label == "safe")
    incompatible_count = sum(1 for c in corpus if c.label == "incompatible")
    uncertain_count = sum(1 for c in corpus if c.label == "uncertain")

    unsafe_rejection_rate = len(unsafe_rejections) / max(safe_count, 1)
    rejection_recall = len(correctly_rejected) / max(incompatible_count, 1)
    rejection_precision = len(correctly_rejected) / max(
        len(correctly_rejected) + len(unsafe_rejections), 1
    )
    uncertain_pass_rate = len(uncertain_passed) / max(uncertain_count, 1)
    mean_latency_ms = (sum(timings) / len(timings) * 1000) if timings else 0

    return {
        "total": total,
        "safe_count": safe_count,
        "incompatible_count": incompatible_count,
        "uncertain_count": uncertain_count,
        "unsafe_rejection_rate": unsafe_rejection_rate,
        "rejection_recall": rejection_recall,
        "rejection_precision": rejection_precision,
        "uncertain_pass_rate": uncertain_pass_rate,
        "mean_latency_ms": mean_latency_ms,
        "unsafe_rejections": unsafe_rejections,
        "correctly_rejected": correctly_rejected,
        "missed_incompatible": missed_incompatible,
        "uncertain_rejected": uncertain_rejected,
    }


# ===========================================================================
# Safety gate: unsafe_rejection_rate MUST be zero
# ===========================================================================

class TestSafetyGate:
    """Primary safety test — any unsafe rejection is a blocker."""

    def test_no_unsafe_rejections(self):
        """All 'safe' corpus cases must pass the gate.

        This is the primary safety metric: false rejection rate must be zero.
        """
        metrics = _run_benchmark(BENCHMARK_CORPUS)
        unsafe = metrics["unsafe_rejections"]
        if unsafe:
            details = "\n".join(
                f"  - [{c.label}] '{c.title}': {c.reason}"
                for c in unsafe
            )
            pytest.fail(
                f"UNSAFE REJECTION: {len(unsafe)} safe case(s) incorrectly rejected:\n{details}\n"
                f"Unsafe rejection rate: {metrics['unsafe_rejection_rate']:.1%}\n"
                "Any unsafe rejection is a release blocker."
            )

    def test_uncertain_cases_always_pass(self):
        """All 'uncertain' corpus cases must pass (UNKNOWN evidence → never reject)."""
        metrics = _run_benchmark(BENCHMARK_CORPUS)
        rejected_uncertain = metrics["uncertain_rejected"]
        if rejected_uncertain:
            details = "\n".join(
                f"  - '{c.title}': {c.reason}"
                for c in rejected_uncertain
            )
            pytest.fail(
                f"UNCERTAIN CASES REJECTED: {len(rejected_uncertain)} uncertain case(s) "
                f"incorrectly rejected:\n{details}\n"
                "Uncertain/ambiguous evidence must never cause rejection."
            )


# ===========================================================================
# Recall / precision metrics (informational — failures logged, not hard blocked)
# ===========================================================================

class TestBenchmarkMetrics:
    """Informational metrics — reported for monitoring; not hard release blockers."""

    @pytest.fixture(scope="class")
    def metrics(self):
        return _run_benchmark(BENCHMARK_CORPUS)

    def test_rejection_recall_above_threshold(self, metrics):
        """At least 70% of known incompatible jobs should be rejected."""
        recall = metrics["rejection_recall"]
        assert recall >= 0.70, (
            f"Rejection recall {recall:.1%} below 70% threshold. "
            f"Missed: {[c.title for c in metrics['missed_incompatible']]}"
        )

    def test_rejection_precision_above_threshold(self, metrics):
        """At least 95% of rejections should be for genuinely incompatible jobs."""
        precision = metrics["rejection_precision"]
        assert precision >= 0.95, (
            f"Rejection precision {precision:.1%} below 95% threshold."
        )

    def test_uncertain_pass_rate_is_100(self, metrics):
        """All uncertain corpus entries must pass (expected 100%)."""
        rate = metrics["uncertain_pass_rate"]
        assert rate == 1.0, (
            f"Uncertain pass rate {rate:.1%} — uncertain cases must always pass."
        )

    def test_latency_under_100ms_per_job(self, metrics):
        """Mean per-job evaluation latency should be under 100ms (heuristic-only)."""
        latency = metrics["mean_latency_ms"]
        assert latency < 100, (
            f"Mean latency {latency:.1f}ms exceeds 100ms budget. "
            "Investigate slow extraction patterns."
        )

    def test_print_benchmark_summary(self, metrics, capsys):
        """Print human-readable benchmark summary for CI logs."""
        print(
            f"\n{'='*60}\n"
            f"HARD FILTER BENCHMARK SUMMARY\n"
            f"{'='*60}\n"
            f"Total corpus:          {metrics['total']}\n"
            f"  Safe (must pass):    {metrics['safe_count']}\n"
            f"  Incompatible:        {metrics['incompatible_count']}\n"
            f"  Uncertain:           {metrics['uncertain_count']}\n"
            f"\nSafety metrics:\n"
            f"  Unsafe rejection rate: {metrics['unsafe_rejection_rate']:.1%}  [TARGET: 0%]\n"
            f"  Rejection recall:      {metrics['rejection_recall']:.1%}  [TARGET: ≥70%]\n"
            f"  Rejection precision:   {metrics['rejection_precision']:.1%}  [TARGET: ≥95%]\n"
            f"  Uncertain pass rate:   {metrics['uncertain_pass_rate']:.1%}  [TARGET: 100%]\n"
            f"  Mean latency:          {metrics['mean_latency_ms']:.1f}ms  [TARGET: <100ms]\n"
        )
        if metrics["missed_incompatible"]:
            print(f"\nMissed incompatible ({len(metrics['missed_incompatible'])}):")
            for c in metrics["missed_incompatible"]:
                print(f"  - '{c.title}': {c.reason}")


# ===========================================================================
# Work mode evidence benchmark
# ===========================================================================

class TestWorkModeBenchmark:
    """Work mode evidence extraction accuracy."""

    WORK_MODE_CORPUS = [
        # (text, remote_meta, expected_signal, contradiction_expected)
        ("100% télétravail", True, WorkModeSignal.FULL_REMOTE, False),
        ("100% remote position", True, WorkModeSignal.FULL_REMOTE, False),
        ("Fully remote, work from anywhere", True, WorkModeSignal.FULL_REMOTE, False),
        ("3 days on site per week", True, WorkModeSignal.HYBRID, True),   # contradicts remote=True
        ("4 jours au bureau par semaine", True, WorkModeSignal.HYBRID, True),
        ("Présence de 5 jours obligatoire", False, WorkModeSignal.ON_SITE, False),
        ("Mode hybride", None, WorkModeSignal.HYBRID, False),
        ("Remote-friendly", None, WorkModeSignal.UNKNOWN, False),
        ("Télétravail possible", None, WorkModeSignal.UNKNOWN, False),
        ("Pas de télétravail", True, WorkModeSignal.ON_SITE, True),  # contradicts remote=True
        ("Exciting opportunity in Paris", None, WorkModeSignal.UNKNOWN, False),
    ]

    def test_extraction_accuracy(self):
        correct = 0
        total = len(self.WORK_MODE_CORPUS)
        failures = []
        for text, _, expected_signal, _ in self.WORK_MODE_CORPUS:
            result = extract_work_mode_evidence(text)
            if result.resolved_signal == expected_signal:
                correct += 1
            else:
                failures.append(
                    f"'{text}' → {result.resolved_signal!r}, expected {expected_signal!r}"
                )
        accuracy = correct / total
        if failures:
            print(f"\nWork mode extraction failures: {failures}")
        assert accuracy >= 0.80, (
            f"Work mode extraction accuracy {accuracy:.1%} below 80%.\nFailures: {failures}"
        )

    def test_contradiction_detection(self):
        detected = 0
        expected_contradictions = [
            (text, meta)
            for text, meta, _, contradiction_expected in self.WORK_MODE_CORPUS
            if contradiction_expected
        ]
        if not expected_contradictions:
            return
        for text, remote_meta in expected_contradictions:
            result = detect_work_mode_contradiction(remote_meta, text)
            if result:
                detected += 1
        recall = detected / len(expected_contradictions)
        assert recall >= 0.50, (
            f"Contradiction detection recall {recall:.1%} below 50%. "
            "Check work_mode_evidence.py patterns."
        )
