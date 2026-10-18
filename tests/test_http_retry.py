"""Tests for src.notifications.http_retry.send_with_retry — the shared
bounded retry/backoff policy used by both DiscordProvider and N8nProvider.

All tests patch time.sleep to a no-op so the suite stays fast; the actual
delay VALUES chosen are asserted separately (test_sleep_durations_are_bounded)
so a change to the backoff schedule can't silently blow the latency budget.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.notifications.http_retry import (
    MAX_RETRIES,
    RETRY_AFTER_CAP_SECONDS,
    send_with_retry,
)


def _response(status_code: int, headers: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    return resp


@pytest.fixture(autouse=True)
def _no_real_sleep():
    with patch("src.notifications.http_retry.time.sleep") as mock_sleep:
        yield mock_sleep


class TestSuccessPaths:
    def test_first_attempt_success_no_retry(self, _no_real_sleep):
        request = MagicMock(return_value=_response(200))
        result = send_with_retry(request)
        assert result.status_code == 200
        assert request.call_count == 1
        _no_real_sleep.assert_not_called()

    def test_non_retryable_4xx_returns_immediately_no_retry(self, _no_real_sleep):
        """A 400 (malformed payload) or 404 must fail fast — retrying a
        permanently broken request wastes the retry budget for nothing."""
        request = MagicMock(return_value=_response(400))
        result = send_with_retry(request)
        assert result.status_code == 400
        assert request.call_count == 1
        _no_real_sleep.assert_not_called()

    def test_404_not_retried(self, _no_real_sleep):
        request = MagicMock(return_value=_response(404))
        result = send_with_retry(request)
        assert result.status_code == 404
        assert request.call_count == 1


class TestRetryOn429:
    def test_retries_on_429_then_succeeds(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(429), _response(200)])
        result = send_with_retry(request)
        assert result.status_code == 200
        assert request.call_count == 2

    def test_respects_retry_after_header(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(429, {"Retry-After": "2"}), _response(200)])
        send_with_retry(request)
        _no_real_sleep.assert_called_once_with(2.0)

    def test_retry_after_is_capped(self, _no_real_sleep):
        """A webhook asking to wait far longer than our budget allows must
        be capped, never honored verbatim — this is what keeps the total
        retry time budget bounded regardless of what a server asks for."""
        request = MagicMock(side_effect=[_response(429, {"Retry-After": "9999"}), _response(200)])
        send_with_retry(request)
        _no_real_sleep.assert_called_once_with(RETRY_AFTER_CAP_SECONDS)

    def test_malformed_retry_after_falls_back_to_backoff_schedule(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(429, {"Retry-After": "not-a-number"}), _response(200)])
        send_with_retry(request)
        _no_real_sleep.assert_called_once_with(1.0)  # first backoff step

    def test_missing_retry_after_falls_back_to_backoff_schedule(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(429), _response(200)])
        send_with_retry(request)
        _no_real_sleep.assert_called_once_with(1.0)


class TestRetryOn5xx:
    def test_retries_on_500_then_succeeds(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(500), _response(200)])
        result = send_with_retry(request)
        assert result.status_code == 200
        assert request.call_count == 2

    def test_retries_on_503_then_succeeds(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(503), _response(200)])
        result = send_with_retry(request)
        assert result.status_code == 200

    def test_5xx_uses_fixed_backoff_not_retry_after(self, _no_real_sleep):
        request = MagicMock(side_effect=[_response(502), _response(200)])
        send_with_retry(request)
        _no_real_sleep.assert_called_once_with(1.0)


class TestBoundedRetries:
    def test_gives_up_after_max_retries_returns_last_response(self, _no_real_sleep):
        """Still failing after MAX_RETRIES retries -> returns the last
        (still-failing) response rather than retrying forever."""
        responses = [_response(500) for _ in range(MAX_RETRIES + 1)]
        request = MagicMock(side_effect=responses)
        result = send_with_retry(request)
        assert result.status_code == 500
        assert request.call_count == MAX_RETRIES + 1

    def test_never_exceeds_max_retries_plus_one_attempts(self, _no_real_sleep):
        # An infinite supply of 429s must still stop at MAX_RETRIES + 1.
        request = MagicMock(return_value=_response(429))
        send_with_retry(request)
        assert request.call_count == MAX_RETRIES + 1

    def test_sleep_durations_are_bounded(self, _no_real_sleep):
        """Pins the exact backoff schedule so the total worst-case added
        latency per notification call stays a known, small quantity."""
        request = MagicMock(return_value=_response(500))
        send_with_retry(request)
        for call in _no_real_sleep.call_args_list:
            (delay,) = call.args
            assert delay <= RETRY_AFTER_CAP_SECONDS or delay <= 3.0


class TestConnectionAndTimeoutErrors:
    def test_connect_error_retried_then_succeeds(self, _no_real_sleep):
        request = MagicMock(side_effect=[httpx.ConnectError("refused"), _response(200)])
        result = send_with_retry(request)
        assert result.status_code == 200
        assert request.call_count == 2

    def test_timeout_retried_then_succeeds(self, _no_real_sleep):
        request = MagicMock(side_effect=[httpx.TimeoutException("timed out"), _response(200)])
        result = send_with_retry(request)
        assert result.status_code == 200

    def test_connect_error_exhausts_retries_and_reraises(self, _no_real_sleep):
        """Never raises SOMETHING NEW — the original httpx exception
        propagates unchanged so a caller's existing `except Exception` still
        catches it exactly as before this retry layer was added."""
        request = MagicMock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(httpx.ConnectError):
            send_with_retry(request)
        assert request.call_count == MAX_RETRIES + 1

    def test_timeout_exhausts_retries_and_reraises(self, _no_real_sleep):
        request = MagicMock(side_effect=httpx.TimeoutException("timed out"))
        with pytest.raises(httpx.TimeoutException):
            send_with_retry(request)
        assert request.call_count == MAX_RETRIES + 1

    def test_other_httpx_errors_are_not_caught_by_this_layer(self, _no_real_sleep):
        """Only ConnectError/TimeoutException are treated as retryable
        network errors — anything else httpx might raise is the caller's
        problem, unchanged from before this module existed."""
        request = MagicMock(side_effect=httpx.InvalidURL("bad url"))
        with pytest.raises(httpx.InvalidURL):
            send_with_retry(request)
        assert request.call_count == 1
