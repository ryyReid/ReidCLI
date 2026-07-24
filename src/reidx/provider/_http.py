"""Shared HTTP helper for provider clients — stdlib urllib, no extra deps."""
from __future__ import annotations

import json
import os
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from reidx.provider.base import ProviderError

TIMEOUT_SECONDS = 120
# Model listing can be huge/slow (NVIDIA NIM etc.) — never block the TUI for 120s.
MODELS_TIMEOUT_SECONDS = 8

# Cloudflare (and similar) often block Python-urllib's default User-Agent with
# HTTP 403 + HTML ("error code: 1010"). Always send a normal client identity.
DEFAULT_USER_AGENT = "ReidX/2 (+https://github.com/reidx; urllib)"

RETRY_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
MAX_RETRIES = 4
RETRY_BASE_DELAY = 0.5
RETRY_MAX_DELAY = 30.0

_SSL_CONTEXT: ssl.SSLContext | None = None

_HTTP_HINTS: dict[int, str] = {
    401: "API key rejected — re-run /connect to re-add it (the key may have expired or been revoked).",
    403: "Check API key permissions.",
    404: "Check base URL and model name (/model, /providers).",
    429: "Wait and try again.",
    500: "Try again later.",
    502: "Try again later.",
    503: "Try again later.",
}


def _merge_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Apply default User-Agent / Accept unless the caller overrides them."""
    out = {
        "user-agent": DEFAULT_USER_AGENT,
        "accept": "application/json",
    }
    if headers:
        # Caller keys win (case-insensitive for overrides of defaults).
        lower_map = {k.lower(): k for k in headers}
        for def_k, _def_v in list(out.items()):
            if def_k in lower_map:
                out.pop(def_k, None)
        out.update(headers)
    return out


def _build_ssl_context() -> ssl.SSLContext:
    insecure = os.environ.get("REIDX_INSECURE", "").strip().lower() in ("1", "true", "yes", "on")
    if insecure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    except ssl.SSLError:
        pass
    return ctx


def _ssl_ctx() -> ssl.SSLContext:
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = _build_ssl_context()
    return _SSL_CONTEXT


def _extract_api_message(err_body: str) -> str:
    """Pull a short human message out of common provider error JSON shapes."""
    text = (err_body or "").strip()
    if not text:
        return "request failed"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text[:300]
    if not isinstance(data, dict):
        return text[:300]
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type") or ""
        if msg:
            return str(msg)[:300]
    if isinstance(err, str) and err:
        return err[:300]
    for key in ("message", "detail", "error_description"):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val[:300]
    return text[:300]


def _http_error(code: int, err_body: str) -> ProviderError:
    """Build a single clean line for the TUI: `HTTP 404: Application not found`."""
    raw = (err_body or "").strip()
    # Cloudflare / HTML error pages — don't dump the whole document into the TUI.
    low = raw.lower()
    if raw.startswith("<!") or "<html" in low or "error code:" in low:
        if "1010" in raw:
            msg = "blocked by Cloudflare (bot protection) — update ReidX / check User-Agent"
        elif "1020" in raw:
            msg = "blocked by Cloudflare firewall"
        else:
            msg = "HTML error page from host (not a JSON API response)"
        hint = _HTTP_HINTS.get(code)
        text = f"HTTP {code}: {msg}" + (f" — {hint}" if hint else "")
        return ProviderError(text, status_code=code)

    msg = _extract_api_message(err_body)
    hint = _HTTP_HINTS.get(code)
    if hint and hint.lower() not in msg.lower():
        text = f"HTTP {code}: {msg} — {hint}"
    else:
        text = f"HTTP {code}: {msg}"
    return ProviderError(text, status_code=code)


def _rate_limit_exhausted_error(exc: urllib.error.HTTPError, err_body: str, retries: int) -> ProviderError:
    """Build an actionable message after all rate-limit retries are exhausted."""
    msg = _extract_api_message(err_body)
    parts = [f"rate limited after {retries} retries (HTTP 429)"]
    if msg:
        parts.append(msg[:200])
    parts.append(
        "the provider's rate or daily limit is likely hit — for free models (':free' suffix), "
        "try the paid variant (drop :free) or wait for the limit window to reset"
    )
    return ProviderError(" — ".join(parts), status_code=429)


def _network_error(exc: BaseException, timeout: int) -> ProviderError:
    """Translate a urllib/socket network failure into one clean, actionable line.

    Distinguishes DNS failure, connection refused, timeout, and SSL so the user
    gets a hint about what to fix instead of a raw `gaierror(11001, ...)`.
    """
    reason = getattr(exc, "reason", exc)
    rstr = str(reason).lower()
    if isinstance(reason, TimeoutError) or "timed out" in rstr or "timeout" in rstr:
        return ProviderError(f"connection timed out after {timeout}s — the host is slow or unreachable")
    errno = getattr(reason, "errno", None)
    import socket as _socket
    if errno == _socket.EAI_NONAME or "getaddrinfo" in rstr or "name or service not known" in rstr or "nodename" in rstr:
        return ProviderError("could not resolve host — check the base URL and your internet connection")
    if "connection refused" in rstr or errno in (111, 10061):
        return ProviderError("connection refused — the host is up but not serving on that port (is the server running?)")
    if "ssl" in rstr or "certificate" in rstr or "verify failed" in rstr:
        return ProviderError(f"SSL/TLS error — {reason} (check the host's certificate or set REIDX_INSECURE=1 for local dev)")
    if "network is unreachable" in rstr or errno in (101, 10051):
        return ProviderError("network is unreachable — check your internet connection")
    return ProviderError(f"connection error: {reason}")


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float | None:
    """Parse retry delay from a 429/503 response.

    Priority (mirrors OpenAI/Anthropic SDKs): retry-after-ms → retry-after as
    float seconds → retry-after as HTTP-date. Not capped at 60s (a CLI can wait
    a real limit window rather than burning retries).
    """
    ms = exc.headers.get("retry-after-ms")
    if ms:
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            pass
    val = exc.headers.get("Retry-After") or exc.headers.get("retry-after")
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(val)
        if dt is not None:
            return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        pass
    return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with flat-multiplier jitter (OpenAI/Anthropic SDK
    style); Retry-After from the server overrides when present."""
    if retry_after is not None:
        return min(retry_after, RETRY_MAX_DELAY * 2)
    base = min(RETRY_BASE_DELAY * (2 ** attempt), RETRY_MAX_DELAY)
    jitter = 1.0 - 0.25 * random.random()
    return base * jitter


