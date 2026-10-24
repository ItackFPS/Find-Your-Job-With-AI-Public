"""Phase NOW / I5 regression tests: a malformed (non-JSON) HTTP response body
must never crash a collector.

Base contract (src/collectors/base.py): "Network errors, HTTP errors,
malformed responses, and timeouts must be caught and turned into error
strings. One failing source must never stop the rest of the pipeline."

Before this fix, the following collectors called ``response.json()`` inside a
try block whose except clause only caught ``(httpx.HTTPError, RuntimeError)``
— ``json.JSONDecodeError`` (a ``ValueError`` subclass) is neither, so a
malformed-but-200-status response would propagate uncaught out of
``collect()``, violating the contract. Each test below simulates exactly that
response shape (a 200 response whose body isn't valid JSON) and asserts
``collect()`` still returns a normal ``CollectorResult`` with the failure
recorded in ``errors``, never an exception.

Collectors confirmed ALREADY safe before this fix (broad ``except Exception``
already in place) are deliberately NOT duplicated here: eures,
france_travail's search-page parsing, indeed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx

from src.collectors.arbetsformedlingen import ArbetsformedlingenCollector
from src.collectors.bundesagentur import BundesagenturCollector
from src.collectors.greenhouse import GreenhouseCollector
from src.collectors.jobicy import JobicyCollector
from src.collectors.lever import LeverCollector
from src.collectors.remote import RemoteCollector
from src.collectors.remoteok import RemoteOKCollector
from src.collectors.smartrecruiters import SmartRecruitersCollector
from src.collectors.workingnomads import WorkingNomadsCollector


def _malformed_json_response(status_code: int = 200) -> MagicMock:
    """A response that looks successful (2xx, raise_for_status is a no-op)
    but whose body is not valid JSON — exactly what a misbehaving or
    mid-incident API can return (an HTML error page served with a 200,
    a truncated body, etc.)."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = "<html>not json</html>"
    resp.raise_for_status = MagicMock()
    resp.json.side_effect = json.JSONDecodeError("Expecting value", resp.text, 0)
    return resp


class TestGreenhouseJsonSafety:
    @patch("src.collectors.greenhouse.GreenhouseCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = GreenhouseCollector(config={"board_identifier": "acme"})
        with patch.object(
            GreenhouseCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "acme" in result.errors[0]


class TestWorkingNomadsJsonSafety:
    @patch("src.collectors.workingnomads.WorkingNomadsCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = WorkingNomadsCollector()
        with patch.object(
            WorkingNomadsCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestRemoteOKJsonSafety:
    @patch("src.collectors.remoteok.RemoteOKCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = RemoteOKCollector()
        with patch.object(
            RemoteOKCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestJobicyJsonSafety:
    @patch("src.collectors.jobicy.JobicyCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = JobicyCollector()
        with patch.object(
            JobicyCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestLeverJsonSafety:
    @patch("src.collectors.lever.LeverCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = LeverCollector(config={"sites": ["acme"]})
        with patch.object(
            LeverCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "acme" in result.errors[0]


class TestSmartRecruitersJsonSafety:
    @patch("src.collectors.smartrecruiters.SmartRecruitersCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = SmartRecruitersCollector(config={"board_identifier": "acme"})
        with patch.object(
            SmartRecruitersCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestBundesagenturJsonSafety:
    @patch("src.collectors.bundesagentur.BundesagenturCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = BundesagenturCollector(config={"search_terms": ["python"]})
        with patch.object(
            BundesagenturCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestArbetsformedlingenJsonSafety:
    @patch("src.collectors.arbetsformedlingen.ArbetsformedlingenCollector._enforce_rate_limit")
    def test_malformed_response_is_captured_not_raised(self, _rl: MagicMock) -> None:
        collector = ArbetsformedlingenCollector(config={"search_terms": ["python"]})
        with patch.object(
            ArbetsformedlingenCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestRemoteApiJsonSafety:
    """RemoteCollector's JSON-API path (type != 'rss'). Its RSS path
    (feedparser-based, no .json() call) is unaffected and not covered here."""

    def test_malformed_response_is_captured_not_raised(self) -> None:
        collector = RemoteCollector(config={"careers_url": "https://example.com/jobs.json"})
        with patch.object(
            RemoteCollector, "_fetch_with_retry", return_value=_malformed_json_response()
        ):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1


class TestFranceTravailTokenJsonSafety:
    """The OAuth2 token endpoint (_fetch_token) is a separate call site from
    the already-safe search-page parsing (france_travail.py:722, which
    already had its own try/except Exception)."""

    def test_malformed_token_response_is_captured_not_raised(self) -> None:
        from src.collectors.france_travail import FranceTravailCollector

        collector = FranceTravailCollector(config={"client_id": "id", "client_secret": "secret"})
        with patch.object(httpx.Client, "post", return_value=_malformed_json_response()):
            result = collector.collect()
        assert result.jobs == []
        assert len(result.errors) == 1
        assert "not valid JSON" in result.errors[0] or "failed" in result.errors[0].lower()
