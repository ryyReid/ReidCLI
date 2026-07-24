"""HTTP retry/backoff logic: 429 handling, Retry-After, status-code policy."""
from __future__ import annotations

import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from reidx.provider._http import (
    MAX_RETRIES,
    RETRY_STATUS_CODES,
    _backoff_delay,
    _retry_after_seconds,
    _retry_request,
)
from reidx.provider.base import ProviderError


def _http_error(code: int, body: str = "{}", headers: dict | None = None) -> urllib.error.HTTPError:
    hdrs = headers or {}
    fp = BytesIO(body.encode())
    return urllib.error.HTTPError("https://x.test/v1/chat", code, "err", hdrs, fp)


def test_retry_status_codes_include_429_and_5xx():
    assert 429 in RETRY_STATUS_CODES
    assert 500 in RETRY_STATUS_CODES
    assert 503 in RETRY_STATUS_CODES
    assert 529 in RETRY_STATUS_CODES
    assert 408 in RETRY_STATUS_CODES


def test_non_retryable_codes_not_included():
    assert 400 not in RETRY_STATUS_CODES
    assert 401 not in RETRY_STATUS_CODES
    assert 403 not in RETRY_STATUS_CODES
    assert 404 not in RETRY_STATUS_CODES


def test_backoff_grows_exponentially():
    d0 = _backoff_delay(0, None)
    d3 = _backoff_delay(3, None)
    assert d0 < d3
    assert d0 <= 0.5
    assert d3 <= 30.0


def test_backoff_retry_after_overrides():
    d = _backoff_delay(0, 5.0)
    assert d == 5.0


def test_retry_after_seconds_parses_float():
    exc = _http_error(429, headers={"Retry-After": "5"})
    assert _retry_after_seconds(exc) == 5.0


def test_retry_after_ms_header():
    exc = _http_error(429, headers={"retry-after-ms": "3000"})
    assert _retry_after_seconds(exc) == 3.0


def test_retry_after_none_when_absent():
    exc = _http_error(429)
    assert _retry_after_seconds(exc) is None


def test_retry_request_succeeds_after_429():
    calls = {"n": 0}

    def perform():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429, body='{"error":"rate"}')
        return '{"ok": true}'

    with patch("reidx.provider._http.time.sleep"):
        result = _retry_request(perform)
    assert result == '{"ok": true}'
    assert calls["n"] == 3


def test_retry_request_does_not_retry_404():
    calls = {"n": 0}

    def perform():
        calls["n"] += 1
        raise _http_error(404, body='{"error":"not found"}')

    with patch("reidx.provider._http.time.sleep"):
        with pytest.raises(ProviderError) as exc_info:
            _retry_request(perform)
    assert calls["n"] == 1
    assert "404" in str(exc_info.value)


def test_retry_request_gives_up_after_max():
    calls = {"n": 0}

    def perform():
        calls["n"] += 1
        raise _http_error(429, body='{"error":"rate limit exceeded"}')

    with patch("reidx.provider._http.time.sleep"):
        with pytest.raises(ProviderError) as exc_info:
            _retry_request(perform)
    assert calls["n"] == MAX_RETRIES + 1
    assert "429" in str(exc_info.value)
    assert "rate limited after" in str(exc_info.value)
    assert ":free" in str(exc_info.value)


def test_retry_request_fires_on_retry_callback():
    events: list[tuple[int, int, float]] = []

    def perform():
        if len(events) < 2:
            raise _http_error(503, body='{"error":"down"}')
        return '{"ok": true}'

    def on_retry(attempt, code, delay):
        events.append((attempt, code, delay))

    with patch("reidx.provider._http.time.sleep"):
        _retry_request(perform, on_retry=on_retry)
    assert len(events) == 2
    assert events[0][1] == 503
    assert events[1][1] == 503


def test_retry_request_fires_on_retry_callback_with_status_zero_for_network_error():
    import urllib.error as ue

    events: list[tuple[int, int, float]] = []
    calls = {"n": 0}

    def perform():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ue.URLError(TimeoutError("timed out"))
        return '{"ok": true}'

    def on_retry(attempt, code, delay):
        events.append((attempt, code, delay))

    with patch("reidx.provider._http.time.sleep"):
        _retry_request(perform, on_retry=on_retry)
    assert len(events) == 1
    assert events[0][1] == 0
