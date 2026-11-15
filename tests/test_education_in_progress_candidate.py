"""A qualification the candidate is preparing is not held, and is not proof they fall short.

Held Bac, BTS in progress, offer asking Bac+2: the level is neither met nor missed, so the
gate records the doubt and lets the offer through, unpenalised. Anything above the level
being prepared still rejects, and a profile with no in-progress entry is unchanged.
"""

from __future__ import annotations

from src.models.profile import JobSearchProfile
from src.requirements.education import (
    candidate_education,
    candidate_education_in_progress,
    extract_education,
)
from src.requirements.gate import HardConstraintGate, gate_jobs
from src.requirements.models import ConstraintState, JobRequirements


def _profile(*entries: tuple[str, int, bool]) -> JobSearchProfile:
    return JobSearchProfile(profile_version=2, education=[
        {"id": f"e{i}", "qualification": name, "level": level, "in_progress": in_progress}
        for i, (name, level, in_progress) in enumerate(entries)
    ])


BAC_PREPARING_BTS = _profile(("Bac", 4, False), ("BTS", 5, True))


def _gate(profile: JobSearchProfile) -> HardConstraintGate:
    max_bac, label, held = candidate_education(profile)
    preparing, preparing_label = candidate_education_in_progress(profile)
    return HardConstraintGate(
        user_max_education_level=max_bac, user_education_label=label,
        user_held_education_level=held,
        user_in_progress_education_level=preparing,
        user_in_progress_education_label=preparing_label,
    )


def _check(profile: JobSearchProfile, text: str):
    return _gate(profile).check(JobRequirements(education=extract_education(text)))


def test_in_progress_level_is_read_only_above_the_held_one():
    assert candidate_education_in_progress(BAC_PREPARING_BTS) == (2, "level 5 (BTS)")
    assert candidate_education_in_progress(_profile(("Bac", 4, False))) == (None, None)
    # preparing something at or below what is already held adds nothing
    assert candidate_education_in_progress(
        _profile(("Master", 7, False), ("BTS", 5, True))) == (None, None)
    assert candidate_education_in_progress(JobSearchProfile(max_education="bac+2")) == (None, None)


def test_required_level_being_prepared_passes_with_the_doubt_recorded():
    r = _check(BAC_PREPARING_BTS, "Bac+2 exigé en informatique.")
    assert r.passes and not r.reasons
    assert r.states["education"] == ConstraintState.UNKNOWN
    assert any("level 5 (BTS)" in f and "preparing" in f for f in r.flags)


def test_without_the_in_progress_entry_the_same_offer_still_rejects():
    r = _check(_profile(("Bac", 4, False)), "Bac+2 exigé en informatique.")
    assert not r.passes and r.reasons[0].type == "education"


def test_a_level_above_the_one_being_prepared_still_rejects():
    r = _check(BAC_PREPARING_BTS, "Bac+5 exigé en informatique.")
    assert not r.passes
    assert r.reasons[0].candidate_value == "level 4 (Bac)"


def test_the_level_being_prepared_is_kept_with_a_lower_score():
    """Owner decision D13a: accepted, with the visible note and the usual education penalty."""
    job = type("J", (), {"id": "j", "title": "Technicien", "description": ""})()
    for text in ("Bac+2 souhaité.", "Bac+2 exigé en informatique."):
        req = JobRequirements(education=extract_education(text))
        passing, results = gate_jobs([job], {"j": req}, _gate(BAC_PREPARING_BTS))
        assert passing and results["j"].education_penalty > 0, text
        assert any("preparing" in f for f in results["j"].flags), text
    # the penalty also applies above the level being prepared (a desirable level, not a bar)
    req = JobRequirements(education=extract_education("Bac+5 souhaité."))
    passing, results = gate_jobs([job], {"j": req}, _gate(BAC_PREPARING_BTS))
    assert passing and results["j"].education_penalty > 0
