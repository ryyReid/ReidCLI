"""Shared HTTP helper for provider clients — stdlib urllib, no extra deps."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 120


def post_json(url: str, payload: dict, headers: dict[str, str], timeout: int = TIMEOUT_SECONDS) -> dict:
    """POST a JSON payload, return the parsed JSON response.

    Raises RuntimeError on HTTP or network errors — providers surface those
    to the agent loop, which turns them into tool-result errors rather than
    crashing the turn.
    """
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"content-type": "application/json", **headers}
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code}: {err_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"connection error: {exc}") from exc
    return json.loads(raw)