def _retry_request(
    perform: Callable[[], object],
    *,
    on_retry: Callable[[int, int, float], None] | None = None,
) -> object:
    """Run `perform()` once; on retryable HTTP errors (429/5xx), back off and retry.

    `perform` must return the response object on success or raise HTTPError/URLError.
    `on_retry(attempt, status_code, delay)` fires before each sleep so the caller
    can surface "retrying in Ns" to the UI. Non-retryable HTTP errors propagate
    immediately as ProviderError via _http_error.
    """
    last_exc: Exception | None = None
    retries_done = 0
    for attempt in range(MAX_RETRIES + 1):
        try:
            return perform()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in RETRY_STATUS_CODES or attempt >= MAX_RETRIES:
                err_body = exc.read().decode("utf-8", errors="replace")[:500]
                if retries_done > 0 and exc.code == 429:
                    raise _rate_limit_exhausted_error(exc, err_body, retries_done) from None
                raise _http_error(exc.code, err_body) from None
            retries_done += 1
            retry_after = _retry_after_seconds(exc)
            delay = _backoff_delay(attempt, retry_after)
            if on_retry is not None:
                try:
                    on_retry(attempt + 1, exc.code, delay)
                except Exception:  # noqa: BLE001 - UI callback must not kill retry
                    pass
            time.sleep(delay)
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            transient = isinstance(reason, TimeoutError) or "timed out" in str(reason).lower()
            if not transient or attempt >= MAX_RETRIES:
                raise _network_error(exc, TIMEOUT_SECONDS) from None
            delay = _backoff_delay(attempt, None)
            if on_retry is not None:
                try:
                    on_retry(attempt + 1, 0, delay)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(delay)
            last_exc = exc
    if isinstance(last_exc, urllib.error.HTTPError):
        err_body = last_exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_error(last_exc.code, err_body) from None
    if last_exc is not None:
        raise _network_error(last_exc, TIMEOUT_SECONDS) from None
    raise ProviderError("request failed with no response")


def post_json(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: int = TIMEOUT_SECONDS,
    *,
    on_retry: Callable[[int, int, float], None] | None = None,
) -> dict:
    """POST a JSON payload, return the parsed JSON response.

    Retries 429/5xx with exponential backoff (respecting Retry-After). Raises
    ProviderError on terminal HTTP/network errors; the agent loop soft-catches.
    """
    body = json.dumps(payload).encode("utf-8")
    hdrs = _merge_headers({"content-type": "application/json", **(headers or {})})

    def _do() -> str:
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.read().decode("utf-8")

    raw = _retry_request(_do, on_retry=on_retry)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON from provider: {exc}") from None


def post_form(
    url: str,
    fields: dict[str, str],
    headers: dict[str, str] | None = None,
    timeout: int = TIMEOUT_SECONDS,
) -> dict:
    """POST an application/x-www-form-urlencoded body, return parsed JSON.

    OAuth token endpoints (RFC 6749) require form encoding rather than JSON.
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    hdrs = _merge_headers(
        {"content-type": "application/x-www-form-urlencoded", **(headers or {})}
    )
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_error(exc.code, err_body) from None
    except urllib.error.URLError as exc:
        raise _network_error(exc, timeout) from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON from provider: {exc}") from None


def iter_sse_json(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: int = TIMEOUT_SECONDS,
    *,
    on_retry: Callable[[int, int, float], None] | None = None,
):
    """POST JSON and yield parsed JSON objects from an SSE (`data: …`) stream.

    Stops on `data: [DONE]`. Retries 429/5xx with backoff before opening the
    stream. Raises ProviderError on terminal HTTP/network failures.
    """
    body = json.dumps(payload).encode("utf-8")
    hdrs = _merge_headers(
        {
            "content-type": "application/json",
            "accept": "text/event-stream",
            **(headers or {}),
        }
    )

    def _do():
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())

    resp = _retry_request(_do, on_retry=on_retry)

    try:
        while True:
            raw_line = resp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or line.startswith(":"):
                # Empty SSE comment / keep-alive
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                if data == "[DONE]":
                    break
                continue
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def get_json(
    url: str,
    headers: dict[str, str],
    timeout: int = TIMEOUT_SECONDS,
    *,
    on_retry: Callable[[int, int, float], None] | None = None,
) -> dict:
    hdrs = _merge_headers(headers)

    def _do() -> str:
        req = urllib.request.Request(url, headers=hdrs, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.read(8_000_000).decode("utf-8", errors="replace")

    raw = _retry_request(_do, on_retry=on_retry)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON from provider: {exc}") from None
