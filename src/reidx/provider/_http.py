"""Shared HTTP helper for provider clients — stdlib urllib, no extra deps."""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

from reidx.provider.base import ProviderError

TIMEOUT_SECONDS = 120
# Model listing can be huge/slow (NVIDIA NIM etc.) — never block the TUI for 120s.
MODELS_TIMEOUT_SECONDS = 8

_SSL_CONTEXT: ssl.SSLContext | None = None

_HTTP_HINTS: dict[int, str] = {
    401: "Check your API key (/connect or env).",
    403: "Check API key permissions.",
    404: "Check base URL and model name (/model, /providers).",
    429: "Wait and try again.",
    500: "Try again later.",
    502: "Try again later.",
    503: "Try again later.",
}


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
    msg = _extract_api_message(err_body)
    # Prefer the API's own message; only append a short hint when it's useful
    # and not already implied by the message text.
    hint = _HTTP_HINTS.get(code)
    if hint and hint.lower() not in msg.lower():
        text = f"HTTP {code}: {msg} — {hint}"
    else:
        text = f"HTTP {code}: {msg}"
    return ProviderError(text, status_code=code)


def post_json(url: str, payload: dict, headers: dict[str, str], timeout: int = TIMEOUT_SECONDS) -> dict:
    """POST a JSON payload, return the parsed JSON response.

    Raises ProviderError on HTTP or network errors. The agent loop soft-catches
    those so the TUI session stays up and shows an inline error.
    """
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"content-type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_error(exc.code, err_body) from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"connection error: {exc.reason if hasattr(exc, 'reason') else exc}") from None
    return json.loads(raw)


def iter_sse_json(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: int = TIMEOUT_SECONDS,
):
    """POST JSON and yield parsed JSON objects from an SSE (`data: …`) stream.

    Stops on `data: [DONE]`. Raises ProviderError on HTTP/network failures.
    Used by OpenAI-compatible chat streaming (NVIDIA NIM, OpenAI, Groq, …).
    """
    body = json.dumps(payload).encode("utf-8")
    hdrs = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        **headers,
    }
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_error(exc.code, err_body) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise ProviderError(f"connection error: timed out after {timeout}s") from None
        raise ProviderError(f"connection error: {reason}") from None

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


def get_json(url: str, headers: dict[str, str], timeout: int = TIMEOUT_SECONDS) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            # Cap body size so a multi‑MB model catalog cannot freeze the process.
            raw = resp.read(8_000_000).decode("utf-8", errors="replace")
    except TimeoutError as exc:
        raise ProviderError(f"connection error: timed out after {timeout}s") from None
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise _http_error(exc.code, err_body) from None
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        # urllib wraps socket timeouts in URLError on some platforms.
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise ProviderError(f"connection error: timed out after {timeout}s") from None
        raise ProviderError(f"connection error: {reason}") from None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON from provider: {exc}") from None
