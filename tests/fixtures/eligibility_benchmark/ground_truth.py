"""Ground truth for the eligibility gate benchmark (tests/fixtures/eligibility_benchmark/jobs.py).

Joined to results ONLY after the eligibility gate has produced its verdicts —
see scripts/benchmark_eligibility_gate.py. Never fed to the prompt or the
extractor.

Evaluated against config/profile.yaml's profile: max_education bac+2,
experience.max_years 3, preferred_levels include internship/alternance/
junior/entry_level/graduate.

``expected`` is one of:
  SHOULD_ACCEPT — the eligibility gate must NOT reject this job
  SHOULD_REJECT — the eligibility gate SHOULD reject this job (high confidence)

Every case here is specifically chosen to be false-rejection-prone: a title
that could trigger an over-eager implicit-mismatch judgment, paired with
context that should (or should not) override it. ``category`` groups them for
the report.
"""

from __future__ import annotations

GROUND_TRUTH: dict[str, dict] = {
    "eg01": {
        "expected": "SHOULD_REJECT",
        "category": "implicit_education_mismatch",
        "note": "Ingenieur title, no explicit degree requirement stated, no junior/alternance context.",
    },
    "eg02": {
        "expected": "SHOULD_ACCEPT",
        "category": "alternance_overrides_title",
        "note": "Same title family as eg01, but explicit alternance framing makes it accessible.",
    },
    "eg03": {
        "expected": "SHOULD_ACCEPT",
        "category": "explicit_lower_level_overrides_title",
        "note": "Ingenieur title, but explicitly states Bac+2 accepted.",
    },
    "eg04": {
        "expected": "SHOULD_ACCEPT",
        "category": "internship_overrides_title",
        "note": "Internship framing, enrolled-student requirement — accessible despite 'Software Engineer' title.",
    },
    "eg05": {
        "expected": "SHOULD_REJECT",
        "category": "implicit_seniority_mismatch",
        "note": "Senior title, description reinforces leadership/mentoring expectations.",
    },
    "eg06": {
        "expected": "SHOULD_ACCEPT",
        "category": "junior_title_no_mismatch",
        "note": "Junior title, Bac+2 explicitly accepted — must not be over-rejected.",
    },
    "eg07": {
        "expected": "SHOULD_ACCEPT",
        "category": "description_overrides_senior_title",
        "note": "Core 'context must matter' case: title says Senior, description is actually an internship for Bac+2/3 students.",
    },
    "eg08": {
        "expected": "SHOULD_REJECT",
        "category": "implicit_education_mismatch",
        "note": "Architecte logiciel — no explicit degree stated, but 'expertise reconnue' signals a senior/high-level role.",
    },
    "eg09": {
        "expected": "SHOULD_ACCEPT",
        "category": "role_appropriate_for_profile",
        "note": "Technicien role, Bac+2 explicitly stated, debutant accepted — no mismatch to find.",
    },
    "eg10": {
        "expected": "SHOULD_REJECT",
        "category": "implicit_seniority_mismatch",
        "note": "Lead Developer, explicit team-leadership and prior-experience expectations.",
    },
    "eg11": {
        "expected": "SHOULD_ACCEPT",
        "category": "alternance_overrides_title",
        "note": "Alternance BTS-level role, no professional experience required.",
    },
    "eg12": {
        "expected": "SHOULD_REJECT",
        "category": "implicit_seniority_mismatch",
        "note": "Staff Engineer, explicit senior individual-contributor framing.",
    },
}
