"""Tests for BaseCollector._fetch_with_retry's HTTP-status retry behavior.

Transient status codes (408, 429, 500, 502, 503, 504) must be retried with
backoff, honouring a Retry-After header when present, while permanent errors
(e.g. 404) must fail immediately without retrying.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.collectors.base import BaseCollector, CollectorResult


class _DummyCollector(BaseCollector):
    """Minimal concrete collector for exercising the shared retry helper."""

    def collect(self) -> CollectorResult:
        raise NotImplementedError


def _mock_http_response(status_code: int, headers: dict[str, str] | None = None) -> MagicMock:
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.headers = headers or {}
    mock.text = f"status {status_code}"
    if status_code >= 400:
        mock.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}", request=MagicMock(), response=mock
        )
    else:
        mock.raise_for_status = MagicMock()
    return mock


class TestFetchWithRetry:
    def test_successful_first_attempt(self) -> None:
        collector = _DummyCollector(retries=3)
        ok = _mock_http_response(200)
        with patch("httpx.get", return_value=ok) as mock_get:
            response = collector._fetch_with_retry("https://example.com")
        assert response is ok
        assert mock_get.call_count == 1

    @patch("time.sleep")
    def test_429_retried_and_succeeds(self, mock_sleep: MagicMock) -> None:
        collector = _DummyCollector(retries=3)
        error_429 = _mock_http_response(429)
        ok = _mock_http_response(200)
        with patch("httpx.get", side_effect=[error_429, ok]) as mock_get:
            response = collector._fetch_with_retry("https://example.com")
        assert response is ok
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once()

    @patch("time.sleep")
    def test_429_honours_retry_after_header(self, mock_sleep: MagicMock) -> None:
        collector = _DummyCollector(retries=3)
        error_429 = _mock_http_response(429, headers={"Retry-After": "5"})
        ok = _mock_http_response(200)
        with patch("httpx.get", side_effect=[error_429, ok]):
            collector._fetch_with_retry("https://example.com")
        mock_sleep.assert_called_once_with(5.0)

    @patch("time.sleep")
    def test_429_without_retry_after_uses_exponential_backoff(self, mock_sleep: MagicMock) -> None:
        collector = _DummyCollector(retries=3)
        error_429 = _mock_http_response(429)
        ok = _mock_http_response(200)
        with patch("httpx.get", side_effect=[error_429, ok]):
            collector._fetch_with_retry("https://example.com")
        mock_sleep.assert_called_once_with(0.5)

    @patch("time.sleep")
    def test_503_is_retried(self, mock_sleep: MagicMock) -> None:
        collector = _DummyCollector(retries=3)
        error_503 = _mock_http_response(503)
        ok = _mock_http_response(200)
        with patch("httpx.get", side_effect=[error_503, ok]) as mock_get:
            response = collector._fetch_with_retry("https://example.com")
        assert response is ok
        assert mock_get.call_count == 2

    def test_404_fails_immediately_without_retry(self) -> None:
        collector = _DummyCollector(retries=3)
        error_404 = _mock_http_response(404)
        with patch("httpx.get", return_value=error_404) as mock_get:
            with pytest.raises(httpx.HTTPStatusError):
                collector._fetch_with_retry("https://example.com")
        assert mock_get.call_count == 1

    @patch("time.sleep")
    def test_retries_exhausted_raises_runtime_error(self, mock_sleep: MagicMock) -> None:
        collector = _DummyCollector(retries=3)
        error_429 = _mock_http_response(429)
        with patch("httpx.get", return_value=error_429) as mock_get:
            with pytest.raises(RuntimeError, match="after 3 attempts"):
                collector._fetch_with_retry("https://example.com")
        assert mock_get.call_count == 3

    @patch("time.sleep")
    def test_connect_error_still_retried(self, mock_sleep: MagicMock) -> None:
        collector = _DummyCollector(retries=3)
        ok = _mock_http_response(200)
        with patch(
            "httpx.get",
            side_effect=[httpx.ConnectError("refused"), ok],
        ) as mock_get:
            response = collector._fetch_with_retry("https://example.com")
        assert response is ok
        assert mock_get.call_count == 2
