"""Tests for job collectors: RemoteOK, Jobicy, WorkingNomads, and registry."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.jobicy import JobicyCollector
from src.collectors.registry import CollectorRegistry
from src.collectors.remoteok import RemoteOKCollector
from src.collectors.workingnomads import WorkingNomadsCollector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(body: Any, status_code: int = 200) -> MagicMock:
    """Return a mock httpx.Response-like object."""
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    if isinstance(body, (dict, list)):
        mock.text = json.dumps(body)
        mock.json.return_value = body
    else:
        mock.text = str(body)
        mock.json.return_value = body
    mock.raise_for_status = MagicMock()
    return mock


SAMPLE_REMOTEOK_METADATA: dict[str, Any] = {
    "legal": "This is the RemoteOK API",
}

SAMPLE_REMOTEOK_JOB: dict[str, Any] = {
    "id": 12345,
    "slug": "remoteok-12345",
    "company": "RemoteCo",
    "position": "Senior Python Engineer",
    "description": "A remote Python job",
    "location": "Worldwide",
    "tags": ["python", "remote"],
    "url": "https://remoteok.com/l/12345",
    "apply_url": "https://remoteok.com/apply/12345",
    "salary_min": 80000,
    "salary_max": 120000,
    "date": "2026-08-01",
}

SAMPLE_JOBICY_JOB: dict[str, Any] = {
    "id": 99,
    "jobSlug": "99-senior-backend",
    "jobTitle": "Senior Backend Engineer",
    "companyName": "CloudCorp",
    "jobDescription": "Build scalable APIs",
    "jobExcerpt": "Backend engineer role",
    "jobType": ["full-time"],
    "jobGeo": "worldwide",
    "jobIndustry": ["Engineering"],
    "url": "https://jobicy.com/jobs/99",
    "pubDate": "2026-08-01 00:00:00",
    "salaryMin": 90000,
    "salaryMax": 130000,
    "salaryCurrency": "USD",
}

SAMPLE_WORKINGNOMADS_JOB: dict[str, Any] = {
    "url": "https://www.workingnomads.com/job/go/999/",
    "title": "Full Stack Developer",
    "company_name": "RemoteStartup",
    "description": "Build our platform remotely",
    "category_name": "development",
    "tags": "python, javascript, react",
    "location": "Worldwide",
    "pub_date": "2026-08-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_new_collectors_registered(self) -> None:
        reg = CollectorRegistry()
        names = reg.list_collectors()
        assert "remoteok" in names
        assert "jobicy" in names
        assert "workingnomads" in names
        assert "bundesagentur" in names
        assert "arbetsformedlingen" in names
        assert "smartrecruiters" in names
        # rss alias must also be registered
        assert "rss" in names

    def test_rss_alias_is_remote_collector(self) -> None:
        from src.collectors.remote import RemoteCollector
        reg = CollectorRegistry()
        assert reg.get_collector_class("rss") is RemoteCollector


# ---------------------------------------------------------------------------
# RemoteOK
# ---------------------------------------------------------------------------

class TestRemoteOKCollector:
    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        api_response = [SAMPLE_REMOTEOK_METADATA, SAMPLE_REMOTEOK_JOB]
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   return_value=_mock_response(api_response)):
            collector = RemoteOKCollector()
            result = collector.collect()

        assert result.source_name == "remoteok"
        assert len(result.jobs) == 1
        assert result.errors == []
        job = result.jobs[0]
        assert job["title"] == "Senior Python Engineer"
        assert job["company"] == "RemoteCo"
        assert job["remote"] is True
        assert "python" in job["skills"]

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_empty_response(self, _mock_rl: MagicMock) -> None:
        # Only metadata, no jobs
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_REMOTEOK_METADATA])):
            result = RemoteOKCollector().collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_network_error(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   side_effect=RuntimeError("connect error")):
            result = RemoteOKCollector().collect()
        assert result.jobs == []
        assert any("connect error" in e for e in result.errors)

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_max_jobs(self, _mock_rl: MagicMock) -> None:
        extra_job = dict(SAMPLE_REMOTEOK_JOB, id=99999, slug="other-job")
        api_response = [SAMPLE_REMOTEOK_METADATA, SAMPLE_REMOTEOK_JOB, extra_job]
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   return_value=_mock_response(api_response)):
            result = RemoteOKCollector(config={"max_jobs": 1}).collect()
        assert len(result.jobs) == 1

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_malformed_entry_skipped(self, _mock_rl: MagicMock) -> None:
        bad_entry = "not a dict"
        api_response = [SAMPLE_REMOTEOK_METADATA, bad_entry, SAMPLE_REMOTEOK_JOB]
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   return_value=_mock_response(api_response)):
            result = RemoteOKCollector().collect()
        assert len(result.jobs) == 1  # bad entry skipped

    def test_normalize_fields(self) -> None:
        out = RemoteOKCollector._normalize_fields(SAMPLE_REMOTEOK_JOB)
        assert out["title"] == "Senior Python Engineer"
        assert out["company"] == "RemoteCo"
        assert out["source_job_id"] == "12345"
        assert out["remote"] is True
        assert out["salary_min"] == 80000.0
        assert out["salary_max"] == 120000.0
        assert out["salary_currency"] == "USD"

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_multi_tag_queries_deduplication(self, _mock_rl: MagicMock) -> None:
        """Multi-query: same job appearing under two tags is collected once."""
        dup_job = dict(SAMPLE_REMOTEOK_JOB, id=12345)
        extra_job = dict(SAMPLE_REMOTEOK_JOB, id=99999, slug="devops-job")
        resp1 = [SAMPLE_REMOTEOK_METADATA, dup_job]
        resp2 = [SAMPLE_REMOTEOK_METADATA, dup_job, extra_job]
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   side_effect=[_mock_response(resp1), _mock_response(resp2)]) as mock_fetch:
            result = RemoteOKCollector(
                config={"discovery_queries": ["python", "devops"]}
            ).collect()

        assert mock_fetch.call_count == 2
        ids = [j["source_job_id"] for j in result.jobs]
        assert ids.count("12345") == 1  # deduplicated
        assert len(result.jobs) == 2

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_discovery_queries_passed_as_tags_param(self, _mock_rl: MagicMock) -> None:
        """discovery_queries → each query sent as ?tags= parameter."""
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_REMOTEOK_METADATA])) as mock_fetch:
            RemoteOKCollector(config={"discovery_queries": ["python"]}).collect()

        params = mock_fetch.call_args.kwargs.get("params", {})
        assert params.get("tags") == "python"

    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_fallback_no_tags_param_when_no_queries(self, _mock_rl: MagicMock) -> None:
        """No discovery_queries → single unfiltered request with no ?tags= param."""
        with patch("src.collectors.remoteok.RemoteOKCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_REMOTEOK_METADATA])) as mock_fetch:
            RemoteOKCollector().collect()

        params = mock_fetch.call_args.kwargs.get("params", {})
        assert "tags" not in params


# ---------------------------------------------------------------------------
# Jobicy
# ---------------------------------------------------------------------------

class TestJobicyCollector:
    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        api_response = {"jobs": [SAMPLE_JOBICY_JOB], "jobCount": 1}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(api_response)):
            result = JobicyCollector().collect()

        assert result.source_name == "jobicy"
        assert len(result.jobs) >= 1
        job = result.jobs[0]
        assert job["title"] == "Senior Backend Engineer"
        assert job["company"] == "CloudCorp"
        assert job["remote"] is True

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_empty_response(self, _mock_rl: MagicMock) -> None:
        api_response = {"jobs": [], "jobCount": 0}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(api_response)):
            result = JobicyCollector().collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_network_error(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   side_effect=RuntimeError("timeout")):
            result = JobicyCollector().collect()
        assert result.jobs == []
        assert len(result.errors) > 0

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_max_jobs(self, _mock_rl: MagicMock) -> None:
        extra_job = dict(SAMPLE_JOBICY_JOB, id=100, jobSlug="100-other")
        api_response = {"jobs": [SAMPLE_JOBICY_JOB, extra_job]}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(api_response)):
            result = JobicyCollector(config={"max_jobs": 1}).collect()
        assert len(result.jobs) == 1

    def test_normalize_fields(self) -> None:
        out = JobicyCollector._normalize_fields(SAMPLE_JOBICY_JOB)
        assert out["title"] == "Senior Backend Engineer"
        assert out["company"] == "CloudCorp"
        assert out["source_job_id"] == "99"
        assert out["remote"] is True
        assert out["salary_min"] == 90000.0
        assert "Engineering" in out["skills"]

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_multi_industry_queries_deduplication(self, _mock_rl: MagicMock) -> None:
        """Baseline + two slugs: same job ID collected across queries only once."""
        dup_job = dict(SAMPLE_JOBICY_JOB, id=99)
        extra_job = dict(SAMPLE_JOBICY_JOB, id=200, jobSlug="200-data-eng", jobTitle="Data Engineer")
        resp_baseline = {"jobs": [dup_job]}
        resp_eng = {"jobs": [dup_job]}          # same id → deduped
        resp_cyber = {"jobs": [dup_job, extra_job]}  # extra_job is new
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   side_effect=[
                       _mock_response(resp_baseline),
                       _mock_response(resp_eng),
                       _mock_response(resp_cyber),
                   ]) as mock_fetch:
            result = JobicyCollector(
                config={"discovery_queries": ["engineering", "cybersecurity"]}
            ).collect()

        assert mock_fetch.call_count == 3  # baseline + 2 slugs
        ids = [j["source_job_id"] for j in result.jobs]
        assert ids.count("99") == 1  # deduplicated across all three queries
        assert len(result.jobs) == 2  # dup_job + extra_job

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_multi_query_partial_failure(self, _mock_rl: MagicMock) -> None:
        """A failing targeted query records an error; baseline jobs are kept."""
        resp_ok = {"jobs": [SAMPLE_JOBICY_JOB]}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   side_effect=[_mock_response(resp_ok), RuntimeError("timeout")]):
            result = JobicyCollector(
                config={"discovery_queries": ["engineering"]}
            ).collect()

        assert len(result.jobs) == 1   # baseline job survives
        assert len(result.errors) == 1

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_discovery_queries_passed_as_industry_param(self, _mock_rl: MagicMock) -> None:
        """discovery_queries → each query sent as ?industry= parameter."""
        resp = {"jobs": []}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(resp)) as mock_fetch:
            JobicyCollector(config={"discovery_queries": ["engineering"]}).collect()

        params = mock_fetch.call_args.kwargs.get("params", {})
        assert params.get("industry") == "engineering"

    # --- Regression tests for baseline+slugs behavior ---

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_baseline_always_collected_when_discovery_queries_exist(self, _mock_rl: MagicMock) -> None:
        """Baseline (no industry filter) is always the first query even with discovery_queries."""
        resp = {"jobs": []}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(resp)) as mock_fetch:
            JobicyCollector(config={"discovery_queries": ["engineering"]}).collect()

        assert mock_fetch.call_count == 2  # baseline + 1 slug
        first_call_params = mock_fetch.call_args_list[0].kwargs.get("params", {})
        assert "industry" not in first_call_params  # baseline has no industry param

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_targeted_queries_executed_after_baseline(self, _mock_rl: MagicMock) -> None:
        """Each discovery_query slug is executed as a separate API call after the baseline."""
        resp = {"jobs": []}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(resp)) as mock_fetch:
            JobicyCollector(config={"discovery_queries": ["engineering", "cybersecurity"]}).collect()

        assert mock_fetch.call_count == 3  # baseline + 2 slugs
        slug_calls = mock_fetch.call_args_list[1:]
        industries = [c.kwargs.get("params", {}).get("industry") for c in slug_calls]
        assert industries == ["engineering", "cybersecurity"]

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_global_dedup_across_baseline_and_slugs(self, _mock_rl: MagicMock) -> None:
        """A job that appears in both the baseline and a slug response is collected once."""
        job_a = dict(SAMPLE_JOBICY_JOB, id=1, jobSlug="1-eng")
        job_b = dict(SAMPLE_JOBICY_JOB, id=2, jobSlug="2-sec")
        resp_baseline = {"jobs": [job_a, job_b]}
        resp_slug = {"jobs": [job_a]}  # job_a is a subset of the baseline
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   side_effect=[_mock_response(resp_baseline), _mock_response(resp_slug)]):
            result = JobicyCollector(config={"discovery_queries": ["engineering"]}).collect()

        ids = [j["source_job_id"] for j in result.jobs]
        assert ids.count("1") == 1
        assert len(result.jobs) == 2

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_max_jobs_respected_across_baseline_and_slugs(self, _mock_rl: MagicMock) -> None:
        """max_jobs stops collection after the limit is reached, regardless of query order."""
        job_a = dict(SAMPLE_JOBICY_JOB, id=1)
        job_b = dict(SAMPLE_JOBICY_JOB, id=2, jobSlug="2-eng")
        resp_baseline = {"jobs": [job_a, job_b]}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   return_value=_mock_response(resp_baseline)) as mock_fetch:
            result = JobicyCollector(config={"discovery_queries": ["engineering"], "max_jobs": 1}).collect()

        assert len(result.jobs) == 1
        # Baseline hit the limit → slug call never happens.
        assert mock_fetch.call_count == 1

    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_failed_targeted_query_does_not_discard_baseline(self, _mock_rl: MagicMock) -> None:
        """When a slug query fails, baseline jobs are retained and the error is recorded."""
        job = dict(SAMPLE_JOBICY_JOB, id=42)
        resp_baseline = {"jobs": [job]}
        with patch("src.collectors.jobicy.JobicyCollector._fetch_with_retry",
                   side_effect=[_mock_response(resp_baseline), RuntimeError("network failure")]):
            result = JobicyCollector(config={"discovery_queries": ["engineering"]}).collect()

        assert len(result.jobs) == 1
        assert result.jobs[0]["source_job_id"] == "42"
        assert len(result.errors) == 1
        assert "engineering" in result.errors[0]



# ---------------------------------------------------------------------------
# WorkingNomads
# ---------------------------------------------------------------------------

class TestWorkingNomadsCollector:
    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_successful_collection(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_WORKINGNOMADS_JOB])):
            result = WorkingNomadsCollector().collect()

        assert result.source_name == "workingnomads"
        assert len(result.jobs) == 1
        job = result.jobs[0]
        assert job["title"] == "Full Stack Developer"
        assert job["company"] == "RemoteStartup"
        assert job["remote"] is True
        assert "python" in job["skills"]

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_empty_response(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([])):
            result = WorkingNomadsCollector().collect()
        assert result.jobs == []
        assert result.errors == []

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_network_error(self, _mock_rl: MagicMock) -> None:
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   side_effect=RuntimeError("timeout")):
            result = WorkingNomadsCollector().collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "timeout" in result.errors[0]

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_single_request_no_category_loop(self, _mock_rl: MagicMock) -> None:
        # Verifies the fix for the ~32s multi-category bottleneck: the
        # collector must make exactly one HTTP request per run now that the
        # API's category filter has been confirmed to be a no-op.
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_WORKINGNOMADS_JOB])) as mock_fetch:
            WorkingNomadsCollector().collect()
        assert mock_fetch.call_count == 1

    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_max_jobs(self, _mock_rl: MagicMock) -> None:
        extra_job = dict(SAMPLE_WORKINGNOMADS_JOB, url="https://www.workingnomads.com/job/go/1000/")
        with patch("src.collectors.workingnomads.WorkingNomadsCollector._fetch_with_retry",
                   return_value=_mock_response([SAMPLE_WORKINGNOMADS_JOB, extra_job])):
            result = WorkingNomadsCollector(config={"max_jobs": 1}).collect()
        assert len(result.jobs) == 1

    def test_normalize_fields(self) -> None:
        out = WorkingNomadsCollector._normalize_fields(SAMPLE_WORKINGNOMADS_JOB)
        assert out["title"] == "Full Stack Developer"
        assert out["company"] == "RemoteStartup"
        assert out["remote"] is True
        assert "python" in out["skills"]
        assert "javascript" in out["skills"]
