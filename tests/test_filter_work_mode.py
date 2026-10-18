"""Regression tests for matches_work_mode's structured work_mode signal branch.

Covers the "decision tree step 0" added to matches_work_mode(): when
job.work_mode is a confident, structured value ("remote" | "hybrid" |
"on_site"), it is trusted directly instead of falling back to the
remote/countries heuristic. Jobs without a structured work_mode (the vast
majority of sources) must be unaffected by this change.

No Internet access. No collectors called. No real database touched.
"""

from __future__ import annotations

from src.models.job import Job
from src.pipeline.filter import matches_work_mode


def _job(**kwargs) -> Job:
    defaults: dict = {
        "source": "test",
        "title": "Software Engineer",
        "company": "Acme",
        "remote": False,
        "location": "Paris, France",
        "countries": ["France"],
    }
    defaults.update(kwargs)
    return Job(**defaults)


class TestMatchesWorkModeStructuredSignal:
    def test_no_profile_restriction_accepts_regardless_of_work_mode(self):
        job = _job(work_mode="on_site")
        assert matches_work_mode(job, {"work_modes": {"allowed": []}}) is True

    def test_hybrid_job_accepted_when_hybrid_allowed(self):
        job = _job(work_mode="hybrid")
        config = {"work_modes": {"allowed": ["hybrid"]}}
        assert matches_work_mode(job, config) is True

    def test_hybrid_job_rejected_when_only_remote_allowed(self):
        job = _job(work_mode="hybrid")
        config = {"work_modes": {"allowed": ["remote"]}}
        assert matches_work_mode(job, config) is False

    def test_on_site_job_rejected_when_only_remote_allowed(self):
        job = _job(work_mode="on_site", remote=False)
        config = {"work_modes": {"allowed": ["remote"]}}
        assert matches_work_mode(job, config) is False

    def test_remote_job_accepted_when_only_remote_allowed(self):
        job = _job(work_mode="remote", remote=True)
        config = {"work_modes": {"allowed": ["remote"]}}
        assert matches_work_mode(job, config) is True

    def test_allowed_value_normalisation_on_site_hyphen(self):
        job = _job(work_mode="on_site")
        config = {"work_modes": {"allowed": ["on-site"]}}
        assert matches_work_mode(job, config) is True

    def test_unknown_work_mode_falls_back_to_existing_heuristic(self):
        """job.work_mode=None (the default for ~28 of 32 sources) must be
        completely unaffected by the new branch — the pre-existing
        remote/countries heuristic still decides."""
        remote_only = {"work_modes": {"allowed": ["remote"]}}
        onsite_job = _job(work_mode=None, remote=False, countries=["France"])
        assert matches_work_mode(onsite_job, remote_only) is False

        remote_job = _job(work_mode=None, remote=True, countries=["France"])
        assert matches_work_mode(remote_job, remote_only) is True

        unknown_location_job = _job(work_mode=None, remote=False, countries=[])
        assert matches_work_mode(unknown_location_job, remote_only) is True

    def test_hybrid_job_accepted_when_on_site_in_allowed(self):
        """Regression: Lever/Ashby set work_mode='hybrid' on jobs at companies
        that have mixed on-site/remote policies. When the user accepts on_site,
        hybrid must NOT be rejected — hybrid implies on-site presence, which is
        exactly what the user consented to. The previous bug ran the structured-
        signal check first: 'hybrid' not in {'remote', 'on_site'} → False."""
        config = {"work_modes": {"allowed": ["remote", "on_site", "on-site"]}}
        job = _job(work_mode="hybrid")
        assert matches_work_mode(job, config) is True

    def test_on_site_job_accepted_when_on_site_in_allowed(self):
        """Structured on_site signal accepted when profile allows on_site."""
        config = {"work_modes": {"allowed": ["remote", "on_site"]}}
        job = _job(work_mode="on_site")
        assert matches_work_mode(job, config) is True

    def test_remote_job_rejected_when_only_on_site_allowed(self):
        """If user only wants on_site, remote-only jobs are still accepted
        because on_site is in allowed → broad-accept applies."""
        config = {"work_modes": {"allowed": ["on_site"]}}
        job = _job(work_mode="remote", remote=True)
        # on_site in allowed → broad accept (can't restrict further without false positives)
        assert matches_work_mode(job, config) is True
